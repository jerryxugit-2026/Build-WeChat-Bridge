"""Unit tests for wechatbridge.grok auth promote/re-link (symlink credential sync).

Covers the credential-refresh bug: the CLI rewrites auth.json via
temp-file + rename, which replaces the session symlink with a regular file.
The bridge must promote that regular file back to host atomically and then
re-link the session. All tests mock _host_grok_dir to a temp dir; the real
/root/.grok/auth.json is never touched. Fixtures are fake {"probe": ...}
JSON — no real tokens.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

from wechatbridge.grok import (
    _session_auth_is_regular,
    _sync_grok_auth,
)


def _write_json(path: str, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def _read_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _is_symlink_to(path: str, target: str) -> bool:
    return os.path.islink(path) and os.path.realpath(path) == os.path.realpath(target)


class GrokAuthSyncTest(unittest.TestCase):
    """_sync_grok_auth promote / re-link behavior."""

    def setUp(self):
        self.td = tempfile.mkdtemp(prefix="wb-grok-auth-")
        self.host_dir = os.path.join(self.td, "host_grok")
        os.makedirs(self.host_dir, mode=0o700)
        self.grok_dir = os.path.join(self.td, "session", ".grok")
        os.makedirs(self.grok_dir, mode=0o700)
        self.host_auth = os.path.join(self.host_dir, "auth.json")
        self.dest = os.path.join(self.grok_dir, "auth.json")
        self._host_patcher = mock.patch(
            "wechatbridge.grok._host_grok_dir", return_value=self.host_dir
        )
        self._host_patcher.start()

    def tearDown(self):
        self._host_patcher.stop()
        shutil.rmtree(self.td, ignore_errors=True)

    def test_regular_session_file_promotes_to_host_and_relinks(self):
        # session carries NEW credentials as a regular file (CLI rename broke
        # the symlink); host still has OLD credentials
        _write_json(self.dest, {"probe": "new"})
        _write_json(self.host_auth, {"probe": "old"})

        self.assertTrue(_sync_grok_auth(self.grok_dir))
        self.assertEqual(_read_json(self.host_auth), {"probe": "new"})
        self.assertTrue(_is_symlink_to(self.dest, self.host_auth))

    def test_cli_rename_over_symlink_promotes_on_next_sync(self):
        # start as a correct symlink
        _write_json(self.host_auth, {"probe": "old"})
        self.assertTrue(_sync_grok_auth(self.grok_dir))
        self.assertTrue(_is_symlink_to(self.dest, self.host_auth))

        # simulate CLI refresh: temp file + os.replace replaces the symlink
        # with a regular file holding the new credentials
        fd, tmp = tempfile.mkstemp(dir=self.grok_dir, prefix=".auth.json.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"probe": "new"}, f)
        os.replace(tmp, self.dest)
        self.assertTrue(_session_auth_is_regular(self.dest))

        # next message sync must promote and re-link
        self.assertTrue(_sync_grok_auth(self.grok_dir))
        self.assertEqual(_read_json(self.host_auth), {"probe": "new"})
        self.assertTrue(_is_symlink_to(self.dest, self.host_auth))

    def test_correct_symlink_untouched(self):
        _write_json(self.host_auth, {"probe": "old"})
        self.assertTrue(_sync_grok_auth(self.grok_dir))
        link_target = os.readlink(self.dest)

        self.assertTrue(_sync_grok_auth(self.grok_dir))
        self.assertEqual(os.readlink(self.dest), link_target)
        self.assertEqual(_read_json(self.host_auth), {"probe": "old"})

    def test_missing_session_file_creates_symlink(self):
        _write_json(self.host_auth, {"probe": "old"})
        self.assertTrue(_sync_grok_auth(self.grok_dir))
        self.assertTrue(_is_symlink_to(self.dest, self.host_auth))

    def test_promote_failure_keeps_session_file(self):
        _write_json(self.dest, {"probe": "new"})
        _write_json(self.host_auth, {"probe": "old"})
        with mock.patch(
            "wechatbridge.grok._atomic_copy_auth", return_value=False
        ) as copy_mock:
            self.assertTrue(_sync_grok_auth(self.grok_dir))

        # session file must NOT be unlinked; host must NOT be overwritten
        self.assertTrue(_session_auth_is_regular(self.dest))
        self.assertEqual(_read_json(self.dest), {"probe": "new"})
        self.assertEqual(_read_json(self.host_auth), {"probe": "old"})
        copy_mock.assert_called_once_with(self.dest, self.host_auth)

    def test_host_missing_regular_session_promotes(self):
        # no host auth.json at all; session has refreshed credentials
        _write_json(self.dest, {"probe": "new"})
        self.assertTrue(_sync_grok_auth(self.grok_dir))
        self.assertEqual(_read_json(self.host_auth), {"probe": "new"})
        self.assertTrue(_is_symlink_to(self.dest, self.host_auth))

    def test_host_dir_missing_regular_session_promotes_and_creates_host_dir(self):
        # host dir removed entirely (never ran `grok login`): promote must
        # create it (0o700) before the atomic copy
        shutil.rmtree(self.host_dir)
        _write_json(self.dest, {"probe": "new"})

        self.assertTrue(_sync_grok_auth(self.grok_dir))
        self.assertEqual(_read_json(self.host_auth), {"probe": "new"})
        self.assertTrue(_is_symlink_to(self.dest, self.host_auth))

    def test_empty_session_file_not_promoted(self):
        # CLI left a 0-byte / half-written auth.json behind: must not be
        # promoted over host credentials, and the session file stays put
        _write_json(self.host_auth, {"probe": "old"})
        open(self.dest, "wb").close()
        self.assertTrue(_session_auth_is_regular(self.dest))

        self.assertTrue(_sync_grok_auth(self.grok_dir))
        self.assertEqual(_read_json(self.host_auth), {"probe": "old"})
        self.assertTrue(_session_auth_is_regular(self.dest))
        self.assertEqual(os.path.getsize(self.dest), 0)


class GrokRunPromoteTest(unittest.IsolatedAsyncioTestCase):
    """run_grok harvests (promotes) after the subprocess exits, even non-zero."""

    async def asyncSetUp(self):
        self.td = tempfile.mkdtemp(prefix="wb-grok-run-")
        self.session_dir = os.path.join(self.td, "s1")
        os.makedirs(os.path.join(self.session_dir, ".grok"), mode=0o700)
        self._patchers = [
            mock.patch(
                "wechatbridge.grok.ensure_user_grok", return_value=self.session_dir
            ),
            mock.patch("wechatbridge.grok._grok_has_credentials", return_value=True),
            mock.patch("wechatbridge.grok.is_dangerous", return_value=False),
            mock.patch("wechatbridge.grok.is_first_message", return_value=True),
            mock.patch("wechatbridge.grok.load_prefs", return_value={}),
            mock.patch("wechatbridge.grok._read_persona", return_value=None),
            mock.patch(
                "wechatbridge.grok._build_grok_command",
                return_value=["grok", "-p", "hi"],
            ),
            mock.patch(
                "wechatbridge.grok.sanitize_env",
                side_effect=lambda d: {"HOME": d},
            ),
            mock.patch(
                "wechatbridge.grok._apply_grok_runtime_env",
                side_effect=lambda env: dict(env),
            ),
            mock.patch(
                "wechatbridge.grok._parse_grok_output",
                return_value=("oops", []),
            ),
        ]
        for p in self._patchers:
            p.start()

    async def asyncTearDown(self):
        for p in self._patchers:
            p.stop()
        shutil.rmtree(self.td, ignore_errors=True)

    async def test_nonzero_exit_still_promotes(self):
        from wechatbridge import grok as grok_mod

        proc = MagicMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(return_value=(b"", b"boom"))
        with mock.patch.object(
            grok_mod.asyncio,
            "create_subprocess_exec",
            AsyncMock(return_value=proc),
        ):
            with mock.patch.object(
                grok_mod, "_promote_session_auth", return_value=True
            ) as promote:
                display, artifacts = await grok_mod.run_grok(
                    "hi", "u1", timeout=30
                )

        promote.assert_called_once_with(
            os.path.join(self.session_dir, ".grok")
        )
        self.assertEqual(artifacts, [])
        # 非零退出 → 格式化错误气泡（"boom" 无已知分类时是通用执行失败）
        self.assertIn("❌", display)

    async def test_subcommand_exit_harvests(self):
        from wechatbridge import grok as grok_mod

        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"models list", b""))
        with mock.patch.object(
            grok_mod.asyncio,
            "create_subprocess_exec",
            AsyncMock(return_value=proc),
        ):
            with mock.patch.object(
                grok_mod, "_promote_session_auth", return_value=True
            ) as promote:
                result = await grok_mod._run_grok_subcommand(["models"], "u1")

        promote.assert_called_once_with(
            os.path.join(self.session_dir, ".grok")
        )
        self.assertIn("models", result)


if __name__ == "__main__":
    unittest.main()
