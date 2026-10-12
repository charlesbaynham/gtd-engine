"""Typed, JSON-serialisable read models — the reading half of the server.

Reads never mutate the vault. Every item carries a handle (see `handles.py`)
computed fresh from current content, so a handle returned here is exactly
what a write tool needs.
"""
from __future__ import annotations

import mimetypes
import re
from pathlib import Path
from typing import Any

from gtd_ci import links as linksmod
from gtd_ci import lines as linesmod
from gtd_ci import projects as projectsmod
from gtd_ci.dates import format_date, parse_date
from gtd_ci.model import TICKLER_OFFSETS, Vault
from gtd_ci.reference import ReferenceNote
from gtd_ci.tables import ManagedTable, is_placeholder

from .ops import OpError, reference_folder, resolve_reference
from .handles import enumerate_logical, hash_cells, hash_text, make_handle

_INBOX_PROV = re.compile(r"^\[from ([^,\]]+)(?:, ([^\]]*))?\]\s*")


def date_field(raw: str) -> dict[str, Any]:
    raw = raw.strip()
    if not raw:
        return {"value": None, "raw": None, "bad_date": False}
    parsed = parse_date(raw)
    if parsed is None:
        return {"value": None, "raw": raw, "bad_date": True}
    return {"value": format_date(parsed.value), "raw": raw, "bad_date": not parsed.canonical}


def _priority_value(raw: str) -> int | None:
    raw = raw.strip()
    return int(raw) if raw.lstrip("-").isdigit() else None


def _resolve_project(vault: Vault, project_name: str | None) -> tuple[str | None, str | None]:
    """(project_stem, project_link_status) for a raw `[[name]]` link name,
    per FORMAT.md §6. status is one of None, 'ok', 'done', 'dangling'."""
    if not project_name:
        return None, None
    link = projectsmod.resolve_link(project_name, vault.project_index)
    if link.resolved is None:
        return project_name, "dangling"
    return link.resolved.stem, ("done" if link.resolved_in_done else "ok")


def next_actions(vault: Vault) -> list[dict[str, Any]]:
    table = vault.next_actions
    if table is None:
        return []
    out = []
    for logical, _raw, entry in enumerate_logical(table.entries, lambda e: not is_placeholder(e.cells)):
        cells = entry.cells
        project_name = projectsmod.parse_link(cells[table.col("project")])
        project_stem, project_link_status = _resolve_project(vault, project_name)
        out.append(
            {
                "handle": make_handle("next-actions", logical, hash_cells(cells)),
                "action": cells[table.col("action")].strip(),
                "project": project_stem,
                "project_link_status": project_link_status,
                "deadline": date_field(cells[table.col("deadline")]),
                "priority": _priority_value(cells[table.col("priority")]),
            }
        )
    return out


def delegated(vault: Vault) -> list[dict[str, Any]]:
    table = vault.delegated
    if table is None:
        return []
    out = []
    for logical, _raw, entry in enumerate_logical(table.entries, lambda e: not is_placeholder(e.cells)):
        cells = entry.cells
        project_name = projectsmod.parse_link(table.cell(cells, "project"))
        project_stem, project_link_status = _resolve_project(vault, project_name)
        out.append(
            {
                "handle": make_handle("delegated", logical, hash_cells(cells)),
                "thing": cells[table.col("thing")].strip(),
                "person": cells[table.col("person")].strip(),
                "chase_by": date_field(cells[table.col("chase by")]),
                "priority": _priority_value(cells[table.col("priority")]),
                "project": project_stem,
                "project_link_status": project_link_status,
            }
        )
    return out


def _event_url(cell: str) -> str | None:
    cell = cell.strip()
    if not cell:
        return None
    m = re.match(r"^\[[^\]]*\]\((.+)\)$", cell)
    return m.group(1) if m else cell


def scheduled(vault: Vault) -> list[dict[str, Any]]:
    table = vault.scheduled
    if table is None:
        return []
    out = []
    for logical, _raw, entry in enumerate_logical(table.entries, lambda e: not is_placeholder(e.cells)):
        cells = entry.cells
        status = cells[table.col("status")].strip().lower()
        project_name = projectsmod.parse_link(table.cell(cells, "project"))
        project_stem, project_link_status = _resolve_project(vault, project_name)
        out.append(
            {
                "handle": make_handle("scheduled", logical, hash_cells(cells)),
                "thing": cells[table.col("thing")].strip(),
                "date": date_field(cells[table.col("date")]),
                "status": status if status in {"to-schedule", "find-existing", "linked"} else None,
                "event_url": _event_url(cells[table.col("event")]),
                "project": project_stem,
                "project_link_status": project_link_status,
            }
        )
    return out


