"""Decisions JSON -> vault operations.

Deterministic first, AI only by explicit opt-in: a tick box, a fixed slot or
a printed id is applied here by plain Python, and the only model in the loop
is the one that transcribed an inked write-in box or — where ✦ AI was
ticked — read the whole row into a `gtd.ai/3` list of operations
(`_execute_operations`). Ambiguity (an unreadable date, a project name that
matches two pages) is reported, never resolved silently.

✦ AI short-circuits everything below it: `apply_task` routes such a row to
`_apply_ai_row` before any deterministic branch, and nothing else touches
it. The scanner already demoted the row's gutter reading into `suggestion`
(and set `action: "none"`), so there is nothing deterministic left to apply
even by accident — but the guard here is explicit, because one row must
have exactly one writer and on an AI row that writer is the agent.

Besides the four list buckets a sheet carries `capture` rows (the Inbox's
blank lines and each project page's add-an-action lines), `newproj` rows
(the New Projects page's blank lines: the text is a would-be project's
first action, the PROJECT slot its name) and `project` rows (an open item on
a project page); a row's NEW box turns it into a project,
plugin-style. Each scanned decision is turned into `gtd_mcp.ops` calls, addressed
by the handle that travelled out in the PDF and back in the decisions'
companion tasks document. Handles carry a content hash, so a row the
nightly sort moved still resolves; a row whose content changed since the
sheet was printed is re-found by its printed text, and one that is gone
(already done in Obsidian, say) is reported and skipped — never guessed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from gtd_ci.dates import parse_date
from gtd_ci.model import Vault
from gtd_mcp import ops, views
from gtd_mcp.handles import Stale, parse_handle

from .tasks import PERIOD_BUCKETS, display_text

_MONTHS = {
    m: i
    for i, names in enumerate(
        [("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"),
         ("may",), ("jun", "june"), ("jul", "july"), ("aug", "august"),
         ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"), ("dec", "december")],
        start=1,
    )
    for m in names
}
_WEEKDAYS = {n: i for i, n in enumerate(["mon", "tue", "wed", "thu", "fri", "sat", "sun"])}
_UNKNOWN_PERSON = "?"


@dataclass
class ApplyReport:
    applied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.applied)


# --- handwriting -> field values -------------------------------------------


def parse_handwritten_date(text: str, today: date) -> str | None:
    """Best-effort date from a handwritten slot -> canonical ``YYYY-MM-DD``.

    Accepts everything FORMAT.md reads plus year-less forms (``6 Jun``,
    ``6/6``, ``Jun 6``, ``6th June``) and weekday names (next such day).
    A year-less date is placed in the current year unless that is more than
    30 days ago, in which case next year. ``None`` when nothing parses.
    """
    raw = text.strip().rstrip(".")
    if not raw:
        return None
    parsed = parse_date(raw)
    if parsed is not None:
        return parsed.value.isoformat()
    low = raw.lower()

    def _assemble(day: int, month: int) -> str | None:
        for year in (today.year, today.year + 1):
            try:
                d = date(year, month, day)
            except ValueError:
                return None
            if d >= today - timedelta(days=30):
                return d.isoformat()
        return None

    m = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?\s*[ /.-]\s*([a-z]+)\.?(?:\s+(\d{4}))?", low)
    if m and m.group(2) in _MONTHS:
        if m.group(3):
            try:
                return date(int(m.group(3)), _MONTHS[m.group(2)], int(m.group(1))).isoformat()
            except ValueError:
                return None
        return _assemble(int(m.group(1)), _MONTHS[m.group(2)])
    m = re.fullmatch(r"([a-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?", low)
    if m and m.group(1) in _MONTHS:
        if m.group(3):
            try:
                return date(int(m.group(3)), _MONTHS[m.group(1)], int(m.group(2))).isoformat()
            except ValueError:
                return None
        return _assemble(int(m.group(2)), _MONTHS[m.group(1)])
    m = re.fullmatch(r"(\d{1,2})\s*[/.]\s*(\d{1,2})(?:\s*[/.]\s*(\d{2,4}))?", low)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        if m.group(3):
            year = int(m.group(3))
            year = year + 2000 if year < 100 else year
            try:
                return date(year, month, day).isoformat()
            except ValueError:
                return None
        return _assemble(day, month)
    m = re.fullmatch(r"(mon|tue|wed|thu|fri|sat|sun)[a-z]*", low)
    if m:
        ahead = (_WEEKDAYS[m.group(1)] - today.weekday()) % 7 or 7
        return (today + timedelta(days=ahead)).isoformat()
    if low in ("today",):
        return today.isoformat()
    if low in ("tomorrow", "tmrw", "tmw"):
        return (today + timedelta(days=1)).isoformat()
    return None


def _field_text(fields: dict, name: str) -> str:
    v = fields.get(name)
    if isinstance(v, dict):
        v = v.get("text", "")
    return (v or "").strip()


# --- handle resolution ---------------------------------------------------------


def _items_of_kind(vault: Vault, kind: str) -> tuple[list[dict], str]:
    if kind == "next-actions":
        return views.next_actions(vault), "action"
    if kind == "delegated":
        return views.delegated(vault), "thing"
    if kind == "inbox":
        return views.inbox(vault), "text"
    if kind.startswith("tickler:"):
        return views.tickler(vault, kind.split(":", 1)[1]), "text"
    if kind.startswith("project:"):
        page = views.get_project(vault, kind.split(":", 1)[1])
        return (page or {}).get("items", []), "text"
    return [], "text"


def refresh_handle(vault: Vault, handle: str, printed_text: str) -> str | None:
    """A handle the vault no longer matches -> the item now carrying the same
    printed text (unique), or ``None``."""
    try:
        kind = parse_handle(handle).kind
    except Stale:
        return None
    items, key = _items_of_kind(vault, kind)
    matches = [it["handle"] for it in items if display_text(it[key]) == printed_text]
    return matches[0] if len(matches) == 1 else None


# --- the mapping --------------------------------------------------------------


class _Skip(Exception):
    pass


def _run(vault: Vault, today: date, task: dict, task_id: str, op, **kwargs) -> ops.OpResult:
    """Call an op with the task's handle, refreshing a stale handle by text once."""
    handle = task.get("handle")
    if not handle:
        raise _Skip(f"{task_id}: no vault handle recorded for this row")
    try:
        return op(vault, today, handle=handle, **kwargs)
    except Stale as exc:
        fresh = refresh_handle(vault, handle, task.get("act", ""))
        if fresh is None:
            raise _Skip(
                f"{task_id} ({task.get('act', '')[:50]!r}): not found in the vault any more "
                f"— {exc.reason}; nothing changed"
            )
        return op(vault, today, handle=fresh, **kwargs)


