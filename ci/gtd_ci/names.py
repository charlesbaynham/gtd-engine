"""File-name validation and near-name matching shared by projects and reference notes."""
from __future__ import annotations

import re

BAD_NAME_CHARS = set('/\\[]#|^:*?"<>')


class NameRejected(ValueError):
    """A name that is unusable, unknown or ambiguous; the message says why."""


def usable_file_name(label: str, value: str) -> str:
    name = " ".join(value.split())
    if not name:
        raise NameRejected(f"{label} must not be blank")
    bad = sorted(set(name) & BAD_NAME_CHARS)
    if bad or name.startswith("."):
        raise NameRejected(f"{name!r} is not a usable file name ({' '.join(bad) or 'leading dot'})")
    return name


def normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, start=1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
        prev = cur
    return prev[-1]


def near_names(names: list[str], name: str) -> tuple[str | None, list[str]]:
    """The names within a normalised Levenshtein distance of
    `max(2, len(norm(name)) // 4)` of `name`, or (for a `name` of 4+ chars)
    whose normalised form starts with or contains it. Exactly one candidate
    resolves to `(it, [it])`; anything else is `(None, candidates)`."""
    target = normalize(name)
    threshold = max(2, len(target) // 4)
    candidates: set[str] = set()
    for candidate in names:
        norm = normalize(candidate)
        if levenshtein(norm, target) <= threshold:
            candidates.add(candidate)
        elif len(target) >= 4 and (norm.startswith(target) or target in norm):
            candidates.add(candidate)
    ordered = sorted(candidates)
    if len(ordered) == 1:
        return ordered[0], ordered
    return None, ordered
