"""Line-file (Inbox, Tickler) parsing per FORMAT.md §5."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .dates import ParsedDate, parse_date

_MARKER = re.compile(r"^([-*] )")
_STAMP_OPEN = re.compile(r"^\[(\d)")


def is_blank(line: str) -> bool:
    return line.strip() == ""


@dataclass
class Item:
    marker: str  # '' , '- ' or '* '
    stamp_raw: str | None  # e.g. '2026-10-08' as written, or None if undated
    stamp: ParsedDate | None
    text: str
    line_index: int


def parse_item(line: str, line_index: int) -> Item:
    marker = ""
    rest = line
    m = _MARKER.match(rest)
    if m:
        marker = m.group(1)
        rest = rest[len(marker) :]

    if _STAMP_OPEN.match(rest):
        close = rest.find("]")
        if close != -1:
            stamp_raw = rest[1:close]
            parsed = parse_date(stamp_raw)
            if parsed is not None:
                text = rest[close + 1 :]
                if text.startswith(" "):
                    text = text[1:]
                return Item(marker, stamp_raw, parsed, text, line_index)

    return Item(marker, None, None, rest, line_index)


def format_stamped(marker: str, date_str: str, text: str) -> str:
    return f"{marker}[{date_str}] {text}"


_TRAILING_LINK = re.compile(r"^(.*?)\s*(\[\[[^\]]+\]\])\s*$")


def split_project_link(text: str) -> tuple[str, str | None]:
    """Splits a trailing `[[Project]]` link off a line's text (FORMAT.md §5.1
    tickler trailing link). Returns (body, name) where name is the link's
    resolved project name (per `projects.parse_link`), or (text, None) when
    there is no trailing link."""
    from . import projects as projectsmod

    m = _TRAILING_LINK.match(text)
    if not m:
        return text, None
    name = projectsmod.parse_link(m.group(2))
    if name is None:
        return text, None
    return m.group(1), name


def format_with_project(text: str, stem: str | None) -> str:
    """Appends ` [[stem]]` to `text` when `stem` is given (FORMAT.md §5.1)."""
    if not stem:
        return text
    return f"{text} [[{stem}]]"
