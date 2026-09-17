"""Run report: the `reMarkable status.md` page and the commit message."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .apply import ApplyReport

STATUS_FILE = "reMarkable status.md"


@dataclass
class SheetResult:
    """One sheet's outcome: the scanner's counts and what reached the vault.

    ``counts`` is the scanner's summary (pages, actions, edits, capture lines,
    new projects, project ticks, scan warnings); ``apply`` carries the applied
    lines, the notes (a fuzzy project name that was matched, say), the skipped
    rows and the warnings.
    """

    name: str
    scanned: bool
    counts: dict = field(default_factory=dict)
    apply: ApplyReport | None = None
    error: str | None = None


def sheet_counts(decisions: dict, base: dict | None = None) -> dict:
    """The scanner's `summarize()` counts plus the two only this side names:
    rows whose NEW box was ticked, and ticks on project-page items."""
    pages = decisions.get("pages") or [decisions]
    tasks = [(p, t) for p in pages for t in p.get("tasks", [])]
    counts = dict(base or {})
    counts["new_projects"] = sum(1 for _p, t in tasks if t.get("new_project"))
    counts["project_ticks"] = sum(
        1 for p, t in tasks if p.get("bucket") == "project" and t.get("action") == "done"
    )
    return counts


def _section(title: str, lines: list[str]) -> list[str]:
    out = [f"## {title}", ""]
    out += [f"- {line}" for line in lines] or ["- none"]
    out.append("")
    return out


def render_status(results: list[SheetResult], run_label: str, today: str) -> str:
    lines = ["---", "gtd: remarkable-status", "---", "# reMarkable status", "",
             f"Run: {run_label} (today = {today})", ""]
    if not results:
        lines += ["No sheet was waiting on the device.", ""]
    for r in results:
        lines += [f"# Sheet {r.name}", ""]
        if r.error:
            lines += _section("Error", [r.error])
            continue
        c = r.counts
        lines += [
            f"{c.get('pages', 0)} pages, {c.get('actions', 0)} actions, {c.get('edits', 0)} edits, "
            f"{c.get('captures', 0)} capture lines, {c.get('new_projects', 0)} new projects, "
            f"{c.get('project_ticks', 0)} project ticks, {c.get('warnings', 0)} scan warnings",
            "",
        ]
        if r.apply is not None:
            lines += _section("Applied", r.apply.applied)
            lines += _section("Notes", r.apply.notes)
            lines += _section("Skipped", r.apply.skipped)
            lines += _section("Warnings", r.apply.warnings)
    return "\n".join(lines).rstrip("\n") + "\n"


def commit_message(results: list[SheetResult]) -> str:
    applied = [a for r in results if r.apply for a in r.apply.applied]
    names = ", ".join(r.name for r in results)
    if len(applied) == 1:
        summary = f"remarkable: {applied[0][:60]}"
    elif applied:
        summary = f"remarkable: {len(applied)} vault updates from {names}"
    else:
        summary = f"remarkable: processed {names or 'no sheets'}, no vault changes"
    body = [f"- {a}" for a in applied] or ["- none"]
    warnings = [w for r in results if r.apply for w in r.apply.warnings + r.apply.skipped]
    if warnings:
        body += ["", "Warnings:"] + [f"- {w}" for w in warnings]
    return summary + "\n\n" + "\n".join(body) + "\n"


def write_status(vault_root: Path, text: str) -> None:
    (vault_root / STATUS_FILE).write_text(text, encoding="utf-8", newline="\n")
