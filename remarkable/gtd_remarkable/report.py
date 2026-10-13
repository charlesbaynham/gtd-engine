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

    ``pending`` marks a sheet that came back with no ink on it: it was neither
    scanned nor archived, because a tablet that has been offline for days looks
    exactly the same from the cloud as one you simply have not written on yet.
    ``archived`` marks one of those that a newer sheet has already replaced;
    it sits in the archive and is read again on every run.
    """

    name: str
    scanned: bool
    counts: dict = field(default_factory=dict)
    apply: ApplyReport | None = None
    error: str | None = None
    pending: bool = False
    archived: bool = False


def sheet_counts(decisions: dict, base: dict | None = None) -> dict:
    """The scanner's `summarize()` counts plus the two only this side names:
    rows whose NEW box was ticked, ticks on project-page items, and
    projects finished from their project row."""
    pages = decisions.get("pages") or [decisions]
    tasks = [(p, t) for p in pages for t in p.get("tasks", [])]
    counts = dict(base or {})
    counts["new_projects"] = sum(1 for _p, t in tasks if t.get("new_project"))
    counts["project_ticks"] = sum(
        1 for p, t in tasks
        if p.get("bucket") == "project" and t.get("action") == "done" and not t.get("id", "").endswith("-PJ")
    )
    counts["projects_finished"] = sum(1 for _p, t in tasks if t.get("id", "").endswith("-PJ") and t.get("action") == "done")
    return counts


def _section(title: str, lines: list[str]) -> list[str]:
    out = [f"## {title}", ""]
    out += [f"- {line}" for line in lines] or ["- none"]
    out.append("")
    return out


def render_status(results: list[SheetResult], run_label: str, today: str, archive_folder: str = "") -> str:
    lines = ["---", "gtd: remarkable-status", "---", "# reMarkable status", "",
             f"Run: {run_label} (today = {today})", ""]
    where = f"`{archive_folder}`" if archive_folder else "the archive"
    waiting = [r for r in results if r.pending and r.archived]
    shown = [r for r in results if not (r.pending and r.archived)]
    if not shown:
        lines += ["No new sheet was waiting on the device.", ""]
    for r in shown:
        lines += [f"# Sheet {r.name}", ""]
        if r.error:
            lines += _section("Error", [r.error])
            continue
        if r.pending:
            lines += [
                "Nothing written on it yet, as far as the cloud knows. When the vault "
                f"changes it is replaced and moves to {where}, which is still read on every "
                "run: if your tablet has been offline, your ink is safe on it and will be "
                "applied as soon as it syncs.",
                "",
            ]
            continue
        c = r.counts
        lines += [
            f"{c.get('pages', 0)} pages, {c.get('actions', 0)} actions, {c.get('edits', 0)} edits, "
            f"{c.get('captures', 0)} capture lines, {c.get('new_projects', 0)} new projects, "
            f"{c.get('project_ticks', 0)} project ticks, {c.get('projects_finished', 0)} projects finished, "
            f"{c.get('warnings', 0)} scan warnings",
            "",
        ]
        if r.apply is not None:
            lines += _section("Applied", r.apply.applied)
            lines += _section("Notes", r.apply.notes)
            lines += _section("Skipped", r.apply.skipped)
            lines += _section("Warnings", r.apply.warnings)
    if waiting:
        lines += [
            f"# Archived, not written on yet ({len(waiting)})",
            "",
            f"Still read on every run from {where}; ink that syncs later is applied then.",
            "",
        ] + [f"- {r.name}" for r in waiting] + [""]
    return "\n".join(lines).rstrip("\n") + "\n"


def commit_message(results: list[SheetResult]) -> str:
    applied = [a for r in results if r.apply for a in r.apply.applied]
    pending = [r for r in results if r.pending]
    names = ", ".join(r.name for r in results if not r.pending)
    if len(applied) == 1:
        summary = f"remarkable: {applied[0][:60]}"
    elif applied:
        summary = f"remarkable: {len(applied)} vault updates from {names}"
    elif pending and not names:
        summary = f"remarkable: {len(pending)} sheet(s) still unwritten on the device"
    else:
        summary = f"remarkable: processed {names or 'no sheets'}, no vault changes"
    body = [f"- {a}" for a in applied] or ["- none"]
    warnings = [w for r in results if r.apply for w in r.apply.warnings + r.apply.skipped]
    if warnings:
        body += ["", "Warnings:"] + [f"- {w}" for w in warnings]
    return summary + "\n\n" + "\n".join(body) + "\n"


def write_status(vault_root: Path, text: str) -> None:
    (vault_root / STATUS_FILE).write_text(text, encoding="utf-8", newline="\n")


REFERENCE_HEADING = "# Reference notes"


@dataclass
class ReferenceSyncResult:
    """What one `reference-sync` run did, as paths under ``Reference/``;
    ``apply`` carries the edits made from ink and every warning."""

    uploaded: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    archived: list[str] = field(default_factory=list)
    apply: ApplyReport = field(default_factory=ApplyReport)

    @property
    def touched(self) -> bool:
        return bool(self.uploaded or self.removed or self.archived or self.apply.applied or self.apply.warnings)


def render_reference_section(result: ReferenceSyncResult, run_label: str, today: str) -> str:
    lines = [REFERENCE_HEADING, "", f"Run: {run_label} (today = {today})", ""]
    lines += _section("Rendered and uploaded", result.uploaded)
    lines += _section("Removed from the device", result.removed)
    lines += _section("Moved to the device archive", result.archived)
    lines += _section("Applied", result.apply.applied)
    lines += _section("Warnings", result.apply.warnings)
    return "\n".join(lines).rstrip("\n") + "\n"


def write_reference_status(vault_root: Path, result: ReferenceSyncResult, run_label: str, today: str) -> None:
    """Put the reference section at the end of `reMarkable status.md`, replacing
    the previous one and leaving the sheet sections `process` wrote."""
    path = vault_root / STATUS_FILE
    existing = path.read_text(encoding="utf-8") if path.exists() else "---\ngtd: remarkable-status\n---\n# reMarkable status\n\n"
    head = existing.split("\n" + REFERENCE_HEADING, 1)[0].rstrip("\n")
    write_status(vault_root, head + "\n\n" + render_reference_section(result, run_label, today))


def reference_commit_message(result: ReferenceSyncResult) -> str:
    applied = result.apply.applied
    if applied:
        summary = f"remarkable: {len(applied)} reference note edit(s) from the device"
    else:
        summary = f"remarkable: reference sync, {len(result.uploaded)} note(s) uploaded, {len(result.removed) + len(result.archived)} removed"
    body = [f"- {a}" for a in applied] or ["- none"]
    if result.apply.warnings:
        body += ["", "Warnings:"] + [f"- {w}" for w in result.apply.warnings]
    return summary + "\n\n" + "\n".join(body) + "\n"
