"""Reference notes over MCP (FORMAT.md §11): reading, the write ops, link
rewriting on rename/move, triage into a note, attachments and search."""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest

from gtd_ci.model import load_vault, save_vault
from gtd_mcp import ops, views
from gtd_mcp.config import Config
from gtd_mcp.server import create_server

MCP_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
TODAY = date(2026, 9, 10)

BOILER = """# Flat hot water

How the system is plumbed.

## Layout

The cylinder is in the airing cupboard. See [[Kitchen refit]].

![[boiler.png]]

## Notes

### 2026-08-01

Serviced.
"""


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _read(root: Path, rel: str) -> str:
    return (root / rel).read_text(encoding="utf-8")


@pytest.fixture
def root(tmp_path) -> Path:
    work = tmp_path / "vault"
    shutil.copytree(MCP_FIXTURES / "mcp-create-project" / "input", work)
    _write(work, "Project details/Kitchen refit.md", "# Goal\n\nNew kitchen.\n\n## Next Actions\n\n- [ ] Ring plumber\n")
    _write(work, "Reference/Flat hot water.md", BOILER)
    _write(work, "Reference/House/Wiring.md", "# Wiring\n\nSee [[Flat hot water]].\n")
    return work


def _run(root: Path, op, **kwargs):
    vault = load_vault(root)
    result = op(vault, TODAY, **kwargs)
    save_vault(vault)
    return result


# --- reading ----------------------------------------------------------------------------


def test_list_reference_with_and_without_a_folder(root):
    vault = load_vault(root)
    assert [n["path"] for n in views.reference_notes(vault)] == [
        "Reference/Flat hot water.md", "Reference/House/Wiring.md",
    ]
    only = views.reference_notes(vault, "House")
    assert [(n["stem"], n["folder"]) for n in only] == [("Wiring", "House")]
    assert views.reference_notes(vault, "Reference/House") == only
    first = views.reference_notes(vault)[0]
    assert set(first) == {"stem", "path", "folder", "title", "headings", "embeds"}
    assert first["embeds"] == ["boiler.png"]


def test_list_reference_with_no_reference_folder(tmp_path):
    shutil.copytree(MCP_FIXTURES / "mcp-create-project" / "input", tmp_path / "v")
    assert views.reference_notes(load_vault(tmp_path / "v")) == []


def test_get_reference_returns_the_body_verbatim(root):
    note = views.get_reference(load_vault(root), "Flat hot water")
    assert note["body"] == BOILER
    assert note["links"] == ["Kitchen refit"]
    assert [b["path"] for b in note["linked_from"]] == ["Reference/House/Wiring.md"]


def test_get_reference_section_only(root):
    note = views.get_reference(load_vault(root), "flat hot water", section="Layout")
    assert note["body"].startswith("## Layout\n")
    assert "Serviced" not in note["body"] and "## Notes" not in note["body"]


def test_get_reference_unknown_section_names_the_real_ones(root):
    with pytest.raises(ops.OpError, match="no '## Missing' section.*Layout, Notes"):
        views.get_reference(load_vault(root), "Flat hot water", section="Missing")


def test_name_resolution(root):
    _write(root, "Reference/Other/Wiring.md", "# Wiring two\n")
    vault = load_vault(root)
    assert views.get_reference(vault, "Flat hot water")["path"] == "Reference/Flat hot water.md"
    assert views.get_reference(vault, "Reference/House/Wiring")["path"] == "Reference/House/Wiring.md"
    assert views.get_reference(vault, "Other/wiring.md")["path"] == "Reference/Other/Wiring.md"
    with pytest.raises(ops.OpError, match="ambiguous: Reference/House/Wiring.md, Reference/Other/Wiring.md"):
        views.get_reference(vault, "Wiring")
    with pytest.raises(ops.OpError, match="did you mean: Flat hot water"):
        views.get_reference(vault, "Flat hot watr")
    with pytest.raises(ops.OpError, match="no reference note named 'Zebra'$"):
        views.get_reference(vault, "Zebra")


def test_exact_stem_beats_a_case_insensitive_twin(root):
    _write(root, "Reference/wiring.md", "# lower\n")
    assert views.get_reference(load_vault(root), "wiring")["path"] == "Reference/wiring.md"


def test_search_reference_reports_heading_line_and_excerpt(root):
    hits = views.search_reference(load_vault(root), "AIRING")
    assert hits == [{
        "stem": "Flat hot water", "path": "Reference/Flat hot water.md",
        "matches": [{"heading": "Layout", "line_no": 7,
                     "excerpt": "The cylinder is in the airing cupboard. See [[Kitchen refit]]."}],
    }]
    assert views.search_reference(load_vault(root), "nothing like this") == []


