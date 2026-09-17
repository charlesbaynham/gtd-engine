"""Semantic write operations: pure functions on a loaded gtd_ci Vault.

Every op signature is `op(vault, today, **kwargs) -> OpResult`. An op either
mutates `vault` in place (marking files dirty for `gtd_ci.model.save_vault`
to flush) or, for operations that create/move files outright
(`create_project`, `archive_project`), writes directly to `vault.root` — the
store discards both kinds identically on a dry run (`git reset --hard` +
`git clean -fd`), so there is no correctness difference between the two.

Ops never touch a file that failed to parse (`_require` raises `OpError`
naming it, per FORMAT.md §9), and never re-implement parsing, rendering,
sorting or date logic — all of that comes from `gtd_ci`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from gtd_ci import lines as linesmod
from gtd_ci import projects as projectsmod
from gtd_ci.dates import parse_date
from gtd_ci.lines import Item
from gtd_ci.model import TICKLER_OFFSETS, Vault, mark_dirty
from gtd_ci.tables import RowEntry, is_placeholder, new_row
from gtd_ci.vault import ParseFailure, RawFile, load_raw, write_raw

from .handles import Stale, enumerate_logical, hash_cells, hash_text, parse_handle, resolve

__all__ = [
    "OpError",
    "OpResult",
    "capture",
    "add_next_action",
    "delegate",
    "schedule",
    "add_to_tickler",
    "triage",
    "complete",
    "update",
    "delete",
    "create_project",
    "add_project_action",
    "tick_project_action",
    "archive_project",
    "run_maintenance",
]

_BUCKET_FILES = {name.removesuffix(".md"): f"Tickler/{name}" for name in TICKLER_OFFSETS}


class OpError(Exception):
    """A validation error: bad arguments, an unresolvable project, a file that
    failed to parse. Distinct from `Stale` (handles.Stale), which means the
    vault moved under a handle rather than the request being wrong."""


@dataclass
class OpResult:
    summary: str
    changed_files: set[str] = field(default_factory=set)
    payload: dict = field(default_factory=dict)


def _require(vault: Vault, relpath: str, obj):
    if obj is None:
        reason = dict(vault.errors).get(relpath, "file missing")
        raise OpError(f"{relpath} failed to parse: {reason}")
    return obj


# --- validation helpers (shape only; calendar/format truth still comes from gtd_ci.dates) ---

_DATE_SHAPE = re.compile(r"^\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2})?$")
_STATUSES = {"", "to-schedule", "find-existing", "linked"}


def _validate_date(value: str | None, field_name: str) -> str:
    if value is None:
        return ""
    value = value.strip()
    if not value:
        return ""
    if not _DATE_SHAPE.match(value):
        raise OpError(f"{field_name}: '{value}' is not a canonical date (YYYY-MM-DD[ HH:MM])")
    if parse_date(value[:10]) is None:
        raise OpError(f"{field_name}: '{value}' is not a valid calendar date")
    return value


DEFAULT_CHASE_DAYS = 7


def _chase_by_or_default(value: str | None, today: date) -> tuple[str, dict]:
    """A Delegated row always gets a chase-by date: without one it just rots.

    Anything the caller gives is validated as usual; a missing or blank value
    becomes today + DEFAULT_CHASE_DAYS, and the returned payload warns that
    the default was used so the caller can pick a better date.
    """
    validated = _validate_date(value, "chase_by")
    if validated:
        return validated, {}
    default = (today + timedelta(days=DEFAULT_CHASE_DAYS)).isoformat()
    return default, {
        "chase_by_defaulted": default,
        "warning": (
            f"No chase_by given: defaulted to {default} (today + {DEFAULT_CHASE_DAYS} days). "
            "Set an explicit chase_by if that is not the right date to follow up."
        ),
    }


def _validate_priority(value) -> str:
    if value is None or value == "":
        return ""
    try:
        return str(int(value))
    except (TypeError, ValueError) as exc:
        raise OpError(f"priority: {value!r} is not an integer") from exc


def _validate_status(value: str | None) -> str:
    if value is None:
        return ""
    value = value.strip().lower()
    if value not in _STATUSES:
        raise OpError(f"status: {value!r} is not one of {sorted(_STATUSES)}")
    return value


def _normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


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


def resolve_project_stem(vault: Vault, name: str) -> str:
    """Resolves a project name to its stem per FORMAT.md §6, or raises OpError
    with near-name suggestions (Levenshtein <= 3 over [a-z0-9]) when it can't."""
    link = projectsmod.resolve_link(name, vault.project_index)
    if link.resolved is not None and not link.resolved_in_done:
        return link.resolved.stem
    if link.resolved is not None and link.resolved_in_done:
        raise OpError(f"project {name!r} only exists under Project details/Done/")
    all_stems = sorted({p.stem for paths in vault.project_index.values() for p in paths})
    target = _normalize(name)
    near = [s for s in all_stems if _levenshtein(_normalize(s), target) <= 3]
    if near:
        raise OpError(f"no project named {name!r}; did you mean: {', '.join(near)}?")
    raise OpError(f"no project named {name!r}")


