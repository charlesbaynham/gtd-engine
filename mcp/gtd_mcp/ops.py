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
from gtd_ci import links as linksmod
from gtd_ci import reference as referencemod
from gtd_ci import names
from gtd_ci.names import NameRejected
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
    "append_project_note",
    "create_reference",
    "append_reference",
    "edit_reference",
    "rename_reference",
    "move_reference",
    "delete_reference",
    "set_project_goal",
    "star_project",
    "tick_project_action",
    "archive_project",
    "rename_project",
    "route_project_action",
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


def resolve_project_stem(vault: Vault, name: str) -> str:
    """Resolves a project name to its stem per FORMAT.md §6, or raises OpError
    with near-name suggestions (Levenshtein <= 3 over [a-z0-9]) when it can't."""
    link = projectsmod.resolve_link(name, vault.project_index)
    if link.resolved is not None and not link.resolved_in_done:
        return link.resolved.stem
    if link.resolved is not None and link.resolved_in_done:
        raise OpError(f"project {name!r} only exists under Project details/Done/")
    all_stems = sorted({p.stem for paths in vault.project_index.values() for p in paths})
    target = names.normalize(name)
    near = [s for s in all_stems if names.levenshtein(names.normalize(s), target) <= 3]
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

    return names.near_names(sorted({p.stem for paths in vault.project_index.values() for p in paths if "Done" not in p.parts}), name)


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
    projectsmod.reparse(page)


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
        projectsmod.reparse(page)


def _surfacing_entries(vault: Vault, page: projectsmod.ProjectPage, text: str):
    """Every view entry surfacing the item `text` of `page` (FORMAT.md §6): a
    row or tickler line linking this page whose Action/Thing/body equals the
    text. Yields `(relpath, container, entry)`; `container` is the table or
    the tickler LineFile."""
    tables = (
        ("Next actions.md", vault.next_actions),
        ("Delegated.md", vault.delegated),
        ("Scheduled.md", vault.scheduled),
    )
    for table_name, table in tables:
        if table is None or not table.has("project"):
            continue
        for entry in table.entries:
            link_name = projectsmod.parse_link(table.cell(entry.cells, "project"))
            if link_name and entry.cells[0].strip() == text:
                if projectsmod.resolve_link(link_name, vault.project_index).resolved == page.path:
                    yield table_name, table, entry
    for relpath, lf in vault.ticklers.items():
        for entry in lf.order:
            if entry is None:
                continue
            body, link_name = linesmod.split_project_link(entry.text)
            if link_name and body.strip() == text:
                if projectsmod.resolve_link(link_name, vault.project_index).resolved == page.path:
                    yield relpath, lf, entry


def _sweep_surfacing(vault: Vault, page: projectsmod.ProjectPage, text: str) -> tuple[int, int]:
    """Remove every view entry surfacing `text` on `page`. Returns
    `(removed Next actions rows, removed other rows/lines)`."""
    return _drop_entries(vault, list(_surfacing_entries(vault, page, text)))


def _drop_entries(vault: Vault, found: list) -> tuple[int, int]:
    removed = removed_other = 0
    for relpath, container, entry in found:
        if relpath == "Next actions.md":
            removed += 1
        else:
            removed_other += 1
        if hasattr(container, "entries"):
            container.entries = [e for e in container.entries if e is not entry]
        else:
            container.order = [e for e in container.order if e is not entry]
        mark_dirty(vault, relpath)
    return removed, removed_other


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
    elif to.startswith("reference:"):
        name = to.split(":", 1)[1]
        note = find_reference(vault, name)
        created = note is None
        if created:
            _append_prose([], "triage", text, DEFAULT_NOTE_HEADING, True, today, fences=True)  # fail before anything exists
            folder, _, stem = name.strip().removesuffix(".md").rpartition("/")
            _, near = names.near_names([n.stem for n in vault.reference_notes().values()], stem)
            if near:
                payload["did_you_mean"] = near
            note = _new_reference(vault, "triage", stem, folder, "")
        note.lines, _ = _append_prose(note.lines, "triage", text, DEFAULT_NOTE_HEADING, True, today, fences=True)
        note.trailing_newline = True
        mark_dirty(vault, note.relpath)
        payload.update(reference=note.relpath, reference_created=created)
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
        removed, removed_other = _sweep_surfacing(vault, page, item.text)
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
        # Re-word the rows surfacing this item too, or the rename would turn
        # every one of them into a row-mismatch (§6).
        for relpath, container, entry in list(_surfacing_entries(vault, loc.container, loc.obj.text)):
            if hasattr(container, "entries"):
                entry.cells[0] = new_text
                entry.source_line = None
            else:
                entry.text = linesmod.format_with_project(new_text, _trailing_link_name(entry.text))
            mark_dirty(vault, relpath)
        loc.container.lines[loc.obj.line_index] = f"{loc.obj.marker} [{'x' if loc.obj.checked else ' '}] {new_text}"
        loc.obj.text = new_text
    mark_dirty(vault, loc.relpath)
    return OpResult(f'Updated "{loc.text}" -> "{new_text}"', set(vault.dirty))


