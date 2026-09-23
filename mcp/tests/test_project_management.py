"""Managing a project as a whole and its steps as ordinary actions:
`route_project_action`, `rename_project`, `archive_project`, and the
surfacing-row upkeep `update`/`delete` do on a project item (FORMAT.md §6).
"""
from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import pytest

from gtd_ci.model import load_vault, save_vault
from gtd_mcp import ops, views

MCP_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
TODAY = date(2026, 9, 10)

PAGE = """# Goal

Ship the sample.

## Next Actions

- [ ] Existing action
- [ ] Second action

## Notes

See also [[Sample#Goal|the goal]].
"""


def _a_vault(tmp_path: Path) -> Path:
    work = tmp_path / "vault"
    shutil.copytree(MCP_FIXTURES / "mcp-create-project" / "input", work)
    (work / "Project details" / "Sample.md").write_text(PAGE, encoding="utf-8")
    return work


def _read(root: Path, rel: str) -> str:
    return (root / rel).read_text(encoding="utf-8")


def _item_handle(vault, text: str, stem: str = "Sample") -> str:
    return next(it["handle"] for it in views.get_project(vault, stem)["items"] if it["text"] == text)


# --- route_project_action ---------------------------------------------------------


def test_delegating_a_step_keeps_it_on_the_page_and_moves_its_row(tmp_path):
    root = _a_vault(tmp_path)
    vault = load_vault(root)
    r = ops.route_project_action(
        vault, TODAY, handle=_item_handle(vault, "Existing action"), to="delegated",
        person="Tom", chase_by="2026-09-20",
    )
    save_vault(vault)
    assert r.payload["removed_next_action_rows"] == 1
    assert "- [ ] Existing action" in _read(root, "Project details/Sample.md")
    assert "Existing action" not in _read(root, "Next actions.md")
    # The priority the old row carried travels with it.
    assert "| Existing action | Tom | 2026-09-20 | 2 | [[Sample]] |" in _read(root, "Delegated.md")

    vault = load_vault(root)
    item = next(it for it in views.get_project(vault, "Sample")["items"] if it["text"] == "Existing action")
    assert item["surfaced"] == "Delegated.md"


def test_completing_the_delegated_row_ticks_the_step(tmp_path):
    root = _a_vault(tmp_path)
    vault = load_vault(root)
    ops.route_project_action(vault, TODAY, handle=_item_handle(vault, "Existing action"), to="delegated", person="Tom")
    save_vault(vault)
    vault = load_vault(root)
    row = views.delegated(vault)[0]
    ops.complete(vault, TODAY, handle=row["handle"])
    save_vault(vault)
    assert "- [x] Existing action" in _read(root, "Project details/Sample.md")


def test_deferring_and_scheduling_a_step(tmp_path):
    root = _a_vault(tmp_path)
    vault = load_vault(root)
    ops.route_project_action(
        vault, TODAY, handle=_item_handle(vault, "Second action"), to="tickler", bucket="Next month",
    )
    ops.route_project_action(
        vault, TODAY, handle=_item_handle(vault, "Existing action"), to="scheduled", date="2026-10-01",
    )
    save_vault(vault)
    assert "Second action [[Sample]]" in _read(root, "Tickler/Next month.md")
    assert "| Existing action | 2026-10-01 |" in _read(root, "Scheduled.md")
    assert "Existing action" not in _read(root, "Next actions.md")

    # Re-activating replaces the tickler line rather than adding a second view.
    vault = load_vault(root)
    ops.route_project_action(vault, TODAY, handle=_item_handle(vault, "Second action"), to="next-actions")
    save_vault(vault)
    assert "Second action" not in _read(root, "Tickler/Next month.md")
    assert "| Second action | [[Sample]] |" in _read(root, "Next actions.md")


def test_a_bad_route_changes_nothing(tmp_path):
    root = _a_vault(tmp_path)
    vault = load_vault(root)
    with pytest.raises(ops.OpError, match="person"):
        ops.route_project_action(vault, TODAY, handle=_item_handle(vault, "Existing action"), to="delegated")
    assert not vault.dirty
    with pytest.raises(ops.OpError, match="not a project item"):
        ops.route_project_action(vault, TODAY, handle=views.next_actions(vault)[0]["handle"], to="delegated",
                                 person="Tom")


