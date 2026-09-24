"""The CI path end to end with a fake device: render -> (device) -> process -> publish.

Needs Chromium (rendering) and the remarkable-gtd package; skipped otherwise.
"""
from __future__ import annotations

import json
import shutil
import stat
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from gtd_remarkable.cli import main

pytest.importorskip("remarkable_gtd")


def _chromium() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            return bool(pw.chromium.executable_path)
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _chromium(), reason="Playwright Chromium not installed")


def _fake_rmapi(tmp_path: Path, monkeypatch, device: Path) -> Path:
    """rmapi stand-in over a directory: ls/get/put/mv/mkdir on files in `device`."""
    log = tmp_path / "rmapi.log"
    script = tmp_path / "rmapi"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, shutil, sys\n"
        "from pathlib import Path\n"
        f"DEV = Path({str(device)!r}); LOG = Path({str(log)!r})\n"
        "args = sys.argv[1:]\n"
        "LOG.open('a').write(' '.join(args) + '\\n')\n"
        "if args[0] == '-ni': args = args[1:]\n"
        "cmd = args[0]\n"
        "if cmd == 'ls':\n"
        "    folder = DEV / args[-1].strip('/')\n"
        "    if not folder.is_dir(): sys.exit(1)\n"
        "    nodes = [{'name': p.stem if p.suffix == '.rmdoc' else p.name,\n"
        "              'type': 'CollectionType' if p.is_dir() else 'DocumentType'} for p in folder.iterdir()]\n"
        "    print(json.dumps(nodes))\n"
        "elif cmd == 'get':\n"
        "    src = DEV / (args[1].strip('/') + '.rmdoc'); shutil.copy(src, Path.cwd() / src.name)\n"
        "elif cmd == 'put':\n"
        "    (DEV / args[-1].strip('/')).mkdir(parents=True, exist_ok=True)\n"
        "    shutil.copy(args[-2], DEV / args[-1].strip('/') / (Path(args[-2]).stem + '.pdf'))\n"
        "elif cmd == 'mkdir':\n"
        "    (DEV / args[1].strip('/')).mkdir(parents=True, exist_ok=True)\n"
        "elif cmd == 'rm':\n"
        "    (DEV / (args[1].strip('/') + '.rmdoc')).unlink()\n"
        "elif cmd == 'mv':\n"
        "    src = DEV / (args[1].strip('/') + '.rmdoc'); dst = DEV / args[2].strip('/')\n"
        "    dst.mkdir(parents=True, exist_ok=True); shutil.move(str(src), str(dst / src.name))\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("RMAPI_BIN", str(script))
    monkeypatch.delenv("RMAPI_DEVICE_TOKEN", raising=False)
    return log


def _pack_rmdoc(pdf: Path, out: Path, *, strokes: bool = False, erased: bool = False) -> None:
    """A minimal .rmdoc: the untouched PDF plus a .content.

    With ``strokes=True`` a real v6 ``.rm`` layer holding one line is written
    for page 0, which is what makes the sheet look written-on to `process`.
    ``erased=True`` writes the same layer with the stroke rubbed out instead.
    """
    layer = strokes or erased
    with zipfile.ZipFile(out, "w") as z:
        pages = [{"id": "page-uuid", "redir": {"value": 0}}] if layer else []
        z.writestr("doc-uuid.content", json.dumps({"cPages": {"pages": pages}}))
        z.write(pdf, "doc-uuid.pdf")
        if layer:
            z.writestr("doc-uuid/page-uuid.rm", _one_stroke_rm(erased=erased))


def _one_stroke_rm(*, erased: bool = False) -> bytes:
    """A v6 ``.rm`` file with a single short line, built with rmscene.

    ``erased=True`` records it the way the tablet records a rubbed-out stroke:
    the item survives with no value and the point count in ``deleted_length``.
    """
    from rmscene import CrdtId, CrdtSequenceItem, SceneLineItemBlock, write_blocks
    from rmscene.scene_items import Line, Pen, PenColor, Point

    line = Line(
        color=PenColor.BLACK,
        tool=Pen.FINELINER_1,
        points=[Point(x=x, y=0.0, speed=1, direction=0, width=10, pressure=100) for x in (0.0, 20.0)],
        thickness_scale=1.0,
        starting_length=0.0,
    )
    item = CrdtSequenceItem(
        item_id=CrdtId(1, 10), left_id=CrdtId(0, 0), right_id=CrdtId(0, 0),
        deleted_length=len(line.points) if erased else 0,
        value=None if erased else line,
    )
    buf = BytesIO()
    write_blocks(buf, [SceneLineItemBlock(parent_id=CrdtId(0, 1), item=item)])
    return buf.getvalue()


