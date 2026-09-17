from __future__ import annotations

from pathlib import Path

import pytest

from gtd_ci.model import load_vault

FM = {"inbox": "inbox", "next-actions": "next-actions", "delegated": "delegated", "scheduled": "scheduled"}


DELEGATED_COLUMNS = ("Thing", "Person", "Chase by", "Priority")


def write_vault(root: Path, *, inbox="", next_rows="", delegated_rows="", ticklers=None, projects=None,
                delegated_columns=DELEGATED_COLUMNS) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "Inbox.md").write_text(f"---\ngtd: inbox\n---\n{inbox}", encoding="utf-8")
    (root / "Next actions.md").write_text(
        "---\ngtd: next-actions\n---\n\n| Action | Project | Deadline | Priority |\n| --- | --- | --- | --- |\n" + next_rows,
        encoding="utf-8",
    )
    header = "| " + " | ".join(delegated_columns) + " |"
    separator = "| " + " | ".join(["---"] * len(delegated_columns)) + " |"
    (root / "Delegated.md").write_text(
        f"---\ngtd: delegated\n---\n\n{header}\n{separator}\n" + delegated_rows,
        encoding="utf-8",
    )
    (root / "Scheduled.md").write_text(
        "---\ngtd: scheduled\n---\n\n| Thing | Date | Status | Event |\n| --- | --- | --- | --- |\n", encoding="utf-8"
    )
    (root / "Tickler").mkdir(exist_ok=True)
    for name in ["Next week.md", "Next two weeks.md", "Next month.md", "Next quarter.md"]:
        body = (ticklers or {}).get(name, "")
        (root / "Tickler" / name).write_text(f"---\ngtd: tickler\n---\n{body}", encoding="utf-8")
    (root / "Project details").mkdir(exist_ok=True)
    (root / "Project details" / "-Project template.md").write_text(
        "# Goal\n\n\n## Context\n\n## Status\n\n## Next Actions\n\n- [ ] \n", encoding="utf-8"
    )
    for name, body in (projects or {}).items():
        (root / "Project details" / f"{name}.md").write_text(body, encoding="utf-8")


@pytest.fixture
def vault_root(tmp_path) -> Path:
    root = tmp_path / "vault"
    write_vault(
        root,
        inbox="Claim for delayed train\n[from Tickler/Next week, due 2026-09-10] Chase the EOM quote\n",
        next_rows=(
            "| Read Ben's paper | [[Wedding 2026]] |  | 3 |\n"
            "| Deal with ![[Re_ Newsletter.msg]] comments |  | 2026-09-20 |  |\n"
            "| Buy an ashtray |  |  |  |\n"
        ),
        delegated_rows="| Send the invoice | Dave | 2026-09-22 |  |\n",
        ticklers={
            "Next week.md": "[2026-09-22] Consider dental coatings\n",
            "Next month.md": "* [2026-10-10] Sample item\n",
        },
        projects={
            "Wedding 2026": (
                "# Goal\n\nGet married\n\n## Status\n\n- \u2705 Venue booked\n\n"
                "## Next Actions\n\n- [ ] Read Ben's paper\n- [ ] Pay Sandra\n"
            ),
            "Boiler service": (
                "# Goal\n\nStop the boiler dying\n\n## Next Actions\n\n"
                "- [x] Find the manual\n- [ ] Send the invoice\n- [ ] Bleed the radiators\n"
            ),
        },
    )
    return root


@pytest.fixture
def vault(vault_root):
    return load_vault(vault_root)
