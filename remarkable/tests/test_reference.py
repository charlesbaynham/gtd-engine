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


def test_scanner_and_apply_do_not_both_report_a_missing_reading(root):
    pg = page(ai=True, warnings=["page 1: ✦ AI is ticked but the 'null' engine gave no reading", "page 1: poor registration"])
    _, report = apply(root, pg)
    assert sum("no reading" in w for w in report.warnings) == 1 and any("poor registration" in w for w in report.warnings)


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

ARCHIVE = "GTD Reference/Archive"


class FakeDevice:
    """`remarkable_gtd.rm.api` over a dict of remote path -> blob."""

    def __init__(self):
        from remarkable_gtd.rm.api import RmapiError

        self.error = RmapiError
        self.docs: dict[str, bytes] = {}
        self.folders: set[str] = set()
        self.log: list[tuple] = []
        self.offline = False
        self.fail_upload = False
        self.renders: list[str] = []
        self.scans: list[str] = []
        self.margin_only = False
        self.scan_text = "from the tablet"

    def install(self, monkeypatch):
        from remarkable_gtd.rm import api

        for name in ("write_config_from_env", "list_dir", "download", "upload", "remove", "move", "mkdir"):
            monkeypatch.setattr(api, name, getattr(self, name))

    def write_config_from_env(self):
        return None

    def list_dir(self, folder):
        from remarkable_gtd.rm.api import Entry

        if self.offline:
            raise self.error("offline")
        folder = folder.strip("/")
        paths = [(d, False) for d in self.docs] + [(f, True) for f in self.folders]
        if folder and folder not in self.folders and not any(p.startswith(folder + "/") for p, _ in paths):
            raise self.error(f"no such folder {folder}")
        out: dict[str, bool] = {}
        for path, is_dir in paths:
            if folder and not path.startswith(folder + "/"):
                continue
            first, _, rest = path[len(folder):].lstrip("/").partition("/")
            if first:
                out[first] = out.get(first, False) or bool(rest) or is_dir
        return [Entry(name, is_dir) for name, is_dir in out.items()]

    def mkdir(self, folder):
        self.folders.add(folder)
        self.log.append(("mkdir", folder))

    def download(self, remote, local_dir):
        local_dir.mkdir(parents=True, exist_ok=True)
        out = local_dir / (remote.rpartition("/")[2] + ".rmdoc")
        out.write_bytes(self.docs[remote])
        return out

    def upload(self, pdf, folder):
        if self.fail_upload:
            raise self.error("upload refused")
        parent = folder.rpartition("/")[0]
        assert not parent or parent in self.folders, f"{parent} was not created first"
        self.folders.add(folder)
        self.docs[f"{folder}/{pdf.stem}"] = b"pdf:" + pdf.read_bytes()
        self.log.append(("upload", f"{folder}/{pdf.stem}"))

    def remove(self, remote):
        del self.docs[remote]
        self.log.append(("remove", remote))

    def move(self, remote, dest):
        parent = dest.rpartition("/")[0]
        assert not parent or parent in self.folders, f"{parent} was not created first"
        self.folders.add(dest)
        self.docs[f"{dest}/{remote.rpartition('/')[2]}"] = self.docs.pop(remote)
        self.log.append(("move", remote, dest))

    def scribble(self, remote, blob=b"inked"):
        self.docs[remote] = blob

    def archived(self) -> list[str]:
        return sorted(d for d in self.docs if d.startswith(ARCHIVE + "/"))


