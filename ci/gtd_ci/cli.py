"""Command-line entry points. FORMAT.md-driven jobs; see repo CLAUDE.md for the pipeline."""
from __future__ import annotations

import argparse
import difflib
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_LONDON = ZoneInfo("Europe/London")

from . import model
from .jobs import expire, promote, sort, stamp
from .jobs import lint as lint_job
from .report import Report, findings_body, render_status

JOB_ORDER = ["expire", "stamp", "promote", "sort", "lint"]
JOB_FUNCS = {
    "expire": expire.run,
    "stamp": stamp.run,
    "promote": promote.run,
    "sort": sort.run,
    "lint": lint_job.run,
}


def run_jobs(vault: model.Vault, today: date, job_names: list[str]) -> Report:
    report = Report()
    names = JOB_ORDER if job_names == ["run"] else job_names
    for name in names:
        JOB_FUNCS[name](vault, today, report)
    return report


def _managed_relpaths(vault: model.Vault) -> dict[str, str]:
    """relpath -> rendered content, for every file the model can write."""
    out: dict[str, str] = {}
    if vault.inbox is not None:
        out["Inbox.md"] = _render_lines(vault.inbox.render_lines(), vault.inbox.trailing_newline)
    for relpath, lf in vault.ticklers.items():
        out[relpath] = _render_lines(lf.render_lines(), lf.trailing_newline)
    for relpath, table in [
        ("Next actions.md", vault.next_actions),
        ("Delegated.md", vault.delegated),
        ("Scheduled.md", vault.scheduled),
    ]:
        if table is not None:
            out[relpath] = _render_lines(table.render_lines(), table.trailing_newline)
    for relpath, page in vault.projects.items():
        out[relpath] = _render_lines(page.lines, page.trailing_newline)
    return out


def _render_lines(lines: list[str], trailing_newline: bool) -> str:
    text = "\n".join(lines)
    return text + "\n" if trailing_newline else text


def _previous_status_body(root: Path) -> str | None:
    path = root / "CI status.md"
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    if "## Changes" not in text:
        return None
    return text.split("## Changes", 1)[1]


def _run_label(today_given: bool, today: date) -> str | None:
    """None => render_status's reproducible '<today> 00:00' default.

    Real local time only when --today was not given, so fixtures (which
    always pass --today) stay byte-reproducible.
    """
    if today_given:
        return None
    return datetime.now(_LONDON).strftime("%Y-%m-%d %H:%M")


def _write_status_if_changed(vault: model.Vault, report: Report, today: date, today_given: bool) -> bool:
    """Writes CI status.md unless only its timestamp would change. Returns True if written."""
    new_body = findings_body(report)
    old_body = _previous_status_body(vault.root)
    if vault.dirty or old_body != new_body:
        rendered = render_status(report, today.isoformat(), run_label=_run_label(today_given, today))
        (vault.root / "CI status.md").write_text(rendered, encoding="utf-8", newline="\n")
        return True
    return False


def _dry_run(vault: model.Vault, report: Report, contents: dict[str, str]) -> None:
    print("Plan:")
    for change in report.changes:
        print(f"  - {change}")
    if not report.changes:
        print("  (no changes)")

    print("\nFindings:")
    for f in report.findings:
        print(f"  [{f.severity}] {f.code} {f.file}: {f.message}")
    if not report.findings:
        print("  (none)")

    for relpath in sorted(vault.dirty):
        path = vault.root / relpath
        old = path.read_text(encoding="utf-8") if path.is_file() else ""
        new = contents.get(relpath, "")
        if old == new:
            continue
        diff = difflib.unified_diff(
            old.splitlines(keepends=True), new.splitlines(keepends=True), fromfile=relpath, tofile=relpath
        )
        print(f"\n--- diff: {relpath} ---")
        sys.stdout.writelines(diff)


def cmd_run(args: argparse.Namespace) -> None:
    root = Path(args.vault)
    today_given = bool(args.today)
    today = date.fromisoformat(args.today) if today_given else date.today()
    vault = model.load_vault(root)
    report = run_jobs(vault, today, ["run"])
    contents = _managed_relpaths(vault)

    if args.dry_run:
        _dry_run(vault, report, contents)
        return

    model.save_vault(vault)
    _write_status_if_changed(vault, report, today, today_given)

    if args.commit_message_file:
        summary = f"ci: {report.changes[0]}" if report.changes else "ci: routine run, no vault changes"
        if len(report.changes) > 1:
            summary = f"ci: {len(report.changes)} vault updates"
        body = "\n".join(f"- {c}" for c in report.changes) or "- none"
        Path(args.commit_message_file).write_text(f"{summary}\n\n{body}\n", encoding="utf-8")


def cmd_job(job_name: str, args: argparse.Namespace) -> None:
    root = Path(args.vault)
    today_given = bool(args.today)
    today = date.fromisoformat(args.today) if today_given else date.today()
    vault = model.load_vault(root)
    report = run_jobs(vault, today, [job_name])
    contents = _managed_relpaths(vault)

    if args.dry_run:
        _dry_run(vault, report, contents)
        return

    model.save_vault(vault)
    if job_name == "lint":
        _write_status_if_changed(vault, report, today, today_given)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gtd_ci")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--vault", default=".")
        p.add_argument("--today")
        p.add_argument("--dry-run", action="store_true")

    run_parser = sub.add_parser("run")
    add_common(run_parser)
    run_parser.add_argument("--commit-message-file")
    run_parser.set_defaults(func=cmd_run)

    for name in JOB_ORDER:
        p = sub.add_parser(name)
        add_common(p)
        p.set_defaults(func=lambda args, name=name: cmd_job(name, args))

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)
    return 0