def test_process_then_publish(vault_root, tmp_path, monkeypatch):
    device = tmp_path / "device" / "GTD Daily"
    device.mkdir(parents=True)
    log = _fake_rmapi(tmp_path, monkeypatch, tmp_path / "device")
    work = tmp_path / "work"

    # Yesterday's sheet: rendered from the vault, "on the device", written on
    # (no ticks land, but the ink is what makes it a sheet we are finished with).
    assert main(["render", "--vault", str(vault_root), "--today", "2026-09-14", "--out", str(work / "y.pdf")]) == 0
    _pack_rmdoc(work / "y.pdf", device / "20260914Z0330_gtd_sheet.rmdoc", strokes=True)
    # Older sheets already in the archive: one inside the 3-day window, one outside.
    (device / "Archive").mkdir()
    _pack_rmdoc(work / "y.pdf", device / "Archive" / "20260912Z0330_gtd_sheet.rmdoc")
    _pack_rmdoc(work / "y.pdf", device / "Archive" / "20260911Z0330_gtd_sheet.rmdoc")
    before = {p.name: p.read_text() for p in vault_root.glob("*.md")}

    msg = tmp_path / "msg.txt"
    rc = main(["process", "--vault", str(vault_root), "--work-dir", str(work), "--today", "2026-09-15",
               "--ocr", "null", "--commit-message-file", str(msg)])
    assert rc == 0
    processed = json.loads((work / "processed.json").read_text())
    assert processed == {"folder": "GTD Daily", "sheets": ["20260914Z0330_gtd_sheet"], "pending": []}
    assert (work / "20260914Z0330_gtd_sheet.decisions.json").exists()
    assert (work / "20260914Z0330_gtd_sheet.tasks.json").exists()  # came out of the PDF
    status = (vault_root / "reMarkable status.md").read_text()
    assert "gtd: remarkable-status" in status and "20260914Z0330_gtd_sheet" in status
    assert msg.read_text().startswith("remarkable: processed 20260914Z0330_gtd_sheet, no vault changes")
    after = {p.name: p.read_text() for p in vault_root.glob("*.md") if p.name != "reMarkable status.md"}
    assert after == before  # a sheet with no ticks on it changes nothing

    rc = main(["publish", "--vault", str(vault_root), "--work-dir", str(work), "--today", "2026-09-15"])
    assert rc == 0
    calls = log.read_text().splitlines()
    assert any(c.startswith("-ni mv GTD Daily/20260914Z0330_gtd_sheet GTD Daily/Archive") for c in calls)
    assert (device / "Archive" / "20260914Z0330_gtd_sheet.rmdoc").exists()
    assert (device / "Archive" / "20260912Z0330_gtd_sheet.rmdoc").exists()      # 3 days old: kept
    assert not (device / "Archive" / "20260911Z0330_gtd_sheet.rmdoc").exists()  # 4 days old: rotated out
    assert any(c.startswith("-ni rm GTD Daily/Archive/20260911Z0330_gtd_sheet") for c in calls)
    uploaded = [p for p in device.iterdir() if p.suffix == ".pdf"]
    assert len(uploaded) == 1 and uploaded[0].name.endswith("_gtd_sheet.pdf")
    assert not (work / "processed.json").exists()

    # The uploaded sheet carries its own manifest + tasks (with vault handles).
    from remarkable_gtd.common.embedded import read_state

    manifest, tasks = read_state(uploaded[0].read_bytes())
    assert manifest["schema"] == "gtd.manifest/1"
    assert tasks["tasks"]["NA-01"]["handle"].startswith("next-actions:")
    assert tasks["tasks"]["IN-01"]["bucket"] == "inbox"


