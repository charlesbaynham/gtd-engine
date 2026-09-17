"""Refresh a vault's FORMAT.md and CLAUDE.md from this package's canonical copies.

The marker line splits CLAUDE.md into an engine-owned body (replaced here on
every run) and a vault-owned tail (kept byte-for-byte). `migrate` handles the
one-time transition of a vault into that shape; this module only refreshes
one already in it (or creates a fresh vault's copy from scratch).
"""
from __future__ import annotations

import sys
from importlib import resources
from pathlib import Path

MARKER = "<!-- gtd-engine: everything above this line is refreshed by the nightly job. Edit below it. -->"

_STUB_TAIL = "\n## This vault\n\n(no vault-specific notes yet.)\n"


def doc_text(name: str) -> str:
    return resources.files("gtd_ci").joinpath("docs", name).read_text(encoding="utf-8")


def refresh_format(vault: Path, dry_run: bool = False) -> str:
    dst = vault / "FORMAT.md"
    new = doc_text("FORMAT.md")
    old = dst.read_text(encoding="utf-8") if dst.is_file() else None
    result = "unchanged FORMAT.md" if old == new else "refreshed FORMAT.md"
    if result.startswith("refreshed") and not dry_run:
        dst.write_text(new, encoding="utf-8")
    print(result)
    return result


def refresh_claude(vault: Path, dry_run: bool = False) -> str:
    dst = vault / "CLAUDE.md"
    body = doc_text("CLAUDE.md")

    if not dst.is_file():
        new = f"{body}\n{MARKER}\n{_STUB_TAIL}"
        if not dry_run:
            dst.write_text(new, encoding="utf-8")
        print("refreshed CLAUDE.md")
        return "refreshed CLAUDE.md"

    old = dst.read_text(encoding="utf-8")
    if MARKER not in old:
        print(f"{dst}: no marker, left as is", file=sys.stderr)
        print("unchanged CLAUDE.md")
        return "unchanged CLAUDE.md"

    tail = old.split(MARKER, 1)[1]
    new = f"{body}\n{MARKER}{tail}"
    result = "unchanged CLAUDE.md" if new == old else "refreshed CLAUDE.md"
    if result.startswith("refreshed") and not dry_run:
        dst.write_text(new, encoding="utf-8")
    print(result)
    return result


def refresh_docs(vault: Path, dry_run: bool = False) -> list[str]:
    if not vault.is_dir():
        raise NotADirectoryError(vault)
    return [refresh_format(vault, dry_run), refresh_claude(vault, dry_run)]
