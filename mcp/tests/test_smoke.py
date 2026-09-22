"""End-to-end smoke test: build the real FastMCP server in-process, list its
tools, and call get_brief against a fixture vault."""
from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

from gtd_mcp.config import Config
from gtd_mcp.server import create_server

FIXTURE_VAULT = Path(__file__).resolve().parents[1].parent / "fixtures" / "round-trip" / "input"


def _config(vault_dir: Path) -> Config:
    return Config(
        vault_dir=str(vault_dir), remote_url=None, push_token=None, push_user="oauth2", webhook_secret=None,
        allowed_users=frozenset(), host="127.0.0.1", port=8000, poll_seconds=0,
        git_name="Test", git_email="test@test", branch="master",
    )


def test_server_lists_tools_and_answers_get_brief(tmp_path: Path) -> None:
    vault_dir = tmp_path / "vault"
    shutil.copytree(FIXTURE_VAULT, vault_dir)

    mcp, _store = create_server(_config(vault_dir))

    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    for expected in [
        "get_brief", "list_next_actions", "list_inbox", "list_delegated", "list_scheduled",
        "list_tickler", "list_projects", "get_project", "search", "lint",
        "capture", "add_next_action", "delegate", "schedule", "add_to_tickler",
        "triage", "complete", "update", "delete", "create_project",
        "add_project_action", "append_project_note", "set_project_goal",
        "tick_project_action", "archive_project", "rename_project",
        "route_project_action", "run_maintenance",
    ]:
        assert expected in names, f"tool {expected!r} missing"

    result = asyncio.run(mcp.call_tool("get_brief", {"today": "2026-09-10"}))
    payload = _unwrap(result)
    assert payload["today"] == "2026-09-10"
    assert isinstance(payload["top_actions"], list)
    assert payload["inbox"]["count"] >= 1


def _unwrap(result):
    """`call_tool` returns either a structured dict or a content-block
    sequence carrying one JSON text block, depending on the tool's return
    type annotation; handle both."""
    if isinstance(result, dict):
        return result.get("result", result)
    for block in result:
        text = getattr(block, "text", None)
        if text is not None:
            return json.loads(text)
    raise AssertionError(f"no JSON content block in {result!r}")
