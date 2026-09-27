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
        "    nodes = [{'name': p.stem if p.suffix in ('.rmdoc', '.pdf') else p.name,\n"
        "              'type': 'CollectionType' if p.is_dir() else 'DocumentType'} for p in folder.iterdir()]\n"
        "    print(json.dumps(nodes))\n"
        "elif cmd == 'get':\n"
        "    src = DEV / (args[1].strip('/') + '.rmdoc')\n"
        "    if src.exists(): shutil.copy(src, Path.cwd() / src.name)\n"
        "    else:\n"
        "        import zipfile\n"
        "        with zipfile.ZipFile(Path.cwd() / src.name, 'w') as z:\n"
        "            z.writestr('doc.content', json.dumps({'cPages': {'pages': []}}))\n"
        "            z.write(src.with_suffix('.pdf'), 'doc.pdf')\n"
        "elif cmd == 'put':\n"
        "    (DEV / args[-1].strip('/')).mkdir(parents=True, exist_ok=True)\n"
        "    shutil.copy(args[-2], DEV / args[-1].strip('/') / (Path(args[-2]).stem + '.pdf'))\n"
        "elif cmd == 'mkdir':\n"
        "    (DEV / args[1].strip('/')).mkdir(parents=True, exist_ok=True)\n"
        "elif cmd == 'rm':\n"
        "    base = DEV / args[1].strip('/')\n"
        "    next(p for p in (base.with_name(base.name + '.rmdoc'), base.with_name(base.name + '.pdf')) if p.exists()).unlink()\n"
        "elif cmd == 'mv':\n"
        "    base = DEV / args[1].strip('/'); dst = DEV / args[2].strip('/')\n"
        "    src = next(p for p in (base.with_name(base.name + '.rmdoc'), base.with_name(base.name + '.pdf')) if p.exists())\n"
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
    assert processed == {"folder": "GTD Daily", "superseded_folder": "GTD Daily/Superseded",
                         "sheets": ["GTD Daily/20260914Z0330_gtd_sheet"], "pending": []}
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


def _sheets(folder: Path) -> list[str]:
    return sorted(p.stem for p in folder.glob("*_gtd_sheet.*"))


@pytest.fixture
def stamps(monkeypatch):
    """Upload stamps that follow ``--today`` instead of the wall clock."""
    import gtd_remarkable.cli as cli

    state = {"day": None}
    real_main = cli.main

    def main_with_day(argv):
        if "--today" in argv:
            state["day"] = argv[argv.index("--today") + 1]
        return real_main(argv)

    monkeypatch.setattr(cli, "_stamp", lambda: state["day"].replace("-", "") + "Z0330")
    return main_with_day


