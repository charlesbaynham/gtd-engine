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
        "    if dst.is_dir(): shutil.move(str(src), str(dst / src.name))\n"
        "    elif dst.parent.is_dir(): shutil.move(str(src), str(dst.with_name(dst.name + src.suffix)))\n"
        "    else: sys.exit(1)\n"
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


def _sheets(folder: Path) -> list[str]:
    return sorted(p.stem for p in folder.glob("*_gtd_sheet*.*"))


@pytest.fixture
def run(monkeypatch):
    """`main`, with upload stamps that follow ``--today`` instead of the wall
    clock: 03:30, 03:31, ... for successive uploads on the same day."""
    import gtd_remarkable.cli as cli

    state = {"day": None, "n": {}}

    def main_with_day(argv):
        if "--today" in argv:
            state["day"] = argv[argv.index("--today") + 1].replace("-", "")
        return main(argv)

    def stamp():
        n = state["n"].get(state["day"], 30)
        state["n"][state["day"]] = n + 1
        return f"{state['day']}Z03{n:02d}"

    monkeypatch.setattr(cli, "_stamp", stamp)
    return main_with_day


def _write_on(sheet: Path) -> None:
    """The tablet syncs ink onto an uploaded sheet: its PDF comes back with strokes."""
    pdf = sheet.with_suffix(".pdf")
    _pack_rmdoc(pdf, sheet.with_suffix(".rmdoc"), strokes=True)
    pdf.unlink()


def _cycle(run, vault_root, work, day, rc=0):
    assert run(["process", "--vault", str(vault_root), "--work-dir", str(work), "--today", day, "--ocr", "null"]) == rc
    processed = json.loads((work / "processed.json").read_text())
    assert run(["publish", "--vault", str(vault_root), "--work-dir", str(work), "--today", day]) == 0
    return processed


def test_process_then_publish(vault_root, tmp_path, monkeypatch, run):
    device = tmp_path / "device" / "GTD Daily"
    archive = device / "Archive"
    archive.mkdir(parents=True)
    log = _fake_rmapi(tmp_path, monkeypatch, tmp_path / "device")
    work = tmp_path / "work"

    # Yesterday's sheet: rendered from the vault, "on the device", written on
    # (no ticks land, but the ink is what makes it a sheet we are finished with).
    assert run(["render", "--vault", str(vault_root), "--today", "2026-09-14", "--out", str(work / "y.pdf")]) == 0
    _pack_rmdoc(work / "y.pdf", device / "20260914Z0330_gtd_sheet.rmdoc", strokes=True)
    # An older applied sheet: never read again, and cleared once a later one is inked.
    _pack_rmdoc(work / "y.pdf", archive / "20260913Z0330_gtd_sheet_applied.rmdoc", strokes=True)
    before = {p.name: p.read_text() for p in vault_root.glob("*.md")}

    msg = tmp_path / "msg.txt"
    rc = run(["process", "--vault", str(vault_root), "--work-dir", str(work), "--today", "2026-09-15",
              "--ocr", "null", "--commit-message-file", str(msg)])
    assert rc == 0
    processed = json.loads((work / "processed.json").read_text())
    assert processed == {"folder": "GTD Daily", "archive_folder": "GTD Daily/Archive",
                         "sheets": ["GTD Daily/20260914Z0330_gtd_sheet"], "pending": []}
    assert "get GTD Daily/Archive/20260913Z0330_gtd_sheet_applied" not in log.read_text()
    assert (work / "20260914Z0330_gtd_sheet.decisions.json").exists()
    assert (work / "20260914Z0330_gtd_sheet.tasks.json").exists()  # came out of the PDF
    status = (vault_root / "reMarkable status.md").read_text()
    assert "gtd: remarkable-status" in status and "20260914Z0330_gtd_sheet" in status
    assert msg.read_text().startswith("remarkable: processed 20260914Z0330_gtd_sheet, no vault changes")
    after = {p.name: p.read_text() for p in vault_root.glob("*.md") if p.name != "reMarkable status.md"}
    assert after == before  # a sheet with no ticks on it changes nothing

    assert run(["publish", "--vault", str(vault_root), "--work-dir", str(work), "--today", "2026-09-15"]) == 0
    calls = log.read_text().splitlines()
    assert "-ni mv GTD Daily/20260914Z0330_gtd_sheet GTD Daily/Archive/20260914Z0330_gtd_sheet_applied" in calls
    assert _sheets(archive) == ["20260914Z0330_gtd_sheet_applied"]  # the older applied one is gone
    assert _sheets(device) == ["20260915Z0330_gtd_sheet"]
    assert not (work / "processed.json").exists()

    # The uploaded sheet carries its own manifest + tasks (with vault handles).
    from remarkable_gtd.common.embedded import read_state

    manifest, tasks = read_state((device / "20260915Z0330_gtd_sheet.pdf").read_bytes())
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


