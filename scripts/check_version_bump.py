#!/usr/bin/env python3
"""Fail when package code changed since the last release tag without a version bump.

Usage (from repo root):

    python scripts/check_version_bump.py

Exit codes:
  0  OK (no package changes, or __version__ advanced and CHANGELOG has the section)
  1  Missing bump / CHANGELOG / unreadable version
  2  Not a git checkout or git command failed

Package-relevant paths (any change since the latest vX.Y.Z tag requires a bump):
  - wechatbridge/**
  - pyproject.toml
  - tests/** (except pure renames of docs-only fixtures — whole tests/ counts)

Docs-only paths (README*, CONTRIBUTING.md, LICENSE, .github docs comments, etc.)
do not by themselves require a bump.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INIT_PY = ROOT / "wechatbridge" / "__init__.py"
CHANGELOG = ROOT / "CHANGELOG.md"
VERSION_RE = re.compile(r"""^__version__\s*=\s*["']([^"']+)["']""", re.M)
TAG_RE = re.compile(r"^v(\d+\.\d+\.\d+)$")


def _run_git(*args: str) -> str:
    try:
        out = subprocess.check_output(
            ["git", *args],
            cwd=ROOT,
            text=True,
            stderr=subprocess.STDOUT,
        )
    except FileNotFoundError:
        print("ERROR: git not found on PATH", file=sys.stderr)
        sys.exit(2)
    except subprocess.CalledProcessError as e:
        print(f"ERROR: git {' '.join(args)} failed:\n{e.output}", file=sys.stderr)
        sys.exit(2)
    return out


def _parse_version(v: str) -> tuple[int, ...]:
    parts = v.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise ValueError(f"not MAJOR.MINOR.PATCH: {v!r}")
    return tuple(int(p) for p in parts)


def _is_package_relevant(path: str) -> bool:
    path = path.replace("\\", "/").lstrip("./")
    if path.startswith("wechatbridge/"):
        return True
    if path == "pyproject.toml":
        return True
    if path.startswith("tests/"):
        return True
    return False


def _read_version() -> str:
    text = INIT_PY.read_text(encoding="utf-8")
    m = VERSION_RE.search(text)
    if not m:
        print(f"ERROR: __version__ not found in {INIT_PY}", file=sys.stderr)
        sys.exit(1)
    return m.group(1)


def _latest_tag_version() -> str | None:
    raw = _run_git("tag", "-l", "v*")
    found: list[str] = []
    for line in raw.splitlines():
        m = TAG_RE.match(line.strip())
        if m:
            found.append(m.group(1))
    if not found:
        return None
    found.sort(key=_parse_version)
    return found[-1]


def _changed_files_since(tag: str) -> list[str]:
    # Committed changes after the tag…
    committed = _run_git("diff", "--name-only", f"v{tag}", "HEAD")
    # …plus unstaged/staged working tree (so local debt is caught before commit)
    dirty = _run_git("diff", "--name-only", "HEAD")
    staged = _run_git("diff", "--name-only", "--cached")
    paths: set[str] = set()
    for block in (committed, dirty, staged):
        for line in block.splitlines():
            line = line.strip()
            if line:
                paths.add(line)
    return sorted(paths)


def main() -> int:
    current = _read_version()
    try:
        current_t = _parse_version(current)
    except ValueError as e:
        print(f"ERROR: invalid __version__: {e}", file=sys.stderr)
        return 1

    last = _latest_tag_version()
    if last is None:
        print("OK: no vX.Y.Z tags yet; skip version-bump check")
        return 0

    changed = _changed_files_since(last)
    relevant = [p for p in changed if _is_package_relevant(p)]
    if not relevant:
        print(f"OK: no package-relevant changes since v{last} (current={current})")
        return 0

    last_t = _parse_version(last)
    if current_t <= last_t:
        print(
            f"ERROR: package code changed since v{last} but __version__ is still "
            f"{current!r} (must be strictly greater).",
            file=sys.stderr,
        )
        print("Changed package-relevant files:", file=sys.stderr)
        for p in relevant:
            print(f"  {p}", file=sys.stderr)
        print(
            "Bump wechatbridge/__init__.py (__version__), add ## [X.Y.Z] to "
            "CHANGELOG.md, then re-run: python scripts/check_version_bump.py",
            file=sys.stderr,
        )
        return 1

    cl = CHANGELOG.read_text(encoding="utf-8") if CHANGELOG.is_file() else ""
    heading = f"## [{current}]"
    if heading not in cl:
        print(
            f"ERROR: CHANGELOG.md is missing a formal section {heading} "
            f"(do not leave user-visible work only under [Unreleased]).",
            file=sys.stderr,
        )
        return 1

    print(
        f"OK: __version__={current} > last tag v{last}; "
        f"CHANGELOG has {heading}; {len(relevant)} package file(s) changed"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