# --- update / delete on a project item -------------------------------------------------


def test_rewording_a_step_rewords_its_row(tmp_path):
    root = _a_vault(tmp_path)
    vault = load_vault(root)
    ops.update(vault, TODAY, handle=_item_handle(vault, "Existing action"), text="Reworded action")
    save_vault(vault)
    assert "- [ ] Reworded action" in _read(root, "Project details/Sample.md")
    assert "| Reworded action | [[Sample]] |  | 2 |" in _read(root, "Next actions.md")


def test_deleting_a_step_removes_its_row_and_later_handles_still_resolve(tmp_path):
    root = _a_vault(tmp_path)
    vault = load_vault(root)
    r = ops.delete(vault, TODAY, handle=_item_handle(vault, "Existing action"))
    assert r.payload["removed_next_action_rows"] == 1
    # The page's items were re-parsed, so the next edit lands on the right line.
    ops.update(vault, TODAY, handle=_item_handle(vault, "Second action"), text="Second, reworded")
    save_vault(vault)
    page = _read(root, "Project details/Sample.md")
    assert "Existing action" not in page
    assert "- [ ] Second, reworded" in page
    assert "## Notes" in page
    assert "Existing action" not in _read(root, "Next actions.md")


# --- rename_project -------------------------------------------------------------------


def test_rename_moves_the_page_and_rewrites_every_link(tmp_path):
    root = _a_vault(tmp_path)
    (root / "Reference").mkdir()
    (root / "Reference" / "Ref.md").write_text("About [[Sample]].\n", encoding="utf-8")
    vault = load_vault(root)
    ops.route_project_action(vault, TODAY, handle=_item_handle(vault, "Second action"), to="tickler",
                             bucket="Next week")
    r = ops.rename_project(vault, TODAY, project="Sample", new_name="Better name")
    # A later op in the same run finds the page under its new name.
    ops.add_project_action(vault, TODAY, project="Better name", text="Third action")
    save_vault(vault)

    assert not (root / "Project details" / "Sample.md").exists()
    page = _read(root, "Project details/Better name.md")
    assert "- [ ] Third action" in page
    assert "[[Better name#Goal|the goal]]" in page
    assert "| Existing action | [[Better name]] |" in _read(root, "Next actions.md")
    assert "Second action [[Better name]]" in _read(root, "Tickler/Next week.md")
    assert r.payload["unmanaged_links"] == 1
    assert _read(root, "Reference/Ref.md") == "About [[Sample]].\n"

    vault = load_vault(root)
    proj = views.get_project(vault, "Better name")
    assert proj is not None
    assert all(it["surfaced"] for it in proj["items"] if it["text"] != "Third action")


@pytest.mark.parametrize("bad, match", [
    ("", "blank"), ("a/b", "file name"), ("Sample", "already called"),
])
def test_rename_rejects_bad_names(tmp_path, bad, match):
    root = _a_vault(tmp_path)
    vault = load_vault(root)
    with pytest.raises(ops.OpError, match=match):
        ops.rename_project(vault, TODAY, project="Sample", new_name=bad)


def test_rename_refuses_to_clobber_another_project(tmp_path):
    root = _a_vault(tmp_path)
    (root / "Project details" / "Other.md").write_text("# Goal\n\nX\n\n## Next Actions\n\n- [ ] y\n", encoding="utf-8")
    vault = load_vault(root)
    with pytest.raises(ops.OpError, match="already exists"):
        ops.rename_project(vault, TODAY, project="Sample", new_name="other")


# --- archive_project ------------------------------------------------------------------


def test_archive_keeps_edits_made_earlier_in_the_same_run(tmp_path):
    root = _a_vault(tmp_path)
    vault = load_vault(root)
    ops.complete(vault, TODAY, handle=_item_handle(vault, "Second action"))
    ops.archive_project(vault, TODAY, name="Sample")
    save_vault(vault)
    assert not (root / "Project details" / "Sample.md").exists()
    assert "- [x] Second action" in _read(root, "Project details/Done/Sample.md")
    assert "Sample" not in _read(root, "Next actions.md")