def _trailing_link_name(text: str) -> str | None:
    """The project name of a line's trailing `[[link]]`, or None."""
    return linesmod.split_project_link(text)[1]


def delete(vault: Vault, today: date, *, handle: str) -> OpResult:
    """Remove an item. Deleting a project's action also removes every row
    surfacing it (§6) — the step is gone, so its view rows would only be
    reported as mismatches."""
    loc = _locate(vault, handle)
    payload: dict = {}
    if loc.domain == "project":
        removed, removed_other = _sweep_surfacing(vault, loc.container, loc.obj.text)
        payload = {"removed_next_action_rows": removed, "removed_other_rows": removed_other}
    _remove(loc)
    mark_dirty(vault, loc.relpath)
    return OpResult(f'Deleted "{loc.text}"', set(vault.dirty), payload)


ROUTE_TARGETS = ("next-actions", "delegated", "scheduled", "tickler")


def route_project_action(
    vault: Vault,
    today: date,
    *,
    handle: str,
    to: str,
    person: str | None = None,
    chase_by: str | None = None,
    date: str | None = None,
    deadline: str | None = None,
    priority=None,
    bucket: str | None = None,
) -> OpResult:
    """Surface a project's action in a different view, keeping it on the page.

    A project step is an ordinary action: it can be on my plate, waiting on
    someone, tied to a date or parked in the tickler. The checkbox on the
    project page stays where it is (the page is the plan); what changes is
    the row that surfaces it (§6). Every existing surfacing row is removed
    and one is written to `to` (`next-actions`, `delegated`, `scheduled` or
    `tickler` with `bucket`), linked back to the project so ticking it off
    there ticks the checkbox too. A priority the old row carried moves with
    it unless one is given.
    """
    loc = _locate(vault, handle)
    if loc.domain != "project":
        raise OpError("route_project_action: handle is not a project item")
    page, item = loc.container, loc.obj
    if item.checked:
        raise OpError(f"route_project_action: {item.text!r} is already ticked off")
    if to not in ROUTE_TARGETS:
        raise OpError(f"route_project_action: 'to' must be one of {list(ROUTE_TARGETS)}, not {to!r}")

    old = list(_surfacing_entries(vault, page, item.text))
    if priority is None:
        for _relpath, container, entry in old:
            if hasattr(container, "entries") and container.cell(entry.cells, "priority"):
                priority = container.cell(entry.cells, "priority")
                break

    # Write the new row before the old ones go, so a bad argument (a missing
    # person, an unreadable date) never leaves the step unsurfaced.
    stem = page.stem
    if to == "next-actions":
        result = add_next_action(vault, today, action=item.text, project=stem, deadline=deadline, priority=priority)
    elif to == "delegated":
        if not (person or "").strip():
            raise OpError("route_project_action: delegating needs a person")
        result = delegate(
            vault, today, thing=item.text, person=person, chase_by=chase_by, priority=priority, project=stem,
        )
    elif to == "scheduled":
        if not date:
            raise OpError("route_project_action: scheduling needs a date")
        result = schedule(vault, today, thing=item.text, date=date, project=stem)
    else:
        result = add_to_tickler(vault, today, bucket=bucket or "Next week", text=item.text, project=stem)

    removed, removed_other = _drop_entries(vault, old)
    payload = dict(result.payload)
    payload.update({"removed_next_action_rows": removed, "removed_other_rows": removed_other})
    return OpResult(f'Routed "{item.text}" ({stem}) to {to}', set(vault.dirty), payload)


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
    _require_no_reference_stem(vault, "create_project", name_v)

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


