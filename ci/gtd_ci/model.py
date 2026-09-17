"""Loads the whole vault into structured, mutable in-memory files."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import lines as linesmod
from . import projects as projectsmod
from . import tables as tablesmod
from .vault import ParseFailure, load_raw, split_front_matter, write_raw

TICKLER_OFFSETS: dict[str, int] = {
    "Next week.md": 7,
    "Next two weeks.md": 14,
    "Next month.md": 30,
    "Next quarter.md": 91,
}

TABLE_SPECS: dict[str, list[str]] = {
    "Next actions.md": ["Action", "Project", "Deadline", "Priority"],
    "Delegated.md": ["Thing", "Person", "Chase by", "Priority"],
    "Scheduled.md": ["Thing", "Date", "Status", "Event"],
}

OPTIONAL_COLUMNS: dict[str, list[str]] = {
    "Delegated.md": ["Project"],
    "Scheduled.md": ["Project"],
}

FIXED_GTD: dict[str, str] = {
    "Inbox.md": "inbox",
    "Next actions.md": "next-actions",
    "Delegated.md": "delegated",
    "Scheduled.md": "scheduled",
}


@dataclass
class LineFile:
    relpath: str
    path: Path
    prefix_lines: list[str]  # front matter, verbatim
    order: list[linesmod.Item | None]  # None = blank line, in source order
    trailing_newline: bool

    @property
    def items(self) -> list[linesmod.Item]:
        return [e for e in self.order if e is not None]

    def render_lines(self) -> list[str]:
        body: list[str] = []
        for entry in self.order:
            if entry is None:
                body.append("")
            else:
                stamp = f"[{entry.stamp_raw}] " if entry.stamp_raw is not None else ""
                body.append(f"{entry.marker}{stamp}{entry.text}")
        return [*self.prefix_lines, *body]


def _load_line_file(relpath: str, path: Path) -> LineFile:
    raw = load_raw(path)
    fm, body_start = split_front_matter(raw.lines)
    expected = FIXED_GTD.get(Path(relpath).name)
    if expected is None and Path(relpath).parent.name == "Tickler":
        expected = "tickler"
    if fm is None or fm.gtd != expected:
        raise ParseFailure(f"missing or wrong front matter (expected gtd: {expected})")

    prefix_lines = raw.lines[:body_start]
    order: list[linesmod.Item | None] = []
    for i, line in enumerate(raw.lines[body_start:]):
        if linesmod.is_blank(line):
            order.append(None)
        else:
            order.append(linesmod.parse_item(line, i))

    return LineFile(
        relpath=relpath,
        path=path,
        prefix_lines=prefix_lines,
        order=order,
        trailing_newline=raw.trailing_newline,
    )


def _load_table_file(relpath: str, path: Path) -> tablesmod.ManagedTable:
    raw = load_raw(path)
    fm, _ = split_front_matter(raw.lines)
    expected = FIXED_GTD[Path(relpath).name]
    if fm is None or fm.gtd != expected:
        raise ParseFailure(f"missing or wrong front matter (expected gtd: {expected})")
    name = Path(relpath).name
    return tablesmod.load_table(
        raw.lines, raw.trailing_newline, TABLE_SPECS[name], tuple(OPTIONAL_COLUMNS.get(name, ()))
    )


@dataclass
class Vault:
    root: Path
    inbox: LineFile | None = None
    ticklers: dict[str, LineFile] = field(default_factory=dict)  # relpath -> LineFile, ordered by offset
    next_actions: tablesmod.ManagedTable | None = None
    delegated: tablesmod.ManagedTable | None = None
    scheduled: tablesmod.ManagedTable | None = None
    projects: dict[str, projectsmod.ProjectPage] = field(default_factory=dict)  # relpath -> page
    project_index: dict[str, list[Path]] = field(default_factory=dict)
    errors: list[tuple[str, str]] = field(default_factory=list)
    dirty: set[str] = field(default_factory=set)


def load_vault(root: Path) -> Vault:
    vault = Vault(root=root)

    def _try(relpath: str, fn):
        path = root / relpath
        if not path.is_file():
            vault.errors.append((relpath, "file missing"))
            return None
        try:
            return fn(relpath, path)
        except ParseFailure as exc:
            vault.errors.append((relpath, str(exc)))
            return None

    vault.inbox = _try("Inbox.md", _load_line_file)
    for fname in ["Next week.md", "Next two weeks.md", "Next month.md", "Next quarter.md"]:
        relpath = f"Tickler/{fname}"
        lf = _try(relpath, _load_line_file)
        if lf is not None:
            vault.ticklers[relpath] = lf

    vault.next_actions = _try("Next actions.md", _load_table_file)
    vault.delegated = _try("Delegated.md", _load_table_file)
    vault.scheduled = _try("Scheduled.md", _load_table_file)

    vault.project_index = projectsmod.index_projects_by_stem(root)
    base = root / "Project details"
    if base.is_dir():
        for p in sorted(base.rglob("*.md")):
            if "Done" in p.relative_to(root).parts or p.name == "-Project template.md":
                continue
            relpath = str(p.relative_to(root))
            try:
                page = projectsmod.load_project(p)
            except ParseFailure as exc:
                vault.errors.append((relpath, str(exc)))
                continue
            if page is not None:
                vault.projects[relpath] = page

    return vault


def mark_dirty(vault: Vault, relpath: str) -> None:
    vault.dirty.add(relpath)


def save_vault(vault: Vault) -> None:
    from .vault import RawFile

    if vault.inbox is not None and "Inbox.md" in vault.dirty:
        write_raw(RawFile(vault.inbox.path, vault.inbox.render_lines(), vault.inbox.trailing_newline))
    for relpath, lf in vault.ticklers.items():
        if relpath in vault.dirty:
            write_raw(RawFile(lf.path, lf.render_lines(), lf.trailing_newline))
    for relpath, table in [
        ("Next actions.md", vault.next_actions),
        ("Delegated.md", vault.delegated),
        ("Scheduled.md", vault.scheduled),
    ]:
        if table is not None and relpath in vault.dirty:
            write_raw(RawFile(vault.root / relpath, table.render_lines(), table.trailing_newline))
    for relpath, page in vault.projects.items():
        if relpath in vault.dirty:
            write_raw(RawFile(page.path, page.lines, page.trailing_newline))