def _tickler_target(period: str | None) -> str:
    bucket = PERIOD_BUCKETS.get(period or "", "Next week")
    return f"tickler:{bucket}"


def _row_updates(fields: dict, bucket: str, today: date, warnings: list[str], task_id: str) -> dict:
    """Slot handwriting -> keyword args for ops.update / ops.triage."""
    out: dict = {}
    pri = _field_text(fields, "priority")
    if pri:
        m = re.search(r"-?\d+", pri)
        if m:
            out["priority"] = int(m.group())
        else:
            warnings.append(f"{task_id}: could not read priority {pri!r}")
    due = _field_text(fields, "due")
    if due:
        parsed = parse_handwritten_date(due, today)
        if parsed:
            out["date"] = parsed
        else:
            warnings.append(f"{task_id}: could not read date {due!r}")
    proj = _field_text(fields, "project")
    if proj:
        out["project"] = proj
    to = _field_text(fields, "to")
    if to:
        out["person"] = to
    return out


# --- projects ------------------------------------------------------------------


def _project_stem(vault: Vault, name: str, task_id: str, report: "ApplyReport") -> str | None:
    """A handwritten project name -> an existing project stem, or ``None``.

    An exact match is used silently; a single near match is applied and noted
    in the run report; no match, or several, is a warning and nothing is
    applied — ambiguity is reported, never resolved silently.
    """
    name = (name or "").strip()
    if not name:
        return None
    stem, candidates = ops.nearest_project_stem(vault, name)
    if stem is not None and not candidates:
        return stem
    if stem is not None:
        report.notes.append(f"{task_id}: project {name!r} matched to {stem!r}")
        return stem
    if candidates:
        report.warnings.append(
            f"{task_id}: project {name!r} not applied — did you mean: {', '.join(candidates)}?"
        )
    else:
        report.warnings.append(f"{task_id}: project {name!r} not applied — no project of that name")
    return None


def _project_kw(vault: Vault, upd: dict, task_id: str, report: "ApplyReport") -> dict:
    stem = _project_stem(vault, upd.get("project", ""), task_id, report)
    return {"project": stem} if stem else {}


def _person_kw(upd: dict, task_id: str, report: "ApplyReport") -> dict:
    person = upd.get("person")
    if not person:
        report.warnings.append(
            f"{task_id}: delegated without a readable name in the TO box — person set to {_UNKNOWN_PERSON!r}"
        )
        person = _UNKNOWN_PERSON
    return {"person": person}


# --- the ✦ AI agent ------------------------------------------------------------


def ai_reading(decision: dict) -> dict | None:
    """The row's structured AI reading (``gtd.ai/3``), whatever its state.

    Reads ``ai_reading``, falling back to the pre-rename ``edit`` key so a
    decisions file written by an older scanner still applies.
    """
    return decision.get("ai_reading") or decision.get("edit") or None


def ai_requested(decision: dict) -> bool:
    """Did I tick ✦ AI on this row? (``edited`` is the pre-rename key.)"""
    return bool(decision.get("ai") or decision.get("edited"))


