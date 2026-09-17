"""File loading and round-trip helpers per FORMAT.md §1-2, §9."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


class ParseFailure(Exception):
    """Raised when a file cannot be parsed under the format contract.

    Carries the human-readable reason used in lint's `parse-failure` finding.
    """


@dataclass
class RawFile:
    """A file's bytes, decoded into lines with round-trip metadata preserved."""

    path: Path
    lines: list[str]
    trailing_newline: bool

    def render(self) -> str:
        text = "\n".join(self.lines)
        if self.trailing_newline:
            text += "\n"
        return text


def load_raw(path: Path) -> RawFile:
    data = path.read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):
        raise ParseFailure("BOM present")
    if b"\r" in data:
        raise ParseFailure("CRLF or bare CR line ending present")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ParseFailure(f"invalid UTF-8: {exc}") from exc

    trailing_newline = text.endswith("\n") or text == ""
    body = text[:-1] if text.endswith("\n") else text
    lines = body.split("\n") if text else []
    return RawFile(path=path, lines=lines, trailing_newline=trailing_newline)


def write_raw(raw: RawFile) -> None:
    raw.path.write_text(raw.render(), encoding="utf-8", newline="\n")


@dataclass
class FrontMatter:
    lines: list[str]  # includes both '---' delimiter lines
    values: dict[str, str] = field(default_factory=dict)

    @property
    def gtd(self) -> str | None:
        return self.values.get("gtd")


def split_front_matter(lines: list[str]) -> tuple[FrontMatter | None, int]:
    """Returns (front_matter, index of first body line) or (None, 0)."""
    if not lines or lines[0].strip() != "---":
        return None, 0
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            fm_lines = lines[: i + 1]
            values: dict[str, str] = {}
            for raw_line in lines[1:i]:
                if ":" not in raw_line:
                    continue
                key, _, val = raw_line.partition(":")
                values[key.strip()] = val.strip()
            return FrontMatter(lines=fm_lines, values=values), i + 1
    raise ParseFailure("front matter opened with '---' but never closed")