def test_search_excerpt_is_trimmed(root):
    _write(root, "Reference/Long.md", "# Long\n\n" + "x" * 500 + " needle\n")
    (hit,) = views.search_reference(load_vault(root), "x" * 10)
    assert len(hit["matches"][0]["excerpt"]) == 200


def test_backlinks_find_every_kind_of_file(root):
    _write(root, "Project details/Done/Old.md", "# Goal\n\n[[Wiring]]\n")
    kinds = {b["kind"] for b in views.backlinks(load_vault(root), "Flat hot water")}
    assert kinds == {"reference"}
    assert {b["kind"] for b in views.backlinks(load_vault(root), "Wiring")} == {"project-done"}


# --- create ---------------------------------------------------------------------------------


def test_create_prefixes_a_title_when_the_text_has_none(root):
    result = _run(root, ops.create_reference, name="Tyres", text="Pressures: 32 front.\n")
    assert result.payload == {"path": "Reference/Tyres.md"}
    assert _read(root, "Reference/Tyres.md") == "# Tyres\n\nPressures: 32 front.\n"


def test_create_keeps_the_texts_own_heading_verbatim(root):
    _run(root, ops.create_reference, name="Tyres", text="# Car tyres\n\n## Front\n\n32\n", folder="Car/Parts")
    assert _read(root, "Reference/Car/Parts/Tyres.md") == "# Car tyres\n\n## Front\n\n32\n"


def test_create_puts_the_title_after_front_matter(root):
    _run(root, ops.create_reference, name="Tyres", text="---\ntags: [car]\n---\nBody\n")
    assert _read(root, "Reference/Tyres.md") == "---\ntags: [car]\n---\n# Tyres\n\nBody\n"


def test_create_with_empty_text_is_just_a_title(root):
    _run(root, ops.create_reference, name="Stub", text="")
    assert _read(root, "Reference/Stub.md") == "# Stub\n"


def test_create_normalises_line_endings(root):
    _run(root, ops.create_reference, name="Win", text="a\r\nb\r\n")
    assert _read(root, "Reference/Win.md") == "# Win\n\na\nb\n"


@pytest.mark.parametrize("name, match", [
    ("", "blank"), ("a/b", "usable file name"), (".hidden", "leading dot"), ("..", "leading dot"),
    ("Flat hot water", "already exists"), ("flat HOT water", "already exists"), ("Kitchen refit", "a project is already called"),
])
def test_create_refuses_bad_or_taken_names(root, name, match):
    vault = load_vault(root)
    with pytest.raises(ops.OpError, match=match):
        ops.create_reference(vault, TODAY, name=name, text="x")


@pytest.mark.parametrize("folder", ["../out", "a/../b", ".git", "a\\..\\b"])
def test_create_refuses_a_folder_that_escapes(root, folder):
    with pytest.raises(ops.OpError, match="not a usable file name"):
        ops.create_reference(load_vault(root), TODAY, name="X", text="x", folder=folder)
    assert not (root.parent / "out").exists()


# --- append / edit --------------------------------------------------------------------------


def test_append_joins_the_open_dated_section_like_a_project_note(root):
    _run(root, ops.append_reference, name="flat hot water", text="Pressure low.")
    _run(root, ops.append_reference, name="Flat hot water", text="Bled radiators.")
    text = _read(root, "Reference/Flat hot water.md")
    assert text.endswith("Serviced.\n\n### 2026-09-10\n\nPressure low.\n\nBled radiators.\n")
    assert text.startswith(BOILER[:200])


def test_append_matches_append_project_note_on_the_same_text(root):
    _write(root, "Project details/Twin.md", "# Goal\n\nG\n\n## Next Actions\n\n- [ ] a\n")
    _write(root, "Reference/Twin note.md", "# Goal\n\nG\n\n## Next Actions\n\n- [ ] a\n")
    for op, key in ((ops.append_project_note, "project"), (ops.append_reference, "name")):
        r = _run(root, op, text="Thought.", heading="Context", **{key: "Twin" if key == "project" else "Twin note"})
    assert _read(root, "Project details/Twin.md") == _read(root, "Reference/Twin note.md")
    assert r.payload == {"path": "Reference/Twin note.md", "heading": "Context", "created_section": True, "dated": "2026-09-10"}