def ai_intent(decision: dict, task_id: str, report: ApplyReport) -> dict | None:
    """The agent's structured read of an inked ✦ AI row (``gtd.ai/3``), or
    ``None`` when there was nothing to interpret or it couldn't be — the
    latter already reported as a warning here, so a caller getting ``None``
    back never needs to warn again."""
    reading = ai_reading(decision)
    if not reading:
        return None
    if not reading.get("understood") or (reading.get("confidence") or 0) < 0.5:
        report.warnings.append(
            f"{task_id}: AI ticked but the annotation could not be interpreted — "
            f"handwriting {reading.get('handwriting', '')!r}; {reading.get('note', '')} — nothing changed"
        )
        return None
    if reading.get("operations") is None:
        report.warnings.append(
            f"{task_id}: AI read as {reading.get('route', '?')!r} by an old scanner "
            "(gtd.edit/1, no operations) — ignored"
        )
        return None
    return reading


class _OpWarning(Exception):
    """An operation that cannot be carried out; reported, then the rest run on."""


def _op_text(op: dict, key: str = "text") -> str:
    return (op.get(key) or "").strip()


def _op_date(op: dict, today: date, key: str = "due") -> str | None:
    raw = _op_text(op, key)
    if not raw:
        return None
    parsed = parse_handwritten_date(raw, today)
    if parsed is None:
        raise _OpWarning(f"could not read date {raw!r}")
    return parsed


def _drop_none(kw: dict) -> dict:
    return {k: v for k, v in kw.items() if v is not None}


_MOVE_TO_TARGET = {"next": "next-actions", "delegated": "delegated", "inbox": "inbox", "scheduled": "scheduled"}


