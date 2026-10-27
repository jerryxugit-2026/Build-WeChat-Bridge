"""muse backend — file-drop task queue + outbox reply watcher.

协议真源: docs/wechat-agent-protocol.md（仓库内副本，wechat-agent 文件协议 v1）

- run_muse(): 把入站消息落盘成 inbox/pending/<mid>.json（原子写、按消息 ID 去重），
  正文含 mp.weixin.qq.com 链接时在本地（家庭 IP）抓正文存 articles/。
- outbox_watch_loop(): 扫 outbox/pending/*.json，用缓存的 context_token 发回微信；
  发送成功 → outbox/processed/，失败 → outbox/failed/（不静默丢，token 刷新后自动重试）。

安全: 本模块是哑管道，绝不把消息正文解析为指令（协议 §安全规矩）。
"""

import asyncio
import contextvars
import html
import json
import logging
import os
import re
import shutil
import time
import urllib.request
import uuid
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

BASE = os.environ.get("WECHAT_AGENT_ROOT", os.path.expanduser("~/.wechat-agent"))
INBOX_PENDING = os.path.join(BASE, "inbox", "pending")
INBOX_PROCESSED = os.path.join(BASE, "inbox", "processed")
INBOX_FAILED = os.path.join(BASE, "inbox", "failed")
OUTBOX_PENDING = os.path.join(BASE, "outbox", "pending")
OUTBOX_PROCESSED = os.path.join(BASE, "outbox", "processed")
OUTBOX_FAILED = os.path.join(BASE, "outbox", "failed")
ARTICLES_DIR = os.path.join(BASE, "articles")
STATE_DIR = os.path.join(BASE, "state")
TOKEN_CACHE = os.path.join(STATE_DIR, "context_tokens.json")

_DIRS = (
    INBOX_PENDING, INBOX_PROCESSED, INBOX_FAILED,
    OUTBOX_PENDING, OUTBOX_PROCESSED, OUTBOX_FAILED,
    ARTICLES_DIR, STATE_DIR,
)

_ACK_TEXT = os.environ.get(
    "WECHAT_MUSE_ACK", "✅ 已收到，处理中，结果稍后回执。"
)
_MP_URL_RE = re.compile(r"https?://mp\.weixin\.qq\.com/\S+")
_WATCH_INTERVAL_S = float(os.environ.get("WECHAT_MUSE_OUTBOX_INTERVAL", "3"))

# 入站消息元数据（main.process_message → run_muse 的桥）
CURRENT_MSG: contextvars.ContextVar = contextvars.ContextVar("muse_current_msg", default=None)
# context_token 刷新信号：user_id → 有待重试的 outbox/failed
_token_refreshed: set = set()
# ack 去抖：短窗口内同用户连发合并为一批只回一条（“转发到其他应用”会拆成 内容+留言 两条入站消息）
_ACK_BATCH_WINDOW_S = float(os.environ.get("WECHAT_MUSE_ACK_WINDOW", "15"))
_last_drop_ts: dict = {}


# ---------------------------------------------------------------------------
# 基础设施
# ---------------------------------------------------------------------------

def ensure_dirs() -> None:
    for d in _DIRS:
        os.makedirs(d, exist_ok=True)
    try:
        os.chmod(STATE_DIR, 0o700)
    except OSError:
        pass


_LOCK_FH = None  # 进程级持锁句柄（防 GC 关闭）