_DATE_SUBHEADING = re.compile(r"^###\s+(\d{4}-\d{2}-\d{2})\s*$")
_BLOCK_HEADING = re.compile(r"^#{1,2}\s")

DEFAULT_NOTE_HEADING = "Notes"


def _last_date_subheading(lines: list[str], start: int, end: int) -> str | None:
    for i in range(end - 1, start - 1, -1):
        m = _DATE_SUBHEADING.match(lines[i].strip())
        if m:
            return m.group(1)
    return None


def _append_note(
    lines: list[str], heading: str, body: list[str], stamp: str | None, *, fences: bool = False
) -> tuple[list[str], bool]:
    """Append `body` to the level-2 section named `heading`, creating it at end
    of file if absent. Returns `(lines, created_section)`.

    A section is bounded by the next level-1/2 heading (FORMAT.md §6), so
    writing at the end of one — or adding a new one after the last — never
    reaches inside any other, `## Next Actions` included.
    """
    lines = list(lines)
    idx = projectsmod.find_heading(lines, heading, level=2, fences=fences)
    if idx is None:
        if lines and lines[-1].strip() != "":
            lines.append("")
        block = [f"## {heading}", ""]
        if stamp:
            block += [f"### {stamp}", ""]
        return lines + block + body, True

    end = projectsmod.section_bounds(lines, idx, fences=fences)
    insert_at = end
    while insert_at > idx + 1 and lines[insert_at - 1].strip() == "":
        insert_at -= 1

    block = [""]
    # One date subheading per day: a second note on the same day joins the one
    # already open rather than opening an identical sibling.
    if stamp and _last_date_subheading(lines, idx + 1, insert_at) != stamp:
        block += [f"### {stamp}", ""]
    lines[insert_at:insert_at] = block + body
    return lines, False


def append_project_note(
    vault: Vault,
    today: date,
    *,
    project: str,
    text: str,
    heading: str = DEFAULT_NOTE_HEADING,
    dated: bool = True,
) -> OpResult:
    """Append free prose to a project page, outside its Next Actions section.

    This is the write path for project *context* — a design decision, what was
    agreed in a conversation — as opposed to `add_project_action`, which is
    for work. `heading` names the level-2 section it lands in (`## Notes` by
    convention, §6); the section is created at end of file when absent.
    `dated` prefixes the note with a `### <today>` subheading, once per day.
    """
    stem = resolve_project_stem(vault, project)
    page, relpath = _find_loaded_project(vault, stem)
    page.lines, payload = _append_prose(
        page.lines, "append_project_note", text, heading, dated, today,
        reserved=("next actions", "'## Next Actions' holds actions, not notes; use add_project_action"),
    )
    projectsmod.reparse(page)
    mark_dirty(vault, relpath)
    return OpResult(
        f'Appended a note to "{stem}" under "## {payload["heading"]}"', set(vault.dirty), payload
    )


def _append_prose(
    lines: list[str], op: str, text: str, heading: str, dated: bool, today: date,
    reserved: tuple[str, str] | None = None, fences: bool = False,
) -> tuple[list[str], dict]:
    """The shared validation and placement for appending prose to a project
    page or a reference note: `(new lines, {heading, created_section, dated})`."""
    heading_v = heading.strip().lstrip("#").strip()
    if not heading_v:
        raise OpError(f"{op}: heading must not be blank")
    if "\n" in heading_v:
        raise OpError(f"{op}: heading must be a single line")
    if reserved and heading_v.lower() == reserved[0]:
        raise OpError(f"{op}: {reserved[1]}")

    body = [line.rstrip() for line in text.strip("\n").split("\n")]
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    if not body:
        raise OpError(f"{op}: text must not be blank")
    for line in body:
        if _BLOCK_HEADING.match(line.strip()):
            raise OpError(
                f"{op}: note text may not contain a level-1/2 heading ({line.strip()!r}) — "
                "it would end the section it is written into; use ### or deeper"
            )

    stamp = today.isoformat() if dated else None
    new_lines, created = _append_note(lines, heading_v, body, stamp, fences=fences)
    return new_lines, {"heading": heading_v, "created_section": created, "dated": stamp}


