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
- ``reference-sync``  mirror ``Reference/`` onto the device, one device-sized
               PDF per note, and apply what was written on a note before its
               copy is replaced (see below).

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

**Reference notes** live in their own folder (``REMARKABLE_REFERENCE_FOLDER``,
default ``GTD Reference``), mirroring the subfolders of ``Reference/``. A note
is rendered and uploaded when its sha256 differs from the one recorded in
``.remarkable-reference.json`` (committed with the run). A copy being replaced
is never deleted: it is moved to ``<folder>/Archive/<run stamp>/``. If it holds
strokes they are scanned and applied to the note first, so the replacement is
rendered from the updated text. ``--read-ink`` also reads the unchanged notes'
copies and any archived copy not yet applied (a download each, so CI runs it
daily); an archived copy is deleted once the *committed* state says it was
applied, or after ``--keep-days``. A note deleted from the vault has its copy
removed, or archived with a warning if it holds ink.

Environment: ``REMARKABLE_FOLDER`` (default ``GTD Daily``),
``REMARKABLE_ARCHIVE_FOLDER`` (default ``<folder>/Archive``),
``REMARKABLE_KEEP_DAYS`` (default 7; 0 keeps everything),
``RMAPI_DEVICE_TOKEN`` (rmapi auth for headless runs), ``OPENROUTER_API_KEY``
/ ``OPENROUTER_MODEL`` (handwriting) / ``OPENROUTER_AI_MODEL`` (the ✦ AI
agent alone; falls back to ``OPENROUTER_MODEL``), ``RMAPI_BIN``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from gtd_ci import reference as referencemod
from gtd_ci.model import load_vault, save_vault

from .apply import ApplyReport, apply_decisions, apply_reference_decisions
from .report import (
    ReferenceSyncResult,
    SheetResult,
    commit_message,
    reference_commit_message,
    render_status,
    sheet_counts,
    write_reference_status,
    write_status,
)
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
REFERENCE_BUCKETS = ("reference", "refnotes")


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
    if any(p.get("bucket") in REFERENCE_BUCKETS for p in decisions.get("pages") or [decisions]):
        report = apply_reference_decisions(vault, _today(args), decisions)
    else:
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


# --- reference notes ----------------------------------------------------------------

REFERENCE_STATE_FILE = ".remarkable-reference.json"
_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"


def _reference_folder(args) -> str:
    return args.reference_folder or os.environ.get("REMARKABLE_REFERENCE_FOLDER", "GTD Reference")


def _reference_archive(args) -> str:
    return (args.reference_archive_folder or os.environ.get("REMARKABLE_REFERENCE_ARCHIVE_FOLDER")
            or f"{_reference_folder(args)}/Archive")