def test_process_with_nothing_on_device(vault_root, tmp_path, monkeypatch):
    (tmp_path / "device" / "GTD Daily").mkdir(parents=True)
    _fake_rmapi(tmp_path, monkeypatch, tmp_path / "device")
    work = tmp_path / "work"
    assert main(["process", "--vault", str(vault_root), "--work-dir", str(work), "--today", "2026-09-15", "--ocr", "null"]) == 0
    assert json.loads((work / "processed.json").read_text())["sheets"] == []
    assert not (vault_root / "reMarkable status.md").exists()


def test_offline_tablet_keeps_its_sheet(vault_root, tmp_path, monkeypatch):
    """The regression: a tablet that is offline holds its ink locally, so the
    cloud copy looks blank. Such a sheet must not be archived, must not be
    rotated out of existence, and must not be buried under a new one."""
    device = tmp_path / "device" / "GTD Daily"
    device.mkdir(parents=True)
    log = _fake_rmapi(tmp_path, monkeypatch, tmp_path / "device")
    work = tmp_path / "work"

    assert main(["render", "--vault", str(vault_root), "--today", "2026-09-14", "--out", str(work / "y.pdf")]) == 0
    sheet = device / "20260914Z0330_gtd_sheet.rmdoc"
    _pack_rmdoc(work / "y.pdf", sheet)  # no strokes: the tablet still has them

    # Three nights with the tablet offline.
    for day in ("2026-09-15", "2026-09-16", "2026-09-17"):
        assert main(["process", "--vault", str(vault_root), "--work-dir", str(work),
                     "--today", day, "--ocr", "null"]) == 0
        processed = json.loads((work / "processed.json").read_text())
        assert processed["sheets"] == []
        assert processed["pending"] == ["20260914Z0330_gtd_sheet"]
        assert main(["publish", "--vault", str(vault_root), "--work-dir", str(work), "--today", day]) == 0

    calls = log.read_text()
    assert "mv GTD Daily/20260914Z0330_gtd_sheet" not in calls  # never archived
    assert "rm GTD Daily" not in calls                          # never deleted
    assert sheet.exists()
    assert [p.name for p in device.iterdir() if p.suffix == ".pdf"] == []  # no new sheets
    status = (vault_root / "reMarkable status.md").read_text()
    assert "left on the device" in status

    # WiFi back on: the ink finally reaches the cloud and is picked up as normal.
    _pack_rmdoc(work / "y.pdf", sheet, strokes=True)
    assert main(["process", "--vault", str(vault_root), "--work-dir", str(work),
                 "--today", "2026-09-18", "--ocr", "null"]) == 0
    processed = json.loads((work / "processed.json").read_text())
    assert processed == {"folder": "GTD Daily", "sheets": ["20260914Z0330_gtd_sheet"], "pending": []}
    assert main(["publish", "--vault", str(vault_root), "--work-dir", str(work), "--today", "2026-09-18"]) == 0
    # Archived, and *not* rotated out in the same run although its name is four
    # days old: the grace period starts when we are finished with a sheet.
    assert (tmp_path / "device" / "GTD Daily" / "Archive" / "20260914Z0330_gtd_sheet.rmdoc").exists()
    assert len([p for p in device.iterdir() if p.suffix == ".pdf"]) == 1


def test_blank_sheet_retired_once_a_newer_one_comes_back_inked(vault_root, tmp_path, monkeypatch):
    """Ink on a newer sheet proves the tablet synced after the older one was
    uploaded, so the older blank really is blank and can be cleared away."""
    device = tmp_path / "device" / "GTD Daily"
    device.mkdir(parents=True)
    log = _fake_rmapi(tmp_path, monkeypatch, tmp_path / "device")
    work = tmp_path / "work"

    assert main(["render", "--vault", str(vault_root), "--today", "2026-09-14", "--out", str(work / "y.pdf")]) == 0
    _pack_rmdoc(work / "y.pdf", device / "20260914Z0330_gtd_sheet.rmdoc")                  # blank
    _pack_rmdoc(work / "y.pdf", device / "20260915Z0330_gtd_sheet.rmdoc", strokes=True)    # inked

    assert main(["process", "--vault", str(vault_root), "--work-dir", str(work),
                 "--today", "2026-09-16", "--ocr", "null"]) == 0
    processed = json.loads((work / "processed.json").read_text())
    assert sorted(processed["sheets"]) == ["20260914Z0330_gtd_sheet", "20260915Z0330_gtd_sheet"]
    assert processed["pending"] == []

    assert main(["publish", "--vault", str(vault_root), "--work-dir", str(work), "--today", "2026-09-16"]) == 0
    archive = tmp_path / "device" / "GTD Daily" / "Archive"
    assert (archive / "20260914Z0330_gtd_sheet.rmdoc").exists()
    assert (archive / "20260915Z0330_gtd_sheet.rmdoc").exists()
    assert len([p for p in device.iterdir() if p.suffix == ".pdf"]) == 1
    assert "mv GTD Daily/20260914Z0330_gtd_sheet" in log.read_text()


