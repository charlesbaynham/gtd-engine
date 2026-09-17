"""Handle resolution: exact match, moved-row (nightly `sort` reordered
things), and staleness (no match / ambiguous match)."""
from __future__ import annotations

import pytest

from gtd_mcp.handles import Stale, enumerate_logical, hash_text, make_handle, resolve


def test_make_and_parse_roundtrip():
    from gtd_mcp.handles import parse_handle

    h = make_handle("tickler:Next month", 3, "9f2e11ab")
    parsed = parse_handle(h)
    assert parsed.kind == "tickler:Next month"
    assert parsed.index == 3
    assert parsed.hash8 == "9f2e11ab"


def test_resolve_exact_index_and_hash():
    seq = ["Alpha", "Beta", "Gamma"]
    entries = enumerate_logical(seq, lambda s: True)
    handle = make_handle("inbox", 1, hash_text("Beta"))
    raw_index, item = resolve(handle, "inbox", entries, hash_text)
    assert (raw_index, item) == (1, "Beta")


def test_resolve_after_row_moved_by_content_search():
    """The handle was made when 'Beta' sat at index 1; after a sort it's now
    at index 0. The stale index/hash pair no longer matches there, but a
    content search finds it uniquely."""
    seq = ["Alpha", "Beta", "Gamma"]
    stale_handle = make_handle("inbox", 1, hash_text("Beta"))

    reordered = ["Beta", "Alpha", "Gamma"]
    entries = enumerate_logical(reordered, lambda s: True)
    raw_index, item = resolve(stale_handle, "inbox", entries, hash_text)
    assert (raw_index, item) == (0, "Beta")


def test_resolve_no_match_is_stale():
    entries = enumerate_logical(["Alpha", "Gamma"], lambda s: True)
    handle = make_handle("inbox", 1, hash_text("Beta"))
    with pytest.raises(Stale):
        resolve(handle, "inbox", entries, hash_text)


def test_resolve_ambiguous_match_is_stale():
    entries = enumerate_logical(["Same", "Other", "Same"], lambda s: True)
    handle = make_handle("inbox", 5, hash_text("Same"))  # wrong index forces a content search
    with pytest.raises(Stale):
        resolve(handle, "inbox", entries, hash_text)


def test_resolve_wrong_kind_is_stale():
    entries = enumerate_logical(["Alpha"], lambda s: True)
    handle = make_handle("delegated", 0, hash_text("Alpha"))
    with pytest.raises(Stale):
        resolve(handle, "inbox", entries, hash_text)