def test_append_undated_into_a_named_section_and_error_cases(root):
    _run(root, ops.append_reference, name="Wiring", text="Circuit 3 is the oven.", heading="Circuits", dated=False)
    assert _read(root, "Reference/House/Wiring.md").endswith("## Circuits\n\nCircuit 3 is the oven.\n")
    vault = load_vault(root)
    with pytest.raises(ops.OpError, match="level-1/2 heading"):
        ops.append_reference(vault, TODAY, name="Wiring", text="## Sneaky")
    with pytest.raises(ops.OpError, match="blank"):
        ops.append_reference(vault, TODAY, name="Wiring", text="  ")
    with pytest.raises(ops.OpError, match="no reference note"):
        ops.append_reference(vault, TODAY, name="Nope", text="x")


def test_append_adds_the_newline_a_note_lacked(root):
    (root / "Reference" / "Bare.md").write_bytes(b"# Bare\n\nno newline")
    _run(root, ops.append_reference, name="Bare", text="more", dated=False)
    assert _read(root, "Reference/Bare.md") == "# Bare\n\nno newline\n\n## Notes\n\nmore\n"


def test_edit_replaces_one_exact_occurrence(root):
    result = _run(root, ops.edit_reference, name="Flat hot water", old="airing cupboard", new="loft")
    assert result.payload == {"replaced": True}
    text = _read(root, "Reference/Flat hot water.md")
    assert "in the loft. See" in text and text.count("\n") == BOILER.count("\n")


def test_edit_can_span_lines_and_delete(root):
    _run(root, ops.edit_reference, name="Wiring", old="# Wiring\n\nSee", new="# Wiring\nSee")
    assert _read(root, "Reference/House/Wiring.md") == "# Wiring\nSee [[Flat hot water]].\n"
    _run(root, ops.edit_reference, name="Wiring", old="See [[Flat hot water]].\n", new="")
    assert _read(root, "Reference/House/Wiring.md") == "# Wiring\n"


def test_edit_refuses_missing_and_ambiguous_text_and_changes_nothing(root):
    before = _read(root, "Reference/Flat hot water.md")
    vault = load_vault(root)
    with pytest.raises(ops.OpError, match="not found"):
        ops.edit_reference(vault, TODAY, name="Flat hot water", old="penthouse", new="x")
    with pytest.raises(ops.OpError, match="occurs 2 times"):
        ops.edit_reference(vault, TODAY, name="Flat hot water", old="the", new="x")
    with pytest.raises(ops.OpError, match="must not be empty"):
        ops.edit_reference(vault, TODAY, name="Flat hot water", old="", new="x")
    with pytest.raises(ops.OpError, match="identical"):
        ops.edit_reference(vault, TODAY, name="Flat hot water", old="the", new="the")
    save_vault(vault)
    assert _read(root, "Reference/Flat hot water.md") == before


# --- rename / move / delete -------------------------------------------------------------------


def _link_everywhere(root: Path, target: str) -> None:
    _write(root, "Inbox.md", f"---\ngtd: inbox\n---\nread [[{target}]]\n")
    _write(root, "Tickler/Next week.md", f"---\ngtd: tickler\n---\n[2026-09-20] check [[Reference/{target}|the note]]\n")
    _write(root, "Next actions.md", "---\ngtd: next-actions\n---\n| Action | Project | Deadline | Priority |\n"
           f"| --- | --- | --- | --- |\n| Look at [[{target}#Layout\\|layout]] | | | |\n")
    _write(root, "Project details/Kitchen refit.md",
           f"# Goal\n\nSee [[{target}]].\n\n## Next Actions\n\n- [ ] Ring plumber\n")
    _write(root, "Project details/Done/Finished.md", f"# Goal\n\nOld, see [[{target}|it]].\n")
    _write(root, "Reference/House/Wiring.md", f"# Wiring\n\nSee [[{target}]] and [[{target.lower()}]].\n")