def read_reference_state(root: Path) -> dict[str, dict]:
    """The record of what is on the device::

        {"notes":   {<Reference/path.md>: {"sha", "uploaded", "ink"?, "error"?}},
         "archive": {<device path>: {"note", "archived", "applied", "read"?, "orphan"?}}}

    ``notes`` are the copies in the reference folder; ``archive`` the replaced
    ones whose ink may still need reading. The first version wrote the notes
    mapping bare, which is migrated here.
    """
    try:
        raw = json.loads((root / REFERENCE_STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    if "notes" not in raw and "archive" not in raw:
        raw = {"notes": raw}
    return {k: {a: b for a, b in (raw.get(k) or {}).items() if isinstance(b, dict)} for k in ("notes", "archive")}


def write_reference_state(root: Path, state: dict[str, dict]) -> None:
    body = {k: dict(sorted(state[k].items())) for k in ("notes", "archive")}
    (root / REFERENCE_STATE_FILE).write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")


def reference_remote(folder: str, relpath: str) -> str:
    """Where a note lives on the device: its ``Reference/`` subfolders, mirrored."""
    parts = Path(relpath).relative_to(referencemod.REFERENCE_DIR).with_suffix("").parts
    return "/".join((folder, *parts))


def ink_fingerprint(rmdoc: Path) -> str:
    """Identifies the strokes on a downloaded copy, so ink already read (and
    left in place because nothing could be applied) is not read again."""
    from remarkable_gtd.rm.annotations import extract_from_rmdoc

    _pdf, rm_by_page = extract_from_rmdoc(rmdoc)
    digest = hashlib.sha256()
    for key in sorted(rm_by_page, key=str):
        digest.update(rm_by_page[key] or b"")
    return digest.hexdigest()


def _fully_applied(report: ApplyReport | None) -> bool:
    """Only a copy whose every operation landed may be deleted from the archive."""
    return bool(report and report.applied and not report.warnings)


class _ReferenceSync:
    """One `reference-sync` run. ``state`` is the committed record, updated as
    work lands and written even when a step raises; ``applied_before`` is what
    the *committed* copy of it already called applied, which alone decides what
    may be deleted from the archive, so a run whose push failed deletes nothing
    it has not yet got on record."""

    def __init__(self, args):
        from remarkable_gtd.gen import reference as refgen
        from remarkable_gtd.rm import api as rm
        from remarkable_gtd.scan.sheet import scan_rmdoc

        self.args, self.refgen, self.rm, self.scan_rmdoc = args, refgen, rm, scan_rmdoc
        self.root = Path(args.vault)
        self.work = Path(args.work_dir)
        self.work.mkdir(parents=True, exist_ok=True)
        self.today = _today(args)
        self.folder = _reference_folder(args)
        self.archive = _reference_archive(args)
        self.stamp = _stamp()
        self.state = read_reference_state(self.root)
        self.applied_before = {r for r, e in self.state["archive"].items() if e.get("applied")}
        self.vault = load_vault(self.root)
        self.result = ReferenceSyncResult()
        self.failed = 0
        self.made: set[str] = set()

    @property
    def notes(self) -> dict[str, dict]:
        return self.state["notes"]

    def warn(self, message: str) -> None:
        self.result.apply.warnings.append(message)

    def error(self, message: str) -> None:
        self.failed += 1
        print(f"  ✗ {message}", file=sys.stderr)
        self.warn(message)

    def sha(self, note) -> str:
        return self.refgen.source_sha256(note.path)

    def has_copy(self, remote: str, strict: bool = False) -> bool:
        """Is there a document at ``remote``? ``strict`` tells "the folder is
        not there" from "the cloud did not answer", which raises."""
        from remarkable_gtd.rm.api import RmapiError

        try:
            entries = self.rm.list_dir(_parent(remote))
        except RmapiError:
            if strict:
                self.rm.list_dir("/")
            return False
        return any(e.name == remote.rpartition("/")[2] for e in entries)

    def read_copy(self, remote: str, strict: bool = False) -> tuple[Path | None, int, int]:
        """``(rmdoc, strokes, unreadable layers)``; the ``rmdoc`` is ``None`` without a copy."""
        if not self.has_copy(remote, strict):
            return None, 0, 0
        rmdoc = self.rm.download(remote, self.work / "device")
        return (rmdoc, *ink(rmdoc))

    def ensure_folder(self, folder: str) -> None:
        parts = folder.split("/") if folder else []
        for i in range(1, len(parts) + 1):
            path = "/".join(parts[:i])
            if path not in self.made:
                self.rm.mkdir(path)
                self.made.add(path)

    def archive_copy(self, remote: str, relpath: str, **flags) -> str:
        """Move a copy into this run's stamped archive folder; nothing on a
        device copy is ever deleted while it is being replaced."""
        dest = f"{self.archive}/{self.stamp}"
        self.ensure_folder(self.archive)
        self.rm.move(remote, dest)
        archived = f"{dest}/{remote.rpartition('/')[2]}"
        self.state["archive"][archived] = {
            "note": relpath, "archived": _iso(datetime.now(timezone.utc)), "applied": False, **flags}
        self.result.archived.append(f"{relpath} → {archived}")
        return archived

    def apply_ink(self, rmdoc: Path, stem: str, where: str) -> ApplyReport:
        decisions, *_ = self.scan_rmdoc(rmdoc, self.work / "reference" / stem, _scan_cfg(self.args.ocr),
                                        today=self.today.isoformat())
        report = apply_reference_decisions(self.vault, self.today, decisions)
        save_vault(self.vault)  # the next render reads the file
        self.result.apply.extend(report)
        if report.warnings:
            self.warn(f"{stem!r}: some ink was not applied; the copy it is on is at '{where}'")
        return report

    def pair_moves(self) -> None:
        """A note moved between folders keeps its stem and content, so its
        device copy moves with it rather than being rendered afresh."""
        fresh = {r: n for r, n in self.vault.reference_notes().items() if r not in self.notes}
        for old, entry in sorted(self.notes.items()):
            if (self.root / old).exists():
                continue
            new = next((r for r, n in fresh.items()
                        if n.stem == Path(old).stem and self.sha(n) == entry.get("sha")), None)
            if new is None:
                continue
            try:
                if not self.has_copy(reference_remote(self.folder, old), strict=True):
                    continue  # nothing to carry: the moved note is uploaded as new
                dest = _parent(reference_remote(self.folder, new))
                self.ensure_folder(dest)
                self.rm.move(reference_remote(self.folder, old), dest)
            except Exception as exc:
                self.error(f"{old}: {exc}")
                continue
            self.notes[new] = self.notes.pop(old)
            del fresh[new]

    def sync_note(self, relpath: str, note) -> None:
        entry = self.notes.get(relpath, {})
        remote = reference_remote(self.folder, relpath)
        if entry.get("sha") == self.sha(note) and not self.args.read_ink:
            return
        print(f"→ {relpath}")
        rmdoc, strokes, unreadable = self.read_copy(remote, strict=True)
        if rmdoc is not None and unreadable and not strokes:
            self.error(f"{relpath}: {unreadable} stroke layer(s) on the device copy but none could be read; left as it is")
            return
        fingerprint, report = None, None
        if strokes:
            fingerprint = ink_fingerprint(rmdoc)
            if fingerprint != entry.get("ink"):
                report = self.apply_ink(rmdoc, note.stem, remote)
                self.notes[relpath] = {**entry, "ink": fingerprint}  # only once read: a failed scan is retried
        sha = self.sha(note)
        if entry.get("sha") == sha and (rmdoc is not None or entry.get("error")):
            if fingerprint:
                self.notes[relpath] = {**entry, "ink": fingerprint}
            return  # nothing to print; ink that changed nothing stays where it is
        if rmdoc is not None:
            self.archive_copy(remote, relpath, applied=_fully_applied(report), **({"ink": fingerprint} if fingerprint else {}))
        self.notes.pop(relpath, None)
        pdf = self.work / f"{note.stem}.pdf"
        try:
            self.refgen.render_reference(note.path, self.root, pdf, source_sha=sha, the_date=self.today, stem=note.stem)
        except (ValueError, RuntimeError) as exc:
            self.warn(f"{relpath}: not rendered — {exc}")
            self.notes[relpath] = {"sha": sha, "error": str(exc)}  # not retried until the note changes
            return
        self.ensure_folder(_parent(_parent(remote)))
        self.rm.upload(pdf, _parent(remote))
        self.notes[relpath] = {"sha": sha, "uploaded": _iso(datetime.now(timezone.utc))}
        self.result.uploaded.append(relpath)

    def drop_gone(self) -> None:
        for relpath in sorted(r for r in self.notes if not (self.root / r).exists()):
            remote = reference_remote(self.folder, relpath)
            print(f"→ {relpath} is gone from the vault")
            try:
                rmdoc, strokes, unreadable = self.read_copy(remote, strict=True)
                if rmdoc is not None and (strokes or unreadable):
                    archived = self.archive_copy(remote, relpath, orphan=True)
                    self.warn(f"{relpath}: deleted from the vault but written on; its device copy is at '{archived}' unread")
                elif rmdoc is not None:
                    self.rm.remove(remote)
                    self.result.removed.append(relpath)
            except Exception as exc:
                self.error(f"{relpath}: {exc}")
                continue
            del self.notes[relpath]

    def adopt_archive(self) -> None:
        """Archived copies the state does not know of (a run whose push failed
        moved them) are read like any other."""
        from remarkable_gtd.rm.api import RmapiError

        try:
            stamps = [e for e in self.rm.list_dir(self.archive) if e.is_dir]
        except RmapiError:
            return
        for stamp in stamps:
            try:
                when = datetime.strptime(stamp.name, _STAMP_FORMAT).replace(tzinfo=timezone.utc)
                docs = [d for d in self.rm.list_dir(f"{self.archive}/{stamp.name}") if not d.is_dir]
            except (ValueError, RmapiError):
                continue
            for doc in docs:
                self.state["archive"].setdefault(
                    f"{self.archive}/{stamp.name}/{doc.name}", {"note": "", "archived": _iso(when), "applied": False})

    def read_archive(self) -> None:
        self.adopt_archive()
        for remote, entry in sorted(self.state["archive"].items()):
            if entry.get("applied") or entry.get("orphan"):
                continue
            print(f"→ {remote} (archived)")
            try:
                rmdoc, strokes, unreadable = self.read_copy(remote, strict=True)
                if rmdoc is None:
                    del self.state["archive"][remote]
                elif strokes and (fingerprint := ink_fingerprint(rmdoc)) != entry.get("ink"):
                    report = self.apply_ink(rmdoc, remote.rpartition("/")[2], remote)
                    entry.update(applied=_fully_applied(report), ink=fingerprint)  # re-read if re-inked
                elif unreadable:
                    self.warn(f"{remote}: {unreadable} stroke layer(s) but none could be read; kept")
            except Exception as exc:
                self.error(f"{remote}: {exc}")

    def clean_archive(self) -> None:
        keep_days = _keep_days(self.args)
        cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days) if keep_days > 0 else None
        emptied: set[str] = set()
        for remote, entry in sorted(self.state["archive"].items()):
            archived = datetime.strptime(entry["archived"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            if remote not in self.applied_before and not (cutoff and archived < cutoff):
                continue
            try:
                if self.has_copy(remote, strict=True):
                    self.rm.remove(remote)
            except Exception as exc:
                self.warn(f"{remote}: could not be deleted ({exc}); will retry")
                continue
            del self.state["archive"][remote]
            emptied.add(remote.rpartition("/")[0])
        for stamp_folder in sorted(emptied):
            if not any(r.startswith(stamp_folder + "/") for r in self.state["archive"]):
                try:
                    self.rm.remove(stamp_folder)
                except Exception:
                    pass  # not empty (a copy the state does not know of) or already gone

    def run(self) -> int:
        try:
            if self.args.read_ink:
                self.read_archive()
            self.pair_moves()
            for relpath, note in sorted(self.vault.reference_notes().items()):
                try:
                    self.sync_note(relpath, note)
                except Exception as exc:  # one broken note must not stop the others
                    self.error(f"{relpath}: {exc}")
            self.drop_gone()
            self.clean_archive()
        finally:
            save_vault(self.vault)
            write_reference_state(self.root, self.state)
        return 1 if self.failed else 0


def cmd_reference_sync(args) -> int:
    from remarkable_gtd.rm import api as rm

    rm.write_config_from_env()
    sync = _ReferenceSync(args)
    code = sync.run()
    result = sync.result
    if result.touched:
        write_reference_status(sync.root, result, _run_label(args), sync.today.isoformat())
    if args.commit_message_file:
        Path(args.commit_message_file).write_text(reference_commit_message(result), encoding="utf-8")
    print(f"reference notes: {len(result.uploaded)} uploaded, {len(result.removed)} removed, "
          f"{len(result.archived)} archived, {len(result.apply.applied)} edit(s) applied")
    return code


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
    s = sub.add_parser("reference-sync"); common(s, work=True)
    s.add_argument("--reference-folder", default=None, help="device folder (env REMARKABLE_REFERENCE_FOLDER, default 'GTD Reference')")
    s.add_argument("--ocr", default="openrouter", choices=["openrouter", "tesseract", "null"])
    s.add_argument("--reference-archive-folder", default=None,
                   help="where replaced copies go until their ink is read (env REMARKABLE_REFERENCE_ARCHIVE_FOLDER, default <folder>/Archive)")
    s.add_argument("--keep-days", type=int, default=None, help="delete archived copies older than this (env REMARKABLE_KEEP_DAYS, default 7; 0 keeps all)")
    s.add_argument("--read-ink", action="store_true",
                   help="also download every unchanged note's device copy and apply what is written on it")
    s.add_argument("--commit-message-file"); s.set_defaults(func=cmd_reference_sync)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
