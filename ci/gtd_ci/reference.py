"""Reference notes (`Reference/**/*.md`) per FORMAT.md §11.

Free-form markdown that automation reads and appends to but never
re-organises. Nothing here validates a note's shape: any readable file is a
note, and only the encoding rules of §1 can make one a parse failure.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from . import projects as projectsmod
from .names import NameRejected, near_names, usable_file_name
from .vault import ParseFailure, load_raw, split_front_matter

REFERENCE_DIR = "Reference"

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_EMBED = re.compile(r"!\[\[([^\]]+)\]\]")
_LINK = re.compile(r"(?<!!)\[\[([^\]]+)\]\]")
_TARGET_END = re.compile(r"\\?\||#")


def _target(inner: str) -> str:
    return inner[: m.start()].strip() if (m := _TARGET_END.search(inner)) else inner.strip()


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


@dataclass
class ReferenceNote:
    path: Path
    relpath: str
    lines: list[str]
    trailing_newline: bool

    @property
    def stem(self) -> str:
        return self.path.stem

    @property
    def folder(self) -> str:
        """Subfolder under `Reference/` ('' for a note at its top level)."""
        parent = Path(self.relpath).parent
        return "" if parent == Path(REFERENCE_DIR) else str(parent.relative_to(REFERENCE_DIR))

    @property
    def text(self) -> str:
        return "\n".join(self.lines) + ("\n" if self.trailing_newline and self.lines else "")

    @property
    def size(self) -> int:
        return len(self.text.encode("utf-8"))

    def set_text(self, text: str) -> None:
        self.trailing_newline = text.endswith("\n") or text == ""
        body = text[:-1] if text.endswith("\n") else text
        self.lines = body.split("\n") if text else []

    @property
    def body_start(self) -> int:
        try:
            return split_front_matter(self.lines)[1]
        except ParseFailure:
            return 0

    @property
    def headings(self) -> list[dict]:
        out, fenced = [], False
        for i in range(self.body_start, len(self.lines)):
            if _FENCE.match(self.lines[i]):
                fenced = not fenced
            elif not fenced and (m := _HEADING.match(self.lines[i])):
                out.append({"level": len(m.group(1)), "text": m.group(2), "line_no": i + 1})
        return out

    @property
    def title(self) -> str:
        return next((h["text"] for h in self.headings if h["level"] == 1), self.stem)

    def _targets(self, pattern: re.Pattern) -> list[str]:
        found, fenced = [], False
        for line in self.lines:
            if _FENCE.match(line):
                fenced = not fenced
            elif not fenced:
                found += [_target(m.group(1)) for m in pattern.finditer(line)]
        return _unique([t for t in found if t])

    @property
    def embeds(self) -> list[str]:
        return self._targets(_EMBED)

    @property
    def links(self) -> list[str]:
        return self._targets(_LINK)

    def section(self, title: str) -> list[str] | None:
        """Lines of the level-2 section called `title` (heading included), up
        to the next level-1/2 heading; None if the note has no such heading."""
        idx = projectsmod.find_heading(self.lines, title, level=2)
        if idx is None:
            return None
        end = projectsmod.section_bounds(self.lines, idx)
        while end > idx + 1 and not self.lines[end - 1].strip():
            end -= 1
        return self.lines[idx:end]


def load_reference(root: Path, path: Path) -> ReferenceNote:
    raw = load_raw(path)
    return ReferenceNote(path, str(path.relative_to(root)), raw.lines, raw.trailing_newline)


def load_references(root: Path, errors: list[tuple[str, str]]) -> dict[str, ReferenceNote]:
    """Every readable note under `Reference/`, keyed by vault-relative path;
    one that fails the encoding rules is reported in `errors` and skipped."""
    notes: dict[str, ReferenceNote] = {}
    base = root / REFERENCE_DIR
    if not base.is_dir():
        return notes
    for path in sorted(base.rglob("*.md")):
        try:
            note = load_reference(root, path)
        except ParseFailure as exc:
            errors.append((str(path.relative_to(root)), str(exc)))
            continue
        notes[note.relpath] = note
    return notes


def reference_dir(note: ReferenceNote) -> str:
    return str(Path(note.relpath).parent)


def normalise_folder(folder: str | None) -> str:
    """A subfolder of `Reference/` as 'A/B' ('' for the top level); each
    segment must be a usable file name."""
    parts = (folder or "").replace("\\", "/").strip("/").split("/")
    if parts[:1] == [REFERENCE_DIR]:
        parts = parts[1:]
    return "/".join(usable_file_name("folder", part) for part in parts if part)


def find_note(notes: dict[str, ReferenceNote], name: str) -> ReferenceNote | None:
    """The note `name` denotes, or None: an exact stem, else a unique
    case-insensitive one; a `Sub/Name` form matches the path below
    `Reference/`. Several matches raise, naming every candidate path."""
    key = name.strip().removesuffix(".md").removeprefix(f"{REFERENCE_DIR}/")

    def below_reference(note: ReferenceNote) -> str:
        return note.relpath.removeprefix(f"{REFERENCE_DIR}/").removesuffix(".md")

    field = below_reference if "/" in key else (lambda note: note.stem)
    candidates = list(notes.values())
    hits = [n for n in candidates if field(n) == key] or [n for n in candidates if field(n).lower() == key.lower()]
    if len(hits) > 1:
        raise NameRejected(f"reference note {name!r} is ambiguous: {', '.join(n.relpath for n in hits)}")
    return hits[0] if hits else None


def resolve_note(notes: dict[str, ReferenceNote], name: str) -> ReferenceNote:
    note = find_note(notes, name)
    if note is not None:
        return note
    _, near = near_names([n.stem for n in notes.values()], name)
    hint = f"; did you mean: {', '.join(near)}?" if near else ""
    raise NameRejected(f"no reference note named {name!r}{hint}")
