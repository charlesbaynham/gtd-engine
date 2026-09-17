"""`create_project` seeds only what the caller asked for.

There is no stock first action. A project created with one carries it in
its Next Actions section and as a row in `Next actions.md`; a project
created without one is simply actionless (and therefore visibly STALLED)
rather than carrying an invented placeholder to delete.
"""
from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

from gtd_ci.model import load_vault, save_vault
from gtd_mcp import ops

MCP_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
TODAY = date(2026, 9, 15)


def _a_vault(tmp_path: Path):
    src = MCP_FIXTURES / "mcp-add-next-action-no-project" / "input"
    work = tmp_path / "vault"
    shutil.copytree(src, work)
    template = work / "Project details" / "-Project template.md"
    if not template.is_file():
        template.parent.mkdir(parents=True, exist_ok=True)
        template.write_text(
            "---\ngtd: project\n---\n\n# Goal\n\n## Next Actions\n\n- [ ] \n",
            encoding="utf-8",
        )
    return work


def _text(root: Path, rel: str) -> str:
    return (root / rel).read_text(encoding="utf-8")


def test_first_action_is_whatever_the_caller_passed(tmp_path):
    root = _a_vault(tmp_path)
    vault = load_vault(root)
    ops.create_project(vault, TODAY, name="Rewire the PSU",
                       goal="A PSU that does not hum", first_action="Order a transformer")
    save_vault(vault)

    page = _text(root, "Project details/Rewire the PSU.md")
    assert "A PSU that does not hum" in page
    assert "- [ ] Order a transformer" in page
    assert "Plan project" not in page
    assert "| Order a transformer | [[Rewire the PSU]] |" in _text(root, "Next actions.md")


def test_no_first_action_seeds_nothing(tmp_path):
    root = _a_vault(tmp_path)
    vault = load_vault(root)
    before = _text(root, "Next actions.md")
    ops.create_project(vault, TODAY, name="Rewire the PSU", goal="A quiet PSU")
    save_vault(vault)

    page = _text(root, "Project details/Rewire the PSU.md")
    assert "A quiet PSU" in page
    assert "Plan project" not in page
    # The template's empty placeholder is left as it was, not filled in.
    assert "- [ ] \n" in page or page.rstrip().endswith("- [ ]")
    assert _text(root, "Next actions.md") == before


def test_a_blank_first_action_is_the_same_as_none(tmp_path):
    root = _a_vault(tmp_path)
    vault = load_vault(root)
    before = _text(root, "Next actions.md")
    ops.create_project(vault, TODAY, name="Rewire the PSU", goal="A quiet PSU",
                       first_action="   ")
    save_vault(vault)
    assert "Plan project" not in _text(root, "Project details/Rewire the PSU.md")
    assert _text(root, "Next actions.md") == before


def test_a_new_project_is_resolvable_within_the_same_run(tmp_path):
    """create_project then add_project_action, the sequence the AI agent writes.

    The page is written straight to disk, so without registering it with the
    loaded vault the very next operation could not find the project it had
    just been asked to create.
    """
    root = _a_vault(tmp_path)
    vault = load_vault(root)
    ops.create_project(vault, TODAY, name="Lab move", goal="Lab in B12",
                       first_action="Book the removals firm")
    ops.add_project_action(vault, TODAY, project="Lab move", text="Label every crate")
    save_vault(vault)

    page = _text(root, "Project details/Lab move.md")
    assert "- [ ] Book the removals firm" in page
    assert "- [ ] Label every crate" in page
