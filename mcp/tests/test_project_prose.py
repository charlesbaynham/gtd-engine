"""Prose on a project page: `append_project_note` and `set_project_goal`.

The invariant these guard is that prose lands *outside* `## Next Actions`
(FORMAT.md §6): whatever else a note does, the actions section and the items
in it come back byte-identical, and a note's own bullet list never becomes an
action.
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
"""


def _a_vault(tmp_path: Path, page: str = PAGE) -> Path:
    work = tmp_path / "vault"
    shutil.copytree(MCP_FIXTURES / "mcp-create-project" / "input", work)
    (work / "Project details" / "Sample.md").write_text(page, encoding="utf-8")
    return work


def _note(root: Path, **kwargs):
    vault = load_vault(root)
    result = ops.append_project_note(vault, TODAY, project="Sample", **kwargs)
    save_vault(vault)
    return result, (root / "Project details" / "Sample.md").read_text(encoding="utf-8")


def test_note_lands_outside_next_actions(tmp_path):
    root = _a_vault(tmp_path)
    _result, text = _note(root, text="Tom takes the cryostat.")
    assert text.startswith(PAGE.rstrip("\n"))
    assert text.endswith("## Notes\n\n### 2026-09-10\n\nTom takes the cryostat.\n")


def test_second_note_on_the_same_day_joins_that_days_subheading(tmp_path):
    root = _a_vault(tmp_path)
    _note(root, text="First thought.")
    _result, text = _note(root, text="Second thought.")
    assert text.count("### 2026-09-10") == 1
    assert text.endswith("First thought.\n\nSecond thought.\n")

    vault = load_vault(root)
    ops.append_project_note(vault, date(2026, 9, 11), project="Sample", text="Next day.")
    save_vault(vault)
    assert "### 2026-09-11\n\nNext day." in (root / "Project details" / "Sample.md").read_text(encoding="utf-8")


def test_note_bullets_are_not_actions(tmp_path):
    root = _a_vault(tmp_path)
    _note(root, text="Decisions:\n\n- single ion\n- pin the Nix env")
    vault = load_vault(root)
    page = vault.projects["Project details/Sample.md"]
    assert [i.text for i in page.items] == ["Existing action", "Second action"]


def test_note_into_an_existing_named_section(tmp_path):
    root = _a_vault(tmp_path, PAGE + "\n## Design\n\nThe first paragraph.\n")
    result, text = _note(root, text="A later paragraph.", heading="Design", dated=False)
    assert result.payload == {"heading": "Design", "created_section": False, "dated": None}
    assert text.endswith("The first paragraph.\n\nA later paragraph.\n")
    assert "## Notes" not in text


def test_note_into_a_section_that_precedes_next_actions(tmp_path):
    """A `## Notes` above `## Next Actions` shifts every item line down; the
    page's parsed items must still be the right ones afterwards."""
    page = "# Goal\n\nShip the sample.\n\n## Notes\n\nOld note.\n" + PAGE.split("Ship the sample.\n", 1)[1]
    root = _a_vault(tmp_path, page)
    vault = load_vault(root)
    ops.append_project_note(vault, TODAY, project="Sample", text="New note.", dated=False)
    loaded = vault.projects["Project details/Sample.md"]
    assert [i.text for i in loaded.items] == ["Existing action", "Second action"]
    save_vault(vault)
    text = (root / "Project details" / "Sample.md").read_text(encoding="utf-8")
    assert text.endswith("- [ ] Second action\n")
    assert "Old note.\n\nNew note.\n" in text


def test_note_refuses_the_actions_section_and_block_headings(tmp_path):
    root = _a_vault(tmp_path)
    with pytest.raises(ops.OpError, match="add_project_action"):
        _note(root, text="Not an action", heading="Next Actions")
    with pytest.raises(ops.OpError, match="level-1/2 heading"):
        _note(root, text="## Next Actions\n\n- [ ] smuggled")
    with pytest.raises(ops.OpError, match="must not be blank"):
        _note(root, text="   \n\n  ")


def test_set_goal_rewrites_only_the_goal_paragraph(tmp_path):
    root = _a_vault(tmp_path)
    vault = load_vault(root)
    result = ops.set_project_goal(vault, TODAY, project="Sample", goal="Ship it on the new rig.")
    save_vault(vault)
    assert result.payload["previous_goal"] == "Ship the sample."
    text = (root / "Project details" / "Sample.md").read_text(encoding="utf-8")
    assert text == PAGE.replace("Ship the sample.", "Ship it on the new rig.")


def test_set_goal_on_a_page_with_no_goal_prose(tmp_path):
    root = _a_vault(tmp_path, "# Goal\n\n## Next Actions\n\n- [ ] Existing action\n")
    vault = load_vault(root)
    result = ops.set_project_goal(vault, TODAY, project="Sample", goal="A goal at last.")
    save_vault(vault)
    assert result.payload["previous_goal"] is None
    assert (root / "Project details" / "Sample.md").read_text(encoding="utf-8") == (
        "# Goal\n\nA goal at last.\n\n## Next Actions\n\n- [ ] Existing action\n"
    )


def test_set_goal_refuses_blank_and_headings(tmp_path):
    root = _a_vault(tmp_path)
    vault = load_vault(root)
    with pytest.raises(ops.OpError, match="must not be blank"):
        ops.set_project_goal(vault, TODAY, project="Sample", goal="  ")
    with pytest.raises(ops.OpError, match="heading"):
        ops.set_project_goal(vault, TODAY, project="Sample", goal="# Goal")


def test_get_project_include_body(tmp_path):
    root = _a_vault(tmp_path, PAGE + "\n## Notes\n\nA note the item view never shows.\n")
    vault = load_vault(root)
    assert views.get_project(vault, "Sample")["body"] is None
    body = views.get_project(vault, "Sample", include_body=True)["body"]
    assert "A note the item view never shows." in body
    assert body == (root / "Project details" / "Sample.md").read_text(encoding="utf-8").rstrip("\n")