def _execute_one(vault: Vault, today: date, task: dict, task_id: str, bucket: str, op: dict,
                 name: str, report: ApplyReport) -> ops.OpResult | None:
    text = _op_text(op)
    printed = task.get("act", "")

    def project_stem(*keys: str) -> str | None:
        for key in keys:
            value = _op_text(op, key)
            if value:
                return _project_stem(vault, value, task_id, report)
        return None

    def existing_project() -> str:
        """The project a project-level op names, defaulting to the row's own."""
        given = _op_text(op, "name") or _op_text(op, "project")
        if given:
            stem = _project_stem(vault, given, task_id, report)
            if stem is None:
                raise _OpWarning(f"no project {given!r} to {name.replace('_', ' ')}")
            return stem
        if task.get("proj"):
            return task["proj"]
        raise _OpWarning(f"{name} without a project name")

    if bucket == "projhead" and name in ("update", "delete", "move"):
        raise _OpWarning(
            f"{name!r} on the project row — use rename_project / set_project_goal / archive_project"
        )
    if bucket == "projhead" and name == "complete":
        name = "archive_project"  # ✓ on the project itself: the whole project is done

    if name == "rename_project":
        if not text:
            raise _OpWarning("rename_project without a new name")
        return ops.rename_project(vault, today, project=existing_project(), new_name=text)

    if name == "set_project_goal":
        goal = _op_text(op, "goal") or text
        if not goal:
            raise _OpWarning("set_project_goal without a goal")
        return ops.set_project_goal(vault, today, project=existing_project(), goal=goal)

    if name == "archive_project":
        return ops.archive_project(vault, today, name=existing_project())

    if name == "update":
        kwargs: dict = {}
        if bucket == "next":
            if text:
                kwargs["action"] = text
            if op.get("priority") is not None:
                kwargs["priority"] = op["priority"]
            deadline = _op_date(op, today)
            if deadline:
                kwargs["deadline"] = deadline
            stem = project_stem("project")
            if stem:
                kwargs["project"] = stem
            if _op_text(op, "person"):
                report.warnings.append(f"{task_id}: a person was given for a Next actions row — ignored")
        elif bucket == "delegated":
            if text:
                kwargs["thing"] = text
            if op.get("priority") is not None:
                kwargs["priority"] = op["priority"]
            chase = _op_date(op, today)
            if chase:
                kwargs["chase_by"] = chase
            if _op_text(op, "person"):
                kwargs["person"] = _op_text(op, "person")
            stem = project_stem("project")
            if stem:
                kwargs["project"] = stem
        else:  # inbox / tickler lines and project items carry text only
            if text:
                kwargs["text"] = text
            extra = [k for k in ("priority", "due", "project", "person") if op.get(k)]
            if extra:
                report.warnings.append(
                    f"{task_id}: {', '.join(extra)} cannot be set on a {bucket or 'line'} item — ignored"
                )
        if not kwargs:
            raise _OpWarning("update with nothing to change")
        return _run(vault, today, task, task_id, ops.update, **kwargs)

    if name == "complete":
        r = _run(vault, today, task, task_id, ops.complete)
        if r.payload.get("no_matching_item"):
            report.warnings.append(f"{task_id}: row removed but no matching unchecked item on its project page")
        return r

    if name == "delete":
        return _run(vault, today, task, task_id, ops.triage, to="trash")

    if name == "move":
        to = (op.get("to") or "").strip().lower()
        if to in _MOVE_TO_TARGET:
            target = _MOVE_TO_TARGET[to]
        elif to == "tickler":
            target = _tickler_target(op.get("period"))
        elif to == "project":
            stem = project_stem("project", "name")
            if stem is None:
                raise _OpWarning("move to a project needs a project name")
            target = f"project:{stem}"
        else:
            raise _OpWarning(f"unknown move destination {op.get('to')!r}")
        if bucket == "project" and to in ("next", "delegated", "scheduled", "tickler"):
            # A project step stays on its page; only where it is surfaced moves.
            if text and text != printed:
                # Reworded and routed in one go: reword the step (and its
                # rows) in place, then route it under its new wording.
                _run(vault, today, task, task_id, ops.update, text=text)
                fresh = refresh_handle(vault, task.get("handle", ""), display_text(text))
                if fresh is None:
                    raise _OpWarning(f"reworded to {text!r} but could not find it again to route it")
                task = {**task, "handle": fresh, "act": display_text(text)}
            route: dict = {"to": {"next": "next-actions"}.get(to, to)}
            if to == "next":
                route.update(priority=op.get("priority"), deadline=_op_date(op, today))
            elif to == "delegated":
                route.update(priority=op.get("priority"), chase_by=_op_date(op, today),
                             **_person_kw({"person": _op_text(op, "person")}, task_id, report))
            elif to == "scheduled":
                route["date"] = _op_date(op, today)
                if route["date"] is None:
                    raise _OpWarning("move to Scheduled needs a date")
            else:
                route["bucket"] = PERIOD_BUCKETS.get((op.get("period") or "").strip(), "Next week")
            return _run(vault, today, task, task_id, ops.route_project_action, **_drop_none(route))
        kw: dict = {"text": text or None}
        if to == "next":
            kw["priority"] = op.get("priority")
            kw["deadline"] = _op_date(op, today)
            stem = project_stem("project")
            if stem:
                kw["project"] = stem
        elif to == "delegated":
            kw["priority"] = op.get("priority")
            kw["chase_by"] = _op_date(op, today)
            kw.update(_person_kw({"person": _op_text(op, "person")}, task_id, report))
        elif to == "scheduled":
            kw["date"] = _op_date(op, today)
            if kw["date"] is None:
                raise _OpWarning("move to Scheduled needs a date")
        return _run(vault, today, task, task_id, ops.triage, to=target, **_drop_none(kw))

    if name == "capture":
        if not text:
            raise _OpWarning("capture without any text")
        return ops.capture(vault, today, text=text)

    if name == "add_next_action":
        if not text:
            raise _OpWarning("add_next_action without any text")
        return ops.add_next_action(
            vault, today, action=text, project=project_stem("project"),
            deadline=_op_date(op, today), priority=op.get("priority"),
        )

    if name == "delegate":
        if not text:
            raise _OpWarning("delegate without any text")
        return ops.delegate(
            vault, today, thing=text, chase_by=_op_date(op, today), priority=op.get("priority"),
            project=project_stem("project"), **_person_kw({"person": _op_text(op, "person")}, task_id, report),
        )

    if name == "schedule":
        when = _op_date(op, today)
        if when is None:
            raise _OpWarning("schedule without a date")
        return ops.schedule(vault, today, thing=text or printed, date=when)

    if name == "add_to_tickler":
        if not text:
            raise _OpWarning("add_to_tickler without any text")
        bucket_name = PERIOD_BUCKETS.get((op.get("period") or "").strip(), "Next week")
        return ops.add_to_tickler(vault, today, bucket=bucket_name, text=text, project=project_stem("project"))

    if name == "create_project":
        new_name = _op_text(op, "name") or _op_text(op, "project")
        if not new_name:
            raise _OpWarning("create_project without a name")
        return ops.create_project(
            vault, today, name=new_name, goal=_op_text(op, "goal") or text or new_name,
            first_action=text,
        )

    if name == "add_project_action":
        if not text:
            raise _OpWarning("add_project_action without any text")
        stem = project_stem("name", "project")
        if stem is None:
            raise _OpWarning("add_project_action without a known project")
        return ops.add_project_action(vault, today, project=stem, text=text)

    raise _OpWarning(f"unknown operation {op.get('op')!r}")


def _execute_operations(vault: Vault, today: date, task: dict, task_id: str, reading: dict,
                        report: ApplyReport) -> int:
    """Run the AI agent's operations in order; a failing one is reported and
    the rest still run."""
    applied = 0
    for i, op in enumerate(reading.get("operations") or [], start=1):
        name = (op.get("op") or "").strip()
        label = f"{task_id} ✦ op{i} {name or '?'}"
        try:
            result = _execute_one(vault, today, task, task_id, task.get("bucket", ""), op, name, report)
        except _Skip as exc:
            report.skipped.append(str(exc))
            continue
        except (_OpWarning, ops.OpError) as exc:
            report.warnings.append(f"{label}: {exc}")
            continue
        if result is None:
            continue
        report.applied.append(f"{label}: {result.summary}")
        applied += 1
    return applied