def test_rename_rewrites_links_in_every_kind_of_file(root):
    _link_everywhere(root, "Flat hot water")
    result = _run(root, ops.rename_reference, name="Flat hot water", new_name="Hot water")

    assert not (root / "Reference/Flat hot water.md").exists()
    assert _read(root, "Reference/Hot water.md") == BOILER
    assert "read [[Hot water]]" in _read(root, "Inbox.md")
    assert "[[Hot water|the note]]" in _read(root, "Tickler/Next week.md")
    assert "[[Hot water#Layout\\|layout]]" in _read(root, "Next actions.md")
    assert "See [[Hot water]]." in _read(root, "Project details/Kitchen refit.md")
    assert "[[Hot water|it]]" in _read(root, "Project details/Done/Finished.md")
    assert "See [[Hot water]] and [[Hot water]]." in _read(root, "Reference/House/Wiring.md")
    assert result.payload == {"old_path": "Reference/Flat hot water.md", "new_path": "Reference/Hot water.md",
                              "rewritten_links": 6}
    assert {"Reference/Flat hot water.md", "Reference/Hot water.md", "Inbox.md", "Project details/Done/Finished.md",
            } <= result.changed_files
    assert [p["stem"] for p in views.reference_notes(load_vault(root))] == ["Hot water", "Wiring"]


def test_rename_rewrites_the_notes_own_links_and_stays_in_its_folder(root):
    _write(root, "Reference/House/Wiring.md", "# Wiring\n\nSee [[Wiring#Top]].\n")
    _run(root, ops.rename_reference, name="Wiring", new_name="Mains")
    assert _read(root, "Reference/House/Mains.md") == "# Wiring\n\nSee [[Mains#Top]].\n"


def test_rename_refusals(root):
    vault = load_vault(root)
    with pytest.raises(ops.OpError, match="already called"):
        ops.rename_reference(vault, TODAY, name="Wiring", new_name="Wiring")
    with pytest.raises(ops.OpError, match="already exists"):
        ops.rename_reference(vault, TODAY, name="Wiring", new_name="flat hot water")
    with pytest.raises(ops.OpError, match="a project is already called"):
        ops.rename_reference(vault, TODAY, name="Wiring", new_name="Kitchen refit")
    with pytest.raises(ops.OpError, match="usable file name"):
        ops.rename_reference(vault, TODAY, name="Wiring", new_name="a/b")


def test_rename_allows_a_case_only_change(root):
    _run(root, ops.rename_reference, name="Wiring", new_name="WIRING")
    assert [p.name for p in (root / "Reference/House").iterdir()] == ["WIRING.md"]


def test_move_changes_folder_and_fixes_only_folder_prefixed_links(root):
    _write(root, "Reference/House/Wiring.md", "# Wiring\n")
    _write(root, "Inbox.md", "---\ngtd: inbox\n---\nbare [[Wiring]] and [[House/Wiring|prefixed]] and [[wiring]]\n")
    result = _run(root, ops.move_reference, name="Wiring", folder="Electrics/Mains")
    assert (root / "Reference/Electrics/Mains/Wiring.md").is_file()
    assert not (root / "Reference/House").exists()  # emptied folder is tidied away
    assert "bare [[Wiring]] and [[Wiring|prefixed]] and [[wiring]]" in _read(root, "Inbox.md")
    assert result.payload["new_path"] == "Reference/Electrics/Mains/Wiring.md"


def test_move_to_the_top_level_and_refusals(root):
    _run(root, ops.move_reference, name="Wiring", folder="")
    assert (root / "Reference/Wiring.md").is_file()
    vault = load_vault(root)
    with pytest.raises(ops.OpError, match="already in that folder"):
        ops.move_reference(vault, TODAY, name="Wiring", folder="")
    with pytest.raises(ops.OpError, match="usable file name"):
        ops.move_reference(vault, TODAY, name="Wiring", folder="../..")


def test_delete_removes_the_file_and_warns_about_links(root):
    result = _run(root, ops.delete_reference, name="Flat hot water")
    assert not (root / "Reference/Flat hot water.md").exists()
    assert result.payload["deleted"] == "Reference/Flat hot water.md"
    assert result.payload["linked_from"] == [{"kind": "reference", "path": "Reference/House/Wiring.md", "count": 1}]
    assert "1 file(s) still link to [[Flat hot water]]: Reference/House/Wiring.md" == result.payload["warning"]


def test_delete_with_no_links_has_no_warning(root):
    result = _run(root, ops.delete_reference, name="Wiring")
    assert result.payload == {"deleted": "Reference/House/Wiring.md"}
    assert not (root / "Reference/House").exists()


# --- project rename reaches Reference/ ----------------------------------------------------------


def test_project_rename_rewrites_reference_notes_but_not_done(root):
    _write(root, "Project details/Done/Finished.md", "# Goal\n\n[[Kitchen refit]]\n")
    result = _run(root, ops.rename_project, project="Kitchen refit", new_name="Kitchen")
    assert "See [[Kitchen]]." in _read(root, "Reference/Flat hot water.md")
    assert _read(root, "Project details/Done/Finished.md") == "# Goal\n\n[[Kitchen refit]]\n"
    assert result.payload["unmanaged_links"] == 1


