"""Vault -> tasks JSON for `gtd-gen`.

Every item carries its `gtd_mcp` handle (content-hashed, see
`gtd_mcp.handles`) in a `handle` key. `remarkable-gtd` copies unknown keys
into the tasks document it embeds in the PDF, so a scanned decision comes
back with the handle it needs to act on the vault. A top-level `context`
key carries the project and person vocabulary the vision model is given
when interpreting a ✦ AI row (see `gtd_remarkable.apply.ai_intent`).

A top-level `projects` key carries one entry per active project page (name,
goal, whether it is starred, status lines, whether it is stalled, and every item with its handle and
the view it is surfaced in), from which `remarkable-gtd` renders the
read-only projects summary page and one page per project.
"""
from __future__ import annotations

import re
from datetime import date

from gtd_ci.model import Vault
from gtd_mcp import views

TICKLER_PERIODS = {
    "Next week": "week",
    "Next two weeks": "week",
    "Next month": "month",
    "Next quarter": "quarter",
}
PERIOD_BUCKETS = {"1w": "Next week", "1m": "Next month", "1q": "Next quarter"}
# A project item's surfaced view (`gtd_mcp.views` names the file) -> the short
# name the sheet prints as a badge.
SURFACED_VIEWS = {
    "Next actions.md": "next",
    "Delegated.md": "delegated",
    "Scheduled.md": "scheduled",
}


def _surfaced(view: str | None) -> str | None:
    if not view:
        return None
    return SURFACED_VIEWS.get(view, "tickler" if view.startswith("Tickler/") else None)

_EMBED = re.compile(r"!\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")
_LINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\\?\|([^\]]*))?\]\]")


def display_text(text: str) -> str:
    """Markdown cell -> what gets printed on the sheet.

    Embeds become `(filename)`, wiki-links their alias or target, `<br>`
    a space. Anything else is left as written.
    """
    text = _EMBED.sub(lambda m: f"({m.group(1).strip()})", text)
    text = _LINK.sub(lambda m: (m.group(2) or m.group(1)).strip(), text)
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.IGNORECASE)
    text = text.replace("\\|", "|")
    return re.sub(r"\s+", " ", text).strip()


def _pri(value: int | None) -> str:
    return "" if value is None else str(value)


def _date_raw(field: dict) -> str:
    return field.get("raw") or ""


def build_tasks(vault: Vault, today: date) -> dict:
    inbox = [
        {"act": display_text(it["text"]), "handle": it["handle"]}
        for it in views.inbox(vault)
        if display_text(it["text"])
    ]
    nxt = [
        {
            "act": display_text(a["action"]),
            "pri": _pri(a["priority"]),
            "due": _date_raw(a["deadline"]),
            "proj": a["project"] or "",
            "handle": a["handle"],
        }
        for a in views.next_actions(vault)
        if display_text(a["action"])
    ]
    deleg = [
        {
            "act": display_text(d["thing"]),
            "to": d["person"],
            "due": _date_raw(d["chase_by"]),
            "pri": _pri(d["priority"]),
            "proj": d.get("project") or "",
            "handle": d["handle"],
        }
        for d in views.delegated(vault)
        if display_text(d["thing"])
    ]
    tick: dict[str, list[dict]] = {"week": [], "month": [], "quarter": []}
    for t in views.tickler(vault):
        period = TICKLER_PERIODS.get(t["bucket"])
        if period is None or not display_text(t["text"]):
            continue
        tick[period].append(
            {
                "act": display_text(t["text"]),
                "due": _date_raw(t["due"]),
                "tickler": t["bucket"],
                "handle": t["handle"],
            }
        )
    projects = []
    for p in views.projects(vault):
        items = [
            {
                "text": display_text(it["text"]),
                "done": it["done"],
                "handle": it["handle"],
                "surfaced": _surfaced(it.get("surfaced")),
            }
            for it in p["items"]
            if display_text(it["text"])
        ]
        projects.append(
            {
                "name": p["stem"],
                "goal": p.get("goal") or "",
                "starred": bool(p.get("starred")),
                "status": list(p.get("status_lines") or []),
                "stalled": not any(not it["done"] and it["surfaced"] for it in items),
                "items": items,
            }
        )
    context = {
        "projects": [pr["name"] for pr in projects],
        "people": sorted({d["person"] for d in views.delegated(vault) if d["person"] and d["person"] != "?"}),
    }
    return {
        "date": today.isoformat(),
        "inbox": inbox,
        "next": nxt,
        "delegated": deleg,
        "tickler": tick,
        "projects": projects,
        "context": context,
    }