# --- NEW ------------------------------------------------------------------------


def _apply_new_project(vault: Vault, today: date, decision: dict, task: dict, task_id: str,
                       report: ApplyReport) -> None:
    """The NEW box: turn this row into a project, plugin-style.

    The row's own text is both the project's Goal and its first (and only)
    action: the thing that made me want a project is the thing I want to do
    about it, so seeding a stock "Plan project" only ever created something
    to delete. The project's NAME still comes from the PROJECT box; only
    the seeded action changed. The source row is consumed, and any routing
    tick on the row is ignored (and reported).
    """
    fields = decision.get("fields") or {}
    action = decision.get("action", "none")
    name = _field_text(fields, "project")
    if not name:
        report.warnings.append(
            f"{task_id}: NEW ticked but the PROJECT box is empty — no project created, nothing else changed"
        )
        return
    goal = (decision.get("act_text") or "").strip() or task.get("act", "").strip()
    priority = _field_text(fields, "priority")
    pri: str | int | None = None
    if priority:
        m = re.search(r"-?\d+", priority)
        if m:
            pri = int(m.group())
        else:
            report.warnings.append(f"{task_id}: could not read priority {priority!r}")
    if pri is None:
        pri = task.get("pri") or None
    try:
        r = ops.create_project(
            vault, today, name=name, goal=goal or name,
            first_action=goal, priority=pri,
        )
    except ops.OpError as exc:
        report.warnings.append(f"{task_id}: NEW project {name!r} not created — {exc}")
        return
    report.applied.append(f"{task_id}: new project — {r.summary}")
    if action != "none":
        report.warnings.append(
            f"{task_id}: NEW was ticked, so the {action!r} tick on the same row was ignored"
        )
    if task.get("bucket") in ("capture", "newproj"):
        return  # a blank write-in line has no source row to consume
    try:
        removed = _run(vault, today, task, task_id, ops.delete)
    except _Skip as exc:
        report.skipped.append(str(exc))
        return
    except ops.OpError as exc:
        report.warnings.append(f"{task_id}: source row not removed — {exc}")
        return
    report.applied.append(f"{task_id}: source row consumed — {removed.summary}")


# --- ✦ AI rows -------------------------------------------------------------------


def _apply_ai_row(vault: Vault, today: date, decision: dict, task: dict, task_id: str,
                  report: ApplyReport) -> None:
    """A row whose ✦ AI box was ticked: the agent's operations, and nothing else.

    This is the whole point of the escape hatch. No gutter action, no slot
    update, no NEW-box project creation runs for this row — not even as a
    fallback when the agent came back with nothing, because "the agent could
    not read it" and "apply the boxes instead" are different answers and
    only the first one is honest. A row that produced nothing is reported
    and left alone for me to fix by hand.
    """
    reading = ai_intent(decision, task_id, report)
    if reading is None:
        if ai_reading(decision) is None:
            report.warnings.append(
                f"{task_id}: AI ticked but the scanner produced no reading of the row "
                "— nothing changed"
            )
        return  # ai_intent already said why
    if not reading.get("operations"):
        report.warnings.append(f"{task_id}: AI ticked but no change was asked for — nothing changed")
        return
    _execute_operations(vault, today, task, task_id, reading, report)

    # The deterministic reading is recorded by the scanner for audit only;
    # say so when it wanted something, so a surprising result is traceable.
    suggested = (decision.get("suggestion") or {}).get("action", "none")
    if suggested not in ("none", None) or (decision.get("suggestion") or {}).get("new_project"):
        report.notes.append(
            f"{task_id}: ✦ AI was ticked, so the row's own boxes "
            f"(suggested {suggested!r}) were passed to the agent as context "
            "and not applied directly"
        )


# --- capture rows ----------------------------------------------------------------


