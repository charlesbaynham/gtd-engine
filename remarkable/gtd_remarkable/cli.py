"""`gtd-remarkable` — the vault side of the reMarkable round-trip.

Subcommands (all take ``--vault``, default cwd):

- ``tasks``    vault -> tasks.json (what `gtd-gen` renders)
- ``render``   tasks -> PDF (+ embedded manifest/tasks), no device access
- ``scan``     an .rmdoc -> decisions.json (device-free; needs the OCR key)
- ``apply``    decisions.json (+ tasks.json) -> vault edits
- ``process``  sweep every unapplied sheet on the device (the folder and its
               archive): download, scan, apply; writes `<work>/processed.json`,
               `reMarkable status.md` and a commit message. Nothing is moved
               on the device here so a failed push can simply be re-run.
- ``publish``  file the applied sheets in the archive, bring the current sheet
               up to date with the (now updated) vault, and clear the archive.

**A sheet is only finished with once we have seen the ink on it.** The cloud
copy of a sheet the tablet is still holding is byte-identical to the one we
uploaded, so "no strokes in the ``.rmdoc``" is indistinguishable from "the
tablet has been offline for three days with your ticks on it". So nothing is
decided from a blank sheet: every run reads the folder *and* the archive, and
ink that turns up on any sheet there is applied, however old the sheet.

That lets the device stay current. Whenever the vault no longer matches what
the current sheet prints, `publish` moves it into the archive and uploads a
fresh one. Moving a document in the cloud is safe while the tablet holds
unsynced strokes for it: the tablet syncs by document id, so the ink follows
the document into its new folder (tested on a real device, 2026-09-27).

Every sheet is named for the moment it was rendered, to the second
(``YYYYMMDDTHHMMSSZ_gtd_sheet``, UTC), and the vault records the stamp of
the newest sheet applied so far (``.remarkable.json``, committed with the
edits it produced). A sheet stamped at or before it is never read again, so
nothing is applied twice and no sheet is ever renamed. The archive is cleared
of every sheet stamped before it (the tablet synced after they were
replaced), and of anything older than ``--keep-days`` (default 7).

Environment: ``REMARKABLE_FOLDER`` (default ``GTD Daily``),
``REMARKABLE_ARCHIVE_FOLDER`` (default ``<folder>/Archive``),
``REMARKABLE_KEEP_DAYS`` (default 7; 0 keeps everything),
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


def _parent(remote: str) -> str:
    return remote.rpartition("/")[0]


# Named for the moment the sheet was rendered, UTC, to the second. Sheets from
# before 2026-09-27 carry the older minute-resolution stamp; both are read.
SHEET_NAME_RE = re.compile(r"^(?:\d{8}T\d{6}Z|\d{8}Z\d{4})_gtd_sheet$")
_STAMP_FORMATS = (("%Y%m%dT%H%M%SZ", 16), ("%Y%m%dZ%H%M", 13))
STATE_FILE = ".remarkable.json"


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sheet_time(name: str) -> datetime | None:
    """When a sheet was rendered (UTC), from its name, or ``None``."""
    if not SHEET_NAME_RE.match(name):
        return None
    for fmt, width in _STAMP_FORMATS:
        try:
            return datetime.strptime(name[:width], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def sheet_date(name: str) -> date | None:
    """The UTC date a sheet was rendered on, from its name, or ``None``."""
    t = sheet_time(name)
    return t.date() if t else None


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_last_applied(root: Path) -> tuple[bool, datetime | None]:
    """``(recorded, stamp)``: whether the vault has a record at all, and the
    stamp of the newest sheet applied to it (``None`` if none has been)."""
    try:
        value = json.loads((root / STATE_FILE).read_text(encoding="utf-8"))["last_applied"]
    except OSError:
        return False, None
    except (ValueError, KeyError, TypeError):
        return True, None
    try:
        return True, datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return True, None


def write_last_applied(root: Path, t: datetime | None) -> None:
    body = {"last_applied": _iso(t) if t is not None else None}
    (root / STATE_FILE).write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8", newline="\n")


def archive_to_delete(names: list[str], last_applied: datetime | None, today: date, keep_days: int) -> list[str]:
    """Archive sheets to delete: every one rendered before the newest applied
    sheet, and every one rendered more than ``keep_days`` ago (0: no age limit).

    Ink on a sheet proves the tablet synced after every older sheet had been
    replaced, so an older sheet still blank is taken to be blank; and an older
    inked one has been applied already.
    """
    cutoff = today - timedelta(days=keep_days) if keep_days > 0 else None
    out = []
    for n in sorted(names, key=lambda n: sheet_time(n) or datetime.min.replace(tzinfo=timezone.utc)):
        t = sheet_time(n)
        if t is None:
            continue
        if (last_applied is not None and t < last_applied) or (cutoff is not None and t.date() < cutoff):
            out.append(n)
    return out


def _keep_days(args) -> int:
    if args.keep_days is not None:
        return args.keep_days
    return int(os.environ.get("REMARKABLE_KEEP_DAYS", "7"))


def embedded_tasks(rmdoc: Path) -> dict | None:
    """The ``gtd.tasks/1`` document a sheet was printed with, or ``None``."""
    import zipfile

    from remarkable_gtd.common.embedded import read_state

    try:
        with zipfile.ZipFile(rmdoc) as z:
            pdf = next((n for n in z.namelist() if n.endswith(".pdf")), None)
            return read_state(z.read(pdf))[1] if pdf else None
    except Exception:
        return None


def sheet_tasks(tasks: dict, today: date) -> dict:
    """The ``gtd.tasks/1`` document `render_pdf` would embed for ``tasks``.

    Built the same way, without the browser, so the current sheet can be
    compared with the vault before deciding whether to render at all.
    """
    import copy

    from remarkable_gtd.common.embedded import tasks_document
    from remarkable_gtd.gen.generate import build_buckets

    doc = tasks_document(build_buckets(copy.deepcopy(tasks)), today.strftime("%Y-%m-%d"), tasks.get("context"))
    return json.loads(json.dumps(doc))


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
    archive = _archive_folder(args)

    # Every sheet not yet applied, wherever it is: a tablet that was offline
    # when its sheet was replaced hands the ink over later, into the archive.
    # A sheet stamped at or before the newest applied one is done with.
    in_folder = [f"{folder}/{n}" for n in rm.list_sheets(folder, SHEET_NAME_RE)]
    in_archive = [f"{archive}/{n}" for n in rm.list_sheets(archive, SHEET_NAME_RE)]
    recorded, last_applied = read_last_applied(root)
    if not recorded and in_archive:
        # First run on this vault: everything an earlier version archived had
        # been applied (or found blank) already, so start after the newest.
        # Written below, so this happens once, never on a later run.
        last_applied = max(sheet_time(r.rpartition("/")[2]) for r in in_archive)
        print(f"no {STATE_FILE} yet: treating sheets archived up to {_iso(last_applied)} as applied")
    remotes = sorted(
        (r for r in in_folder + in_archive
         if last_applied is None or sheet_time(r.rpartition("/")[2]) > last_applied),
        key=lambda r: sheet_time(r.rpartition("/")[2]),
    )
    print(f"{len(remotes)} unapplied sheet(s) in '{folder}' and '{archive}': "
          f"{', '.join(r.rpartition('/')[2] for r in remotes) or '-'}")
    results: list[SheetResult] = []
    processed: list[str] = []
    pending: list[str] = []
    failed = 0

    vault = load_vault(root)
    for remote in remotes:
        name = remote.rpartition("/")[2]
        archived = _parent(remote) == archive
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
            # simply be offline with a week of ticks on its own copy, so it is
            # not scanned, and read again next run wherever `publish` puts it.
            print("  no ink yet")
            results.append(SheetResult(name, scanned=False, pending=True, archived=archived))
            pending.append(remote)
            if not archived and (printed := embedded_tasks(rmdoc)) is not None:
                # What the current sheet prints, for `publish` to compare with the vault.
                (work / f"{name}.tasks.json").write_text(json.dumps(printed, indent=2), encoding="utf-8")
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
        last_applied = sheet_time(name)

    if args.dry_run:
        print(f"(dry run — {len(vault.dirty)} file(s) would change: {sorted(vault.dirty)})")
        if pending:
            print(f"(not written on yet: {', '.join(pending)})")
        print(render_status(results, _run_label(args), today.isoformat(), archive))
        return 1 if failed else 0

    save_vault(vault)
    if (True, last_applied) != read_last_applied(root):
        write_last_applied(root, last_applied)  # committed with the edits it covers
    if results:
        write_status(root, render_status(results, _run_label(args), today.isoformat(), archive))
    (work / PROCESSED_FILE).write_text(
        json.dumps({"folder": folder, "archive_folder": archive, "sheets": processed, "pending": pending}, indent=2),
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
    archive = _archive_folder(args)

    processed_path = work / PROCESSED_FILE
    applied: list[str] = []
    pending: list[str] = []
    if processed_path.exists():
        info = json.loads(processed_path.read_text(encoding="utf-8"))
        folder = info.get("folder", folder)
        archive = info.get("archive_folder", archive)
        applied = list(info.get("sheets", []))
        pending = list(info.get("pending", []))
        processed_path.unlink()

    # 1. An applied sheet in the folder goes to the archive with the rest. It
    #    is never read again: the vault's record says it has been applied.
    for remote in applied:
        if _parent(remote) != archive:
            print(f"→ archiving {remote} -> {archive} (applied)")
            rm.move(remote, archive)

    # 2. The current sheet: keep it only while it still prints what the vault
    #    holds. Otherwise it goes to the archive, where it is still read.
    vault = load_vault(root)
    tasks = build_tasks(vault, today)
    current = sorted((r for r in pending if _parent(r) == folder), key=lambda r: sheet_time(r.rpartition("/")[2]))
    keep = None
    if current:
        printed = work / f"{current[-1].rpartition('/')[2]}.tasks.json"
        if printed.exists() and json.loads(printed.read_text(encoding="utf-8")) == sheet_tasks(tasks, today):
            keep = current[-1]
    for remote in current:
        if remote != keep:
            print(f"→ archiving {remote} -> {archive} (not written on yet; still read every run)")
            rm.move(remote, archive)

    # 3. Clear the archive. A failed delete is left for the next run rather
    #    than failing this one: the cloud rate-limits bursts of calls.
    for name in archive_to_delete(rm.list_sheets(archive, SHEET_NAME_RE), read_last_applied(root)[1], today, _keep_days(args)):
        print(f"→ deleting {archive}/{name}")
        try:
            rm.remove(f"{archive}/{name}")
        except Exception as exc:
            print(f"  ✗ {exc} (will retry next run)", file=sys.stderr)

    if keep is not None:
        print(f"✓ {keep.rpartition('/')[2]} is up to date — nothing to publish")
        return 0

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
            sp.add_argument("--archive-folder", default=None,
                            help="where replaced and applied sheets go; unapplied ones there are still "
                                 "read every run (env REMARKABLE_ARCHIVE_FOLDER, default <folder>/Archive)")

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
    s.add_argument("--keep-days", type=int, default=None, help="delete archived sheets uploaded more than this many days ago (env REMARKABLE_KEEP_DAYS, default 7; 0 keeps all)")
    s.set_defaults(func=cmd_publish)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
