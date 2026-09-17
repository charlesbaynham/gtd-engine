"""sort — reorder Next actions.md and Delegated.md. FORMAT.md §7."""
from __future__ import annotations

from datetime import date, timedelta

from ..dates import parse_date
from ..model import Vault, mark_dirty
from ..report import Report

_FAR_FUTURE = date.max - timedelta(days=1)


def _priority_key(cell: str) -> float:
    text = cell.strip()
    if text.lstrip("-").isdigit():
        return -int(text)
    return float("inf")


def _date_key(cell: str):
    parsed = parse_date(cell)
    return parsed.value if parsed is not None else _FAR_FUTURE


def _sort_table(table, priority_col: str | None, date_col: str) -> list:
    date_i = table.col(date_col)
    if priority_col is not None:
        prio_i = table.col(priority_col)
        key = lambda e: (_priority_key(e.cells[prio_i]), _date_key(e.cells[date_i]))
    else:
        key = lambda e: _date_key(e.cells[date_i])
    return sorted(table.entries, key=key)


def run(vault: Vault, today: date, report: Report) -> None:
    if vault.next_actions is not None:
        new_order = _sort_table(vault.next_actions, "Priority", "Deadline")
        if new_order != vault.next_actions.entries:
            vault.next_actions.entries = new_order
            mark_dirty(vault, "Next actions.md")
            report.add_change("Sorted Next actions.md by priority, then deadline")

    if vault.delegated is not None:
        new_order = _sort_table(vault.delegated, None, "Chase by")
        if new_order != vault.delegated.entries:
            vault.delegated.entries = new_order
            mark_dirty(vault, "Delegated.md")
            report.add_change("Sorted Delegated.md by chase-by date")
