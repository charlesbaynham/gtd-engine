"""stamp — date every undated tickler item. FORMAT.md §7."""
from __future__ import annotations

from datetime import date

from ..dates import add_days, format_date
from ..model import TICKLER_OFFSETS, Vault, mark_dirty
from ..report import Report


def run(vault: Vault, today: date, report: Report) -> None:
    for relpath, tickler in vault.ticklers.items():
        offset = TICKLER_OFFSETS[relpath.split("/", 1)[1]]
        stamp = format_date(add_days(today, offset))
        for entry in tickler.order:
            if entry is not None and entry.stamp_raw is None:
                entry.stamp_raw = stamp
                mark_dirty(vault, relpath)
                report.add_change(f"Stamped '{entry.text}' in {relpath} with [{stamp}]")
