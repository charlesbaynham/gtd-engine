"""Reads must never rewrite: every view function, over every fixture vault,
must leave `vault.dirty` empty and every file byte-identical."""
from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import pytest

from gtd_ci.model import load_vault, save_vault
from gtd_mcp import brief, views

CI_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
MCP_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _input_dirs() -> list[Path]:
    dirs = []
    for base in (CI_FIXTURES, MCP_FIXTURES):
        if base.is_dir():
            dirs += [p / "input" for p in base.iterdir() if (p / "input").is_dir()]
    return sorted(dirs)


def _all_bytes(root: Path) -> dict[Path, bytes]:
    return {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("input_dir", _input_dirs(), ids=lambda p: f"{p.parent.name}")
def test_views_never_mutate(input_dir: Path, tmp_path: Path) -> None:
    work = tmp_path / "vault"
    shutil.copytree(input_dir, work)
    before = _all_bytes(work)

    vault = load_vault(work)
    views.next_actions(vault)
    views.delegated(vault)
    views.scheduled(vault)
    views.inbox(vault)
    views.tickler(vault)
    views.projects(vault, include_done=True)
    brief.get_brief(vault, date(2026, 9, 10))

    assert vault.dirty == set(), "a read view marked something dirty"

    save_vault(vault)
    assert _all_bytes(work) == before