# --- triage into a note -------------------------------------------------------------------------


def _inbox_handle(root: Path, text: str) -> str:
    return next(i["handle"] for i in views.inbox(load_vault(root)) if i["text"] == text)


def test_triage_to_an_existing_reference_note(root):
    _write(root, "Inbox.md", "---\ngtd: inbox\n---\n[from Tickler, 2026-09-01] Boiler pressure 1.2 bar\nother\n")
    handle = _inbox_handle(root, "Boiler pressure 1.2 bar")
    result = _run(root, ops.triage, handle=handle, to="reference:flat hot water")
    assert _read(root, "Reference/Flat hot water.md").endswith("### 2026-09-10\n\nBoiler pressure 1.2 bar\n")
    assert result.payload == {"reference": "Reference/Flat hot water.md", "reference_created": False}
    assert _read(root, "Inbox.md") == "---\ngtd: inbox\n---\nother\n"


def test_triage_creates_a_missing_note(root):
    _write(root, "Inbox.md", "---\ngtd: inbox\n---\nWifi password is on the fridge\n")
    result = _run(root, ops.triage, handle=_inbox_handle(root, "Wifi password is on the fridge"),
                  to="reference:Home/Wifi")
    assert result.payload["reference_created"] is True
    assert _read(root, "Reference/Home/Wifi.md") == "# Wifi\n\n## Notes\n\n### 2026-09-10\n\nWifi password is on the fridge\n"
    assert "fridge" not in _read(root, "Inbox.md")


def test_triage_to_reference_failure_keeps_the_source_and_creates_nothing(root):
    _write(root, "Inbox.md", "---\ngtd: inbox\n---\n## looks like a heading\n")
    vault = load_vault(root)
    handle = _inbox_handle(root, "## looks like a heading")
    with pytest.raises(ops.OpError, match="level-1/2 heading"):
        ops.triage(vault, TODAY, handle=handle, to="reference:Brand new")
    save_vault(vault)
    assert not (root / "Reference/Brand new.md").exists()
    assert "looks like a heading" in _read(root, "Inbox.md")


def test_triage_to_an_ambiguous_name_is_refused(root):
    _write(root, "Reference/Other/Wiring.md", "# W\n")
    _write(root, "Inbox.md", "---\ngtd: inbox\n---\nthing\n")
    with pytest.raises(ops.OpError, match="ambiguous"):
        ops.triage(load_vault(root), TODAY, handle=_inbox_handle(root, "thing"), to="reference:Wiring")


# --- the MCP surface ----------------------------------------------------------------------------


def _config(vault_dir: Path) -> Config:
    return Config(
        vault_dir=str(vault_dir), remote_url=None, push_token=None, push_user="oauth2", webhook_secret=None,
        allowed_users=frozenset(), host="127.0.0.1", port=8000, poll_seconds=0,
        git_name="Test", git_email="test@test", branch="master",
    )


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


@pytest.fixture
def mcp(root, monkeypatch):
    monkeypatch.setenv("GTD_MCP_FIXED_TODAY", "2026-09-10")
    _git(root, "init", "-q", "-b", "master")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    server, _store = create_server(_config(root))
    return server


def _call(mcp, tool: str, **args):
    return asyncio.run(mcp.call_tool(tool, args))


def _json(result):
    """The JSON a tool returned, whether as structured content or a text block."""
    if isinstance(result, tuple):
        result = result[1]
    if isinstance(result, dict):
        return result.get("result", result)
    return json.loads(result[0].text)


def test_all_reference_tools_are_registered(mcp):
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert {"list_reference", "get_reference", "get_attachment", "create_reference", "append_reference",
            "edit_reference", "rename_reference", "move_reference", "delete_reference"} <= names


def test_read_tools(mcp):
    listed = _json(_call(mcp, "list_reference", folder="House"))
    assert [n["stem"] for n in listed] == ["Wiring"]
    got = _json(_call(mcp, "get_reference", name="Flat hot water", section="Layout"))
    assert got["body"].startswith("## Layout")


