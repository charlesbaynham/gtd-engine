from datetime import date

import pytest

from gtd_ci.dates import parse_date
from gtd_ci.projects import parse_link
from gtd_ci.tables import ParseFailure, find_table, format_row, split_cells


def test_parse_date_canonical():
    p = parse_date("2026-09-10")
    assert p is not None and p.value == date(2026, 9, 10) and p.canonical


def test_parse_date_tolerated_slash():
    p = parse_date("2026/09/10")
    assert p is not None and p.value == date(2026, 9, 10) and not p.canonical


def test_parse_date_day_month_no_year_unparseable():
    assert parse_date("7 Mar") is None


def test_parse_date_blank():
    assert parse_date("   ") is None


def test_split_cells_respects_wikilinks_and_keeps_escape_literal():
    line = r"| [[A|alias]] | escaped \| pipe | plain |"
    assert split_cells(line) == [" [[A|alias]] ", r" escaped \| pipe ", " plain "]


def test_format_row_escapes_bare_pipe():
    assert format_row(["a | b", "c"]) == r"| a \| b | c |"


def test_format_row_does_not_double_escape_already_escaped_pipe():
    assert format_row([r"a \| b"]) == r"| a \| b |"


def test_find_table_requires_row_to_start_with_pipe_unstripped():
    # Leading whitespace before '|' disqualifies a line as header/separator/data
    # per FORMAT.md §4 — the plugin's strict parser must agree byte-for-byte.
    lines = [
        "  | Action | Project |",
        "| ---- | ---- |",
        "| a | b |",
    ]
    with pytest.raises(ParseFailure):
        find_table(lines)


def test_find_table_rejects_indented_data_row():
    lines = [
        "| Action | Project |",
        "| ---- | ---- |",
        "  | indented, not a data row |",
    ]
    table = find_table(lines)
    assert table.rows == []
    assert table.data_end == 2


def test_parse_link_cuts_at_escaped_alias_pipe():
    # Obsidian writes an aliased link inside a table cell with the alias
    # pipe escaped: [[Widget\|the widget project]] (FORMAT.md §6).
    assert parse_link(r"[[Widget\|the widget project]]") == "Widget"


def test_parse_link_cuts_at_bare_alias_pipe():
    assert parse_link("[[Widget|the widget project]]") == "Widget"


def test_parse_link_cuts_at_heading():
    assert parse_link("[[Widget#Status]]") == "Widget"