@pytest.fixture
def device(tmp_path, monkeypatch):
    pytest.importorskip("remarkable_gtd")
    import hashlib

    from remarkable_gtd.gen import reference as refgen

    dev = FakeDevice()
    dev.install(monkeypatch)

    def fake_render(note_path, vault_root, out_path, source_sha=None, the_date=None, stem=None, **_):
        dev.renders.append(stem)
        if "|" in (stem or ""):
            raise ValueError("reference note name may not contain '|'")
        if "overflow" in Path(note_path).read_text(encoding="utf-8"):
            raise RuntimeError("body overflows")
        Path(out_path).write_bytes(f"{stem}:{source_sha}".encode())

    def fake_scan(rmdoc, work_dir, cfg=None, today=None, **_):
        dev.scans.append(rmdoc.stem)
        stem = rmdoc.stem
        if dev.margin_only:
            return {"pages": [page(stem=stem, ink_elsewhere=True)]}, {}, {}, rmdoc
        rows = [{"id": "RF-C1", "inked": True, "act_text": dev.scan_text}]
        return {"pages": [page(stem=stem, rows=rows)]}, {}, {}, rmdoc

    ticks = iter(range(10**6))
    monkeypatch.setattr(cli, "_stamp", lambda: f"20261012T{next(ticks):06d}Z")  # runs here share a second
    monkeypatch.setattr(refgen, "render_reference", fake_render)
    monkeypatch.setattr(cli, "ink", lambda rmdoc: {b"inked": (3, 0), b"broken": (0, 1)}.get(rmdoc.read_bytes(), (0, 0)))
    monkeypatch.setattr(cli, "ink_fingerprint", lambda rmdoc: hashlib.sha256(rmdoc.read_bytes()).hexdigest())
    monkeypatch.setattr("remarkable_gtd.scan.sheet.scan_rmdoc", fake_scan)
    return dev


def sync(root, *extra):
    return cli.main(["reference-sync", "--vault", str(root), "--work-dir", str(root / "out"), "--today", "2026-10-12",
                     "--ocr", "null", *extra])


def state(root):
    return json.loads((root / ".remarkable-reference.json").read_text(encoding="utf-8"))


def status(root) -> str:
    return (root / "reMarkable status.md").read_text(encoding="utf-8")


def hot_water(root) -> Path:
    return root / "Reference" / "Hot water.md"


def change_note(root, extra="More.\n"):
    hot_water(root).write_text(hot_water(root).read_text(encoding="utf-8") + extra, encoding="utf-8")


def cli_sha(root):
    from remarkable_gtd.gen.reference import source_sha256

    return source_sha256(hot_water(root))


def test_new_note_is_rendered_into_a_mirrored_folder_and_recorded(root, device):
    (root / "Reference" / "House").mkdir()
    (root / "Reference" / "House" / "Boiler.md").write_text("# Boiler\n", encoding="utf-8")
    assert sync(root) == 0
    assert set(device.docs) == {"GTD Reference/Hot water", "GTD Reference/House/Boiler"}
    assert device.docs["GTD Reference/Hot water"].startswith(b"pdf:Hot water:")
    assert set(state(root)["notes"]) == {"Reference/Hot water.md", "Reference/House/Boiler.md"}
    assert "Hot water.md" in status(root)


def test_unchanged_notes_cost_no_device_calls(root, device):
    sync(root)
    device.log.clear()
    assert sync(root) == 0
    assert device.log == []


def test_replaced_copy_is_archived_never_deleted(root, device):
    sync(root)
    change_note(root)
    assert sync(root) == 0
    assert not any(entry[0] == "remove" for entry in device.log)
    (archived,) = device.archived()
    assert archived.endswith("/Hot water") and state(root)["archive"][archived]["applied"] is False
    assert device.docs["GTD Reference/Hot water"].startswith(b"pdf:") and device.scans == []


def test_inked_copy_is_applied_before_the_replacement_is_rendered(root, device):
    sync(root)
    device.scribble("GTD Reference/Hot water")
    change_note(root, "Edited by hand.\n")
    assert sync(root) == 0
    text = hot_water(root).read_text(encoding="utf-8")
    assert "Edited by hand." in text and text.endswith("from the tablet\n")
    assert device.docs["GTD Reference/Hot water"] == f"pdf:Hot water:{cli_sha(root)}".encode()
    assert state(root)["notes"]["Reference/Hot water.md"]["sha"] == cli_sha(root)
    (archived,) = device.archived()
    assert state(root)["archive"][archived]["applied"] is True and device.scans == ["Hot water"]
    assert "RF-C1: Appended a note" in status(root)


