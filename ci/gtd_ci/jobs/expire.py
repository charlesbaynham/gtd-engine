"""expire — move past-due items into the Inbox. FORMAT.md §7."""
from __future__ import annotations

from datetime import date

from .. import lines as linesmod
from .. import projects as projectsmod
from ..dates import format_date, parse_date
from ..model import Vault, mark_dirty
from ..report import Report


def _tickler_source_name(relpath: str) -> str:
    return relpath.removesuffix(".md")


def run(vault: Vault, today: date, report: Report) -> None:
    if vault.inbox is None:
        return

    new_inbox_items: list[linesmod.Item] = []

    for relpath, tickler in vault.ticklers.items():
        source = _tickler_source_name(relpath)
        keep: list[linesmod.Item | None] = []
        for entry in tickler.order:
            if entry is not None and entry.stamp is not None and entry.stamp.value <= today:
                due = format_date(entry.stamp.value)
                text = f"[from {source}, due {due}] {entry.text}"
                new_inbox_items.append(linesmod.Item("", None, None, text, -1))
                report.add_change(f"Expired '{entry.text}' from {relpath} to Inbox.md")
                mark_dirty(vault, relpath)
                mark_dirty(vault, "Inbox.md")
            else:
                keep.append(entry)
        tickler.order = keep

    if vault.scheduled is not None:
        date_col = vault.scheduled.col("Date")
        status_col = vault.scheduled.col("Status")
        event_col = vault.scheduled.col("Event")
        thing_col = vault.scheduled.col("Thing")
        keep_entries = []
        for entry in vault.scheduled.entries:
            cells = entry.cells
            if all(c == "" for c in cells):
                keep_entries.append(entry)
                continue
            parsed = parse_date(cells[date_col])
            if parsed is not None and parsed.value <= today:
                detail = cells[date_col].strip()
                if cells[event_col].strip():
                    url = cells[event_col].strip()
                    if url.startswith("["):
                        close = url.find("](")
                        if close != -1:
                            url = url[close + 2 : -1] if url.endswith(")") else url
                    detail += f", cal: {url}"
                text = f"[from Scheduled, {detail}] {cells[thing_col].strip()}"
                if vault.scheduled.has("project"):
                    project_name = projectsmod.parse_link(vault.scheduled.cell(cells, "project"))
                    text = linesmod.format_with_project(text, project_name)
                new_inbox_items.append(linesmod.Item("", None, None, text, -1))
                report.add_change(f"Expired '{cells[thing_col].strip()}' from Scheduled.md to Inbox.md")
                mark_dirty(vault, "Scheduled.md")
                mark_dirty(vault, "Inbox.md")
                if cells[status_col].strip().lower() == "to-schedule" and parsed.value < today:
                    report.add("warn", "missed-schedule", "Scheduled.md", f"'{cells[thing_col].strip()}' was never scheduled")
            else:
                keep_entries.append(entry)
        vault.scheduled.entries = keep_entries

    if vault.delegated is not None:
        chase_col = vault.delegated.col("Chase by")
        thing_col = vault.delegated.col("Thing")
        person_col = vault.delegated.col("Person")
        keep_entries = []
        for entry in vault.delegated.entries:
            cells = entry.cells
            if all(c == "" for c in cells):
                keep_entries.append(entry)
                continue
            parsed = parse_date(cells[chase_col])
            if parsed is not None and parsed.value <= today:
                detail = f"{cells[person_col].strip()}, chase by {cells[chase_col].strip()}"
                text = f"[from Delegated, {detail}] {cells[thing_col].strip()}"
                if vault.delegated.has("project"):
                    project_name = projectsmod.parse_link(vault.delegated.cell(cells, "project"))
                    text = linesmod.format_with_project(text, project_name)
                new_inbox_items.append(linesmod.Item("", None, None, text, -1))
                report.add_change(f"Expired '{cells[thing_col].strip()}' from Delegated.md to Inbox.md")
                mark_dirty(vault, "Delegated.md")
                mark_dirty(vault, "Inbox.md")
            else:
                keep_entries.append(entry)
        vault.delegated.entries = keep_entries

    if new_inbox_items:
        vault.inbox.order = list(new_inbox_items) + vault.inbox.order
