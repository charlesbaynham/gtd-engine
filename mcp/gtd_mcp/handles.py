"""Content-hashed handles: `<kind>:<index>:<hash8>`.

`kind` is one of `next-actions`, `delegated`, `scheduled`, `inbox`,
`tickler:<bucket>` or `project:<stem>`. `index` is the item's position among
data rows / items of that kind (placeholder rows, blank lines and non-item
project lines excluded). `hash8` is the first 8 hex digits of a sha256 over
its content, so a handle carries its own optimistic-concurrency check: if the
vault moved underneath it (the nightly `sort` reorders rows every night),
resolution falls back to a content search before giving up.

This module is FORMAT-neutral: it knows nothing about tables, line files or
project pages. Callers hand it `(logical_index, raw_index, content)` triples
computed by `ops.py`/`views.py` and get back a resolved raw index.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Sequence, TypeVar

_HASH_LEN = 8


class Stale(Exception):
    """A handle no longer resolves unambiguously against current vault state."""

    def __init__(self, handle: str, reason: str):
        super().__init__(f"{handle}: {reason}")
        self.handle = handle
        self.reason = reason


def hash_cells(cells: Sequence[str]) -> str:
    """Table row hash: sha256 over trimmed cells joined by 0x1f.

    Trailing blank cells are stripped first, so a table gaining an optional
    column (FORMAT.md §4.2/§4.3) with a blank padding cell on this row does
    not change the row's outstanding handles.
    """
    trimmed = [c.strip() for c in cells]
    while trimmed and trimmed[-1] == "":
        trimmed.pop()
    joined = "\x1f".join(trimmed)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:_HASH_LEN]


def hash_text(text: str) -> str:
    """Line-file item / project item hash: sha256 over the item's own text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:_HASH_LEN]


@dataclass(frozen=True)
class Handle:
    kind: str
    index: int
    hash8: str

    def __str__(self) -> str:
        return f"{self.kind}:{self.index}:{self.hash8}"


def parse_handle(handle: str) -> Handle:
    try:
        kind, index_str, hash8 = handle.rsplit(":", 2)
        index = int(index_str)
    except ValueError as exc:
        raise Stale(handle, "malformed handle") from exc
    if not kind or len(hash8) != _HASH_LEN:
        raise Stale(handle, "malformed handle")
    return Handle(kind=kind, index=index, hash8=hash8)


def make_handle(kind: str, logical_index: int, content_hash: str) -> str:
    return str(Handle(kind, logical_index, content_hash))


T = TypeVar("T")


def enumerate_logical(seq: Sequence[T], include) -> list[tuple[int, int, T]]:
    """(logical_index, raw_index, item) for every element `include` accepts."""
    out: list[tuple[int, int, T]] = []
    logical = 0
    for raw_index, item in enumerate(seq):
        if not include(item):
            continue
        out.append((logical, raw_index, item))
        logical += 1
    return out


def resolve(handle_str: str, expected_kind: str, entries: list[tuple[int, int, T]], hash_of) -> tuple[int, T]:
    """Resolves a handle against `entries` (as returned by `enumerate_logical`).

    Returns (raw_index, item). Raises Stale if the kind doesn't match, or if
    the handle resolves to zero or more than one row after a content search.
    """
    handle = parse_handle(handle_str)
    if handle.kind != expected_kind:
        raise Stale(handle_str, f"expected a {expected_kind!r} handle, got {handle.kind!r}")

    for logical, raw_index, item in entries:
        if logical == handle.index and hash_of(item) == handle.hash8:
            return raw_index, item

    matches = [(raw_index, item) for _, raw_index, item in entries if hash_of(item) == handle.hash8]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise Stale(handle_str, "no matching row found — it may already have been moved or removed")
    raise Stale(handle_str, "ambiguous — multiple rows now share this content")
