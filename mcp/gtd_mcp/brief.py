"""get_brief: the one-call morning-brief aggregation over the typed views."""
from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

from gtd_ci.model import Vault

from . import views


def _priority_deadline_key(item: dict[str, Any]) -> tuple[float, date]:
    prio = item["priority"]
    deadline = item["deadline"]["value"]
    return (-prio if prio is not None else math.inf, date.fromisoformat(deadline) if deadline else date.max)


def _bucket_by_date(items: list[dict[str, Any]], date_key: str, today: date) -> dict[str, list[dict[str, Any]]]:
    overdue, due_today, due_this_week = [], [], []
    week_end = today + timedelta(days=6)
    for item in items:
        raw = item[date_key]["value"]
        if not raw:
            continue
        d = date.fromisoformat(raw)
        if d < today:
            overdue.append(item)
        elif d == today:
            due_today.append(item)
        elif d <= week_end:
            due_this_week.append(item)
    return {"overdue": overdue, "due_today": due_today, "due_this_week": due_this_week}


def get_brief(vault: Vault, today: date) -> dict[str, Any]:
    from gtd_ci.jobs import lint as lint_job
    from gtd_ci.report import Report

    actions = views.next_actions(vault)
    top_actions = sorted(actions, key=_priority_deadline_key)[:10]
    action_buckets = _bucket_by_date(actions, "deadline", today)

    delegated = views.delegated(vault)
    delegated_buckets = _bucket_by_date(delegated, "chase_by", today)

    scheduled = views.scheduled(vault)
    scheduled_buckets = _bucket_by_date(scheduled, "date", today)

    tickler_items = views.tickler(vault)
    week_end = today + timedelta(days=6)
    landing_this_week = [
        t for t in tickler_items if t["due"]["value"] and today <= date.fromisoformat(t["due"]["value"]) <= week_end
    ]

    inbox_items = views.inbox(vault)

    report = Report()
    lint_job.run(vault, today, report)
    findings = [{"severity": f.severity, "code": f.code, "file": f.file, "message": f.message} for f in report.findings]
    stalled = [f for f in findings if f["code"] == "project-stalled"]

    return {
        "today": today.isoformat(),
        "top_actions": top_actions,
        "overdue": {
            "actions": action_buckets["overdue"],
            "delegated": delegated_buckets["overdue"],
            "scheduled": scheduled_buckets["overdue"],
        },
        "due_today": {
            "actions": action_buckets["due_today"],
            "delegated": delegated_buckets["due_today"],
            "scheduled": scheduled_buckets["due_today"],
        },
        "due_this_week": {
            "actions": action_buckets["due_this_week"],
            "delegated": delegated_buckets["due_this_week"],
            "scheduled": scheduled_buckets["due_this_week"],
        },
        "inbox": {
            "count": len(inbox_items),
            "returned_recently": [i for i in inbox_items if i["from"] is not None],
        },
        "tickler_landing_this_week": landing_this_week,
        "stalled_projects": stalled,
        "lint_findings": findings,
    }
