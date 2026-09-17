"""`gtd-remarkable` — the vault side of the reMarkable round-trip.

Subcommands (all take ``--vault``, default cwd):

- ``tasks``    vault -> tasks.json (what `gtd-gen` renders)
- ``render``   tasks -> PDF (+ embedded manifest/tasks), no device access
- ``scan``     an .rmdoc -> decisions.json (device-free; needs the OCR key)
- ``apply``    decisions.json (+ tasks.json) -> vault edits
- ``process``  every sheet on the device: download, scan, apply; writes
               `<work>/processed.json`, `reMarkable status.md` and a commit
               message. Nothing is archived on the device here so a failed
               push can simply be re-run.
- ``publish``  archive the sheets `process` handled, delete archived sheets
               older than ``--keep-days`` (default 3), render today's sheet
               from the (now updated) vault and upload it.

Environment: ``REMARKABLE_FOLDER`` (default ``GTD Daily``),
``REMARKABLE_ARCHIVE_FOLDER`` (default ``<folder>/Archive``),
``REMARKABLE_KEEP_DAYS`` (default 3; 0 keeps everything),
``RMAPI_DEVICE_TOKEN`` (rmapi auth for headless runs), ``OPENROUTER_API_KEY``
/ ``OPENROUTER_MODEL`` (handwriting) / ``OPENROUTER_AI_MODEL`` (the ✦ AI
agent alone; falls back to ``OPENROUTER_MODEL``), ``RMAPI_BIN``.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from gtd_ci.model import load_vault, save_vault

from .apply import apply_decisions
from .report import SheetResult, commit_message, render_status, sheet_counts, write_status
from .tasks import build_tasks

_LONDON = ZoneInfo("Europe/London")
PROCESSED_FILE = "processed.json"


def _today(args) -> date:
    return date.fromisoformat(args.today) if args.today else datetime.now(_LONDON).date()


def _run_label(args) -> str:
    if args.today:
        return f"{args.today} 00:00"
    return datetime.now(_LONDON).strftime("%Y-%m-%d %H:%M")


def _folder(args) -> str:
    return args.folder or os.environ.get("REMARKABLE_FOLDER", "GTD Daily")


def _archive_folder(args) -> str:
    return args.archive_folder or os.environ.get("REMARKABLE_ARCHIVE_FOLDER") or f"{_folder(args)}/Archive"


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dZ%H%M")


_SHEET_STAMP = re.compile(r"^(\d{4})(\d{2})(\d{2})Z\d{4}_gtd_sheet$")


def sheet_date(name: str) -> date | None:
    """The date in a ``YYYYMMDDZHHMM_gtd_sheet`` name, or ``None``."""
    m = _SHEET_STAMP.match(name)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def stale_sheets(names: list[str], today: date, keep_days: int) -> list[str]:
    """Sheets dated before ``today - keep_days``; ``keep_days <= 0`` keeps all."""
    if keep_days <= 0:
        return []
    cutoff = today - timedelta(days=keep_days)
    return [n for n in names if (d := sheet_date(n)) is not None and d < cutoff]


def _keep_days(args) -> int:
    if args.keep_days is not None:
        return args.keep_days
    return int(os.environ.get("REMARKABLE_KEEP_DAYS", "3"))


def _scan_cfg(ocr: str):
    from remarkable_gtd.scan.pipeline import ScanConfig

    return ScanConfig(ocr_engine=ocr)


# --- subcommands ------------------------------------------------------------------


def cmd_tasks(args) -> int:
    vault = load_vault(Path(args.vault))
    tasks = build_tasks(vault, _today(args))
    text = json.dumps(tasks, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


def cmd_render(args) -> int:
    from remarkable_gtd.gen.generate import render_pdf

    vault = load_vault(Path(args.vault))
    today = _today(args)
    tasks = build_tasks(vault, today)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    render_pdf(tasks, today, out, manifest_path=out.with_suffix(".manifest.json"))
    out.with_suffix(".tasks.json").write_text(json.dumps(tasks, indent=2), encoding="utf-8")
    print(f"wrote {out}")
    return 0


def cmd_scan(args) -> int:
    from remarkable_gtd.scan.sheet import scan_rmdoc, summarize

    work = Path(args.work_dir)
    decisions, _manifest, tasks_doc, annotated = scan_rmdoc(Path(args.rmdoc), work, _scan_cfg(args.ocr))
    if tasks_doc is not None:
        (work / f"{Path(args.rmdoc).stem}.tasks.json").write_text(json.dumps(tasks_doc, indent=2), encoding="utf-8")
    print(f"rendered {annotated}; {summarize(decisions)}")
    return 0


def _print_report(report) -> None:
    for line in report.applied:
        print(f"  ✓ {line}")
    for line in report.notes:
        print(f"  · {line}")
    for line in report.skipped:
        print(f"  – {line}")
    for line in report.warnings:
        print(f"  ⚠ {line}")


def cmd_apply(args) -> int:
    root = Path(args.vault)
    vault = load_vault(root)
    decisions = json.loads(Path(args.decisions).read_text(encoding="utf-8"))
    tasks_doc = json.loads(Path(args.tasks).read_text(encoding="utf-8")) if args.tasks else None
    report = apply_decisions(vault, _today(args), decisions, tasks_doc)
    _print_report(report)
    if args.dry_run:
        print(f"(dry run — {len(vault.dirty)} file(s) would change: {sorted(vault.dirty)})")
        return 0
    save_vault(vault)
    return 0


def cmd_process(args) -> int:
    from remarkable_gtd.rm import api as rm
    from remarkable_gtd.scan.sheet import scan_rmdoc, summarize

    rm.write_config_from_env()
    root = Path(args.vault)
    work = Path(args.work_dir)
    work.mkdir(parents=True, exist_ok=True)
    today = _today(args)
    folder = _folder(args)

    names = rm.list_sheets(folder)
    print(f"{len(names)} sheet(s) in '{folder}': {', '.join(names) or '-'}")
    results: list[SheetResult] = []
    processed: list[str] = []
    failed = 0

    vault = load_vault(root)
    for name in names:
        remote = f"{folder}/{name}"
        print(f"→ {remote}")
        try:
            rmdoc = rm.download(remote, work)
            decisions, _manifest, tasks_doc, _annotated = scan_rmdoc(rmdoc, work, _scan_cfg(args.ocr))
        except Exception as exc:  # a broken sheet stays on the device for a human to look at
            print(f"  ✗ {exc}", file=sys.stderr)
            results.append(SheetResult(name, scanned=False, error=str(exc)))
            failed += 1
            continue
        counts = sheet_counts(decisions, summarize(decisions))
        print(f"  scanned: {counts}")
        if tasks_doc is not None:
            (work / f"{name}.tasks.json").write_text(json.dumps(tasks_doc, indent=2), encoding="utf-8")
        report = apply_decisions(vault, today, decisions, tasks_doc)
        _print_report(report)
        results.append(SheetResult(name, scanned=True, counts=counts, apply=report))
        processed.append(name)

    if args.dry_run:
        print(f"(dry run — {len(vault.dirty)} file(s) would change: {sorted(vault.dirty)})")
        print(render_status(results, _run_label(args), today.isoformat()))
        return 1 if failed else 0

    save_vault(vault)
    if results:
        write_status(root, render_status(results, _run_label(args), today.isoformat()))
    (work / PROCESSED_FILE).write_text(json.dumps({"folder": folder, "sheets": processed}, indent=2), encoding="utf-8")
    if args.commit_message_file:
        Path(args.commit_message_file).write_text(commit_message(results), encoding="utf-8")
    return 1 if failed else 0


def cmd_publish(args) -> int:
    from remarkable_gtd.gen.generate import render_pdf
    from remarkable_gtd.rm import api as rm

    rm.write_config_from_env()
    root = Path(args.vault)
    work = Path(args.work_dir)
    work.mkdir(parents=True, exist_ok=True)
    today = _today(args)
    folder = _folder(args)

    archive = _archive_folder(args)
    processed_path = work / PROCESSED_FILE
    if processed_path.exists():
        info = json.loads(processed_path.read_text(encoding="utf-8"))
        for name in info.get("sheets", []):
            remote = f"{info.get('folder', folder)}/{name}"
            print(f"→ archiving {remote} -> {archive}")
            rm.move(remote, archive)
        processed_path.unlink()

    # Rotate: the archive only needs the last few days (the decisions are in
    # git and remarkable-out/ is a CI artifact).
    for name in stale_sheets(rm.list_sheets(archive), today, _keep_days(args)):
        print(f"→ deleting {archive}/{name} (older than {_keep_days(args)} days)")
        rm.remove(f"{archive}/{name}")

    vault = load_vault(root)
    tasks = build_tasks(vault, today)
    stamp = _stamp()
    pdf = work / f"{stamp}_gtd_sheet.pdf"
    print(f"→ rendering {pdf.name}")
    render_pdf(tasks, today, pdf, manifest_path=pdf.with_suffix(".manifest.json"))
    pdf.with_suffix(".tasks.json").write_text(json.dumps(tasks, indent=2), encoding="utf-8")
    print(f"→ uploading to '{folder}'")
    rm.upload(pdf, folder)
    counts = {k: len(v) for k, v in tasks.items() if isinstance(v, list)}
    counts["tickler"] = sum(len(v) for v in tasks["tickler"].values())
    print(f"✓ {pdf.name} on the device — {counts}")
    return 0


# --- parser --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gtd-remarkable", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp, work=False, device=False):
        sp.add_argument("--vault", default=".")
        sp.add_argument("--today", help="YYYY-MM-DD (default: today, Europe/London)")
        if work:
            sp.add_argument("--work-dir", default="remarkable-out")
        if device:
            sp.add_argument("--folder", default=None, help="reMarkable folder (env REMARKABLE_FOLDER)")

    s = sub.add_parser("tasks"); common(s); s.add_argument("-o", "--out"); s.set_defaults(func=cmd_tasks)
    s = sub.add_parser("render"); common(s); s.add_argument("--out", default="remarkable-out/sheet.pdf"); s.set_defaults(func=cmd_render)
    s = sub.add_parser("scan"); common(s, work=True); s.add_argument("rmdoc")
    s.add_argument("--ocr", default="openrouter", choices=["openrouter", "tesseract", "null"]); s.set_defaults(func=cmd_scan)
    s = sub.add_parser("apply"); common(s); s.add_argument("decisions"); s.add_argument("--tasks", help="gtd.tasks/1 document (from the PDF)")
    s.add_argument("--dry-run", action="store_true"); s.set_defaults(func=cmd_apply)
    s = sub.add_parser("process"); common(s, work=True, device=True)
    s.add_argument("--ocr", default="openrouter", choices=["openrouter", "tesseract", "null"])
    s.add_argument("--dry-run", action="store_true"); s.add_argument("--commit-message-file"); s.set_defaults(func=cmd_process)
    s = sub.add_parser("publish"); common(s, work=True, device=True)
    s.add_argument("--archive-folder", default=None, help="where processed sheets go (env REMARKABLE_ARCHIVE_FOLDER)")
    s.add_argument("--keep-days", type=int, default=None, help="delete archived sheets older than this (env REMARKABLE_KEEP_DAYS, default 3; 0 keeps all)")
    s.set_defaults(func=cmd_publish)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