def nearest_project_stem(vault: Vault, name: str) -> tuple[str | None, list[str]]:
    """Resolves `name` to a project stem, or names the near-miss candidates.

    Exact resolution (per FORMAT.md §6, not under Done/) returns `(stem, [])`.
    Otherwise, candidates are active stems within a normalised Levenshtein
    distance of `max(2, len(norm(name)) // 4)`, or (for a `name` of 4+ chars)
    whose normalised stem starts with or contains `norm(name)`. Exactly one
    candidate resolves to `(stem, [stem])`; anything else is `(None,
    candidates)` for the caller to disambiguate.
    """
    link = projectsmod.resolve_link(name, vault.project_index)
    if link.resolved is not None and not link.resolved_in_done:
        return link.resolved.stem, []

    target = _normalize(name)
    all_stems = sorted({p.stem for paths in vault.project_index.values() for p in paths if "Done" not in p.parts})
    threshold = max(2, len(target) // 4)
    candidates: set[str] = set()
    for stem in all_stems:
        norm_stem = _normalize(stem)
        if _levenshtein(norm_stem, target) <= threshold:
            candidates.add(stem)
        elif len(target) >= 4 and (norm_stem.startswith(target) or target in norm_stem):
            candidates.add(stem)
    ordered = sorted(candidates)
    if len(ordered) == 1:
        return ordered[0], ordered
    return None, ordered


def _find_loaded_project(vault: Vault, stem: str) -> tuple[projectsmod.ProjectPage, str]:
    for relpath, page in vault.projects.items():
        if page.stem == stem:
            return page, relpath
    for relpath, page in vault.projects.items():
        if page.stem.lower() == stem.lower():
            return page, relpath
    for relpath, reason in vault.errors:
        if Path(relpath).stem.lower() == stem.lower():
            raise OpError(f"{relpath} failed to parse: {reason}")
    raise OpError(f"no project page named {stem!r} (it may be missing a '## Next Actions' heading)")


# --- project page line-surgery, per FORMAT.md §6 "Appending" ---

_PLACEHOLDER_ITEM = re.compile(r"^[-*] \[ \]\s*$")


def _append_or_fill_item(lines: list[str], text: str) -> list[str]:
    lines = list(lines)
    heading_idx = projectsmod.find_next_actions_heading(lines)
    if heading_idx is None:
        if lines and lines[-1].strip() != "":
            lines.append("")
        lines += ["## Next Actions", "", f"- [ ] {text}"]
        return lines
    end = projectsmod.section_bounds(lines, heading_idx)
    for i in range(heading_idx + 1, end):
        if _PLACEHOLDER_ITEM.match(lines[i]):
            lines[i] = f"- [ ] {text}"
            return lines
    insert_at = end
    while insert_at > heading_idx + 1 and lines[insert_at - 1].strip() == "":
        insert_at -= 1
    lines[insert_at:insert_at] = [f"- [ ] {text}"]
    return lines


def _add_item_to_project(page: projectsmod.ProjectPage, text: str) -> None:
    page.lines = _append_or_fill_item(page.lines, text)
    page.heading_index = projectsmod.find_next_actions_heading(page.lines)
    page.section_end = projectsmod.section_bounds(page.lines, page.heading_index)
    page.items = projectsmod.parse_items(page.lines, page.heading_index + 1, page.section_end)


def _tick_item(page: projectsmod.ProjectPage, item: projectsmod.ProjectItem) -> None:
    if item.checked:
        return
    page.lines[item.line_index] = f"{item.marker} [x] {item.text}"
    item.checked = True


# --- handle resolution across every addressable collection ---

_PROV = re.compile(r"^\[from [^\]]*\]\s*")
_STAMP = re.compile(r"^\[\d{4}-\d{2}-\d{2}\]\s*")

_TABLES = {
    "next-actions": ("Next actions.md", lambda v: v.next_actions),
    "delegated": ("Delegated.md", lambda v: v.delegated),
    "scheduled": ("Scheduled.md", lambda v: v.scheduled),
}


def strip_returned_text(text: str) -> str:
    """FORMAT.md §5.1 stripping: marker is already split out by gtd_ci's line
    parser, so only the provenance prefix and a leftover stamp remain."""
    text = _PROV.sub("", text, count=1)
    text = _STAMP.sub("", text, count=1)
    return text.strip()


@dataclass
class Located:
    domain: str  # "table" | "line" | "project"
    relpath: str
    raw_index: int
    obj: object  # RowEntry | Item | ProjectItem
    text: str
    container: object  # ManagedTable | LineFile | ProjectPage
    row_priority: str | None = None
    project: str | None = None  # raw [[link]] name, from a Project column/trailing link


def _locate(vault: Vault, handle_str: str) -> Located:
    handle = parse_handle(handle_str)
    kind = handle.kind

    if kind in _TABLES:
        relpath, getter = _TABLES[kind]
        table = _require(vault, relpath, getter(vault))
        entries = enumerate_logical(table.entries, lambda e: not is_placeholder(e.cells))
        raw_index, entry = resolve(handle_str, kind, entries, lambda e: hash_cells(e.cells))
        priority = entry.cells[table.col("priority")] if "priority" in table.columns else None
        project_name = projectsmod.parse_link(table.cell(entry.cells, "project")) if table.has("project") else None
        return Located("table", relpath, raw_index, entry, entry.cells[0].strip(), table, priority, project_name)

    if kind == "inbox":
        lf = _require(vault, "Inbox.md", vault.inbox)
        entries = enumerate_logical(lf.order, lambda e: e is not None)
        raw_index, item = resolve(handle_str, "inbox", entries, lambda it: hash_text(it.text))
        body, project_name = linesmod.split_project_link(strip_returned_text(item.text))
        return Located("line", "Inbox.md", raw_index, item, body, lf, project=project_name)

    if kind.startswith("tickler:"):
        bucket = kind.split(":", 1)[1]
        relpath = _BUCKET_FILES.get(bucket)
        if relpath is None:
            raise OpError(f"tickler bucket {bucket!r} is not one of {sorted(_BUCKET_FILES)}")
        lf = _require(vault, relpath, vault.ticklers.get(relpath))
        entries = enumerate_logical(lf.order, lambda e: e is not None)
        raw_index, item = resolve(handle_str, kind, entries, lambda it: hash_text(it.text))
        body, project_name = linesmod.split_project_link(strip_returned_text(item.text))
        return Located("line", relpath, raw_index, item, body, lf, project=project_name)

    if kind.startswith("project:"):
        stem = kind.split(":", 1)[1]
        page, relpath = _find_loaded_project(vault, stem)
        entries = enumerate_logical(page.items, lambda i: True)
        raw_index, item = resolve(handle_str, kind, entries, lambda it: hash_text(it.text))
        return Located("project", relpath, raw_index, item, item.text, page)

    raise Stale(handle_str, f"unknown handle kind {kind!r}")


def _remove(loc: Located) -> None:
    if loc.domain == "table":
        loc.container.entries.pop(loc.raw_index)
    elif loc.domain == "line":
        loc.container.order.pop(loc.raw_index)
    else:
        page = loc.container
        del page.lines[loc.obj.line_index]
        if loc.obj.line_index < page.section_end:
            page.section_end -= 1


# --- capture and simple table appends ---


def capture(vault: Vault, today: date, *, text: str) -> OpResult:
    inbox = _require(vault, "Inbox.md", vault.inbox)
    text_v = text.strip()
    if not text_v:
        raise OpError("capture: text must not be blank")
    inbox.order.append(Item(marker="", stamp_raw=None, stamp=None, text=text_v, line_index=-1))
    mark_dirty(vault, "Inbox.md")
    return OpResult(f'Captured "{text_v}" to Inbox.md', set(vault.dirty))


def add_next_action(
    vault: Vault, today: date, *, action: str, project: str | None = None, deadline: str | None = None, priority=None
) -> OpResult:
    table = _require(vault, "Next actions.md", vault.next_actions)
    action_v = action.strip()
    if not action_v:
        raise OpError("add_next_action: action must not be blank")
    project_cell = f"[[{resolve_project_stem(vault, project)}]]" if project else ""
    cells = [action_v, project_cell, _validate_date(deadline, "deadline"), _validate_priority(priority)]
    table.entries.append(RowEntry(cells=cells, source_line=None))
    mark_dirty(vault, "Next actions.md")
    return OpResult(f'Added next action "{action_v}"', set(vault.dirty))


def delegate(
    vault: Vault, today: date, *, thing: str, person: str, chase_by: str | None = None, priority=None,
    project: str | None = None,
) -> OpResult:
    table = _require(vault, "Delegated.md", vault.delegated)
    thing_v, person_v = thing.strip(), person.strip()
    if not thing_v or not person_v:
        raise OpError("delegate: thing and person must not be blank")
    chase_v, warning = _chase_by_or_default(chase_by, today)
    project_cell = f"[[{resolve_project_stem(vault, project)}]]" if project else ""
    cells = new_row(
        table,
        {"Thing": thing_v, "Person": person_v, "Chase by": chase_v, "Priority": _validate_priority(priority), "Project": project_cell},
    )
    table.entries.append(RowEntry(cells=cells, source_line=None))
    mark_dirty(vault, "Delegated.md")
    return OpResult(f'Delegated "{thing_v}" to {person_v}', set(vault.dirty), warning)


def schedule(
    vault: Vault, today: date, *, thing: str, date: str, status: str | None = None, event: str | None = None,
    project: str | None = None,
) -> OpResult:
    table = _require(vault, "Scheduled.md", vault.scheduled)
    thing_v = thing.strip()
    if not thing_v:
        raise OpError("schedule: thing must not be blank")
    date_v = _validate_date(date, "date")
    if not date_v:
        raise OpError("schedule: date is required")
    project_cell = f"[[{resolve_project_stem(vault, project)}]]" if project else ""
    cells = new_row(
        table,
        {"Thing": thing_v, "Date": date_v, "Status": _validate_status(status), "Event": (event or "").strip(), "Project": project_cell},
    )
    table.entries.append(RowEntry(cells=cells, source_line=None))
    mark_dirty(vault, "Scheduled.md")
    return OpResult(f'Scheduled "{thing_v}" for {date_v}', set(vault.dirty))


def add_to_tickler(vault: Vault, today: date, *, bucket: str, text: str, project: str | None = None) -> OpResult:
    relpath = _BUCKET_FILES.get(bucket)
    if relpath is None:
        raise OpError(f"tickler bucket {bucket!r} is not one of {sorted(_BUCKET_FILES)}")
    lf = _require(vault, relpath, vault.ticklers.get(relpath))
    text_v = text.strip()
    if not text_v:
        raise OpError("add_to_tickler: text must not be blank")
    final_text = text_v
    if project:
        final_text = linesmod.format_with_project(text_v, resolve_project_stem(vault, project))
    lf.order.append(Item(marker="", stamp_raw=None, stamp=None, text=final_text, line_index=-1))
    mark_dirty(vault, relpath)
    return OpResult(f'Added "{text_v}" to Tickler/{bucket}', set(vault.dirty))


# --- triage, complete, update, delete ---


def triage(vault: Vault, today: date, *, handle: str, to: str, text: str | None = None, **fields) -> OpResult:
    loc = _locate(vault, handle)
    if text is not None:
        text = text.strip()
        if not text:
            raise OpError("triage: text must not be blank")
    else:
        text = loc.text
    source_priority = loc.row_priority
    source_project = loc.project
    payload: dict = {}

    def _carried_priority(target_table) -> str | int | None:
        priority = fields.get("priority")
        if priority is None and source_priority and "priority" in target_table.columns:
            priority = source_priority
        return priority

    def _carried_project() -> str | None:
        project = fields.get("project")
        if project is None and source_project:
            project = source_project
        return project

    if to == "trash":
        pass  # nothing to write; falls through to the removal below
    elif to == "next-actions":
        table = _require(vault, "Next actions.md", vault.next_actions)
        project_value = _carried_project()
        project_cell = f"[[{resolve_project_stem(vault, project_value)}]]" if project_value else ""
        cells = new_row(
            table,
            {
                "Action": text,
                "Project": project_cell,
                "Deadline": _validate_date(fields.get("deadline"), "deadline"),
                "Priority": _validate_priority(_carried_priority(table)),
            },
        )
        table.entries.append(RowEntry(cells=cells, source_line=None))
        mark_dirty(vault, "Next actions.md")
    elif to == "delegated":
        table = _require(vault, "Delegated.md", vault.delegated)
        person = fields.get("person")
        if not person:
            raise OpError("triage to delegated: person is required")
        chase_v, chase_payload = _chase_by_or_default(fields.get("chase_by"), today)
        payload.update(chase_payload)
        project_value = _carried_project()
        project_cell = f"[[{resolve_project_stem(vault, project_value)}]]" if project_value else ""
        cells = new_row(
            table,
            {
                "Thing": text,
                "Person": person,
                "Chase by": chase_v,
                "Priority": _validate_priority(_carried_priority(table)),
                "Project": project_cell,
            },
        )
        table.entries.append(RowEntry(cells=cells, source_line=None))
        mark_dirty(vault, "Delegated.md")
    elif to == "scheduled":
        table = _require(vault, "Scheduled.md", vault.scheduled)
        date_v = _validate_date(fields.get("date"), "date")
        if not date_v:
            raise OpError("triage to scheduled: date is required")
        project_value = _carried_project()
        project_cell = f"[[{resolve_project_stem(vault, project_value)}]]" if project_value else ""
        cells = new_row(
            table,
            {
                "Thing": text,
                "Date": date_v,
                "Status": _validate_status(fields.get("status")),
                "Event": (fields.get("event") or "").strip(),
                "Project": project_cell,
            },
        )
        table.entries.append(RowEntry(cells=cells, source_line=None))
        mark_dirty(vault, "Scheduled.md")
    elif to == "inbox":
        inbox = _require(vault, "Inbox.md", vault.inbox)
        inbox.order.append(Item(marker="", stamp_raw=None, stamp=None, text=text, line_index=-1))
        mark_dirty(vault, "Inbox.md")
    elif to.startswith("tickler:"):
        bucket = to.split(":", 1)[1]
        relpath = _BUCKET_FILES.get(bucket)
        if relpath is None:
            raise OpError(f"triage: tickler bucket {bucket!r} is not one of {sorted(_BUCKET_FILES)}")
        lf = _require(vault, relpath, vault.ticklers.get(relpath))
        project_value = _carried_project()
        final_text = text
        if project_value:
            final_text = linesmod.format_with_project(text, resolve_project_stem(vault, project_value))
        lf.order.append(Item(marker="", stamp_raw=None, stamp=None, text=final_text, line_index=-1))
        mark_dirty(vault, relpath)
    elif to.startswith("project:"):
        stem = to.split(":", 1)[1]
        page, page_relpath = _find_loaded_project(vault, stem)
        _add_item_to_project(page, text)
        mark_dirty(vault, page_relpath)
    else:
        raise OpError(f"triage: unknown destination {to!r}")

    _remove(loc)
    mark_dirty(vault, loc.relpath)
    return OpResult(f'Triaged "{text}" to {to}', set(vault.dirty), payload)


def complete(vault: Vault, today: date, *, handle: str) -> OpResult:
    loc = _locate(vault, handle)
    payload: dict = {}

    if loc.domain in ("table", "line") and loc.project:
        ticked = False
        link = projectsmod.resolve_link(loc.project, vault.project_index)
        if link.resolved is not None:
            page_relpath = str(link.resolved.relative_to(vault.root))
            page = vault.projects.get(page_relpath)
            if page is not None:
                for item in page.items:
                    if not item.checked and item.text == loc.text:
                        _tick_item(page, item)
                        mark_dirty(vault, page_relpath)
                        ticked = True
                        break
        if not ticked:
            payload["no_matching_item"] = True
        _remove(loc)
        mark_dirty(vault, loc.relpath)
        return OpResult(f'Completed "{loc.text}"', set(vault.dirty), payload)

    if loc.domain == "project":
        page = loc.container
        item = loc.obj
        _tick_item(page, item)
        mark_dirty(vault, loc.relpath)
        # Sweep every view that surfaces this item (FORMAT.md §6): a row or
        # line linking this page whose text equals the item's text.
        removed = 0
        removed_other = 0
        tables = (
            ("Next actions.md", vault.next_actions),
            ("Delegated.md", vault.delegated),
            ("Scheduled.md", vault.scheduled),
        )
        for table_name, table in tables:
            if table is None or not table.has("project"):
                continue
            keep = []
            for entry in table.entries:
                link_name = projectsmod.parse_link(table.cell(entry.cells, "project"))
                if link_name and entry.cells[0].strip() == item.text:
                    link = projectsmod.resolve_link(link_name, vault.project_index)
                    if link.resolved == page.path:
                        if table_name == "Next actions.md":
                            removed += 1
                        else:
                            removed_other += 1
                        mark_dirty(vault, table_name)
                        continue
                keep.append(entry)
            table.entries = keep
        for relpath, lf in vault.ticklers.items():
            keep_order = []
            for entry in lf.order:
                if entry is not None:
                    body, link_name = linesmod.split_project_link(entry.text)
                    if link_name and body.strip() == item.text:
                        link = projectsmod.resolve_link(link_name, vault.project_index)
                        if link.resolved == page.path:
                            removed_other += 1
                            mark_dirty(vault, relpath)
                            continue
                keep_order.append(entry)
            lf.order = keep_order
        payload["removed_other_rows"] = removed_other
        payload["removed_next_action_rows"] = removed
        return OpResult(f'Completed "{loc.text}"', set(vault.dirty), payload)

    _remove(loc)
    mark_dirty(vault, loc.relpath)
    return OpResult(f'Completed "{loc.text}"', set(vault.dirty), payload)


_TABLE_FIELDS = {
    "Next actions.md": {"action": "Action", "project": "Project", "deadline": "Deadline", "priority": "Priority"},
    "Delegated.md": {
        "thing": "Thing", "person": "Person", "chase_by": "Chase by", "priority": "Priority", "project": "Project",
    },
    "Scheduled.md": {
        "thing": "Thing", "date": "Date", "status": "Status", "event": "Event", "project": "Project",
    },
}
_DATE_FIELDS = {"deadline", "chase_by", "date"}


def update(vault: Vault, today: date, *, handle: str, **fields) -> OpResult:
    loc = _locate(vault, handle)

    if loc.domain == "table":
        colmap = _TABLE_FIELDS[loc.relpath]
        unknown = set(fields) - set(colmap)
        if unknown:
            raise OpError(f"update: {loc.relpath} has no field(s) {sorted(unknown)}")
        cells = list(loc.obj.cells)
        payload: dict = {}
        for field_name, value in fields.items():
            col_name = colmap[field_name]
            if field_name == "project":
                if not loc.container.has(col_name):
                    if not value:
                        continue  # blanking an absent optional column is a no-op (§4.2)
                    loc.container.ensure_column(col_name)
                    cells.append("")  # mirror the padding ensure_column just gave every row
                idx = loc.container.col(col_name)
                cells[idx] = f"[[{resolve_project_stem(vault, value)}]]" if value else ""
                continue
            idx = loc.container.col(col_name)
            if field_name == "chase_by" and loc.relpath == "Delegated.md":
                cells[idx], chase_payload = _chase_by_or_default(value, today)
                payload.update(chase_payload)
            elif field_name in _DATE_FIELDS:
                cells[idx] = _validate_date(value, field_name)
            elif field_name == "priority":
                cells[idx] = _validate_priority(value)
            elif field_name == "status":
                cells[idx] = _validate_status(value)
            else:
                cells[idx] = "" if value is None else str(value).strip()
        loc.obj.cells = cells
        loc.obj.source_line = None
        mark_dirty(vault, loc.relpath)
        return OpResult(f'Updated "{loc.text}"', set(vault.dirty), payload)

    if set(fields) != {"text"}:
        raise OpError("update: line items and project items only take a 'text' field")
    new_text = str(fields["text"]).strip()
    if not new_text:
        raise OpError("update: text must not be blank")
    if loc.domain == "line":
        loc.obj.text = new_text
    else:
        loc.container.lines[loc.obj.line_index] = f"{loc.obj.marker} [{'x' if loc.obj.checked else ' '}] {new_text}"
        loc.obj.text = new_text
    mark_dirty(vault, loc.relpath)
    return OpResult(f'Updated "{loc.text}" -> "{new_text}"', set(vault.dirty))


def delete(vault: Vault, today: date, *, handle: str) -> OpResult:
    loc = _locate(vault, handle)
    _remove(loc)
    mark_dirty(vault, loc.relpath)
    return OpResult(f'Deleted "{loc.text}"', set(vault.dirty))


# --- projects ---

_GOAL_HEADING = re.compile(r"^#\s+Goal\s*$")


def create_project(
    vault: Vault, today: date, *, name: str, goal: str, first_action: str = "", priority=None
) -> OpResult:
    """Create a project page from the template.

    ``first_action`` seeds the project's Next Actions section and gets a row
    in ``Next actions.md``. It is deliberately NOT defaulted to a stock
    placeholder: a caller that knows what the project is for should pass
    that text, and one that does not should leave the project without an
    action rather than have an invented one to delete. A project with no
    open action simply reads as STALLED, which is true and visible.
    """
    name_v = name.strip()
    if not name_v:
        raise OpError("create_project: name must not be blank")
    if name_v.lower() in vault.project_index:
        raise OpError(f"create_project: a project named {name_v!r} already exists")

    template_path = vault.root / "Project details" / "-Project template.md"
    if not template_path.is_file():
        raise OpError("create_project: Project details/-Project template.md is missing")
    raw = load_raw(template_path)
    lines = list(raw.lines)

    goal_idx = next((i for i, line in enumerate(lines) if _GOAL_HEADING.match(line.strip())), None)
    if goal_idx is None:
        raise OpError("create_project: template has no '# Goal' heading")
    goal_text = goal.strip()
    if goal_idx + 1 < len(lines) and lines[goal_idx + 1].strip() == "":
        lines[goal_idx + 1] = goal_text
    else:
        lines.insert(goal_idx + 1, goal_text)

    action_v = (first_action or "").strip()
    if action_v:
        lines = _append_or_fill_item(lines, action_v)

    target_path = vault.root / "Project details" / f"{name_v}.md"
    write_raw(RawFile(target_path, lines, raw.trailing_newline))

    # Register the page with the loaded vault, so a later op in the same run
    # (create_project then add_project_action, which is exactly what the
    # reMarkable AI agent writes) can resolve the project it just asked for.
    vault.project_index.setdefault(name_v.lower(), []).append(target_path)
    try:
        page = projectsmod.load_project(target_path)
    except ParseFailure:
        page = None
    if page is not None:
        vault.projects[f"Project details/{name_v}.md"] = page

    if action_v:
        table = _require(vault, "Next actions.md", vault.next_actions)
        cells = [action_v, f"[[{name_v}]]", "", _validate_priority(priority)]
        table.entries.append(RowEntry(cells=cells, source_line=None))
        mark_dirty(vault, "Next actions.md")

    changed = set(vault.dirty) | {f"Project details/{name_v}.md"}
    return OpResult(f'Created project "{name_v}"', changed, {"path": f"Project details/{name_v}.md"})


def add_project_action(vault: Vault, today: date, *, project: str, text: str) -> OpResult:
    stem = resolve_project_stem(vault, project)
    page, relpath = _find_loaded_project(vault, stem)
    text_v = text.strip()
    if not text_v:
        raise OpError("add_project_action: text must not be blank")
    _add_item_to_project(page, text_v)
    mark_dirty(vault, relpath)
    return OpResult(f'Added "{text_v}" to {stem}', set(vault.dirty))


def tick_project_action(vault: Vault, today: date, *, handle: str) -> OpResult:
    loc = _locate(vault, handle)
    if loc.domain != "project":
        raise OpError("tick_project_action: handle is not a project item")
    _tick_item(loc.container, loc.obj)
    mark_dirty(vault, loc.relpath)
    return OpResult(f'Ticked "{loc.text}"', set(vault.dirty))


def archive_project(vault: Vault, today: date, *, name: str) -> OpResult:
    stem_lookup = name.strip()
    candidates = vault.project_index.get(stem_lookup.lower(), [])
    non_done = [p for p in candidates if "Done" not in p.parts]
    if not non_done:
        if candidates:
            raise OpError(f"archive_project: {stem_lookup!r} is already under Project details/Done/")
        raise OpError(f"archive_project: no project named {stem_lookup!r}")
    if len(non_done) > 1:
        raise OpError(f"archive_project: multiple projects named {stem_lookup!r}: {[str(p) for p in non_done]}")

    src_path = non_done[0]
    stem = src_path.stem
    src_relpath = str(src_path.relative_to(vault.root))
    done_dir = vault.root / "Project details" / "Done"
    done_dir.mkdir(parents=True, exist_ok=True)
    dst_path = done_dir / src_path.name
    if dst_path.exists():
        raise OpError(f"archive_project: {dst_path.relative_to(vault.root)} already exists")
    src_path.rename(dst_path)
    dst_relpath = str(dst_path.relative_to(vault.root))
    vault.projects.pop(src_relpath, None)

    removed = 0
    if vault.next_actions is not None:
        project_col = vault.next_actions.col("Project")
        keep = []
        for entry in vault.next_actions.entries:
            link_name = projectsmod.parse_link(entry.cells[project_col])
            if link_name and link_name.lower() == stem.lower():
                removed += 1
                mark_dirty(vault, "Next actions.md")
                continue
            keep.append(entry)
        vault.next_actions.entries = keep

    removed_other = 0
    for table_name, table in (("Delegated.md", vault.delegated), ("Scheduled.md", vault.scheduled)):
        if table is None or not table.has("project"):
            continue
        keep = []
        for entry in table.entries:
            if is_placeholder(entry.cells):
                keep.append(entry)
                continue
            link_name = projectsmod.parse_link(table.cell(entry.cells, "project"))
            if link_name and link_name.lower() == stem.lower():
                removed_other += 1
                mark_dirty(vault, table_name)
                continue
            keep.append(entry)
        table.entries = keep

    for relpath, lf in vault.ticklers.items():
        keep_order = []
        for entry in lf.order:
            if entry is None:
                keep_order.append(entry)
                continue
            _body, link_name = linesmod.split_project_link(entry.text)
            if link_name and link_name.lower() == stem.lower():
                removed_other += 1
                mark_dirty(vault, relpath)
                continue
            keep_order.append(entry)
        lf.order = keep_order

    changed = set(vault.dirty) | {src_relpath, dst_relpath}
    return OpResult(
        f'Archived project "{stem}"',
        changed,
        {"removed_next_action_rows": removed, "removed_other_rows": removed_other, "new_path": dst_relpath},
    )


def run_maintenance(vault: Vault, today: date) -> OpResult:
    from gtd_ci.jobs import expire, promote, sort, stamp
    from gtd_ci.jobs import lint as lint_job
    from gtd_ci.report import Report

    report = Report()
    for job in (expire, stamp, promote, sort):
        job.run(vault, today, report)
    lint_job.run(vault, today, report)

    findings = [{"severity": f.severity, "code": f.code, "file": f.file, "message": f.message} for f in report.findings]
    summary = f"{len(report.changes)} change(s)" if report.changes else "no changes"
    return OpResult(
        f"Maintenance run: {summary}", set(vault.dirty), {"changes": list(report.changes), "findings": findings}
    )