def test_offline_tablet_keeps_its_sheet(vault_root, tmp_path, monkeypatch, stamps):
    """The regression: a tablet that is offline holds its ink locally, so the
    cloud copy looks blank. Such a sheet is moved aside, never archived and
    never deleted, while a fresh sheet goes up every day; when the ink finally
    syncs into the superseded folder it is applied as normal."""
    run = stamps
    device = tmp_path / "device" / "GTD Daily"
    device.mkdir(parents=True)
    superseded = device / "Superseded"
    log = _fake_rmapi(tmp_path, monkeypatch, tmp_path / "device")
    work = tmp_path / "work"

    assert run(["render", "--vault", str(vault_root), "--today", "2026-09-14", "--out", str(work / "y.pdf")]) == 0
    _pack_rmdoc(work / "y.pdf", device / "20260914Z0330_gtd_sheet.rmdoc")  # no strokes: the tablet still has them

    # Three nights with the tablet offline: a fresh sheet each day, the old
    # ones moved aside and still read.
    for day in ("2026-09-15", "2026-09-16", "2026-09-17"):
        assert run(["process", "--vault", str(vault_root), "--work-dir", str(work),
                    "--today", day, "--ocr", "null"]) == 0
        processed = json.loads((work / "processed.json").read_text())
        assert processed["sheets"] == []
        assert run(["publish", "--vault", str(vault_root), "--work-dir", str(work), "--today", day]) == 0
        stamp = day.replace("-", "")
        assert _sheets(device) == [f"{stamp}Z0330_gtd_sheet"]

    assert processed["pending"] == [
        "GTD Daily/Superseded/20260914Z0330_gtd_sheet",
        "GTD Daily/Superseded/20260915Z0330_gtd_sheet",
        "GTD Daily/20260916Z0330_gtd_sheet",
    ]
    assert _sheets(superseded) == [
        "20260914Z0330_gtd_sheet", "20260915Z0330_gtd_sheet", "20260916Z0330_gtd_sheet",
    ]
    assert " rm " not in log.read_text()          # nothing deleted
    assert not (device / "Archive").exists()      # nothing archived
    status = (vault_root / "reMarkable status.md").read_text()
    assert "Superseded, not written on yet (2)" in status and "20260914Z0330_gtd_sheet" in status

    # WiFi back on: the ink reaches the cloud on the document, now in the
    # superseded folder, and is picked up from there.
    _pack_rmdoc(work / "y.pdf", superseded / "20260914Z0330_gtd_sheet.rmdoc", strokes=True)
    assert run(["process", "--vault", str(vault_root), "--work-dir", str(work),
                "--today", "2026-09-18", "--ocr", "null"]) == 0
    processed = json.loads((work / "processed.json").read_text())
    assert processed["sheets"] == ["GTD Daily/Superseded/20260914Z0330_gtd_sheet"]
    assert run(["publish", "--vault", str(vault_root), "--work-dir", str(work), "--today", "2026-09-18"]) == 0
    # Archived, and *not* rotated out in the same run although its name is four
    # days old: the grace period starts when we are finished with a sheet.
    assert _sheets(device / "Archive") == ["20260914Z0330_gtd_sheet"]
    assert _sheets(superseded) == [
        "20260915Z0330_gtd_sheet", "20260916Z0330_gtd_sheet", "20260917Z0330_gtd_sheet",
    ]
    assert _sheets(device) == ["20260918Z0330_gtd_sheet"]


def test_a_newer_inked_sheet_does_not_retire_an_older_blank(vault_root, tmp_path, monkeypatch, stamps):
    """Ink on a newer sheet only proves the tablet synced once; the older sheet
    can still be written on later, offline. So it is kept readable, not archived."""
    run = stamps
    device = tmp_path / "device" / "GTD Daily"
    device.mkdir(parents=True)
    _fake_rmapi(tmp_path, monkeypatch, tmp_path / "device")
    work = tmp_path / "work"

    assert run(["render", "--vault", str(vault_root), "--today", "2026-09-14", "--out", str(work / "y.pdf")]) == 0
    _pack_rmdoc(work / "y.pdf", device / "20260914Z0330_gtd_sheet.rmdoc")                  # blank
    _pack_rmdoc(work / "y.pdf", device / "20260915Z0330_gtd_sheet.rmdoc", strokes=True)    # inked

    assert run(["process", "--vault", str(vault_root), "--work-dir", str(work),
                "--today", "2026-09-16", "--ocr", "null"]) == 0
    processed = json.loads((work / "processed.json").read_text())
    assert processed["sheets"] == ["GTD Daily/20260915Z0330_gtd_sheet"]
    assert processed["pending"] == ["GTD Daily/20260914Z0330_gtd_sheet"]

    assert run(["publish", "--vault", str(vault_root), "--work-dir", str(work), "--today", "2026-09-16"]) == 0
    assert _sheets(device / "Archive") == ["20260915Z0330_gtd_sheet"]
    assert _sheets(device / "Superseded") == ["20260914Z0330_gtd_sheet"]
    assert _sheets(device) == ["20260916Z0330_gtd_sheet"]


