"""Reference notes: loading `Reference/**/*.md` and the link walk over them."""
from __future__ import annotations

from pathlib import Path

from gtd_ci.links import Retarget, backlinks, rewrite_everywhere, rewrite_links
from gtd_ci.model import load_vault, save_vault

NOTE = """---
tags: [boiler]
---
# Flat hot water

Intro with [[Kitchen refit|the refit]] and [[Other note#Part]].

![[boiler.png|300]]

## Notes

### 2026-09-10

```
# not a heading
[[Fenced]]
```

Done.
"""


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_vault_without_reference_folder_has_no_notes(tmp_path):
    assert load_vault(tmp_path).reference_notes() == {}


def test_notes_are_loaded_lazily_from_nested_folders(tmp_path):
    _write(tmp_path, "Reference/Flat hot water.md", NOTE)
    _write(tmp_path, "Reference/House/Boiler.md", "just text\n")
    vault = load_vault(tmp_path)
    assert vault._references is None
    notes = vault.reference_notes()
    assert sorted(notes) == ["Reference/Flat hot water.md", "Reference/House/Boiler.md"]
    assert notes["Reference/House/Boiler.md"].folder == "House"
    assert notes["Reference/Flat hot water.md"].folder == ""


def test_note_metadata(tmp_path):
    _write(tmp_path, "Reference/Flat hot water.md", NOTE)
    note = load_vault(tmp_path).reference_notes()["Reference/Flat hot water.md"]
    assert note.stem == "Flat hot water"
    assert note.title == "Flat hot water"
    assert [(h["level"], h["text"]) for h in note.headings] == [(1, "Flat hot water"), (2, "Notes"), (3, "2026-09-10")]
    assert note.embeds == ["boiler.png"]
    assert note.links == ["Kitchen refit", "Other note"]
    assert note.size == len(NOTE.encode())
    assert note.text == NOTE


def test_note_without_a_heading_is_titled_by_its_stem(tmp_path):
    _write(tmp_path, "Reference/Loose.md", "no headings here\n")
    note = load_vault(tmp_path).reference_notes()["Reference/Loose.md"]
    assert note.title == "Loose"
    assert note.headings == []


def test_note_failing_the_encoding_rules_is_reported_and_skipped(tmp_path):
    _write(tmp_path, "Reference/Good.md", "fine\n")
    (tmp_path / "Reference" / "Bad.md").write_bytes(b"a\r\nb\r\n")
    vault = load_vault(tmp_path)
    assert list(vault.reference_notes()) == ["Reference/Good.md"]
    assert ("Reference/Bad.md", "CRLF or bare CR line ending present") in vault.errors


def test_section_runs_to_the_next_level_two_heading(tmp_path):
    _write(tmp_path, "Reference/N.md", "# T\n\n## A\n\none\n\n### sub\n\ntwo\n\n## B\n\nthree\n")
    note = load_vault(tmp_path).reference_notes()["Reference/N.md"]
    assert note.section("A") == ["## A", "", "one", "", "### sub", "", "two"]
    assert note.section("missing") is None


def test_dirty_note_is_saved_and_clean_one_is_not(tmp_path):
    _write(tmp_path, "Reference/A.md", "# A\n")
    _write(tmp_path, "Reference/B.md", "# B\n")
    vault = load_vault(tmp_path)
    notes = vault.reference_notes()
    notes["Reference/A.md"].set_text("# A\n\nchanged\n")
    vault.dirty.add("Reference/A.md")
    save_vault(vault)
    assert (tmp_path / "Reference/A.md").read_text() == "# A\n\nchanged\n"


def test_rewrite_links_keeps_alias_and_heading_and_drops_the_prefix():
    text = "[[Old]] [[old|a]] [[Dir/Old#h]] [[Older]] ![[Old.png]] [[Old\\|x]]"
    assert rewrite_links(text, Retarget("Old", "New")) == (
        "[[New]] [[New|a]] [[New#h]] [[Older]] ![[Old.png]] [[New\\|x]]", 0,
    )


def test_a_prefixed_link_matches_only_a_path_suffix_of_the_real_directory():
    retarget = Retarget("Boiler", "Heater", within="Reference/X/A", bare=False, prefix_for=lambda p: p)
    text = "[[A/Boiler]] [[X/A/Boiler]] [[Reference/X/A/Boiler|a]] [[B/Boiler]] [[Boiler]] [[Z/A/Boiler]]"
    assert rewrite_links(text, retarget) == (
        "[[A/Heater]] [[X/A/Heater]] [[Reference/X/A/Heater|a]] [[B/Boiler]] [[Boiler]] [[Z/A/Boiler]]", 1,
    )


def test_backlinks_and_rewrite_cover_every_kind_of_file(tmp_path):
    root = tmp_path
    _write(root, "Reference/Target.md", "# Target\n")
    _write(root, "Reference/Other.md", "see [[Target]]\n")
    _write(root, "Project details/P.md", "# Goal\n\nSee [[Target]].\n\n## Next Actions\n\n- [ ] read [[Target]]\n")
    _write(root, "Project details/Done/Old.md", "# Goal\n\n[[target|t]]\n")
    _write(root, "Inbox.md", "---\ngtd: inbox\n---\nlook at [[Target]]\n")
    _write(root, "Next actions.md", "---\ngtd: next-actions\n---\n| Action | Project | Deadline | Priority |\n| --- | --- | --- | --- |\n| x [[Target]] | | | |\n")
    vault = load_vault(root)

    assert {b["kind"] for b in backlinks(vault, "Target")} == {
        "reference", "project", "project-done", "inbox", "next-actions",
    }

    result = rewrite_everywhere(vault, Retarget("Target", "Renamed"), done=True)
    save_vault(vault)
    assert result.count == 6
    assert result.written_directly == {"Project details/Done/Old.md"}
    assert (root / "Reference/Other.md").read_text() == "see [[Renamed]]\n"
    assert "- [ ] read [[Renamed]]" in (root / "Project details/P.md").read_text()
    assert load_vault(root).projects["Project details/P.md"].items[0].text == "read [[Renamed]]"
    assert (root / "Project details/Done/Old.md").read_text() == "# Goal\n\n[[Renamed|t]]\n"
    assert "look at [[Renamed]]" in (root / "Inbox.md").read_text()
    assert "x [[Renamed]]" in (root / "Next actions.md").read_text()


def test_rewrite_without_done_leaves_done_pages(tmp_path):
    _write(tmp_path, "Project details/Done/Old.md", "[[Target]]\n")
    vault = load_vault(tmp_path)
    assert rewrite_everywhere(vault, Retarget("Target", "New"), done=False).count == 0
    assert (tmp_path / "Project details/Done/Old.md").read_text() == "[[Target]]\n"


def test_find_heading_ignores_fenced_text(tmp_path):
    from gtd_ci.projects import find_heading, section_bounds

    lines = ["# G", "## Notes", "```", "## Other", "```", "tail", "## End"]
    assert find_heading(["```", "## Notes", "```"], "Notes") is None
    assert section_bounds(lines, 1) == 6
