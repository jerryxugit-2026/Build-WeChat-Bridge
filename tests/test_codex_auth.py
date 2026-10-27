"""Unit tests for wechatbridge.codex auth promote/re-link (symlink credential sync).

Mirrors tests/test_grok_auth.py: the CLI rewrites auth.json via
temp-file + rename, which replaces the session symlink with a regular file.
The bridge must promote that regular file back to host atomically (creating
the host dir when missing) and then re-link the session. All tests mock
_host_codex_dir to a temp dir; the real /root/.codex/auth.json is never
touched. Fixtures are fake {"probe": ...} JSON — no real tokens.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

from wechatbridge.codex import (
    _session_auth_is_regular,
    _sync_codex_auth,
)


def _write_json(path: str, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def _read_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _is_symlink_to(path: str, target: str) -> bool:
    return os.path.islink(path) and os.path.realpath(path) == os.path.realpath(target)


class CodexAuthSyncTest(unittest.TestCase):
    """_sync_codex_auth promote / re-link behavior."""

    def setUp(self):
        self.td = tempfile.mkdtemp(prefix="wb-codex-auth-")
        self.host_dir = os.path.join(self.td, "host_codex")
        os.makedirs(self.host_dir, mode=0o700)
        self.codex_dir = os.path.join(self.td, "session", ".codex")
        os.makedirs(self.codex_dir, mode=0o700)
        self.host_auth = os.path.join(self.host_dir, "auth.json")
        self.dest = os.path.join(self.codex_dir, "auth.json")
        self._host_patcher = mock.patch(
            "wechatbridge.codex._host_codex_dir", return_value=self.host_dir
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

        self.assertTrue(_sync_codex_auth(self.codex_dir))
        self.assertEqual(_read_json(self.host_auth), {"probe": "new"})
        self.assertTrue(_is_symlink_to(self.dest, self.host_auth))

    def test_cli_rename_over_symlink_promotes_on_next_sync(self):
        # start as a correct symlink
        _write_json(self.host_auth, {"probe": "old"})
        self.assertTrue(_sync_codex_auth(self.codex_dir))
        self.assertTrue(_is_symlink_to(self.dest, self.host_auth))

        # simulate CLI refresh: temp file + os.replace replaces the symlink
        # with a regular file holding the new credentials
        fd, tmp = tempfile.mkstemp(dir=self.codex_dir, prefix=".auth.json.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"probe": "new"}, f)
        os.replace(tmp, self.dest)
        self.assertTrue(_session_auth_is_regular(self.dest))

        # next message sync must promote and re-link
        self.assertTrue(_sync_codex_auth(self.codex_dir))
        self.assertEqual(_read_json(self.host_auth), {"probe": "new"})
        self.assertTrue(_is_symlink_to(self.dest, self.host_auth))

    def test_correct_symlink_untouched(self):
        _write_json(self.host_auth, {"probe": "old"})
        self.assertTrue(_sync_codex_auth(self.codex_dir))
        link_target = os.readlink(self.dest)

        self.assertTrue(_sync_codex_auth(self.codex_dir))
        self.assertEqual(os.readlink(self.dest), link_target)
        self.assertEqual(_read_json(self.host_auth), {"probe": "old"})

    def test_missing_session_file_creates_symlink(self):
        _write_json(self.host_auth, {"probe": "old"})
        self.assertTrue(_sync_codex_auth(self.codex_dir))
        self.assertTrue(_is_symlink_to(self.dest, self.host_auth))

    def test_promote_failure_keeps_session_file(self):
        _write_json(self.dest, {"probe": "new"})
        _write_json(self.host_auth, {"probe": "old"})
        with mock.patch(
            "wechatbridge.codex._atomic_copy_auth", return_value=False
        ) as copy_mock:
            self.assertTrue(_sync_codex_auth(self.codex_dir))

        # session file must NOT be unlinked; host must NOT be overwritten
        self.assertTrue(_session_auth_is_regular(self.dest))
        self.assertEqual(_read_json(self.dest), {"probe": "new"})
        self.assertEqual(_read_json(self.host_auth), {"probe": "old"})
        copy_mock.assert_called_once_with(self.dest, self.host_auth)

    def test_host_missing_regular_session_promotes_and_creates_host_dir(self):
        # no host dir / auth.json at all; session has refreshed credentials
        shutil.rmtree(self.host_dir)
        _write_json(self.dest, {"probe": "new"})

        self.assertTrue(_sync_codex_auth(self.codex_dir))
        self.assertEqual(_read_json(self.host_auth), {"probe": "new"})
        self.assertTrue(_is_symlink_to(self.dest, self.host_auth))

    def test_empty_session_file_not_promoted(self):
        # CLI left a 0-byte / half-written auth.json behind: must not be
        # promoted over host credentials, and the session file stays put
        _write_json(self.host_auth, {"probe": "old"})
        open(self.dest, "wb").close()
        self.assertTrue(_session_auth_is_regular(self.dest))

        self.assertTrue(_sync_codex_auth(self.codex_dir))
        self.assertEqual(_read_json(self.host_auth), {"probe": "old"})
        self.assertTrue(_session_auth_is_regular(self.dest))
        self.assertEqual(os.path.getsize(self.dest), 0)


class CodexRunPromoteTest(unittest.IsolatedAsyncioTestCase):
    """run_codex / _run_codex_subcommand harvest (promote) after exit."""

    async def asyncSetUp(self):
        self.td = tempfile.mkdtemp(prefix="wb-codex-run-")
        self.session_dir = os.path.join(self.td, "s1")
        os.makedirs(os.path.join(self.session_dir, ".codex"), mode=0o700)
        self._patchers = [
            mock.patch(
                "wechatbridge.codex.ensure_user_codex",
                return_value=self.session_dir,
            ),
            mock.patch("wechatbridge.codex.is_dangerous", return_value=False),
            mock.patch("wechatbridge.codex.is_first_message", return_value=True),
            mock.patch("wechatbridge.codex.load_prefs", return_value={}),
            mock.patch(
                "wechatbridge.codex._resolve_add_dirs", return_value=[]
            ),
            mock.patch(
                "wechatbridge.codex._build_codex_command",
                return_value=["codex", "exec", "--json", "hi"],
            ),
            mock.patch(
                "wechatbridge.codex._snapshot_regular_files", return_value=[]
            ),
            mock.patch(
                "wechatbridge.codex.sanitize_env",
                side_effect=lambda d: {"HOME": d},
            ),
            mock.patch(
                "wechatbridge.codex._collect_fallback_artifacts",
                return_value=[],
            ),
            mock.patch(
                "wechatbridge.codex._parse_codex_output",
                return_value=("ok", [], None, False),
            ),
        ]
        for p in self._patchers:
            p.start()

    async def asyncTearDown(self):
        for p in self._patchers:
            p.stop()
        shutil.rmtree(self.td, ignore_errors=True)

    def _fake_proc(self, returncode: int = 0, out: bytes = b"", err: bytes = b""):
        proc = MagicMock()
        proc.returncode = returncode
        proc.communicate = AsyncMock(return_value=(out, err))
        return proc

    async def test_run_codex_exit_harvests_and_keeps_session_env(self):
        from wechatbridge import codex as codex_mod

        proc = self._fake_proc(returncode=0, out=b"ok")
        with mock.patch.object(
            codex_mod.asyncio,
            "create_subprocess_exec",
            AsyncMock(return_value=proc),
        ) as spawn:
            with mock.patch.object(
                codex_mod, "_promote_session_auth", return_value=True
            ) as promote:
                display, artifacts = await codex_mod.run_codex(
                    "hi", "u1", timeout=30
                )

        promote.assert_called_once_with(
            os.path.join(self.session_dir, ".codex")
        )
        self.assertEqual(display, "ok")
        self.assertEqual(artifacts, [])
        env = spawn.call_args.kwargs["env"]
        # HOME still points at the session dir; CODEX_HOME stays session-private
        self.assertEqual(env["HOME"], self.session_dir)
        self.assertEqual(
            env["CODEX_HOME"], os.path.join(self.session_dir, ".codex")
        )

    async def test_run_codex_nonzero_exit_still_promotes(self):
        from wechatbridge import codex as codex_mod

        proc = self._fake_proc(returncode=1, err=b"boom")
        with mock.patch.object(
            codex_mod.asyncio,
            "create_subprocess_exec",
            AsyncMock(return_value=proc),
        ):
            with mock.patch.object(
                codex_mod, "_promote_session_auth", return_value=True
            ) as promote:
                display, artifacts = await codex_mod.run_codex(
                    "hi", "u1", timeout=30
                )

        promote.assert_called_once_with(
            os.path.join(self.session_dir, ".codex")
        )
        self.assertEqual(artifacts, [])
        # 非零退出 → 格式化错误气泡（"boom" 无已知分类时是通用执行失败）
        self.assertIn("❌", display)

    async def test_subcommand_exit_harvests(self):
        from wechatbridge import codex as codex_mod

        proc = self._fake_proc(returncode=0, out=b"models list")
        with mock.patch.object(
            codex_mod.asyncio,
            "create_subprocess_exec",
            AsyncMock(return_value=proc),
        ):
            with mock.patch.object(
                codex_mod, "_promote_session_auth", return_value=True
            ) as promote:
                result = await codex_mod._run_codex_subcommand(
                    ["debug", "models"], "u1"
                )

        promote.assert_called_once_with(
            os.path.join(self.session_dir, ".codex")
        )
        self.assertIn("models", result)


if __name__ == "__main__":
    unittest.main()
