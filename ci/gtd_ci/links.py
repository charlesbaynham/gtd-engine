"""Wiki-link matching, rewriting and back-references across the vault.

A link to `name` is `[[name]]`, `[[folder/name]]`, `[[name#h]]` or
`[[name|alias]]` (FORMAT.md §6), matched case-insensitively. Both project
pages and reference notes are linked this way, so renaming either one uses
the same walk.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from . import projects as projectsmod
from .model import Vault, mark_dirty
from .vault import ParseFailure, RawFile, load_raw, write_raw


def link_pattern(name: str, *, folder_prefixed_only: bool = False) -> re.Pattern:
    prefix = r"(?:[^\]|#]*/)" + ("" if folder_prefixed_only else "?")
    return re.compile(r"\[\[" + prefix + re.escape(name) + r"\s*((?:#|\\?\|)[^\]]*)?\]\]", re.IGNORECASE)


def rewrite_links(text: str, old: str, new: str) -> str:
    """Every link to `old` in `text`, retargeted to `new` with its alias and
    heading kept. With `old == new` (a move) only folder-prefixed links
    change: a bare link survives a move, a prefixed one does not."""
    pattern = link_pattern(old, folder_prefixed_only=old == new)
    return pattern.sub(lambda m: f"[[{new}{m.group(1) or ''}]]", text)


@dataclass
class _Site:
    """One file's worth of text a link can live in. `units` are its strings;
    `store` writes back replacements (same length) and marks the file dirty."""

    kind: str
    relpath: str
    units: list[str]
    store: Callable[[list[str]], None]


def _table_sites(vault: Vault) -> Iterator[_Site]:
    for relpath, kind, table in (
        ("Next actions.md", "next-actions", vault.next_actions),
        ("Delegated.md", "delegated", vault.delegated),
        ("Scheduled.md", "scheduled", vault.scheduled),
    ):
        if table is None:
            continue

        def store(units, table=table, relpath=relpath):
            at = 0
            for entry in table.entries:
                cells = units[at : at + len(entry.cells)]
                at += len(cells)
                if cells != entry.cells:
                    entry.cells, entry.source_line = cells, None
                    mark_dirty(vault, relpath)

        yield _Site(kind, relpath, [c for e in table.entries for c in e.cells], store)


def _line_file_sites(vault: Vault) -> Iterator[_Site]:
    for kind, lf in [("inbox", vault.inbox), *(("tickler", t) for t in vault.ticklers.values())]:
        if lf is None:
            continue

        def store(units, lf=lf):
            for item, text in zip(lf.items, units):
                if item.text != text:
                    item.text = text
                    mark_dirty(vault, lf.relpath)

        yield _Site(kind, lf.relpath, [i.text for i in lf.items], store)


def _project_sites(vault: Vault) -> Iterator[_Site]:
    for relpath, page in vault.projects.items():

        def store(units, page=page, relpath=relpath):
            if units != page.lines:
                page.lines = units
                page.section_end = projectsmod.section_bounds(units, page.heading_index)
                page.items = projectsmod.parse_items(units, page.heading_index + 1, page.section_end)
                mark_dirty(vault, relpath)

        yield _Site("project", relpath, list(page.lines), store)


def _reference_sites(vault: Vault) -> Iterator[_Site]:
    for relpath, note in vault.reference_notes().items():

        def store(units, note=note, relpath=relpath):
            if units != note.lines:
                note.lines = units
                mark_dirty(vault, relpath)

        yield _Site("reference", relpath, list(note.lines), store)


def _done_sites(vault: Vault) -> Iterator[_Site]:
    for path in sorted((vault.root / "Project details" / "Done").rglob("*.md")):
        try:
            raw = load_raw(path)
        except ParseFailure:
            continue

        def store(units, raw=raw):
            if units != raw.lines:
                raw.lines = units
                write_raw(raw)

        yield _Site("project-done", str(path.relative_to(vault.root)), list(raw.lines), store)


def _sites(vault: Vault, *, done: bool) -> Iterator[_Site]:
    yield from _table_sites(vault)
    yield from _line_file_sites(vault)
    yield from _project_sites(vault)
    yield from _reference_sites(vault)
    if done:
        yield from _done_sites(vault)


def backlinks(vault: Vault, name: str) -> list[dict]:
    """Which files link to `name`: `[{kind, path, count}]`, in vault order."""
    pattern = link_pattern(name)
    out = []
    for site in _sites(vault, done=True):
        count = sum(len(pattern.findall(u)) for u in site.units)
        if count:
            out.append({"kind": site.kind, "path": site.relpath, "count": count})
    return out


@dataclass
class Rewrite:
    count: int = 0  # units (cells, lines, items) changed
    written_directly: set[str] = field(default_factory=set)  # Done/ pages, which the loaded vault does not own


def rewrite_everywhere(vault: Vault, old: str, new: str, *, done: bool) -> Rewrite:
    """Retarget every link to `old` as `new` in the tables, Inbox, Tickler,
    active project pages and reference notes (and Done/ pages when `done`).
    Loaded files are marked dirty for `save_vault`; Done/ pages are written
    at once and named in the result."""
    result = Rewrite()
    for site in _sites(vault, done=done):
        units = [rewrite_links(u, old, new) for u in site.units]
        changed = sum(a != b for a, b in zip(units, site.units))
        if changed:
            result.count += changed
            site.store(units)
            if site.kind == "project-done":
                result.written_directly.add(site.relpath)
    return result
