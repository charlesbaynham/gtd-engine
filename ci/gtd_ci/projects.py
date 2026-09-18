"""Project page discovery, links and Next Actions section per FORMAT.md §6."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .model import Vault

_HEADING = re.compile(r"^(#{1,2})\s+(.*)$")
_ITEM = re.compile(r"^([-*]) \[( |x|X)\] (.*)$")
_LINK = re.compile(r"\[\[([^\]]+)\]\]")


@dataclass
class ProjectItem:
    marker: str
    checked: bool
    text: str
    line_index: int


@dataclass
class ProjectPage:
    path: Path
    stem: str
    lines: list[str]
    trailing_newline: bool
    heading_index: int | None  # index of '## Next Actions' line, or None if absent
    section_end: int  # exclusive line index of end of Next Actions section
    items: list[ProjectItem]


def is_project_dir(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root / "Project details" / "Done")
        return False
    except ValueError:
        pass
    return path.name != "-Project template.md"


def find_heading(lines: list[str], title: str, level: int = 2) -> int | None:
    """Index of the first heading of `level` whose text equals `title`
    (trimmed, case-insensitive), or None."""
    want = title.strip().lower()
    for i, line in enumerate(lines):
        m = _HEADING.match(line.strip())
        if m and len(m.group(1)) == level and m.group(2).strip().lower() == want:
            return i
    return None


def find_next_actions_heading(lines: list[str]) -> int | None:
    return find_heading(lines, "Next Actions", level=2)


def goal_paragraph(lines: list[str]) -> tuple[int, int] | None:
    """(start, end) line indices of the paragraph under `# Goal` (§6).

    `end` is exclusive; `start == end` means the heading is there but carries
    no prose yet, and names the line prose would be inserted at (past the
    blank line the heading is usually followed by). Returns None when the
    page has no `# Goal` heading at all.
    """
    idx = find_heading(lines, "Goal", level=1)
    if idx is None:
        return None
    i = idx + 1
    while i < len(lines) and lines[i].strip() == "":
        i += 1
    if i >= len(lines) or lines[i].strip().startswith("#"):
        return i, i
    start = i
    while i < len(lines) and lines[i].strip() != "" and not lines[i].strip().startswith("#"):
        i += 1
    return start, i


def section_bounds(lines: list[str], heading_index: int) -> int:
    """End (exclusive) of the section opened at `heading_index`: the next
    level-1/2 heading, or EOF (§6, same bound for every level-2 section)."""
    for i in range(heading_index + 1, len(lines)):
        m = _HEADING.match(lines[i].strip())
        if m and len(m.group(1)) in (1, 2):
            return i
    return len(lines)


def parse_items(lines: list[str], start: int, end: int) -> list[ProjectItem]:
    items = []
    for i in range(start, end):
        m = _ITEM.match(lines[i])
        if m:
            marker, box, text = m.group(1), m.group(2), m.group(3)
            items.append(ProjectItem(marker, box.lower() == "x", text.strip(), i))
    return items


def load_project(path: Path) -> ProjectPage | None:
    """Returns None when the file has no '## Next Actions' heading (ignored)."""
    from .vault import load_raw

    raw = load_raw(path)
    heading_index = find_next_actions_heading(raw.lines)
    if heading_index is None:
        return None
    end = section_bounds(raw.lines, heading_index)
    items = parse_items(raw.lines, heading_index + 1, end)
    return ProjectPage(
        path=path,
        stem=path.stem,
        lines=raw.lines,
        trailing_newline=raw.trailing_newline,
        heading_index=heading_index,
        section_end=end,
        items=items,
    )


@dataclass
class LinkTarget:
    name: str
    resolved: Path | None
    resolved_in_done: bool


_LINK_TARGET_END = re.compile(r"\\\||\||#")


def parse_link(cell: str) -> str | None:
    """Extracts the project name from the first [[...]] link in a cell, if any.

    FORMAT.md §6: a table cell may carry an escaped alias pipe (Obsidian
    writes `[[Widget\\|alias]]` inside a table cell), so both `\\|` and a
    bare `|` end the link target, same as `#`.
    """
    m = _LINK.search(cell)
    if not m:
        return None
    target = m.group(1)
    end = _LINK_TARGET_END.search(target)
    if end:
        target = target[: end.start()]
    name = target.rsplit("/", 1)[-1].strip()
    return name or None


def resolve_link(name: str, all_projects: dict[str, list[Path]]) -> LinkTarget:
    candidates = all_projects.get(name.lower(), [])
    if not candidates:
        return LinkTarget(name, None, False)
    non_done = [p for p in candidates if "Done" not in p.parts]
    if non_done:
        chosen = min(non_done, key=lambda p: len(p.parts))
        return LinkTarget(name, chosen, False)
    chosen = min(candidates, key=lambda p: len(p.parts))
    return LinkTarget(name, chosen, True)


def surfaced_items(vault: "Vault", page_path: Path) -> tuple[dict[str, list[str]], list[str]]:
    """Where a project's items are surfaced (FORMAT.md §6 "surfaced").

    Returns `(matched, mismatched)`:
    - `matched`: {item text (trimmed): [view names]} for every unchecked item
      that some Next actions/Delegated/Scheduled row, or tickler line,
      links to this project with a body/Action/Thing equal to that text.
    - `mismatched`: view names that link to this project but whose
      body/Action/Thing matches no unchecked item.

    A "view name" is `Next actions.md`, `Delegated.md`, `Scheduled.md`, or a
    tickler file's relpath. Inbox is never a surfaced view (§6).
    """
    from . import lines as linesmod

    unchecked_texts: set[str] = set()
    page = vault.projects.get(str(page_path.relative_to(vault.root)))
    if page is not None:
        unchecked_texts = {i.text for i in page.items if not i.checked}

    matched: dict[str, list[str]] = {}
    mismatched: list[str] = []

    def _consider(view: str, body: str, link_name: str | None) -> None:
        if not link_name:
            return
        link = resolve_link(link_name, vault.project_index)
        if link.resolved != page_path:
            return
        text = body.strip()
        if text in unchecked_texts:
            matched.setdefault(text, []).append(view)
        else:
            mismatched.append(view)

    tables = [
        (vault.next_actions, "Next actions.md", "action", "project"),
        (vault.delegated, "Delegated.md", "thing", "project"),
        (vault.scheduled, "Scheduled.md", "thing", "project"),
    ]
    for table, view, body_col, link_col in tables:
        if table is None or not table.has(link_col):
            continue
        for entry in table.entries:
            cells = entry.cells
            if all(c == "" for c in cells):
                continue
            name = parse_link(table.cell(cells, link_col))
            _consider(view, table.cell(cells, body_col), name)

    for relpath, tickler in vault.ticklers.items():
        for item in tickler.items:
            body, name = linesmod.split_project_link(item.text)
            _consider(relpath, body, name)

    return matched, mismatched


def is_surfaced(vault: "Vault", page: "ProjectPage") -> bool:
    """True if `page` has at least one unchecked item that is surfaced (§6)."""
    matched, _mismatched = surfaced_items(vault, page.path)
    return bool(matched)


def index_projects_by_stem(root: Path) -> dict[str, list[Path]]:
    base = root / "Project details"
    index: dict[str, list[Path]] = {}
    if not base.is_dir():
        return index
    for p in base.rglob("*.md"):
        index.setdefault(p.stem.lower(), []).append(p)
    return index