def _apply_capture_row(vault: Vault, today: date, decision: dict, task: dict, task_id: str,
                       report: ApplyReport) -> None:
    """A blank write-in row.

    Three kinds arrive here: the Inbox's capture lines (which carry the
    Inbox gutter), each project page's add-an-action lines (which carry no
    gutter, and whose ``proj`` says where the text goes) and the New
    Projects page's lines (bucket ``newproj``).

    A ``newproj`` row means "create this project" without needing a NEW
    tick — the whole page says so. But a routing tick on such a row wins:
    writing something down as a project and then ticking → Deleg is me
    changing my mind, so the text is filed like any Inbox item and no
    project is created. (This is the opposite way round from a NEW tick on
    a printed row, where the project wins and the routing tick is
    reported as ignored — there the page is not the thing asking.)
    """
    text = (decision.get("act_text") or "").strip()
    inked = bool(decision.get("inked"))
    action = decision.get("action", "none")
    period = decision.get("defer_period")
    fields = decision.get("fields") or {}
    is_new_project_row = task.get("bucket") == "newproj"
    new_project = bool(decision.get("new_project")) or (
        is_new_project_row and action == "none"
    )
    proj = (task.get("proj") or "").strip()

    if not text:
        if not inked and action == "none":
            return  # an untouched blank line
        if inked:
            report.warnings.append(f"{task_id}: ink found on a capture line but nothing legible was transcribed")
        else:
            report.warnings.append(f"{task_id}: a box was ticked on a capture line but nothing was written")
        return

    if proj:  # a project page's add-an-action line
        try:
            r = ops.add_project_action(vault, today, project=proj, text=text)
        except ops.OpError as exc:
            report.warnings.append(f"{task_id}: {exc}")
            return
        report.applied.append(f"{task_id} {text[:60]!r}: added to {proj} — {r.summary}")
        return

    if new_project:
        _apply_new_project(vault, today, decision, task, task_id, report)
        return
    if is_new_project_row:
        report.notes.append(
            f"{task_id} {text[:60]!r}: written on the New Projects page but routed "
            f"{action!r} instead, so no project was created"
        )

    upd = _row_updates(fields, "capture", today, report.warnings, task_id)
    label = f"{task_id} {text[:60]!r}"
    try:
        if action == "none":
            r = ops.capture(vault, today, text=text)
        elif action in ("to_next", "to_me", "activate"):
            r = ops.add_next_action(
                vault, today, action=text, deadline=upd.get("date"), priority=upd.get("priority"),
                **_project_kw(vault, upd, task_id, report),
            )
        elif action == "to_deleg":
            r = ops.delegate(
                vault, today, thing=text, chase_by=upd.get("date"), priority=upd.get("priority"),
                **_person_kw(upd, task_id, report), **_project_kw(vault, upd, task_id, report),
            )
        elif action == "defer":
            r = ops.add_to_tickler(
                vault, today, bucket=PERIOD_BUCKETS.get(period or "1w", "Next week"), text=text,
                **_project_kw(vault, upd, task_id, report),
            )
        elif action == "drop":
            report.notes.append(f"{label}: written on a capture line then dropped — nothing captured")
            return
        else:
            report.warnings.append(f"{task_id}: unknown action {action!r} on a capture line — ignored")
            return
    except ops.OpError as exc:
        report.warnings.append(f"{task_id}: {exc}")
        return
    report.applied.append(f"{label}: {action} — {r.summary}")


# --- project item rows -------------------------------------------------------------


def _apply_project_item(vault: Vault, today: date, decision: dict, task: dict, task_id: str,
                        report: ApplyReport) -> None:
    """A step on a project page: an ordinary action that lives on its page.

    ✓ Done ticks it (and drops every row surfacing it). → Deleg and the
    Defer trio re-surface it — waiting on the person in TO, chase-by DUE,
    or parked in the tickler — while the checkbox stays on the page, so the
    project keeps its plan and ticking the Delegated row off later ticks the
    step. ✗ Drop deletes the step and its rows. A ✦ AI row never reaches
    here — ``apply_task`` routes it to ``_apply_ai_row`` first.
    """
    action = decision.get("action", "none")
    period = decision.get("defer_period")
    label = f"{task_id} {task.get('act', '')[:60]!r}"
    upd = _row_updates(decision.get("fields") or {}, "project", today, report.warnings, task_id)

    if action == "none":
        if upd:
            report.warnings.append(
                f"{task_id}: {', '.join(sorted(upd))} written on a project step but no box ticked — ignored"
            )
        return
    try:
        if action == "done":
            r = _run(vault, today, task, task_id, ops.complete)
        elif action == "to_deleg":
            r = _run(vault, today, task, task_id, ops.route_project_action, to="delegated",
                     **_drop_none({"chase_by": upd.get("date"), **_person_kw(upd, task_id, report)}))
            if r.payload.get("chase_by_defaulted"):
                report.warnings.append(f"{task_id}: chase-by defaulted to {r.payload['chase_by_defaulted']}")
        elif action == "defer":
            r = _run(vault, today, task, task_id, ops.route_project_action, to="tickler",
                     bucket=PERIOD_BUCKETS.get(period or "1w", "Next week"))
        elif action == "drop":
            r = _run(vault, today, task, task_id, ops.delete)
        else:
            report.warnings.append(f"{task_id}: {action!r} is not something a project step takes — ignored")
            return
    except _Skip as exc:
        report.skipped.append(str(exc))
        return
    except ops.OpError as exc:
        report.warnings.append(f"{task_id}: {exc}")
        return
    if action in ("done", "drop") and upd:
        report.warnings.append(f"{task_id}: {', '.join(sorted(upd))} ignored on a {action!r} project step")
    report.applied.append(
        f"{label}: {action}{f' [{period}]' if action == 'defer' and period else ''} — {r.summary}"
    )


