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


def link_pattern(name: str) -> re.Pattern:
    """Groups: 1 the folder prefix ('' or 'A/'), 2 the heading/alias suffix."""
    return re.compile(r"\[\[((?:[^\]|#]*/)?)" + re.escape(name) + r"\s*((?:#|\\?\|)[^\]]*)?\]\]", re.IGNORECASE)


def _prefix_points_at(prefix: str, directory: str) -> bool:
    wanted, have = prefix.rstrip("/").lower(), directory.lower()
    return have == wanted or have.endswith("/" + wanted)


def _targets_note_in(match: re.Match, within: str | None) -> bool:
    """Whether a link names the note living in directory `within`: a bare
    link does, a prefixed one only if its prefix is a path-suffix of that
    directory. `within=None` accepts any prefix (project pages)."""
    prefix = match.group(1)
    return within is None or not prefix or _prefix_points_at(prefix, within)


def _drop_prefix(prefix: str) -> str:
    return ""


@dataclass(frozen=True)
class Retarget:
    """Retarget links to `old` as `new`, keeping each alias and heading.

    `within` is the target's real directory, so that links to a same-named
    note elsewhere are left alone. With `bare=False` unprefixed links are
    not rewritten (they stay valid, or are ambiguous). `prefix_for` gives the
    new folder prefix of a rewritten prefixed link.
    """

    old: str
    new: str
    within: str | None = None
    bare: bool = True
    prefix_for: Callable[[str], str] = _drop_prefix


def rewrite_links(text: str, retarget: Retarget) -> tuple[str, int]:
    """`text` with the matching links rewritten, and the count of bare links
    left unrewritten because `retarget.bare` is false."""
    bare_left = 0

    def replace(m: re.Match) -> str:
        nonlocal bare_left
        prefix = m.group(1)
        if not _targets_note_in(m, retarget.within):
            return m.group(0)
        if not prefix and not retarget.bare:
            bare_left += 1
            return m.group(0)
        new_prefix = retarget.prefix_for(prefix) if prefix else ""
        return f"[[{new_prefix}{retarget.new}{m.group(2) or ''}]]"

    return link_pattern(retarget.old).sub(replace, text), bare_left


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
                projectsmod.reparse(page)
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


def backlinks(vault: Vault, name: str, within: str | None = None) -> list[dict]:
    """Which files link to `name` (the note in directory `within`, if given):
    `[{kind, path, count}]`, in vault order."""
    pattern = link_pattern(name)
    out = []
    for site in _sites(vault, done=True):
        count = sum(_targets_note_in(m, within) for u in site.units for m in pattern.finditer(u))
        if count:
            out.append({"kind": site.kind, "path": site.relpath, "count": count})
    return out


@dataclass
class Rewrite:
    count: int = 0
    written_directly: set[str] = field(default_factory=set)
    bare_left: int = 0


def rewrite_everywhere(vault: Vault, retarget: Retarget, *, done: bool) -> Rewrite:
    """Apply `retarget` in the tables, Inbox, Tickler, active project pages
    and reference notes (and Done/ pages when `done`). Loaded files are marked
    dirty for `save_vault`; Done/ pages are written at once and named in the
    result."""
    result = Rewrite()
    for site in _sites(vault, done=done):
        rewritten = [rewrite_links(u, retarget) for u in site.units]
        units = [text for text, _ in rewritten]
        result.bare_left += sum(left for _, left in rewritten)
        changed = sum(a != b for a, b in zip(units, site.units))
        if changed:
            result.count += changed
            site.store(units)
            if site.kind == "project-done":
                result.written_directly.add(site.relpath)
    return result
