"""
wechatbridge Update Check Module
Periodically check PyPI for new versions and notify admin users via WeChat.
"""

import asyncio
import logging
import re

import httpx

from . import __version__
from .config import config

logger = logging.getLogger(__name__)

PYPI_URL = "https://pypi.org/pypi/wechatbridge-cli/json"
RELEASES_URL = "https://github.com/jerryxugit-2026/Build-WeChat-Bridge/releases"


# ---------------------------------------------------------------------------
# Version comparison helpers
# ---------------------------------------------------------------------------

def _parse_version(v: str) -> tuple:
    """Parse a semver string into a numeric tuple for comparison.

    Each dot-segment is parsed by taking the leading digits; non-digit
    suffixes (e.g. ``-beta``) are ignored. Missing segments default to 0.
    """
    parts = v.split(".")
    result = []
    for seg in parts:
        m = re.match(r"\d+", seg)
        result.append(int(m.group()) if m else 0)
    return tuple(result)


def is_newer(latest: str, current: str = __version__) -> bool:
    """Return True if ``latest`` is a higher version than ``current``."""
    return _parse_version(latest) > _parse_version(current)


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

latest_version: str | None = None
_notified_admins: set = set()


# ---------------------------------------------------------------------------
# Network operations
# ---------------------------------------------------------------------------

async def fetch_latest_version() -> str | None:
    """Query PyPI JSON API for the latest published version.

    Returns version string on success, ``None`` on any failure.
    """
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(PYPI_URL)
            resp.raise_for_status()
            data = resp.json()
            version: str = data["info"]["version"]
            logger.debug("PyPI latest version: %s", version)
            return version
    except Exception as e:
        logger.debug("Failed to fetch latest version from PyPI: %s", e)
        return None


# ---------------------------------------------------------------------------
# Background loop
# ---------------------------------------------------------------------------

async def update_check_loop() -> None:
    """Background task: check PyPI immediately, then every ``update_check_interval`` seconds.

    Never raises; all exceptions are caught and logged.
    """
    global latest_version

    while True:
        try:
            version = await fetch_latest_version()
            if version is not None:
                if latest_version is None or is_newer(version, latest_version):
                    latest_version = version
                    if is_newer(version):
                        logger.warning(
                            "发现新版本: 当前 %s → 最新 %s。"
                            "更新: `pipx upgrade wechatbridge-cli` | %s",
                            __version__, version, RELEASES_URL,
                        )
                elif version == latest_version:
                    pass  # same version, no action
            # else: fetch failed, will retry next cycle
        except Exception as e:
            logger.exception("update_check_loop 未知异常: %s", e)

        await asyncio.sleep(config.update_check_interval)


# ---------------------------------------------------------------------------
# Query helpers (used by /version)
# ---------------------------------------------------------------------------

def update_available() -> bool:
    """Return True if a newer version has been detected."""
    if latest_version is None:
        return False
    return is_newer(latest_version)


def format_update_hint() -> str:
    """Return a formatted update hint string, or empty string if no update.

    Designed to be appended to ``/version`` replies.
    """
    if not update_available():
        return ""
    return (
        f"\n\n🆕 发现新版本 `{latest_version}`，更新：`pipx upgrade wechatbridge-cli`"
    )


# ---------------------------------------------------------------------------
# Admin notification
# ---------------------------------------------------------------------------

async def maybe_notify_admin(client, from_user: str, context_token: str) -> None:
    """If an update is available and ``from_user`` is an admin, send a WeChat notification.

    Each admin is only notified once per process lifetime (tracked in
    ``_notified_admins``). On send failure, the admin is removed from the set
    so the next message from them will retry.

    All network/send exceptions are caught and logged.
    """
    if not update_available():
        return
    if from_user not in config.admin_users:
        return
    if from_user in _notified_admins:
        return

    # Mark as notified immediately to prevent concurrent duplicates
    _notified_admins.add(from_user)

    text = (
        f"🆕 **wechatbridge 有新版本** 🆕\n\n"
        f"当前 `{__version__}` → 最新 `{latest_version}`\n\n"
        f"更新方法：\n"
        f"`pipx upgrade wechatbridge-cli && sudo systemctl restart wechatbridge`\n\n"
        f"更新内容：{RELEASES_URL}"
    )

    try:
        success = await client.send_message(
            to_user_id=from_user,
            text=text,
            context_token=context_token,
            baseurl=client.state.baseurl,
            bot_token=client.state.bot_token,
        )
        if success:
            logger.info("已向管理员 %s 发送版本更新通知", from_user)
        else:
            logger.warning("向管理员 %s 发送版本更新通知失败", from_user)
            _notified_admins.discard(from_user)
    except Exception as e:
        logger.warning("向管理员 %s 发送版本更新通知异常: %s", from_user, e)
        _notified_admins.discard(from_user)
