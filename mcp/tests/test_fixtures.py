"""Runs every case under mcp/fixtures/ against the ops layer directly
(no git — that plumbing is exercised separately in test_store.py).

case.yml keys: `today`, `op` (a gtd_mcp.ops function name), `args` (kwargs
for it; an `args.handle_of: {kind, match}` entry is resolved to a real
handle via the read views before the op runs, so fixtures don't hardcode
content hashes), and an optional `expect` (a subset of the op's
OpResult.payload to assert).
"""
from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import pytest
import yaml

from gtd_ci.model import load_vault, save_vault
from gtd_mcp import ops as opsmod
from gtd_mcp import views

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"

_OPS = {
    name: getattr(opsmod, name)
    for name in [
        "capture", "add_next_action", "delegate", "schedule", "add_to_tickler",
        "triage", "complete", "update", "delete", "create_project",
        "add_project_action", "append_project_note", "set_project_goal",
        "tick_project_action", "archive_project", "run_maintenance",
    ]
}


def _all_files(root: Path) -> set[Path]:
    return {p.relative_to(root) for p in root.rglob("*") if p.is_file()}


def _case_dirs() -> list[Path]:
    if not FIXTURES_DIR.is_dir():
        return []
    return sorted(p for p in FIXTURES_DIR.iterdir() if p.is_dir())


def resolve_handle_of(vault, spec: dict) -> str:
    kind, match = spec["kind"], spec["match"]
    if kind == "next-actions":
        items, key = views.next_actions(vault), "action"
    elif kind == "delegated":
        items, key = views.delegated(vault), "thing"
    elif kind == "scheduled":
        items, key = views.scheduled(vault), "thing"
    elif kind == "inbox":
        items, key = views.inbox(vault), "text"
    elif kind.startswith("tickler:"):
        items, key = views.tickler(vault, kind.split(":", 1)[1]), "text"
    elif kind.startswith("project:"):
        stem = kind.split(":", 1)[1]
        page = relpath = None
        for rp, p in vault.projects.items():
            if p.stem == stem:
                page, relpath = p, rp
        assert page is not None, f"no loaded project page named {stem!r}"
        items, key = views.project(vault, page, relpath)["items"], "text"
    else:
        raise ValueError(f"unknown handle_of kind {kind!r}")
    for item in items:
        if match in item[key]:
            return item["handle"]
    raise AssertionError(f"no {kind} item matching {match!r}")


@pytest.mark.parametrize("case_dir", _case_dirs(), ids=lambda p: p.name)
def test_fixture(case_dir: Path, tmp_path: Path) -> None:
    case = yaml.safe_load((case_dir / "case.yml").read_text(encoding="utf-8"))
    today = date.fromisoformat(str(case["today"]))
    op = _OPS[case["op"]]
    args = dict(case.get("args") or {})

    work = tmp_path / "vault"
    shutil.copytree(case_dir / "input", work)
    vault = load_vault(work)

    handle_spec = args.pop("handle_of", None)
    if handle_spec is not None:
        args["handle"] = resolve_handle_of(vault, handle_spec)

    result = op(vault, today, **args)
    save_vault(vault)

    expected_dir = case_dir / "expected"
    expected_files = _all_files(expected_dir)
    actual_files = _all_files(work)
    assert actual_files == expected_files, f"{case_dir.name}: file set mismatch"
    for rel in expected_files:
        assert (work / rel).read_bytes() == (expected_dir / rel).read_bytes(), f"{case_dir.name}: {rel} differs"

    for key, value in (case.get("expect") or {}).items():
        assert result.payload.get(key) == value, f"{case_dir.name}: payload[{key!r}] mismatch"
