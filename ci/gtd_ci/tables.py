"""Table file parsing and rewriting per FORMAT.md §4."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .vault import ParseFailure

_SEPARATOR = re.compile(r"^\|(\s*:?-+:?\s*\|)+\s*$")


def split_cells(line: str) -> list[str]:
    """Split a table row on '|' outside [[...]]/![[...]] and not escaped as \\|.

    Cell content is opaque (FORMAT.md §4.1): an escaped pipe is kept in the
    cell as the two literal characters '\\|', not unescaped to '|'.
    """
    cells: list[str] = []
    current = []
    depth = 0
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if ch == "\\" and i + 1 < n and line[i + 1] == "|":
            current.append("\\|")
            i += 2
            continue
        if ch == "[" and line[i + 1 : i + 2] == "[":
            depth += 1
            current.append("[[")
            i += 2
            continue
        if ch == "]" and line[i + 1 : i + 2] == "]" and depth > 0:
            depth -= 1
            current.append("]]")
            i += 2
            continue
        if ch == "|" and depth == 0:
            cells.append("".join(current))
            current = []
            i += 1
            continue
        current.append(ch)
        i += 1
    cells.append("".join(current))
    # Outer pipes produce an empty first and last piece.
    if cells and cells[0] == "":
        cells = cells[1:]
    if cells and cells[-1] == "":
        cells = cells[:-1]
    return cells


@dataclass
class Table:
    header_index: int
    separator_index: int
    header_cells: list[str]
    data_start: int
    data_end: int  # exclusive
    rows: list[list[str]]  # trimmed cells, one list per data row
    row_lines: list[str]  # original source line, one per data row


def find_table(lines: list[str]) -> Table:
    # FORMAT.md §4: header/separator/data rows are lines that START with '|',
    # checked verbatim (no lstrip) so this agrees byte-for-byte with the
    # plugin's strict parser.
    sep_idx = None
    for i, line in enumerate(lines):
        if _SEPARATOR.match(line) and i > 0 and lines[i - 1].startswith("|"):
            sep_idx = i
            break
    if sep_idx is None:
        raise ParseFailure("no table found (missing separator row)")

    header_idx = sep_idx - 1
    header_cells = [c.strip() for c in split_cells(lines[header_idx])]
    if len(split_cells(lines[sep_idx])) != len(header_cells):
        raise ParseFailure(f"separator row {sep_idx + 1} has a different cell count from the header")

    data_start = sep_idx + 1
    i = data_start
    rows: list[list[str]] = []
    row_lines: list[str] = []
    while i < len(lines) and lines[i].startswith("|"):
        cells = split_cells(lines[i])
        if len(cells) != len(header_cells):
            raise ParseFailure(f"row {i + 1} has {len(cells)} cells, header has {len(header_cells)}")
        rows.append([c.strip() for c in cells])
        row_lines.append(lines[i])
        i += 1

    return Table(
        header_index=header_idx,
        separator_index=sep_idx,
        header_cells=header_cells,
        data_start=data_start,
        data_end=i,
        rows=rows,
        row_lines=row_lines,
    )


def is_placeholder(row: list[str]) -> bool:
    return all(cell == "" for cell in row)


def _escape_pipe(cell: str) -> str:
    """Escapes a bare '|' as '\\|' (FORMAT.md §4.3) without double-escaping."""
    return re.sub(r"(?<!\\)\|", r"\\|", cell)


def format_row(cells: list[str]) -> str:
    return "|" + "|".join(f" {_escape_pipe(c)} " for c in cells) + "|"


@dataclass
class RowEntry:
    cells: list[str]
    source_line: str | None  # None => new row, format on write


@dataclass
class ManagedTable:
    prefix_lines: list[str]
    header_line: str
    separator_line: str
    entries: list[RowEntry]
    suffix_lines: list[str]
    trailing_newline: bool
    columns: dict[str, int]  # canonical lowercase name -> cell index
    optional_columns: list[str] = None  # lowercase names allowed but currently absent

    def __post_init__(self):
        if self.optional_columns is None:
            self.optional_columns = []

    def col(self, name: str) -> int:
        return self.columns[name.lower()]

    def has(self, name: str) -> bool:
        return name.lower() in self.columns

    def cell(self, cells: list[str], name: str) -> str:
        """`cells[self.col(name)]`, or "" when the column is absent (§4.2)."""
        if not self.has(name):
            return ""
        return cells[self.col(name)]

    def ensure_column(self, name: str) -> None:
        """Adds an optional column if not already present (§4.2/§4.3): the
        only place a managed table's column count changes. No-op if `name`
        is already a column."""
        if self.has(name):
            return
        idx = len(self.columns)
        self.header_line = self.header_line.rstrip() + f" {name} |"
        self.separator_line = self.separator_line.rstrip() + " ---- |"
        for entry in self.entries:
            entry.cells.append("")
            if entry.source_line is not None:
                entry.source_line = entry.source_line.rstrip() + "  |"
        self.columns[name.lower()] = idx
        if name.lower() in self.optional_columns:
            self.optional_columns.remove(name.lower())

    def render_lines(self) -> list[str]:
        rows = [e.source_line if e.source_line is not None else format_row(e.cells) for e in self.entries]
        return [*self.prefix_lines, self.header_line, self.separator_line, *rows, *self.suffix_lines]


def new_row(table: ManagedTable, values: dict[str, str]) -> list[str]:
    """Builds a row's cells in `table.columns` order (FORMAT.md §4.2).

    `ensure_column` is called only for a non-blank value whose column is not
    yet present, so a table stays at its narrower width until a Project (or
    other optional) value actually needs writing.
    """
    for name, value in values.items():
        if value and not table.has(name):
            table.ensure_column(name)
    cells = [""] * len(table.columns)
    for name, value in values.items():
        if table.has(name):
            cells[table.col(name)] = value
    return cells


def load_table(
    raw_lines: list[str],
    trailing_newline: bool,
    expected_columns: list[str],
    optional_columns: tuple[str, ...] = (),
) -> ManagedTable:
    table = find_table(raw_lines)
    normalized = [c.lower() for c in table.header_cells]
    expected_lower = [c.lower() for c in expected_columns]
    optional_lower = [c.lower() for c in optional_columns]
    full_lower = expected_lower + optional_lower

    if normalized == expected_lower:
        column_names = expected_lower
    elif optional_lower and normalized == full_lower:
        column_names = full_lower
    else:
        accepted = [expected_columns]
        if optional_lower:
            accepted.append(list(expected_columns) + list(optional_columns))
        raise ParseFailure(f"unexpected header {table.header_cells!r}, expected one of {accepted!r}")

    columns = {name: i for i, name in enumerate(column_names)}
    remaining_optional = [name for name in optional_lower if name not in column_names]
    entries = [RowEntry(cells=row, source_line=line) for row, line in zip(table.rows, table.row_lines)]
    return ManagedTable(
        prefix_lines=raw_lines[: table.header_index],
        header_line=raw_lines[table.header_index],
        separator_line=raw_lines[table.separator_index],
        entries=entries,
        suffix_lines=raw_lines[table.data_end :],
        trailing_newline=trailing_newline,
        columns=columns,
        optional_columns=remaining_optional,
    )