def test_unreadable_strokes_are_a_failure_not_a_wait(vault_root, tmp_path, monkeypatch):
    """A sheet that has been written on but whose strokes will not parse must be
    reported, not waited on forever as if it were blank."""
    device = tmp_path / "device" / "GTD Daily"
    device.mkdir(parents=True)
    _fake_rmapi(tmp_path, monkeypatch, tmp_path / "device")
    work = tmp_path / "work"

    assert main(["render", "--vault", str(vault_root), "--today", "2026-09-14", "--out", str(work / "y.pdf")]) == 0
    out = device / "20260914Z0330_gtd_sheet.rmdoc"
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("doc-uuid.content", json.dumps({"cPages": {"pages": [{"id": "page-uuid", "redir": {"value": 0}}]}}))
        z.write(work / "y.pdf", "doc-uuid.pdf")
        z.writestr("doc-uuid/page-uuid.rm", b"not a v6 stroke file at all")

    assert main(["process", "--vault", str(vault_root), "--work-dir", str(work),
                 "--today", "2026-09-15", "--ocr", "null"]) == 1
    processed = json.loads((work / "processed.json").read_text())
    assert processed["sheets"] == [] and processed["pending"] == []
    status = (vault_root / "reMarkable status.md").read_text()
    assert "none could be read" in status


def test_an_erased_sheet_is_blank_not_broken(vault_root, tmp_path, monkeypatch):
    """Written on and rubbed out reads cleanly and has no ink, so the sheet waits
    like any blank one. Calling it unreadable failed the run and left the sheet
    on the device, so the same failure repeated hourly and could not self-clear."""
    device = tmp_path / "device" / "GTD Daily"
    device.mkdir(parents=True)
    _fake_rmapi(tmp_path, monkeypatch, tmp_path / "device")
    work = tmp_path / "work"

    assert main(["render", "--vault", str(vault_root), "--today", "2026-09-14", "--out", str(work / "y.pdf")]) == 0
    _pack_rmdoc(work / "y.pdf", device / "20260914Z0330_gtd_sheet.rmdoc", erased=True)

    assert main(["process", "--vault", str(vault_root), "--work-dir", str(work),
                 "--today", "2026-09-15", "--ocr", "null"]) == 0
    processed = json.loads((work / "processed.json").read_text())
    assert processed["sheets"] == [] and processed["pending"] == ["20260914Z0330_gtd_sheet"]
    assert "none could be read" not in (vault_root / "reMarkable status.md").read_text()


def test_max_pending_allows_a_bounded_pile(vault_root, tmp_path, monkeypatch):
    """--max-pending 2 lets one more sheet land on top of a single blank one."""
    device = tmp_path / "device" / "GTD Daily"
    device.mkdir(parents=True)
    _fake_rmapi(tmp_path, monkeypatch, tmp_path / "device")
    work = tmp_path / "work"

    assert main(["render", "--vault", str(vault_root), "--today", "2026-09-14", "--out", str(work / "y.pdf")]) == 0
    _pack_rmdoc(work / "y.pdf", device / "20260914Z0330_gtd_sheet.rmdoc")

    assert main(["process", "--vault", str(vault_root), "--work-dir", str(work),
                 "--today", "2026-09-15", "--ocr", "null"]) == 0
    assert main(["publish", "--vault", str(vault_root), "--work-dir", str(work),
                 "--today", "2026-09-15", "--max-pending", "2"]) == 0
    assert len([p for p in device.iterdir() if p.suffix == ".pdf"]) == 1