# --- the project row -----------------------------------------------------------------


def _apply_project_head(vault: Vault, today: date, decision: dict, task: dict, task_id: str,
                        report: ApplyReport) -> None:
    """A project page's project row: the project itself.

    NEW GOAL rewrites the goal, RENAME TO renames the page and every link
    to it, ✓ Finish archives the project to ``Done/`` — in that order, so a
    project can be re-goaled, renamed and finished on one sheet. Runs after
    every other row of the sheet (``apply_decisions``), so the steps ticked
    or routed on the same page are applied to the page before it moves.
    """
    stem = (task.get("proj") or task.get("act") or "").strip()
    fields = decision.get("fields") or {}
    action = decision.get("action", "none")
    goal = _field_text(fields, "goal")
    new_name = _field_text(fields, "name")
    label = f"{task_id} project {stem!r}"
    if not stem:
        report.warnings.append(f"{task_id}: project row carries no project name — nothing changed")
        return
    if "goal" in fields and not goal:
        report.warnings.append(f"{task_id}: ink in NEW GOAL but nothing legible was transcribed — goal unchanged")
    if "name" in fields and not new_name:
        report.warnings.append(f"{task_id}: ink in RENAME TO but nothing legible was transcribed — name unchanged")

    if goal:
        try:
            r = ops.set_project_goal(vault, today, project=stem, goal=goal)
            report.applied.append(f"{label}: new goal — {r.summary}")
        except ops.OpError as exc:
            report.warnings.append(f"{task_id}: goal not set — {exc}")
    if new_name:
        try:
            r = ops.rename_project(vault, today, project=stem, new_name=new_name)
            report.applied.append(f"{label}: renamed — {r.summary}")
            if r.payload.get("unmanaged_links"):
                report.notes.append(
                    f"{task_id}: {r.payload['unmanaged_links']} link(s) to [[{stem}]] outside the managed "
                    "files (Reference/, Done/) still point at the old name"
                )
            stem = r.payload["new_path"].rsplit("/", 1)[-1].removesuffix(".md")
        except ops.OpError as exc:
            report.warnings.append(f"{task_id}: not renamed to {new_name!r} — {exc}")
    if action == "done":
        try:
            r = ops.archive_project(vault, today, name=stem)
            report.applied.append(f"{task_id} project {stem!r}: finished — {r.summary}")
        except ops.OpError as exc:
            report.warnings.append(f"{task_id}: project not archived — {exc}")
    elif action != "none":
        report.warnings.append(f"{task_id}: the project row only takes ✓ Finish and ✦ AI — {action!r} ignored")


# --- one scanned row ----------------------------------------------------------------


def apply_task(vault: Vault, today: date, decision: dict, task: dict, report: ApplyReport) -> None:
    task_id = decision.get("id", "?")
    bucket = task.get("bucket", "")
    # ✦ AI first, and unconditionally: it short-circuits every branch below
    # so that a row can never be written twice.
    if ai_requested(decision):
        _apply_ai_row(vault, today, decision, task, task_id, report)
        return
    if bucket in ("capture", "newproj"):
        _apply_capture_row(vault, today, decision, task, task_id, report)
        return
    if decision.get("new_project"):
        _apply_new_project(vault, today, decision, task, task_id, report)
        return
    if bucket == "project":
        _apply_project_item(vault, today, decision, task, task_id, report)
        return
    if bucket == "projhead":
        _apply_project_head(vault, today, decision, task, task_id, report)
        return

    action = decision.get("action", "none")
    period = decision.get("defer_period")
    fields = decision.get("fields") or {}
    label = f"{task_id} {task.get('act', '')[:60]!r}"

    upd = _row_updates(fields, bucket, today, report.warnings, task_id)

    if action == "none":
        if not upd:
            return
        try:
            r = _apply_keep(vault, today, task, task_id, bucket, upd, report)
        except _Skip as exc:
            report.skipped.append(str(exc))
            return
        except ops.OpError as exc:
            report.warnings.append(f"{task_id}: {exc}")
            return
        if r is not None:
            report.applied.append(f"{label}: slots — {r.summary}")
        return

    try:
        if action == "done":
            r = _run(vault, today, task, task_id, ops.complete)
            if r.payload.get("no_matching_item"):
                report.warnings.append(f"{task_id}: row removed but no matching unchecked item on its project page")
        elif action == "drop":
            r = _run(vault, today, task, task_id, ops.triage, to="trash")
        elif action in ("to_next", "to_me", "activate"):
            kw = {"priority": upd.get("priority"), "deadline": upd.get("date"),
                  **_project_kw(vault, upd, task_id, report)}
            r = _run(vault, today, task, task_id, ops.triage, to="next-actions", **_drop_none(kw))
        elif action == "to_deleg":
            kw = {"priority": upd.get("priority"), "chase_by": upd.get("date"),
                  **_person_kw(upd, task_id, report)}
            r = _run(vault, today, task, task_id, ops.triage, to="delegated", **_drop_none(kw))
            if r.payload.get("chase_by_defaulted"):
                report.warnings.append(f"{task_id}: chase-by defaulted to {r.payload['chase_by_defaulted']}")
        elif action == "defer":
            r = _run(vault, today, task, task_id, ops.triage, to=_tickler_target(period))
        else:
            report.warnings.append(f"{task_id}: unknown action {action!r} ignored")
            return
    except _Skip as exc:
        report.skipped.append(str(exc))
        return
    except ops.OpError as exc:
        report.warnings.append(f"{task_id}: {exc}")
        return

    report.applied.append(
        f"{label}: {action}{f' [{period}]' if action == 'defer' and period else ''} — {r.summary}"
    )