@pytest.mark.parametrize("tool, args, check", [
    ("create_reference", dict(name="New", text="hello"), lambda r: (r / "Reference/New.md").exists()),
    ("append_reference", dict(name="Wiring", text="more"), lambda r: "more" in _read(r, "Reference/House/Wiring.md")),
    ("edit_reference", dict(name="Wiring", old="See", new="Look at"), lambda r: "Look at" in _read(r, "Reference/House/Wiring.md")),
    ("rename_reference", dict(name="Wiring", new_name="Mains"), lambda r: (r / "Reference/House/Mains.md").exists()),
    ("move_reference", dict(name="Wiring", folder="Elsewhere"), lambda r: (r / "Reference/Elsewhere/Wiring.md").exists()),
    ("delete_reference", dict(name="Wiring"), lambda r: not (r / "Reference/House/Wiring.md").exists()),
])
def test_write_tool_dry_run_then_real(mcp, root, tool, args, check):
    dry = _json(_call(mcp, tool, dry_run=True, **args))
    assert dry["ok"] and dry["commit"] is None and dry["diff"]
    assert _git(root, "status", "--porcelain") == ""

    real = _json(_call(mcp, tool, **args))
    assert real["ok"] and real["commit"]
    assert check(root)
    assert _git(root, "status", "--porcelain") == ""


def test_write_tool_errors_come_back_as_a_failed_outcome(mcp):
    out = _json(_call(mcp, "edit_reference", name="Wiring", old="absent text", new="x"))
    assert out["ok"] is False and "not found" in out["error"]
    out = _json(_call(mcp, "create_reference", name="Wiring", text="x"))
    assert out["ok"] is False and "already exists" in out["error"]


def test_triage_tool_accepts_a_reference_target(mcp, root):
    handle = _json(_call(mcp, "list_inbox"))[0]["handle"]
    out = _json(_call(mcp, "triage", handle=handle, to="reference:Wiring"))
    assert out["ok"] and out["reference"] == "Reference/House/Wiring.md"


def test_search_tool_reference_kind(mcp):
    hits = _json(_call(mcp, "search", query="cylinder", kinds=["reference"]))
    assert list(hits) == ["reference"] and hits["reference"][0]["stem"] == "Flat hot water"
    assert "reference" in _json(_call(mcp, "search", query="cylinder"))


# --- attachments --------------------------------------------------------------------------------

PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360000002000001e221bc330000000049454e44ae426082")


def test_image_attachment_comes_back_as_image_content(mcp, root):
    (root / "attachments").mkdir()
    (root / "attachments" / "boiler.png").write_bytes(PNG)
    result = _call(mcp, "get_attachment", name="boiler.png")
    blocks = result[0] if isinstance(result, tuple) else result
    assert [b.type for b in blocks] == ["text", "image"]
    assert json.loads(blocks[0].text) == {"name": "boiler.png", "size": len(PNG), "mime": "image/png"}
    assert blocks[1].mimeType == "image/png"


def test_text_and_binary_attachments(mcp, root):
    (root / "attachments").mkdir()
    (root / "attachments" / "notes.txt").write_text("hello", encoding="utf-8")
    (root / "attachments" / "mail.msg").write_bytes(b"\xd0\xcf\x11\xe0\x00\xff")
    text = _call(mcp, "get_attachment", name="notes.txt")
    blocks = text[0] if isinstance(text, tuple) else text
    assert [b.text for b in blocks][1] == "hello"
    binary = _call(mcp, "get_attachment", name="mail.msg")
    blocks = binary[0] if isinstance(binary, tuple) else binary
    assert len(blocks) == 1 and json.loads(blocks[0].text)["size"] == 6


@pytest.mark.parametrize("name", ["../Inbox.md", "../../etc/passwd", "/etc/passwd", "sub/../../Inbox.md", "missing.png"])
def test_attachment_outside_the_folder_or_missing_is_refused(mcp, root, name):
    (root / "attachments").mkdir()
    with pytest.raises(Exception, match="not inside attachments|no attachment named"):
        _call(mcp, "get_attachment", name=name)


def test_attachment_symlink_out_is_refused(mcp, root):
    (root / "attachments").mkdir()
    (root / "attachments" / "link.md").symlink_to(root / "Inbox.md")
    with pytest.raises(Exception, match="not inside attachments"):
        _call(mcp, "get_attachment", name="link.md")


def test_oversize_attachment_is_refused(mcp, root, monkeypatch):
    (root / "attachments").mkdir()
    (root / "attachments" / "big.png").write_bytes(PNG)
    monkeypatch.setattr(views, "MAX_ATTACHMENT_BYTES", 10)
    with pytest.raises(Exception, match="over the 10-byte limit"):
        _call(mcp, "get_attachment", name="big.png")
