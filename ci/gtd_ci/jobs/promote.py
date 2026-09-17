"""promote — give unrepresented projects a row in Next actions.md. FORMAT.md §7."""
from __future__ import annotations

from datetime import date

from .. import projects as projectsmod
from ..model import Vault, mark_dirty
from ..tables import RowEntry, new_row
from ..report import Report


def run(vault: Vault, today: date, report: Report) -> None:
    if vault.next_actions is None:
        return
    for relpath, page in vault.projects.items():
        unchecked = [i for i in page.items if not i.checked]
        if not unchecked:
            continue
        matched, mismatched = projectsmod.surfaced_items(vault, page.path)
        if matched or mismatched:
            # Already surfaced somewhere, or a row/line links this project but
            # matches no unchecked item (row-mismatch suppresses promotion,
            # same as before).
            continue
        row = new_row(vault.next_actions, {"Action": unchecked[0].text, "Project": f"[[{page.stem}]]"})
        vault.next_actions.entries.append(RowEntry(cells=row, source_line=None))
        mark_dirty(vault, "Next actions.md")
        report.add_change(f"Promoted '{unchecked[0].text}' from {relpath} to Next actions.md")
