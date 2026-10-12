"""Reference notes on the device: applying scanned ink, and `reference-sync` against a fake device."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from gtd_ci.model import load_vault
from gtd_remarkable import cli
from gtd_remarkable.apply import apply_reference_decisions

from .conftest import write_vault

TODAY = date(2026, 10, 12)
NOTE = "# Hot water\n\nThe tank is 120 litres.\nThe tank is 120 litres.\nPressure is 2 bar.\n"


def page(no=1, *, stem="Hot water", ai=False, reading=None, rows=(), ink_elsewhere=False, warnings=()) -> dict:
    out = {"page_key": f"REF|{stem}#{no}|abc", "page_no": no, "bucket": "refnotes" if rows else "reference",
           "note": {"stem": stem, "path": f"Reference/{stem}.md"}, "ai": ai, "ink_elsewhere": ink_elsewhere,
           "tasks": list(rows), "warnings": list(warnings)}
    if reading is not None:
        out["ai_reading"] = reading
    return out


def reading(*ops, understood=True, confidence=0.9, handwriting="scribble"):
    return {"handwriting": handwriting, "understood": understood, "confidence": confidence, "note": "", "operations": list(ops)}


def op(kind, **kw):
    return {"op": kind, "text": None, "old": None, "new": None, "heading": None, **kw}


@pytest.fixture
def root(tmp_path) -> Path:
    write_vault(tmp_path)
    (tmp_path / "Reference").mkdir()
    (tmp_path / "Reference" / "Hot water.md").write_text(NOTE, encoding="utf-8")
    return tmp_path


def apply(root: Path, *pages: dict):
    vault = load_vault(root)
    report = apply_reference_decisions(vault, TODAY, {"pages": list(pages)})
    return vault, report


def note_text(vault) -> str:
    return vault.reference_notes()["Reference/Hot water.md"].text


def test_notes_page_line_is_appended_dated(root):
    vault, report = apply(root, page(rows=[{"id": "RF-C1", "inked": True, "act_text": "Service due in March"},
                                           {"id": "RF-C2", "inked": False}]))
    assert note_text(vault).endswith("## Notes\n\n### 2026-10-12\n\nService due in March\n")
    assert len(report.applied) == 1 and not report.warnings


def test_inked_line_with_no_text_is_a_warning(root):
    vault, report = apply(root, page(rows=[{"id": "RF-C1", "inked": True, "act_text": ""}]))
    assert note_text(vault) == NOTE
    assert "RF-C1" in report.warnings[0] and "legible" in report.warnings[0]


def test_ai_operations_append_edit_and_capture(root):
    r = reading(op("reference_append", text="Annual service", heading="Maintenance"),
                op("reference_edit", old="Pressure is 2 bar.", new="Pressure is 1.5 bar."),
                op("capture", text="Ring the plumber"))
    vault, report = apply(root, page(ai=True, reading=r))
    text = note_text(vault)
    assert "## Maintenance\n\n### 2026-10-12\n\nAnnual service" in text and "Pressure is 1.5 bar." in text
    assert [i.text for i in vault.inbox.order] == ["Ring the plumber"]
    assert len(report.applied) == 3 and not report.warnings


def test_ambiguous_or_missing_edit_is_a_warning_and_the_rest_still_run(root):
    r = reading(op("reference_edit", old="The tank is 120 litres.", new="x"),
                op("reference_edit", old="not on the page", new="y"),
                op("reference_append", text="kept"))
    vault, report = apply(root, page(ai=True, reading=r))
    assert "The tank is 120 litres.\nThe tank is 120 litres." in note_text(vault) and note_text(vault).endswith("kept\n")
    assert len(report.warnings) == 2 and "2 times" in report.warnings[0] and "not found" in report.warnings[1]
    assert len(report.applied) == 1


def test_not_understood_quotes_the_handwriting_and_changes_nothing(root):
    r = reading(op("capture", text="ignored"), understood=False, handwriting="swirl swirl")
    vault, report = apply(root, page(ai=True, reading=r))
    assert note_text(vault) == NOTE and not vault.inbox.order
    assert "swirl swirl" in report.warnings[0]


def test_ai_without_reading_quotes_the_suggested_lines(root):
    rows = [{"id": "RF-C1", "inked": True, "suggestion": {"text": "Ring Dave"}}]
    vault, report = apply(root, page(ai=True, rows=rows))
    assert note_text(vault) == NOTE
    assert "no reading" in report.warnings[0] and "Ring Dave" in report.warnings[0]


def test_ai_page_does_not_also_apply_its_lines(root):
    rows = [{"id": "RF-C1", "inked": True, "act_text": "would double-write"}]
    vault, _ = apply(root, page(ai=True, rows=rows, reading=reading(op("capture", text="once"))))
    assert "double-write" not in note_text(vault)


def test_ink_elsewhere_without_ai_is_reported_not_applied(root):
    vault, report = apply(root, page(2, ink_elsewhere=True))
    assert note_text(vault) == NOTE
    assert "page 2" in report.warnings[0] and "Hot water" in report.warnings[0] and "✦ AI" in report.warnings[0]


def test_note_deleted_since_printing_is_a_warning(root):
    (root / "Reference" / "Hot water.md").unlink()
    _, report = apply(root, page(rows=[{"id": "RF-C1", "inked": True, "act_text": "lost"}]))
    assert report.warnings and not report.applied


def test_skipped_and_errored_pages(root):
    _, report = apply(root, {"page_key": "k", "skipped": True}, {"page_key": "k2", "error": "no marks"})
    assert report.warnings == ["k2: page not scanned — no marks"]


# --- reference-sync against a fake device -------------------------------------------------


class FakeDevice:
    """`remarkable_gtd.rm.api` over a dict of remote path -> blob."""

    def __init__(self, tmp_path: Path):
        self.docs: dict[str, bytes] = {}
        self.folders: set[str] = set()
        self.tmp = tmp_path / "dl"
        self.log: list[tuple] = []

    def install(self, monkeypatch):
        from remarkable_gtd.rm import api

        for name in ("write_config_from_env", "exists", "download", "upload", "remove", "move", "mkdir"):
            monkeypatch.setattr(api, name, getattr(self, name))

    def write_config_from_env(self):
        return None

    def exists(self, path):
        return path in self.docs

    def mkdir(self, folder):
        self.folders.add(folder)
        self.log.append(("mkdir", folder))

    def download(self, remote, local_dir):
        local_dir.mkdir(parents=True, exist_ok=True)
        out = local_dir / (remote.rpartition("/")[2] + ".rmdoc")
        out.write_bytes(self.docs[remote])
        return out

    def upload(self, pdf, folder):
        assert folder in self.folders, f"{folder} was not created first"
        self.docs[f"{folder}/{pdf.stem}"] = b"pdf:" + pdf.read_bytes()
        self.log.append(("upload", f"{folder}/{pdf.stem}"))

    def remove(self, remote):
        del self.docs[remote]
        self.log.append(("remove", remote))

    def move(self, remote, dest):
        self.folders.add(dest)
        self.docs[f"{dest}/{remote.rpartition('/')[2]}"] = self.docs.pop(remote)
        self.log.append(("move", remote, dest))

    def scribble(self, remote):
        self.docs[remote] = b"inked"


@pytest.fixture
def device(tmp_path, monkeypatch):
    pytest.importorskip("remarkable_gtd")
    dev = FakeDevice(tmp_path)
    dev.install(monkeypatch)
    from remarkable_gtd.gen import reference as refgen

    def fake_render(note_path, vault_root, out_path, source_sha=None, the_date=None, stem=None, **_):
        if "|" in (stem or ""):
            raise ValueError("reference note name may not contain '|'")
        if "overflow" in Path(note_path).read_text(encoding="utf-8"):
            raise RuntimeError("body overflows")
        Path(out_path).write_bytes(f"{stem}:{source_sha}".encode())

    monkeypatch.setattr(refgen, "render_reference", fake_render)
    monkeypatch.setattr(cli, "ink", lambda rmdoc: {b"inked": (3, 0), b"broken": (0, 1)}.get(rmdoc.read_bytes(), (0, 0)))
    scanned = []

    def fake_scan(rmdoc, work_dir, cfg=None, today=None, **_):
        scanned.append(today)
        return {"pages": [page(rows=[{"id": "RF-C1", "inked": True, "act_text": "from the tablet"}])]}, {}, {}, rmdoc

    monkeypatch.setattr("remarkable_gtd.scan.sheet.scan_rmdoc", fake_scan)
    dev.scanned = scanned
    return dev


def sync(root, *extra):
    return cli.main(["reference-sync", "--vault", str(root), "--work-dir", str(root / "out"), "--today", "2026-10-12",
                     "--ocr", "null", *extra])


def state(root):
    return json.loads((root / ".remarkable-reference.json").read_text(encoding="utf-8"))


def test_new_note_is_rendered_into_a_mirrored_folder_and_recorded(root, device):
    (root / "Reference" / "House").mkdir()
    (root / "Reference" / "House" / "Boiler.md").write_text("# Boiler\n", encoding="utf-8")
    assert sync(root) == 0
    assert set(device.docs) == {"GTD Reference/Hot water", "GTD Reference/House/Boiler"}
    assert device.docs["GTD Reference/Hot water"].startswith(b"pdf:Hot water:")
    assert set(state(root)) == {"Reference/Hot water.md", "Reference/House/Boiler.md"}
    assert "Hot water.md" in (root / "reMarkable status.md").read_text(encoding="utf-8")


def test_unchanged_notes_cost_no_device_calls(root, device):
    sync(root)
    device.log.clear()
    assert sync(root) == 0
    assert device.log == []


def test_changed_note_with_clean_device_copy_is_replaced(root, device):
    sync(root)
    (root / "Reference" / "Hot water.md").write_text(NOTE + "More.\n", encoding="utf-8")
    assert sync(root) == 0
    assert ("remove", "GTD Reference/Hot water") in device.log and device.log[-1] == ("upload", "GTD Reference/Hot water")
    assert device.scanned == []


def test_inked_copy_is_applied_before_the_replacement_is_rendered(root, device):
    sync(root)
    device.scribble("GTD Reference/Hot water")
    (root / "Reference" / "Hot water.md").write_text(NOTE + "Edited by hand.\n", encoding="utf-8")
    assert sync(root) == 0
    text = (root / "Reference" / "Hot water.md").read_text(encoding="utf-8")
    assert "Edited by hand." in text and text.endswith("from the tablet\n")
    sha = cli_sha(root)
    assert device.docs["GTD Reference/Hot water"] == f"pdf:Hot water:{sha}".encode()
    assert state(root)["Reference/Hot water.md"]["sha"] == sha
    assert device.scanned == ["2026-10-12"]
    assert "RF-C1: Appended a note" in (root / "reMarkable status.md").read_text(encoding="utf-8")


def cli_sha(root):
    from remarkable_gtd.gen.reference import source_sha256

    return source_sha256(root / "Reference" / "Hot water.md")


def test_read_ink_picks_up_ink_on_an_unchanged_note_once(root, device):
    sync(root)
    assert sync(root, "--read-ink") == 0 and device.scanned == []
    device.scribble("GTD Reference/Hot water")
    assert sync(root, "--read-ink") == 0 and len(device.scanned) == 1
    assert sync(root, "--read-ink") == 0 and len(device.scanned) == 1


def test_unreadable_ink_leaves_the_device_copy_and_fails_the_run(root, device):
    sync(root)
    device.docs["GTD Reference/Hot water"] = b"broken"
    (root / "Reference" / "Hot water.md").write_text(NOTE + "More.\n", encoding="utf-8")
    assert sync(root) == 1
    assert device.docs["GTD Reference/Hot water"] == b"broken"
    assert "none could be read" in (root / "reMarkable status.md").read_text(encoding="utf-8")


def test_deleted_note_is_removed_from_the_device(root, device):
    sync(root)
    (root / "Reference" / "Hot water.md").unlink()
    assert sync(root) == 0
    assert device.docs == {} and state(root) == {}


def test_deleted_inked_note_is_archived_with_a_warning(root, device):
    sync(root)
    device.scribble("GTD Reference/Hot water")
    (root / "Reference" / "Hot water.md").unlink()
    assert sync(root) == 0
    assert list(device.docs) == ["GTD Reference/Archive/Hot water"] and state(root) == {}
    assert "moved to 'GTD Reference/Archive'" in (root / "reMarkable status.md").read_text(encoding="utf-8")


def test_unrenderable_notes_are_skipped_with_a_warning(root, device):
    (root / "Reference" / "Big.md").write_text("overflow\n", encoding="utf-8")
    assert sync(root) == 0
    assert set(device.docs) == {"GTD Reference/Hot water"} and "Reference/Big.md" not in state(root)
    assert "body overflows" in (root / "reMarkable status.md").read_text(encoding="utf-8")


def test_folder_and_commit_message_options(root, device, monkeypatch):
    monkeypatch.setenv("REMARKABLE_REFERENCE_FOLDER", "Refs")
    msg = root / "msg.txt"
    assert sync(root, "--commit-message-file", str(msg)) == 0
    assert list(device.docs) == ["Refs/Hot water"] and msg.read_text(encoding="utf-8").startswith("remarkable: reference sync")


def test_status_section_replaces_itself_and_keeps_the_sheet_sections(root, device):
    (root / "reMarkable status.md").write_text("---\ngtd: remarkable-status\n---\n# Sheet s\n\n## Applied\n\n- none\n", encoding="utf-8")
    sync(root)
    (root / "Reference" / "Hot water.md").write_text(NOTE + "More.\n", encoding="utf-8")
    sync(root)
    status = (root / "reMarkable status.md").read_text(encoding="utf-8")
    assert status.count("# Reference notes") == 1 and "# Sheet s" in status