def test_a_second_run_in_a_day_does_not_churn(vault_root, tmp_path, monkeypatch, stamps):
    """Today's sheet, still blank, is left alone by a later run the same day —
    unless that run applied ink from another sheet, which makes it out of date."""
    run = stamps
    device = tmp_path / "device" / "GTD Daily"
    device.mkdir(parents=True)
    log = _fake_rmapi(tmp_path, monkeypatch, tmp_path / "device")
    work = tmp_path / "work"

    for _ in range(2):
        assert run(["process", "--vault", str(vault_root), "--work-dir", str(work),
                    "--today", "2026-09-15", "--ocr", "null"]) == 0
        assert run(["publish", "--vault", str(vault_root), "--work-dir", str(work), "--today", "2026-09-15"]) == 0
    assert _sheets(device) == ["20260915Z0330_gtd_sheet"]
    assert log.read_text().count(" put ") == 1

    # Ink from an older, superseded sheet syncs later the same day: applying it
    # changes the vault, so today's sheet is replaced after all.
    assert run(["render", "--vault", str(vault_root), "--today", "2026-09-14", "--out", str(work / "y.pdf")]) == 0
    (device / "Superseded").mkdir()
    _pack_rmdoc(work / "y.pdf", device / "Superseded" / "20260914Z0330_gtd_sheet.rmdoc", strokes=True)
    (device / "20260915Z0330_gtd_sheet.pdf").rename(device / "20260915Z0300_gtd_sheet.pdf")
    assert run(["process", "--vault", str(vault_root), "--work-dir", str(work),
                "--today", "2026-09-15", "--ocr", "null"]) == 0
    assert run(["publish", "--vault", str(vault_root), "--work-dir", str(work), "--today", "2026-09-15"]) == 0
    assert _sheets(device) == ["20260915Z0330_gtd_sheet"]
    assert _sheets(device / "Superseded") == ["20260915Z0300_gtd_sheet"]
    assert _sheets(device / "Archive") == ["20260914Z0330_gtd_sheet"]


def test_long_unwritten_superseded_sheets_are_deleted(vault_root, tmp_path, monkeypatch, stamps):
    """After --superseded-keep-days a blank superseded sheet is given up on; a
    younger one stays, and so does an old one that failed to read."""
    run = stamps
    device = tmp_path / "device" / "GTD Daily"
    superseded = device / "Superseded"
    superseded.mkdir(parents=True)
    log = _fake_rmapi(tmp_path, monkeypatch, tmp_path / "device")
    work = tmp_path / "work"

    assert run(["render", "--vault", str(vault_root), "--today", "2026-09-01", "--out", str(work / "y.pdf")]) == 0
    _pack_rmdoc(work / "y.pdf", superseded / "20260901Z0330_gtd_sheet.rmdoc")   # 15 days: goes
    _pack_rmdoc(work / "y.pdf", superseded / "20260902Z0330_gtd_sheet.rmdoc")   # 14 days: stays
    with zipfile.ZipFile(superseded / "20260831Z0330_gtd_sheet.rmdoc", "w") as z:  # unreadable: stays
        z.writestr("doc-uuid.content", json.dumps({"cPages": {"pages": [{"id": "p", "redir": {"value": 0}}]}}))
        z.write(work / "y.pdf", "doc-uuid.pdf")
        z.writestr("doc-uuid/p.rm", b"not a v6 stroke file at all")

    assert run(["process", "--vault", str(vault_root), "--work-dir", str(work),
                "--today", "2026-09-16", "--ocr", "null"]) == 1
    assert run(["publish", "--vault", str(vault_root), "--work-dir", str(work), "--today", "2026-09-16"]) == 0
    assert _sheets(superseded) == ["20260831Z0330_gtd_sheet", "20260902Z0330_gtd_sheet"]
    assert "rm GTD Daily/Superseded/20260901Z0330_gtd_sheet" in log.read_text()
    assert _sheets(device) == ["20260916Z0330_gtd_sheet"]


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
    assert processed["sheets"] == [] and processed["pending"] == ["GTD Daily/20260914Z0330_gtd_sheet"]
    assert "none could be read" not in (vault_root / "reMarkable status.md").read_text()