def set_project_goal(vault: Vault, today: date, *, project: str, goal: str) -> OpResult:
    """Rewrite the paragraph under `# Goal` (§6), so a goal set at creation
    can be corrected later."""
    stem = resolve_project_stem(vault, project)
    page, relpath = _find_loaded_project(vault, stem)

    body = [line.rstrip() for line in goal.strip("\n").split("\n")]
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    if not body:
        raise OpError("set_project_goal: goal must not be blank")
    for line in body:
        if line.strip().startswith("#"):
            raise OpError(f"set_project_goal: goal may not contain a heading ({line.strip()!r})")

    bounds = projectsmod.goal_paragraph(page.lines)
    if bounds is None:
        raise OpError(f"set_project_goal: {relpath} has no '# Goal' heading")
    start, end = bounds
    previous = "\n".join(page.lines[start:end]).strip() or None
    # An empty Goal section inserts before whatever follows, so the new
    # paragraph needs its own blank line to stay a paragraph.
    separator = [""] if start == end and start < len(page.lines) else []
    page.lines[start:end] = body + separator
    projectsmod.reparse(page)
    mark_dirty(vault, relpath)
    return OpResult(
        f'Set the goal of "{stem}"',
        set(vault.dirty),
        {"previous_goal": previous, "goal": "\n".join(body)},
    )


def star_project(vault: Vault, today: date, *, project: str, starred: bool = True) -> OpResult:
    """Star or unstar a project (§6: `starred: true` in its front matter).
    A starred project sorts first and sits in the hotbar of the reMarkable
    sheet. Setting the state it already has changes nothing."""
    stem = resolve_project_stem(vault, project)
    page, relpath = _find_loaded_project(vault, stem)
    was = projectsmod.is_starred(page.lines)
    verb = "Starred" if starred else "Unstarred"
    if was == starred:
        return OpResult(f'"{stem}" is already {verb.lower()}', set(), {"starred": starred, "changed": False})
    try:
        page.lines[:] = projectsmod.set_starred(page.lines, starred)
    except ParseFailure as exc:
        raise OpError(f"star_project: {relpath}: {exc}") from exc
    projectsmod.reparse(page)
    mark_dirty(vault, relpath)
    return OpResult(f'{verb} "{stem}"', set(vault.dirty), {"starred": starred, "changed": True})


def _usable_file_name(op: str, label: str, value: str) -> str:
    try:
        return names.usable_file_name(label, value)
    except NameRejected as exc:
        raise OpError(f"{op}: {exc}") from exc