def inbox(vault: Vault) -> list[dict[str, Any]]:
    lf = vault.inbox
    if lf is None:
        return []
    out = []
    for logical, _raw, item in enumerate_logical(lf.order, lambda e: e is not None):
        m = _INBOX_PROV.match(item.text)
        from_ = None
        text = item.text
        if m:
            from_ = {"source": m.group(1), "detail": m.group(2)}
            text = item.text[m.end() :]
        body, project_name = linesmod.split_project_link(text)
        out.append(
            {
                "handle": make_handle("inbox", logical, hash_text(item.text)),
                "text": text.strip(),
                "from": from_,
                "project": project_name,
            }
        )
    return out


def tickler(vault: Vault, bucket: str | None = None) -> list[dict[str, Any]]:
    out = []
    for relpath, lf in vault.ticklers.items():
        name = relpath.split("/", 1)[1].removesuffix(".md")
        if bucket is not None and name != bucket:
            continue
        for logical, _raw, item in enumerate_logical(lf.order, lambda e: e is not None):
            _body, project_name = linesmod.split_project_link(item.text)
            out.append(
                {
                    "handle": make_handle(f"tickler:{name}", logical, hash_text(item.text)),
                    "bucket": name,
                    "due": date_field(item.stamp_raw) if item.stamp_raw is not None else date_field(""),
                    "text": item.text,
                    "project": project_name,
                }
            )
    return out


def project(
    vault: Vault, page: projectsmod.ProjectPage, relpath: str, include_body: bool = False
) -> dict[str, Any]:
    goal = None
    bounds = projectsmod.goal_paragraph(page.lines)
    if bounds is not None:
        start, end = bounds
        goal = "\n".join(line.strip() for line in page.lines[start:end]) or None

    status_lines: list[str] = []
    for i, line in enumerate(page.lines):
        if re.match(r"^##\s+Status\s*$", line.strip()):
            for cand in page.lines[i + 1 :]:
                stripped = cand.strip()
                if re.match(r"^#{1,2}\s", stripped):
                    break
                if stripped.startswith(("-", "*")):
                    status_lines.append(stripped[1:].strip())
            break

    matched, _mismatched = projectsmod.surfaced_items(vault, page.path)

    items = []
    next_action = None
    for logical, _raw, item in enumerate_logical(page.items, lambda i: True):
        views_for_item = sorted(matched.get(item.text, [])) if not item.checked else []
        items.append(
            {
                "handle": make_handle(f"project:{page.stem}", logical, hash_text(item.text)),
                "text": item.text,
                "done": item.checked,
                "surfaced": views_for_item[0] if views_for_item else None,
            }
        )
        if next_action is None and not item.checked:
            next_action = item.text

    row_handle = None
    if vault.next_actions is not None:
        for nx in next_actions(vault):
            if nx["project"] == page.stem:
                row_handle = nx["handle"]
                break

    surfaced_in = sorted(matched.get(next_action, [])) if next_action is not None else []

    return {
        "stem": page.stem,
        "path": relpath,
        "goal": goal,
        "starred": projectsmod.is_starred(page.lines),
        "status_lines": status_lines,
        "items": items,
        "next_action": next_action,
        "row": row_handle,
        "done": "Done" in relpath.split("/"),
        "surfaced_in": surfaced_in,
        # The whole page verbatim, for an editing caller that would otherwise
        # have to guess at the text outside Next Actions (issue #3).
        "body": "\n".join(page.lines) if include_body else None,
    }


def projects(vault: Vault, include_done: bool = False, include_body: bool = False) -> list[dict[str, Any]]:
    out = [project(vault, page, relpath, include_body) for relpath, page in vault.projects.items()]
    if include_done:
        # vault.projects never holds Done/ pages (model.load_vault skips them);
        # load them directly, same rules (must carry '## Next Actions').
        done_dir = vault.root / "Project details" / "Done"
        if done_dir.is_dir():
            for path in sorted(done_dir.rglob("*.md")):
                try:
                    page = projectsmod.load_project(path)
                except Exception:  # a broken Done/ page shouldn't break the listing
                    continue
                if page is not None:
                    out.append(project(vault, page, str(path.relative_to(vault.root)), include_body))
    return sorted(out, key=lambda p: p["stem"].lower())


