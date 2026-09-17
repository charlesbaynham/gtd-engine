"""lint — report-only findings. FORMAT.md §7, severities in the lint table."""
from __future__ import annotations

import re
from datetime import date
from itertools import combinations

from .. import lines as linesmod
from .. import projects as projectsmod
from ..dates import parse_date
from ..model import Vault
from ..report import Report

_ALLOWED_STATUS = {"", "to-schedule", "find-existing", "linked"}


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, start=1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
        prev = cur
    return prev[-1]


def _normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _check_date_cell(cell: str, file: str, report: Report) -> None:
    text = cell.strip()
    if not text:
        return
    parsed = parse_date(text)
    if parsed is None or not parsed.canonical:
        report.add("warn", "bad-date", file, f"'{text}' is not a canonical date")


def _check_project_link(vault: Vault, view: str, body_text: str, link_name: str | None, report: Report) -> None:
    """dangling-link / links-done-project / row-mismatch for one project link
    found in a table row or tickler line (FORMAT.md §6/§7)."""
    if not link_name:
        return
    resolved = projectsmod.resolve_link(link_name, vault.project_index)
    if resolved.resolved is None:
        report.add("warn", "dangling-link", view, f"[[{link_name}]] resolves to no file")
        return
    if resolved.resolved_in_done:
        report.add("warn", "links-done-project", view, f"[[{link_name}]] is under Done/")
        return
    page = vault.projects.get(str(resolved.resolved.relative_to(vault.root)))
    if page is not None:
        text = body_text.strip()
        if not any(not i.checked and i.text == text for i in page.items):
            report.add("warn", "row-mismatch", view, f"row for [[{link_name}]] matches no unchecked item")


def run(vault: Vault, today: date, report: Report) -> None:
    for file, reason in vault.errors:
        report.add("error", "parse-failure", file, reason)

    if vault.next_actions is not None:
        table = vault.next_actions
        for entry in table.entries:
            cells = entry.cells
            if all(c == "" for c in cells):
                continue
            _check_date_cell(table.cell(cells, "deadline"), "Next actions.md", report)
            prio = table.cell(cells, "priority").strip()
            if prio and not prio.lstrip("-").isdigit():
                report.add("warn", "bad-priority", "Next actions.md", f"'{prio}' is not an integer")
            name = projectsmod.parse_link(table.cell(cells, "project"))
            _check_project_link(vault, "Next actions.md", table.cell(cells, "action"), name, report)

    if vault.delegated is not None:
        table = vault.delegated
        for entry in table.entries:
            cells = entry.cells
            if all(c == "" for c in cells):
                continue
            _check_date_cell(table.cell(cells, "chase by"), "Delegated.md", report)
            prio = table.cell(cells, "priority").strip()
            if prio and not prio.lstrip("-").isdigit():
                report.add("warn", "bad-priority", "Delegated.md", f"'{prio}' is not an integer")
            if table.has("project"):
                name = projectsmod.parse_link(table.cell(cells, "project"))
                _check_project_link(vault, "Delegated.md", table.cell(cells, "thing"), name, report)

    if vault.scheduled is not None:
        table = vault.scheduled
        for entry in table.entries:
            cells = entry.cells
            if all(c == "" for c in cells):
                continue
            _check_date_cell(table.cell(cells, "date"), "Scheduled.md", report)
            status = table.cell(cells, "status").strip()
            if status != "" and status not in _ALLOWED_STATUS:
                report.add("warn", "bad-status", "Scheduled.md", f"'{status}' is not a recognised status")
            if table.has("project"):
                name = projectsmod.parse_link(table.cell(cells, "project"))
                _check_project_link(vault, "Scheduled.md", table.cell(cells, "thing"), name, report)

    for relpath, tickler in vault.ticklers.items():
        for entry in tickler.items:
            if entry.stamp is not None and not entry.stamp.canonical:
                report.add("warn", "bad-date", relpath, f"'[{entry.stamp_raw}]' is not a canonical date")
            body, name = linesmod.split_project_link(entry.text)
            _check_project_link(vault, relpath, body, name, report)

    for relpath, page in vault.projects.items():
        unchecked = [i for i in page.items if not i.checked]
        if not unchecked:
            report.add("warn", "project-stalled", relpath, "no unchecked item")
        elif not projectsmod.is_surfaced(vault, page):
            report.add(
                "warn",
                "project-stalled",
                relpath,
                "has unchecked item(s) but none is surfaced in any table or tickler",
            )

    base = vault.root / "Project details"
    if base.is_dir():
        all_pages = sorted(base.rglob("*.md"))
        seen: set[tuple[str, str]] = set()
        for a, b in combinations(all_pages, 2):
            na, nb = _normalize(a.stem), _normalize(b.stem)
            if not na or not nb:
                continue
            if _levenshtein(na, nb) <= 3:
                key = tuple(sorted((str(a.relative_to(vault.root)), str(b.relative_to(vault.root)))))
                if key not in seen:
                    seen.add(key)
                    report.add("info", "similar-names", "Project details", f"{key[0]} ~ {key[1]}")