def rename_project(vault: Vault, today: date, *, project: str, new_name: str) -> OpResult:
    """Rename a project page and every link to it.

    The file moves to `<same folder>/<new_name>.md`, and every `[[old]]` link
    in the managed files — Next actions/Delegated/Scheduled cells, Inbox and
    Tickler lines, the prose of every active project page and every
    Reference/ note — is rewritten to `[[new_name]]` (alias and heading
    kept), the way Obsidian does on a rename. Links in files automation does
    not manage (Done/ pages) are left alone and counted in the payload as
    `unmanaged_links`.
    """
    stem = resolve_project_stem(vault, project)
    page, relpath = _find_loaded_project(vault, stem)
    new_v = _usable_file_name("rename_project", "new_name", new_name)
    if new_v == stem:
        raise OpError(f"rename_project: the project is already called {stem!r}")
    if new_v.lower() != stem.lower() and new_v.lower() in vault.project_index:
        raise OpError(f"rename_project: a project named {new_v!r} already exists")
    _require_no_reference_stem(vault, "rename_project", new_v)

    old_path = page.path
    new_path = old_path.with_name(f"{new_v}.md")
    new_relpath = str(new_path.relative_to(vault.root))

    rewritten = linksmod.rewrite_everywhere(vault, linksmod.Retarget(stem, new_v), done=False).count

    unmanaged = 0
    managed = {str(p.path) for p in vault.projects.values()} | {str(n.path) for n in vault.reference_notes().values()}
    link_re = linksmod.link_pattern(stem)
    for md in vault.root.rglob("*.md"):
        rel = md.relative_to(vault.root)
        if rel.parts[0].startswith(".") or str(md) in managed or rel.parent == Path(".") or rel.parts[0] == "Tickler":
            continue
        try:
            unmanaged += len(link_re.findall(md.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError):
            continue

    # Move the page: write it (with any edits made earlier in this run) to
    # the new path, drop the old file, and re-key the loaded vault so later
    # ops in the same run find it under its new name.
    if new_v.lower() == stem.lower():
        # A case-only rename: on a case-insensitive filesystem the two paths
        # are one file, so rename it rather than write-then-unlink.
        write_raw(RawFile(old_path, page.lines, page.trailing_newline))
        old_path.rename(new_path)
    else:
        write_raw(RawFile(new_path, page.lines, page.trailing_newline))
        old_path.unlink()
    page.path = new_path
    page.stem = new_v
    vault.projects.pop(relpath)
    vault.projects[new_relpath] = page
    vault.dirty.discard(relpath)
    mark_dirty(vault, new_relpath)
    paths = [p for p in vault.project_index.get(stem.lower(), []) if p != old_path]
    if paths:
        vault.project_index[stem.lower()] = paths
    else:
        vault.project_index.pop(stem.lower(), None)
    vault.project_index.setdefault(new_v.lower(), []).append(new_path)

    changed = set(vault.dirty) | {relpath, new_relpath}
    return OpResult(
        f'Renamed project "{stem}" to "{new_v}"',
        changed,
        {"old_path": relpath, "new_path": new_relpath, "rewritten_links": rewritten, "unmanaged_links": unmanaged},
    )


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
    page = vault.projects.pop(src_relpath, None)
    if page is not None and src_relpath in vault.dirty:
        # Edits made earlier in the same run (a step ticked off on the sheet
        # before the project itself was) travel with the page.
        write_raw(RawFile(src_path, page.lines, page.trailing_newline))
    vault.dirty.discard(src_relpath)

    # Done/ is a graveyard of every project ever finished, so a name can
    # recur (a yearly "2026 UROP"), or the page may already have been copied
    # there by hand. Neither is a reason to leave a finished project active.
    collision = None
    dst_path = done_dir / src_path.name
    if dst_path.exists():
        if dst_path.read_bytes() == src_path.read_bytes():
            collision = "identical"
        else:
            collision = "renamed"
            dst_path = done_dir / f"{stem} ({today.isoformat()}).md"
            n = 2
            while dst_path.exists():
                dst_path = done_dir / f"{stem} ({today.isoformat()}) {n}.md"
                n += 1
    if collision == "identical":
        src_path.unlink()
    else:
        src_path.rename(dst_path)
    dst_relpath = str(dst_path.relative_to(vault.root))
    others = [p for p in vault.project_index.get(stem.lower(), []) if p != src_path]
    if collision == "renamed":
        vault.project_index[stem.lower()] = others
        vault.project_index.setdefault(dst_path.stem.lower(), []).append(dst_path)
    elif collision == "identical":
        vault.project_index[stem.lower()] = others
    else:
        vault.project_index[stem.lower()] = others + [dst_path]

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
        {"removed_next_action_rows": removed, "removed_other_rows": removed_other, "new_path": dst_relpath}
        | ({"done_name_collision": collision} if collision else {}),
    )


# --- reference notes (FORMAT.md §11) -------------------------------------------------------


def find_reference(vault: Vault, name: str) -> referencemod.ReferenceNote | None:
    try:
        return referencemod.find_note(vault.reference_notes(), name)
    except NameRejected as exc:
        raise OpError(str(exc)) from exc


def resolve_reference(vault: Vault, name: str) -> referencemod.ReferenceNote:
    try:
        return referencemod.resolve_note(vault.reference_notes(), name)
    except NameRejected as exc:
        raise OpError(str(exc)) from exc


def _reference_folder(op: str, folder: str | None) -> str:
    try:
        return referencemod.normalise_folder(folder)
    except NameRejected as exc:
        raise OpError(f"{op}: {exc}") from exc


def _reference_relpath(stem: str, folder: str) -> str:
    return "/".join(p for p in (referencemod.REFERENCE_DIR, folder, f"{stem}.md") if p)


def _require_no_reference_stem(vault: Vault, op: str, stem: str, *, ignoring: referencemod.ReferenceNote | None = None) -> None:
    """A link names only a stem, so a stem a reference note already answers
    to would make `[[stem]]` ambiguous."""
    clash = next((n for n in vault.reference_notes().values() if n is not ignoring and n.stem.lower() == stem.lower()), None)
    if clash is not None:
        raise OpError(f"{op}: {clash.relpath} already exists; a link to {stem!r} would be ambiguous")


