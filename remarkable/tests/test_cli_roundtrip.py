"""The CI path end to end with a fake device: render -> (device) -> process -> publish.

Needs Chromium (rendering) and the remarkable-gtd package; skipped otherwise.
"""
from __future__ import annotations

import json
import shutil
import stat
import zipfile
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


def _pack_rmdoc(pdf: Path, out: Path) -> None:
    """A minimal .rmdoc: the untouched PDF plus a .content (no strokes)."""
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("doc-uuid.content", json.dumps({"cPages": {"pages": []}}))
        z.write(pdf, "doc-uuid.pdf")


def test_process_then_publish(vault_root, tmp_path, monkeypatch):
    device = tmp_path / "device" / "GTD Daily"
    device.mkdir(parents=True)
    log = _fake_rmapi(tmp_path, monkeypatch, tmp_path / "device")
    work = tmp_path / "work"

    # Yesterday's sheet: rendered from the vault, "on the device", unannotated.
    assert main(["render", "--vault", str(vault_root), "--today", "2026-09-14", "--out", str(work / "y.pdf")]) == 0
    _pack_rmdoc(work / "y.pdf", device / "20260914Z0330_gtd_sheet.rmdoc")
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
    assert processed == {"folder": "GTD Daily", "sheets": ["20260914Z0330_gtd_sheet"]}
    assert (work / "20260914Z0330_gtd_sheet.decisions.json").exists()
    assert (work / "20260914Z0330_gtd_sheet.tasks.json").exists()  # came out of the PDF
    status = (vault_root / "reMarkable status.md").read_text()
    assert "gtd: remarkable-status" in status and "20260914Z0330_gtd_sheet" in status
    assert msg.read_text().startswith("remarkable: processed 20260914Z0330_gtd_sheet, no vault changes")
    after = {p.name: p.read_text() for p in vault_root.glob("*.md") if p.name != "reMarkable status.md"}
    assert after == before  # a blank sheet changes nothing

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