def test_applied_archive_entries_are_deleted_by_the_next_run_only(root, device):
    sync(root)
    device.scribble("GTD Reference/Hot water")
    change_note(root)
    sync(root)
    assert len(device.archived()) == 1
    assert sync(root) == 0
    assert device.archived() == [] and state(root)["archive"] == {}


def test_uncommitted_apply_is_not_forgotten_when_the_push_fails(root, device):
    """The state file is what is committed; a lost push loses the record, and the
    archived copy is found again by listing."""
    sync(root)
    device.scribble("GTD Reference/Hot water")
    change_note(root)
    saved = (root / ".remarkable-reference.json").read_text(encoding="utf-8")
    note_before = hot_water(root).read_text(encoding="utf-8")
    sync(root)
    (root / ".remarkable-reference.json").write_text(saved, encoding="utf-8")  # the push failed
    hot_water(root).write_text(note_before, encoding="utf-8")
    assert sync(root, "--read-ink") == 0
    assert device.scans == ["Hot water", "Hot water"] and len(device.archived()) >= 1
    assert hot_water(root).read_text(encoding="utf-8").count("from the tablet") == 1


def test_read_ink_picks_up_ink_on_an_unchanged_note_once(root, device):
    sync(root)
    assert sync(root, "--read-ink") == 0 and device.scans == []
    device.scribble("GTD Reference/Hot water")
    assert sync(root, "--read-ink") == 0 and len(device.scans) == 1
    assert sync(root, "--read-ink") == 0 and len(device.scans) == 1


def test_margin_ink_with_nothing_to_apply_leaves_the_copy_and_warns_once(root, device):
    sync(root)
    device.margin_only = True
    device.scribble("GTD Reference/Hot water")
    assert sync(root, "--read-ink") == 0
    assert device.docs["GTD Reference/Hot water"] == b"inked" and device.archived() == []
    assert "was not read" in status(root)
    assert sync(root, "--read-ink") == 0 and device.scans == ["Hot water"]


def test_late_ink_in_the_archive_is_applied_on_a_read_ink_run(root, device):
    sync(root)
    change_note(root)
    sync(root)
    (archived,) = device.archived()
    assert sync(root, "--read-ink") == 0 and device.scans == []  # still blank: kept
    device.scribble(archived)
    assert sync(root, "--read-ink") == 0 and device.scans == ["Hot water"]
    assert state(root)["archive"][archived]["applied"] is True and hot_water(root).read_text(encoding="utf-8").endswith("from the tablet\n")


def test_unreadable_note_does_not_stop_an_inked_one_and_the_run_fails(root, device):
    (root / "Reference" / "Boiler.md").write_text("# Boiler\n", encoding="utf-8")
    sync(root)
    device.docs["GTD Reference/Boiler"] = b"broken"
    device.scribble("GTD Reference/Hot water")
    change_note(root)
    (root / "Reference" / "Boiler.md").write_text("# Boiler\nMore\n", encoding="utf-8")
    assert sync(root) == 1
    assert device.docs["GTD Reference/Boiler"] == b"broken"
    assert hot_water(root).read_text(encoding="utf-8").endswith("from the tablet\n")
    assert "none could be read" in status(root)


def test_failed_upload_after_the_move_recovers_on_the_next_run(root, device):
    sync(root)
    change_note(root)
    device.fail_upload = True
    assert sync(root) == 1
    assert "GTD Reference/Hot water" not in device.docs and len(device.archived()) == 1
    device.fail_upload = False
    assert sync(root) == 0
    assert device.docs["GTD Reference/Hot water"] == f"pdf:Hot water:{cli_sha(root)}".encode()


def test_failed_render_after_apply_is_not_applied_again(root, device):
    sync(root)
    device.scribble("GTD Reference/Hot water")
    device.scan_text = "overflow now"
    assert sync(root, "--read-ink") == 0
    assert "GTD Reference/Hot water" not in device.docs and device.scans == ["Hot water"]
    assert sync(root) == 0 and sync(root, "--read-ink") == 0
    assert device.scans == ["Hot water"] and hot_water(root).read_text(encoding="utf-8").count("overflow now") == 1


def test_deleted_note_is_removed_from_the_device(root, device):
    sync(root)
    hot_water(root).unlink()
    assert sync(root) == 0
    assert device.docs == {} and state(root)["notes"] == {}