def _require_free_stem(vault: Vault, op: str, stem: str, *, ignoring: referencemod.ReferenceNote | None = None) -> None:
    if stem.lower() in vault.project_index:
        raise OpError(f"{op}: a project is already called {stem!r}; a link to it would be ambiguous")
    _require_no_reference_stem(vault, op, stem, ignoring=ignoring)


def _titled(stem: str, text: str) -> str:
    """`text` as note content: LF endings, one trailing newline, and a
    `# <stem>` heading after any front matter unless it has its own."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff").rstrip()
    draft = referencemod.ReferenceNote(Path(), "", text.split("\n") if text else [], True)
    if not any(h["level"] == 1 for h in draft.headings):
        at = draft.body_start
        draft.lines[at:at] = [f"# {stem}", *([""] if draft.lines[at:] else [])]
    return draft.text


def _new_reference(vault: Vault, op: str, stem: str, folder: str, text: str) -> referencemod.ReferenceNote:
    stem = _usable_file_name(op, "name", stem.removesuffix(".md"))
    folder = _reference_folder(op, folder)
    _require_free_stem(vault, op, stem)
    relpath = _reference_relpath(stem, folder)
    path = vault.root / relpath
    if path.exists():
        raise OpError(f"{op}: {relpath} already exists")
    note = referencemod.ReferenceNote(path, relpath, [], True)
    note.set_text(_titled(stem, text))
    vault.reference_notes()[relpath] = note
    mark_dirty(vault, relpath)
    return note


def create_reference(vault: Vault, today: date, *, name: str, text: str, folder: str | None = None) -> OpResult:
    """Create `Reference/<folder>/<name>.md` with `text` written verbatim,
    headed `# <name>` when it has no level-1 heading of its own. Refuses a
    name that is taken, by a note or a project, or is not a usable file name."""
    note = _new_reference(vault, "create_reference", name, folder or "", text)
    return OpResult(f'Created reference note "{note.stem}"', set(vault.dirty), {"path": note.relpath})


def append_reference(
    vault: Vault,
    today: date,
    *,
    name: str,
    text: str,
    heading: str = DEFAULT_NOTE_HEADING,
    dated: bool = True,
) -> OpResult:
    """Append free prose to a reference note, exactly as `append_project_note`
    does to a project page: at the end of the level-2 section `heading`
    (created at end of file if absent), under one `### <today>` subheading
    per day when `dated`."""
    note = resolve_reference(vault, name)
    note.lines, payload = _append_prose(note.lines, "append_reference", text, heading, dated, today, fences=True)
    note.trailing_newline = True
    mark_dirty(vault, note.relpath)
    return OpResult(
        f'Appended a note to reference "{note.stem}" under "## {payload["heading"]}"',
        set(vault.dirty),
        {"path": note.relpath, **payload},
    )


def edit_reference(vault: Vault, today: date, *, name: str, old: str, new: str) -> OpResult:
    """Replace the one occurrence of the exact text `old` in a reference note
    with `new`. No occurrence, or several (overlapping ones count), is refused
    and nothing changes."""
    note = resolve_reference(vault, name)
    old, new = (t.replace("\r\n", "\n") for t in (old, new))
    if not old:
        raise OpError("edit_reference: old must not be empty")
    if old == new:
        raise OpError("edit_reference: old and new are identical")
    text = note.text
    count = len(re.findall(f"(?={re.escape(old)})", text))
    if count == 0:
        raise OpError(f"edit_reference: the text to replace was not found in {note.relpath}")
    if count > 1:
        raise OpError(
            f"edit_reference: the text to replace occurs {count} times in {note.relpath}; "
            "quote more surrounding text so it matches once"
        )
    note.set_text(text.replace(old, new))
    mark_dirty(vault, note.relpath)
    return OpResult(f'Edited reference note "{note.stem}"', set(vault.dirty), {"replaced": True})


def _remove_empty_reference_dirs(vault: Vault, path: Path) -> None:
    base = vault.root / referencemod.REFERENCE_DIR
    for parent in path.parents:
        if parent == base or base not in parent.parents or any(parent.iterdir()):
            return
        parent.rmdir()


def _relocate_reference(
    vault: Vault, op: str, note: referencemod.ReferenceNote, stem: str, folder: str
) -> OpResult:
    old_stem, old_relpath, old_path = note.stem, note.relpath, note.path
    if old_stem.lower() in vault.project_index:
        raise OpError(f"{op}: a project is also called {old_stem!r}, so its links cannot be told apart from this note's")
    new_relpath = _reference_relpath(stem, folder)
    new_path = vault.root / new_relpath
    if new_path.exists() and not new_path.samefile(old_path):
        raise OpError(f"{op}: {new_relpath} already exists")

    shares_stem = any(n is not note and n.stem.lower() == old_stem.lower() for n in vault.reference_notes().values())
    moving = stem == old_stem

    def prefix_for(prefix: str) -> str:
        if not moving:
            return prefix
        top = f"{referencemod.REFERENCE_DIR}/" if prefix.lower().startswith(f"{referencemod.REFERENCE_DIR.lower()}/") else ""
        return top + (f"{folder}/" if folder else "")

    retarget = linksmod.Retarget(
        old_stem, stem, within=referencemod.reference_dir(note),
        bare=not (moving or shares_stem), prefix_for=prefix_for,
    )
    rewritten = linksmod.rewrite_everywhere(vault, retarget, done=True)
    write_raw(RawFile(old_path, note.lines, note.trailing_newline))
    new_path.parent.mkdir(parents=True, exist_ok=True)
    old_path.rename(new_path)
    _remove_empty_reference_dirs(vault, old_path)

    notes = vault.reference_notes()
    del notes[old_relpath]
    vault.dirty.discard(old_relpath)
    note.path, note.relpath = new_path, new_relpath
    notes[new_relpath] = note
    payload = {"old_path": old_relpath, "new_path": new_relpath, "rewritten_links": rewritten.count}
    if shares_stem:
        payload["ambiguous_links"] = rewritten.bare_left
    return OpResult(
        f'Moved reference note "{old_stem}" to {new_relpath}',
        set(vault.dirty) | {old_relpath, new_relpath} | rewritten.written_directly,
        payload,
    )


def rename_reference(vault: Vault, today: date, *, name: str, new_name: str) -> OpResult:
    """Rename a reference note in place and rewrite every link to it — in the
    tables, Inbox, Tickler, project pages (Done/ included) and other
    reference notes — keeping each link's alias, heading and folder prefix.
    If another note shares its stem, only links whose prefix names this
    note's folder are rewritten; the unprefixed ones cannot be attributed and
    are counted as `ambiguous_links`."""
    note = resolve_reference(vault, name)
    stem = _usable_file_name("rename_reference", "new_name", new_name.removesuffix(".md"))
    if stem == note.stem:
        raise OpError(f"rename_reference: the note is already called {stem!r}")
    _require_free_stem(vault, "rename_reference", stem, ignoring=note)
    return _relocate_reference(vault, "rename_reference", note, stem, note.folder)


def move_reference(vault: Vault, today: date, *, name: str, folder: str) -> OpResult:
    """Move a reference note to another subfolder of `Reference/` (an empty
    `folder` is `Reference/` itself). Links that spell out its old folder,
    such as `[[House/Name]]` or `[[Reference/House/Name]]`, are rewritten to
    the new one (`[[Electrics/Name]]`, bare `[[Name]]` for the top level); a
    bare `[[Name]]` still resolves and is left alone. If another note shares
    the stem, the bare links are counted as `ambiguous_links`."""
    note = resolve_reference(vault, name)
    target = _reference_folder("move_reference", folder)
    if target == note.folder:
        raise OpError(f"move_reference: {note.relpath} is already in that folder")
    return _relocate_reference(vault, "move_reference", note, note.stem, target)


def delete_reference(vault: Vault, today: date, *, name: str) -> OpResult:
    """Delete a reference note outright (git keeps the history). The payload
    carries a `warning` naming any files that still link to it."""
    note = resolve_reference(vault, name)
    linked = [
        b for b in linksmod.backlinks(vault, note.stem, within=referencemod.reference_dir(note))
        if b["path"] != note.relpath
    ]
    note.path.unlink()
    _remove_empty_reference_dirs(vault, note.path)
    del vault.reference_notes()[note.relpath]
    vault.dirty.discard(note.relpath)
    payload: dict = {"deleted": note.relpath}
    if linked:
        payload["linked_from"] = linked
        payload["warning"] = (
            f"{len(linked)} file(s) still link to [[{note.stem}]]: {', '.join(b['path'] for b in linked)}"
        )
    return OpResult(f'Deleted reference note "{note.stem}"', set(vault.dirty) | {note.relpath}, payload)


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