def get_project(vault: Vault, name: str, include_body: bool = False) -> dict[str, Any] | None:
    target = name.strip().lower()
    for p in projects(vault, include_done=True, include_body=include_body):
        if p["stem"].lower() == target:
            return p
    return None


def all_tickler_buckets() -> list[str]:
    return [name.removesuffix(".md") for name in TICKLER_OFFSETS]


# --- reference notes (FORMAT.md §11) ---


def _reference_summary(note: ReferenceNote) -> dict[str, Any]:
    return {
        "stem": note.stem,
        "path": note.relpath,
        "folder": note.folder,
        "title": note.title,
        "headings": [{"level": h["level"], "text": h["text"]} for h in note.headings],
        "embeds": note.embeds,
    }


def reference_notes(vault: Vault, folder: str | None = None) -> list[dict[str, Any]]:
    """Every note under `Reference/` (or only under `folder`, subfolders
    included), by path."""
    wanted = reference_folder("list_reference", folder)
    notes = sorted(vault.reference_notes().values(), key=lambda n: n.relpath.lower())
    return [
        _reference_summary(n) for n in notes
        if not wanted or n.folder == wanted or n.folder.startswith(f"{wanted}/")
    ]


def get_reference(vault: Vault, name: str, section: str | None = None) -> dict[str, Any]:
    """One note with its verbatim `body`; with `section`, only that level-2
    section (heading line included)."""
    note = resolve_reference(vault, name)
    if section is None:
        body = note.text
    else:
        lines = note.section(section)
        if lines is None:
            raise OpError(
                f"{note.relpath} has no '## {section.strip()}' section; "
                f"its level-2 headings are: {', '.join(h['text'] for h in note.headings if h['level'] == 2) or 'none'}"
            )
        body = "\n".join(lines) + "\n"
    return {
        **_reference_summary(note),
        "links": note.links,
        "size": note.size,
        "linked_from": [b for b in backlinks(vault, note.stem) if b["path"] != note.relpath],
        "section": section.strip() if section is not None else None,
        "body": body,
    }


def backlinks(vault: Vault, stem: str) -> list[dict[str, Any]]:
    """The files that link to `stem`: `[{kind, path, count}]`."""
    return linksmod.backlinks(vault, stem)


_EXCERPT_CHARS = 200


def search_reference(vault: Vault, query: str) -> list[dict[str, Any]]:
    """Notes whose text contains `query` (case-insensitive), each with the
    matching lines and the heading they sit under."""
    needle = query.lower()
    out = []
    for note in sorted(vault.reference_notes().values(), key=lambda n: n.relpath.lower()):
        headings = note.headings
        matches = []
        for line_no, line in enumerate(note.lines, start=1):
            if needle in line.lower():
                under = [h["text"] for h in headings if h["line_no"] <= line_no]
                matches.append({"heading": under[-1] if under else None, "line_no": line_no, "excerpt": line.strip()[:_EXCERPT_CHARS]})
        if matches:
            out.append({"stem": note.stem, "path": note.relpath, "matches": matches})
    return out


# --- attachments ---

MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024
TEXT_MIMES = {"application/json", "application/xml", "application/x-yaml"}
IMAGE_FORMATS = {"image/png": "png", "image/jpeg": "jpeg", "image/webp": "webp", "image/gif": "gif"}


def attachment_file(vault: Vault, name: str) -> Path:
    """The file `attachments/<name>`, which must exist and resolve to a place
    inside `attachments/` (no `..`, no absolute path, no symlink out)."""
    base = (vault.root / "attachments").resolve()
    path = (base / name).resolve()
    if base not in path.parents:
        raise ValueError(f"attachment {name!r} is not inside attachments/")
    if not path.is_file():
        raise ValueError(f"no attachment named {name!r}")
    size = path.stat().st_size
    if size > MAX_ATTACHMENT_BYTES:
        raise ValueError(f"attachment {name!r} is {size} bytes, over the {MAX_ATTACHMENT_BYTES}-byte limit")
    return path


def attachment_mime(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"