def _apply_keep(vault, today, task, task_id, bucket, upd, report) -> ops.OpResult | None:
    """In-place update from the row's write-in slots, without moving it.

    Only the labelled slots (PRIORITY / DUE / PROJECT / TO) can do this
    deterministically. Re-wording the printed action needs a model reading
    the row, which is the ✦ AI path and never reaches here.
    """
    new_text = None
    kwargs: dict = {}
    if bucket == "next":
        if new_text:
            kwargs["action"] = new_text
        if "priority" in upd:
            kwargs["priority"] = upd["priority"]
        if "date" in upd:
            kwargs["deadline"] = upd["date"]
        kwargs.update(_project_kw(vault, upd, task_id, report))
        if "person" in upd:
            report.warnings.append(f"{task_id}: a name was written in TO but Delegate was not ticked — ignored")
    elif bucket == "delegated":
        if new_text:
            kwargs["thing"] = new_text
        if "priority" in upd:
            kwargs["priority"] = upd["priority"]
        if "date" in upd:
            kwargs["chase_by"] = upd["date"]
        if "person" in upd:
            kwargs["person"] = upd["person"]
        kwargs.update(_project_kw(vault, upd, task_id, report))
    else:  # inbox / tickler line items only carry text
        if new_text:
            kwargs["text"] = new_text
        if upd:
            report.warnings.append(
                f"{task_id}: metadata written ({', '.join(sorted(upd))}) but no routing box ticked — ignored"
            )
    if not kwargs:
        return None
    return _run(vault, today, task, task_id, ops.update, **kwargs)


def apply_captures(vault: Vault, today: date, captures: list[dict], page_key: str, report: ApplyReport) -> None:
    for cap in captures:
        if not cap.get("inked"):
            continue
        text = (cap.get("text") or "").strip()
        line = cap.get("line", "?")
        if not text:
            report.warnings.append(f"{page_key} capture {line}: ink found but nothing legible was transcribed")
            continue
        r = ops.capture(vault, today, text=text)
        report.applied.append(f"capture {line}: {r.summary}")


def apply_decisions(vault: Vault, today: date, decisions: dict, tasks_doc: dict | None) -> ApplyReport:
    """Apply a whole scanned sheet (``{"pages": [...]}``) to a loaded vault.

    The vault is mutated in memory; the caller saves it (``gtd_ci.model.save_vault``).
    """
    report = ApplyReport()
    tasks = (tasks_doc or {}).get("tasks", {})
    if not tasks:
        report.warnings.append("no tasks document: sheet ids cannot be mapped to vault rows")
    pages = decisions.get("pages")
    if pages is None:
        pages = [decisions]
    # A project row can rename or archive its project; every other row of
    # the sheet (its own steps included) is addressed by the name the sheet
    # was printed with, so the project rows run once all of those have.
    project_rows: list[tuple[dict, dict]] = []
    for page in pages:
        page_key = page.get("page_key", page.get("header_qr", "?"))
        if page.get("skipped"):
            continue  # a read-only page (the projects summary): nothing to scan
        if "error" in page:
            report.warnings.append(f"{page_key}: page not scanned — {page['error']}")
            continue
        for w in page.get("warnings", []):
            if "multiple" in w or "header QR" in w or "poor" in w:
                report.warnings.append(f"{page_key}: {w}")
        for decision in page.get("tasks", []):
            task_id = decision.get("id", "?")
            task = tasks.get(task_id)
            if task is None:
                if (decision.get("action", "none") != "none" or decision.get("fields")
                        or ai_requested(decision)):
                    report.skipped.append(f"{task_id}: marked on the sheet but absent from the tasks document")
                continue
            if not decision.get("qr_verified", True) and decision.get("action", "none") != "none":
                report.warnings.append(f"{task_id}: row QR did not verify; applying by position")
            if task.get("bucket") == "projhead":
                project_rows.append((decision, task))
                continue
            apply_task(vault, today, decision, task, report)
        apply_captures(vault, today, page.get("captures", []), page_key, report)
    for decision, task in project_rows:
        apply_task(vault, today, decision, task, report)
    return report
