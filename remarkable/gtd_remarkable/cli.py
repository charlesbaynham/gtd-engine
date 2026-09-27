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
               older than ``--keep-days`` (default 3), move unwritten sheets
               out of the way into the superseded folder, render today's
               sheet from the (now updated) vault and upload it.

**A sheet is only finished with once we have seen the ink on it.** The cloud
copy of a sheet the tablet is still holding is byte-identical to the one we
uploaded, so "no strokes in the ``.rmdoc``" is indistinguishable from "the
tablet has been offline for three days with your ticks on it". Such a sheet
is *pending*: `process` neither scans nor archives it. `publish` still puts a
fresh sheet on the device (at most one replacement a day), and moves the
pending one into the superseded folder, which `process` reads on every run
just like the main one. Moving a document in the cloud is safe while the
tablet holds unsynced strokes for it: the tablet syncs by document id, so the
ink follows the document into its new folder (tested on a real device,
2026-09-27). A superseded sheet still blank after ``--superseded-keep-days``
(default 14) is deleted.

Environment: ``REMARKABLE_FOLDER`` (default ``GTD Daily``),
``REMARKABLE_ARCHIVE_FOLDER`` (default ``<folder>/Archive``),
``REMARKABLE_KEEP_DAYS`` (default 3; 0 keeps everything),
``REMARKABLE_SUPERSEDED_FOLDER`` (default ``<folder>/Superseded``),
``REMARKABLE_SUPERSEDED_KEEP_DAYS`` (default 14; 0 keeps everything),
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


def _superseded_folder(args) -> str:
    return (
        args.superseded_folder
        or os.environ.get("REMARKABLE_SUPERSEDED_FOLDER")
        or f"{_folder(args)}/Superseded"
    )


def _parent(remote: str) -> str:
    return remote.rpartition("/")[0]


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dZ%H%M")


_SHEET_STAMP = re.compile(r"^(\d{4})(\d{2})(\d{2})Z(\d{2})(\d{2})_gtd_sheet$")


def sheet_date(name: str) -> date | None:
    """The date in a ``YYYYMMDDZHHMM_gtd_sheet`` name, or ``None``."""
    m = _SHEET_STAMP.match(name)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def sheet_local_date(name: str) -> date | None:
    """The London date a ``YYYYMMDDZHHMM_gtd_sheet`` was uploaded on, or ``None``.

    The stamp is UTC; "was today's sheet already published" is a question
    about the vault's day, so it is converted.
    """
    m = _SHEET_STAMP.match(name)
    if not m:
        return None
    try:
        at = datetime(*(int(g) for g in m.groups()), tzinfo=timezone.utc)
    except ValueError:
        return None
    return at.astimezone(_LONDON).date()


def stale_sheets(
    names: list[str], today: date, keep_days: int, just_archived: set[str] | None = None
) -> list[str]:
    """Sheets dated before ``today - keep_days``; ``keep_days <= 0`` keeps all.

    The date is the one in the name, i.e. when the sheet was *uploaded*, not
    when it was archived. A sheet the tablet held onto while it was offline is
    therefore already "stale" the moment we finally read it, so a sheet
    archived in this very run is never deleted in the same run: the grace
    period is meant to start when we are finished with a sheet.
    """
    if keep_days <= 0:
        return []
    cutoff = today - timedelta(days=keep_days)
    just_archived = just_archived or set()
    return [
        n
        for n in names
        if n not in just_archived and (d := sheet_date(n)) is not None and d < cutoff
    ]


def _keep_days(args) -> int:
    if args.keep_days is not None:
        return args.keep_days
    return int(os.environ.get("REMARKABLE_KEEP_DAYS", "3"))


def _superseded_keep_days(args) -> int:
    if args.superseded_keep_days is not None:
        return args.superseded_keep_days
    return int(os.environ.get("REMARKABLE_SUPERSEDED_KEEP_DAYS", "14"))