def test_the_sheet_follows_the_vault(vault_root, tmp_path, monkeypatch, run):
    """Hourly runs: the sheet is replaced exactly when the vault no longer
    matches what it prints, and the replaced one goes to the archive."""
    device = tmp_path / "device" / "GTD Daily"
    device.mkdir(parents=True)
    log = _fake_rmapi(tmp_path, monkeypatch, tmp_path / "device")
    work = tmp_path / "work"

    for _ in range(3):
        _cycle(run, vault_root, work, "2026-09-15")
    assert _sheets(device) == ["20260915Z0330_gtd_sheet"]
    assert log.read_text().count(" put ") == 1

    inbox = vault_root / "Inbox.md"
    inbox.write_text(inbox.read_text() + "Ring the plumber\n", encoding="utf-8")
    _cycle(run, vault_root, work, "2026-09-15")
    assert _sheets(device) == ["20260915Z0331_gtd_sheet"]
    assert _sheets(device / "Archive") == ["20260915Z0330_gtd_sheet"]

    from remarkable_gtd.common.embedded import read_state

    _manifest, tasks = read_state((device / "20260915Z0331_gtd_sheet.pdf").read_bytes())
    assert any(t["act"] == "Ring the plumber" for t in tasks["tasks"].values())

    # A new day is a change too: the date is printed on the sheet.
    _cycle(run, vault_root, work, "2026-09-16")
    assert _sheets(device) == ["20260916Z0330_gtd_sheet"]


def test_offline_tablet_keeps_its_sheet(vault_root, tmp_path, monkeypatch, run):
    """The regression: a tablet that is offline holds its ink locally, so the
    cloud copy looks blank. Such a sheet is archived but still read every run,
    while fresh sheets keep going up; when the ink finally syncs into the
    archive it is applied, and only then are the sheets before it cleared."""
    device = tmp_path / "device" / "GTD Daily"
    device.mkdir(parents=True)
    archive = device / "Archive"
    log = _fake_rmapi(tmp_path, monkeypatch, tmp_path / "device")
    work = tmp_path / "work"

    assert run(["render", "--vault", str(vault_root), "--today", "2026-09-14", "--out", str(work / "y.pdf")]) == 0
    _pack_rmdoc(work / "y.pdf", device / "20260914Z0330_gtd_sheet.rmdoc")  # no strokes: the tablet still has them

    for day in ("2026-09-15", "2026-09-16", "2026-09-17"):
        processed = _cycle(run, vault_root, work, day)
        assert processed["sheets"] == []
        assert _sheets(device) == [f"{day.replace('-', '')}Z0330_gtd_sheet"]
    assert processed["pending"] == [
        "GTD Daily/Archive/20260914Z0330_gtd_sheet",
        "GTD Daily/Archive/20260915Z0330_gtd_sheet",
        "GTD Daily/20260916Z0330_gtd_sheet",
    ]
    assert _sheets(archive) == ["20260914Z0330_gtd_sheet", "20260915Z0330_gtd_sheet", "20260916Z0330_gtd_sheet"]
    assert " rm " not in log.read_text()
    status = (vault_root / "reMarkable status.md").read_text()
    assert "Archived, not written on yet (2)" in status and "20260914Z0330_gtd_sheet" in status

    # WiFi back on: the ink reaches the cloud on the archived document.
    _write_on(archive / "20260915Z0330_gtd_sheet")
    processed = _cycle(run, vault_root, work, "2026-09-18")
    assert processed["sheets"] == ["GTD Daily/Archive/20260915Z0330_gtd_sheet"]
    # The older blank is cleared, the newer ones are still waiting.
    assert _sheets(archive) == [
        "20260915Z0330_gtd_sheet_applied", "20260916Z0330_gtd_sheet", "20260917Z0330_gtd_sheet",
    ]
    assert _sheets(device) == ["20260918Z0330_gtd_sheet"]

    # A later sheet comes back inked: everything before it goes.
    _write_on(archive / "20260917Z0330_gtd_sheet")
    _cycle(run, vault_root, work, "2026-09-19")
    assert _sheets(archive) == ["20260917Z0330_gtd_sheet_applied", "20260918Z0330_gtd_sheet"]
    assert _sheets(device) == ["20260919Z0330_gtd_sheet"]


def test_archived_sheets_go_after_a_week(vault_root, tmp_path, monkeypatch, run):
    """Blank and applied sheets uploaded more than --keep-days ago are deleted;
    a sheet that failed to read is kept for a human."""
    device = tmp_path / "device" / "GTD Daily"
    archive = device / "Archive"
    archive.mkdir(parents=True)
    log = _fake_rmapi(tmp_path, monkeypatch, tmp_path / "device")
    work = tmp_path / "work"

    assert run(["render", "--vault", str(vault_root), "--today", "2026-09-01", "--out", str(work / "y.pdf")]) == 0
    _pack_rmdoc(work / "y.pdf", archive / "20260907Z0330_gtd_sheet.rmdoc")          # 8 days: goes
    _pack_rmdoc(work / "y.pdf", archive / "20260908Z0330_gtd_sheet.rmdoc")          # 7 days: stays
    _pack_rmdoc(work / "y.pdf", archive / "20260906Z0330_gtd_sheet_applied.rmdoc")  # 9 days: goes
    with zipfile.ZipFile(archive / "20260905Z0330_gtd_sheet.rmdoc", "w") as z:     # unreadable: stays
        z.writestr("doc-uuid.content", json.dumps({"cPages": {"pages": [{"id": "p", "redir": {"value": 0}}]}}))
        z.write(work / "y.pdf", "doc-uuid.pdf")
        z.writestr("doc-uuid/p.rm", b"not a v6 stroke file at all")

    _cycle(run, vault_root, work, "2026-09-15", rc=1)
    assert _sheets(archive) == ["20260905Z0330_gtd_sheet", "20260908Z0330_gtd_sheet"]
    assert "rm GTD Daily/Archive/20260907Z0330_gtd_sheet" in log.read_text()
    assert _sheets(device) == ["20260915Z0330_gtd_sheet"]


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