def test_deleted_inked_note_is_archived_with_a_warning(root, device):
    sync(root)
    device.scribble("GTD Reference/Hot water")
    hot_water(root).unlink()
    assert sync(root) == 0
    (archived,) = device.archived()
    assert list(device.docs) == [archived] and state(root)["notes"] == {}
    assert f"'{archived}'" in status(root)
    sync(root, "--read-ink")
    assert device.scans == []  # nothing to apply it to


def test_unreachable_cloud_keeps_a_deleted_notes_record(root, device):
    sync(root)
    hot_water(root).unlink()
    device.offline = True
    assert sync(root) == 1
    assert "Reference/Hot water.md" in state(root)["notes"]
    device.offline = False
    assert sync(root) == 0 and device.docs == {}


def test_moved_note_takes_its_device_copy_with_it(root, device):
    sync(root)
    (root / "Reference" / "House").mkdir()
    hot_water(root).rename(root / "Reference" / "House" / "Hot water.md")
    device.log.clear()
    assert sync(root) == 0
    assert list(device.docs) == ["GTD Reference/House/Hot water"] and device.renders == ["Hot water"]
    assert list(state(root)["notes"]) == ["Reference/House/Hot water.md"]


def test_unrenderable_note_is_recorded_and_not_retried_until_it_changes(root, device):
    (root / "Reference" / "Big.md").write_text("overflow\n", encoding="utf-8")
    assert sync(root) == 0
    assert set(device.docs) == {"GTD Reference/Hot water"} and "body overflows" in status(root)
    assert state(root)["notes"]["Reference/Big.md"]["error"] == "body overflows"
    device.renders.clear()
    before = (root / ".remarkable-reference.json").read_text(encoding="utf-8")
    assert sync(root) == 0 and device.renders == []
    assert (root / ".remarkable-reference.json").read_text(encoding="utf-8") == before
    (root / "Reference" / "Big.md").write_text("fits\n", encoding="utf-8")
    sync(root)
    assert "GTD Reference/Big" in device.docs


def test_old_flat_state_is_migrated(root, device):
    sync(root)
    flat = state(root)["notes"]
    (root / ".remarkable-reference.json").write_text(json.dumps(flat), encoding="utf-8")
    device.log.clear()
    assert sync(root) == 0 and device.log == []
    assert set(state(root)) == {"notes", "archive"} and set(state(root)["notes"]) == set(flat)


def test_archive_entries_expire(root, device):
    sync(root)
    change_note(root)
    sync(root)
    (archived,) = device.archived()
    st = state(root)
    st["archive"][archived]["archived"] = "2026-01-01T00:00:00Z"
    (root / ".remarkable-reference.json").write_text(json.dumps(st), encoding="utf-8")
    assert sync(root) == 0 and device.archived() == []


def test_folder_and_commit_message_options(root, device, monkeypatch):
    monkeypatch.setenv("REMARKABLE_REFERENCE_FOLDER", "Refs")
    msg = root / "msg.txt"
    assert sync(root, "--commit-message-file", str(msg)) == 0
    assert list(device.docs) == ["Refs/Hot water"] and msg.read_text(encoding="utf-8").startswith("remarkable: reference sync")


def test_status_section_replaces_itself_and_keeps_the_sheet_sections(root, device):
    (root / "reMarkable status.md").write_text("---\ngtd: remarkable-status\n---\n# Sheet s\n\n## Applied\n\n- none\n", encoding="utf-8")
    sync(root)
    change_note(root)
    sync(root)
    assert status(root).count("# Reference notes") == 1 and "# Sheet s" in status(root)


def test_apply_command_routes_reference_decisions(root):
    decisions = root / "decisions.json"
    decisions.write_text(json.dumps({"pages": [page(rows=[{"id": "RF-C1", "inked": True, "act_text": "by hand"}])]}), encoding="utf-8")
    assert cli.main(["apply", "--vault", str(root), "--today", "2026-10-12", str(decisions)]) == 0
    assert hot_water(root).read_text(encoding="utf-8").endswith("by hand\n")