def ink(rmdoc: Path) -> tuple[int, int]:
    """``(strokes read, layers whose ink would not parse)`` on a downloaded sheet.

    A sheet the tablet is still holding comes back byte-identical to the one
    we uploaded: no ``.rm`` layers at all. That is the only evidence we get
    that the tablet has not yet handed its copy back — an offline tablet keeps
    the ink locally while the cloud copy stays pristine — so it is what
    decides whether a sheet is finished with or still live.

    ⚠️ An erased stroke is not an unreadable one. The tablet records an erasure
    as a line item with no value, so a sheet written on and then rubbed out
    parses cleanly and simply has nothing left on it — "layers but no strokes"
    is not evidence the ink could not be read. Only a layer that raises
    ``UnreadableLayer`` is "written on and we cannot read it", which is a
    failure to shout about rather than a sheet to go on waiting for. Counting
    erasures as unreadable wedged the pipeline for 13 hourly runs on
    2026-09-24: the sheet is deliberately left on the device, so the same
    failure repeated with no way to clear itself.
    """
    from remarkable_gtd.rm.annotations import (
        UnreadableLayer,
        extract_from_rmdoc,
        read_annotations,
    )

    _pdf, rm_by_page = extract_from_rmdoc(rmdoc)
    strokes = unreadable = 0
    for blob in rm_by_page.values():
        if not blob:
            continue
        try:
            strokes += len(read_annotations(blob))
        except UnreadableLayer:
            unreadable += 1
    return strokes, unreadable


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
    superseded = _superseded_folder(args)

    # Superseded sheets are read exactly like current ones: a tablet that was
    # offline when its sheet was moved still hands the ink over, into the new
    # folder. Oldest first across both: the names sort by upload time.
    remotes = sorted(
        [f"{folder}/{n}" for n in rm.list_sheets(folder)]
        + [f"{superseded}/{n}" for n in rm.list_sheets(superseded)],
        key=lambda r: r.rpartition("/")[2],
    )
    print(f"{len(remotes)} sheet(s) in '{folder}' and '{superseded}': "
          f"{', '.join(r.rpartition('/')[2] for r in remotes) or '-'}")
    results: list[SheetResult] = []
    processed: list[str] = []
    pending: list[str] = []
    failed = 0

    vault = load_vault(root)
    for remote in remotes:
        name = remote.rpartition("/")[2]
        is_superseded = _parent(remote) == superseded
        print(f"→ {remote}")
        try:
            rmdoc = rm.download(remote, work)
            strokes, unreadable = ink(rmdoc)
        except Exception as exc:  # a broken sheet stays on the device for a human to look at
            print(f"  ✗ {exc}", file=sys.stderr)
            results.append(SheetResult(name, scanned=False, error=str(exc)))
            failed += 1
            continue
        if not strokes and unreadable:
            # Written on, but the strokes will not parse. Don't wait on this
            # sheet as if it were blank — say so and leave it for a human.
            exc = f"{unreadable} stroke layer(s) on the sheet but none could be read"
            print(f"  ✗ {exc}", file=sys.stderr)
            results.append(SheetResult(name, scanned=False, error=exc))
            failed += 1
            continue
        if not strokes:
            # Nothing written on it *as far as the cloud knows*. The tablet may
            # simply be offline with a week of ticks on its own copy, so this
            # sheet is not scanned and not archived; `publish` may move it into
            # the superseded folder, where it is still read on every run.
            print("  no ink yet — left for a later run")
            results.append(SheetResult(name, scanned=False, pending=True, superseded=is_superseded))
            pending.append(remote)
            continue
        try:
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
        processed.append(remote)

    if args.dry_run:
        print(f"(dry run — {len(vault.dirty)} file(s) would change: {sorted(vault.dirty)})")
        if pending:
            print(f"(not written on yet: {', '.join(pending)})")
        print(render_status(results, _run_label(args), today.isoformat(), superseded))
        return 1 if failed else 0

    save_vault(vault)
    if results:
        write_status(root, render_status(results, _run_label(args), today.isoformat(), superseded))
    (work / PROCESSED_FILE).write_text(
        json.dumps(
            {"folder": folder, "superseded_folder": superseded, "sheets": processed, "pending": pending},
            indent=2,
        ),
        encoding="utf-8",
    )
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
    superseded = _superseded_folder(args)

    archive = _archive_folder(args)
    processed_path = work / PROCESSED_FILE
    pending: list[str] = []
    archived: set[str] = set()
    if processed_path.exists():
        info = json.loads(processed_path.read_text(encoding="utf-8"))
        folder = info.get("folder", folder)
        superseded = info.get("superseded_folder", superseded)
        pending = list(info.get("pending", []))
        for remote in info.get("sheets", []):
            print(f"→ archiving {remote} -> {archive}")
            rm.move(remote, archive)
            archived.add(remote.rpartition("/")[2])
        processed_path.unlink()

    # Rotate: the archive only needs the last few days (the decisions are in
    # git and remarkable-out/ is a CI artifact). Only sheets we have actually
    # read the ink off ever reach the archive, so nothing deleted here can
    # still be holding writing the tablet has not handed over.
    for name in stale_sheets(rm.list_sheets(archive), today, _keep_days(args), archived):
        print(f"→ deleting {archive}/{name} (older than {_keep_days(args)} days)")
        rm.remove(f"{archive}/{name}")

    # A superseded sheet nobody has written on for this long is given up on.
    # Only ones `process` just saw blank are candidates: a superseded sheet
    # that failed to scan may be holding ink, and stays for a human.
    keep = _superseded_keep_days(args)
    old_blanks = [r.rpartition("/")[2] for r in pending if _parent(r) == superseded]
    for name in stale_sheets(old_blanks, today, keep):
        print(f"→ deleting {superseded}/{name} (never written on, older than {keep} days)")
        rm.remove(f"{superseded}/{name}")

    # The current sheet came back blank. If it went up today and nothing has
    # been applied since, leave it: a second run in a day should not churn out
    # another. Otherwise move it out of the way — still read on every run —
    # and put a fresh one up.
    current_blank = [r for r in pending if _parent(r) == folder]
    if not archived and any(sheet_local_date(r.rpartition("/")[2]) == today for r in current_blank):
        print("→ not publishing: today's sheet is already on the device, not written on yet")
        return 0
    for remote in current_blank:
        print(f"→ superseding {remote} -> {superseded} (not written on yet; still read every run)")
        rm.move(remote, superseded)

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
            sp.add_argument("--superseded-folder", default=None,
                            help="where unwritten sheets go once replaced; still read every run "
                                 "(env REMARKABLE_SUPERSEDED_FOLDER, default <folder>/Superseded)")

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
    s.add_argument("--superseded-keep-days", type=int, default=None, help="delete superseded sheets never written on after this many days (env REMARKABLE_SUPERSEDED_KEEP_DAYS, default 14; 0 keeps all)")
    s.set_defaults(func=cmd_publish)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