def acquire_singleton_lock() -> None:
    """单实例锁：flock 抢占，抢不到即退出（双实例会重复收发，2026-10-21 实证事故）。"""
    global _LOCK_FH
    ensure_dirs()
    inst = re.sub(r"[^A-Za-z0-9._-]", "_", os.environ.get("WECHATBRIDGE_INSTANCE", "default"))
    lock_path = os.path.join(STATE_DIR, "daemon-%s.lock" % inst)
    _LOCK_FH = open(lock_path, "w")
    try:
        import fcntl
        fcntl.flock(_LOCK_FH, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit(
            "muse: 已有 daemon 实例在运行（%s 锁被持有），本进程退出。"
            "若确认无人运行，删除该文件后重试。" % lock_path
        )
    _LOCK_FH.write(str(os.getpid()))
    _LOCK_FH.flush()
    logger.info("muse: 单实例锁 acquired (%s)", lock_path)


def _atomic_write_json(path: str, obj: dict) -> None:
    """先写 .tmp 再 rename —— rename 即就绪信号（协议 §2）。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def hook_message(msg: dict) -> None:
    """main.process_message 每条入站消息调用：缓存 context_token 并挂元数据。

    - token 持久化到 state/context_tokens.json（0600），供 outbox 回执使用；
    - 该用户 token 刷新过 → 触发 outbox/failed 一次性重试（协议 §4）。
    """
    ensure_dirs()
    CURRENT_MSG.set(msg)
    user = msg.get("from_user_id", "")
    token = msg.get("context_token", "")
    if not user or not token:
        return
    cache = _load_token_cache()
    if cache.get(user) != token:
        cache[user] = token
        _save_token_cache(cache)
        if _has_outbox_failed(user):
            _token_refreshed.add(user)
        logger.info("muse: 缓存 context_token user=%s", user)


def _load_token_cache() -> dict:
    try:
        with open(TOKEN_CACHE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_token_cache(cache: dict) -> None:
    tmp = TOKEN_CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)
    os.replace(tmp, TOKEN_CACHE)
    try:
        os.chmod(TOKEN_CACHE, 0o600)
    except OSError:
        pass


def _has_outbox_failed(user: str) -> bool:
    try:
        for name in os.listdir(OUTBOX_FAILED):
            p = os.path.join(OUTBOX_FAILED, name)
            if not name.endswith(".json"):
                continue
            try:
                with open(p, encoding="utf-8") as f:
                    if json.load(f).get("to_user") == user:
                        return True
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    return False


# ---------------------------------------------------------------------------
# 任务落盘（daemon → muse）
# ---------------------------------------------------------------------------

def _task_filename(msg: dict) -> str:
    mid = msg.get("message_id")
    if mid:
        return "mid_%s.json" % mid
    cid = re.sub(r"[^A-Za-z0-9._-]", "_", str(msg.get("client_id") or ""))[:80]
    if cid:
        return "cl_%s.json" % cid
    return "ts_%s_%s.json" % (msg.get("create_time_ms") or int(time.time() * 1000), uuid.uuid4().hex[:8])


def _task_exists(name: str) -> bool:
    return any(os.path.exists(os.path.join(d, name)) for d in (INBOX_PENDING, INBOX_PROCESSED, INBOX_FAILED))


def extract_mp_urls(text: str) -> list:
    return _MP_URL_RE.findall(text or "")


_FETCH_UAS = (
    (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.49"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 WindowsWechat 2.9.5"
    ),
)
_WALL_SIGS = ("环境异常", "完成验证", "weixin110", "参数错误", "该内容已被发布者删除")
_MIN_ARTICLE_CHARS = 80  # 正文少于此长度视为反爬空白页，不得误判 ok（2026-10-21 实证）


def fetch_article(url: str, timeout: float = 20.0) -> dict:
    """本地（家庭 IP）抓公众号正文。UA 轮换重试 + 内容校验，空白页判 blocked。"""
    last_reason = "untried"
    for ua in _FETCH_UAS:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": ua,
                "Referer": "https://mp.weixin.qq.com/",
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read(4 * 1024 * 1024)
            page = raw.decode("utf-8", errors="replace")
        except Exception as e:
            last_reason = "fetch_err:%s" % e
            continue

        if any(sig in page for sig in _WALL_SIGS):
            last_reason = "wall"
            continue

        m = re.search(r'<meta property="og:title" content="([^"]*)"', page)
        title = html.unescape(m.group(1)).strip() if m else ""
        body = page
        m2 = re.search(r'id="js_content"(.*?)(id="js_pc_qr_code"|</body>)', page, re.S)
        if m2:
            body = m2.group(1)
        body = re.sub(r"<script.*?</script>", " ", body, flags=re.S)
        body = re.sub(r"<style.*?</style>", " ", body, flags=re.S)
        body = re.sub(r"<[^>]+>", "\n", body)
        text = re.sub(r"\n{2,}", "\n", html.unescape(body)).strip()

        if len(text) < _MIN_ARTICLE_CHARS:
            last_reason = "blank(text=%d)" % len(text)
            continue

        md_name = "art_%s.md" % uuid.uuid4().hex[:12]
        md_path = os.path.join(ARTICLES_DIR, md_name)
        with open(md_path + ".tmp", "w", encoding="utf-8") as f:
            f.write("# %s\n\n来源: %s\n\n%s\n" % (title or "(无标题)", url, text))
        os.replace(md_path + ".tmp", md_path)
        return {"url": url, "fetch_status": "ok", "title": title, "markdown_path": md_path}

    logger.warning("muse: 公众号抓取未获正文 url=%s reason=%s", url[:80], last_reason)
    hint = (
        "该链接是公众号图片消息（JS 渲染页，无文字正文）。请提示用户：长按图片本体转发给 bot，"
        "或直接发截图——daemon 会落盘图片附件供你读图。"
        if "t=pages/image_detail" in url
        else "未抓到正文。请提示用户：转发文章截图或复制全文。"
    )
    return {"url": url, "fetch_status": "blocked", "hint": hint}


async def run_muse(prompt: str, user_id: str, timeout: int = None) -> tuple:
    """后端入口（与 run_dsh 同签名）。落盘任务文件，返回即时 ack。"""
    ensure_dirs()
    msg = CURRENT_MSG.get() or {}
    name = _task_filename(msg)
    if _task_exists(name):
        logger.info("muse: 消息已入队，跳过去重（不重复 ack）%s", name)
        return ("", [])

    # 接入上游 1.7.1 的 @路径 消毒：越界/敏感路径一律替换为 [blocked-path]
    try:
        from .runner_common import _sanitize_prompt_at_paths, get_session_dir
        prompt = _sanitize_prompt_at_paths(prompt, get_session_dir(user_id))
    except Exception as e:
        logger.warning("muse: prompt 消毒未生效（按原文继续）: %s", e)

    article = None
    urls = extract_mp_urls(prompt)
    if urls:
        article = await asyncio.to_thread(fetch_article, urls[0])
        article["url"] = urls[0]

    attachments = [
        {"kind": "image" if p.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic")) else "file",
         "path": p}
        for p in re.findall(r"@(/[^\s@]+)", prompt or "")
    ]

    task = {
        "schema": "wechat-agent.task.v1",
        "msg_id": str(msg.get("message_id") or ""),
        "client_id": str(msg.get("client_id") or ""),
        "from_user": msg.get("from_user_id") or user_id,
        "create_time_ms": msg.get("create_time_ms"),
        "received_at": _now_iso(),
        "kind": "message",
        "text": prompt or "",
        "attachments": attachments,
    }
    if article:
        task["article"] = article

    _atomic_write_json(os.path.join(INBOX_PENDING, name), task)
    _now = time.time()
    burst = _now - _last_drop_ts.get(user_id, 0.0) < _ACK_BATCH_WINDOW_S
    _last_drop_ts[user_id] = _now
    logger.info(
        "muse: 任务已落盘 %s (user=%s%s)", name, user_id,
        "，ack 去抖跳过" if burst else "",
    )
    return ("" if burst else _ACK_TEXT, [])


# ---------------------------------------------------------------------------
# outbox 回执（muse → daemon → 微信）
# ---------------------------------------------------------------------------

def _move(src: str, dst_dir: str) -> str:
    base = os.path.basename(src)
    if base.endswith(".claim"):
        base = base[: -len(".claim")]
    dst = os.path.join(dst_dir, base)
    if os.path.exists(dst):
        os.remove(dst)  # 幂等：目标已存在则覆盖为最新
    shutil.move(src, dst)
    return dst


def _claim(path: str):
    """原子认领：rename 抢占（同 FS 原子），防多实例抢发同一回执。"""
    claim = path + ".claim"
    try:
        os.replace(path, claim)
        return claim
    except FileNotFoundError:
        return None


async def _hud_notify_muse(text: str) -> None:
    """Muse 回执直达 HUD(2026-10-25):bot 流任务回执发微信成功后推一条通知。
    确定性覆盖所有 muse 会话形态(临时/主会话经 bot 流的都算);失败静默不影响回执。"""
    try:
        import httpx
        token = os.environ.get("HUD_PUSH_TOKEN", "").strip()
        url = os.environ.get("HUD_PUSH_URL", "").strip()
        if not token or not url:
            return
        excerpt = " ".join((text or "").split())[:180]
        async with httpx.AsyncClient() as http:
            await http.post(
                url,
                headers={"authorization": f"Bearer {token}",
                         "user-agent": "Mozilla/5.0 (Macintosh) wechatbridge/1.0"},
                json={"app": "muse", "title": "Muse 已回复",
                      "subtitle": "微信任务完成", "body": excerpt},
                timeout=15)
    except Exception as e:
        logger.debug("muse HUD push 失败: %s", e)


async def _send_reply(client, reply: dict) -> bool:
    to_user = reply.get("to_user", "")
    text = (reply.get("text") or "").strip()
    if not to_user or not text:
        return False
    token = _load_token_cache().get(to_user, "")
    if not token:
        logger.warning("muse: 无 context_token，无法回执 user=%s", to_user)
        return False
    try:
        return bool(
            await client.send_message(
                to_user_id=to_user,
                text=text,
                context_token=token,
                baseurl=client.state.baseurl,
                bot_token=client.state.bot_token,
            )
        )
    except Exception as e:
        logger.warning("muse: 回执发送异常 user=%s err=%s", to_user, e)
        return False


async def _drain_outbox_once(client) -> None:
    ensure_dirs()
    # 1) 主队列
    for name in sorted(os.listdir(OUTBOX_PENDING)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(OUTBOX_PENDING, name)
        claim = _claim(path)  # rename 原子认领，防双实例抢发
        if claim is None:
            continue
        try:
            with open(claim, encoding="utf-8") as f:
                reply = json.load(f)
        except (OSError, ValueError):
            _move(claim, OUTBOX_FAILED)
            continue
        if await _send_reply(client, reply):
            _move(claim, OUTBOX_PROCESSED)
            logger.info("muse: 回执已发送 %s", name)
            asyncio.create_task(_hud_notify_muse(reply.get("text") or ""))
        else:
            _move(claim, OUTBOX_FAILED)
            logger.warning("muse: 回执发送失败 → %s", os.path.join(OUTBOX_FAILED, name))
    # 2) token 刷新用户的一次性失败重试（协议 §4）
    if _token_refreshed:
        users = set(_token_refreshed)
        _token_refreshed.clear()
        for name in sorted(os.listdir(OUTBOX_FAILED)):
            if not name.endswith(".json"):
                continue
            path = os.path.join(OUTBOX_FAILED, name)
            claim = _claim(path)  # 同样原子认领
            if claim is None:
                continue
            try:
                with open(claim, encoding="utf-8") as f:
                    reply = json.load(f)
            except (OSError, ValueError):
                _move(claim, OUTBOX_FAILED)
                continue
            if reply.get("to_user") not in users:
                _move(claim, OUTBOX_FAILED)
                continue
            if await _send_reply(client, reply):
                _move(claim, OUTBOX_PROCESSED)
                logger.info("muse: 失败回执重试成功 %s", name)
                asyncio.create_task(_hud_notify_muse(reply.get("text") or ""))
            else:
                _move(claim, OUTBOX_FAILED)
                logger.warning("muse: 失败回执重试仍失败，留待下次 token 刷新 %s", name)


async def outbox_watch_loop(client) -> None:
    """常驻扫描 outbox/pending（协议 §5）。随登录会话生命周期由 main_loop 托管。"""
    ensure_dirs()
    logger.info("muse: outbox 监听已启动 interval=%.1fs", _WATCH_INTERVAL_S)
    while True:
        try:
            await _drain_outbox_once(client)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("muse: outbox 扫描异常: %s", e)
        await asyncio.sleep(_WATCH_INTERVAL_S)
