"""Date parsing per FORMAT.md §3.

Canonical form (the only form ever written): YYYY-MM-DD[ HH:MM].
Additional readable forms are tolerated but flagged; anything else is
unparseable and treated as blank by every job.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

_MONTHS = {
    m.lower(): i
    for i, m in enumerate(
        [
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        ],
        start=1,
    )
}
for _i, _m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
    start=1,
):
    _MONTHS[_m.lower()] = _i

_CANONICAL = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?: (\d{2}):(\d{2}))?$")
_SLASH_DOT = re.compile(r"^(\d{4})[/.](\d{2})[/.](\d{2})$")
_DAY_MONTH_YEAR = re.compile(r"^(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})$")


@dataclass(frozen=True)
class ParsedDate:
    value: date
    canonical: bool  # False => reading a tolerated non-canonical form


def parse_date(text: str) -> ParsedDate | None:
    """Parse a date cell/stamp value; returns None if unparseable."""
    text = text.strip()
    if not text:
        return None

    m = _CANONICAL.match(text)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return ParsedDate(date(y, mo, d), canonical=True)
        except ValueError:
            return None

    m = _SLASH_DOT.match(text)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return ParsedDate(date(y, mo, d), canonical=False)
        except ValueError:
            return None

    m = _DAY_MONTH_YEAR.match(text)
    if m:
        d, month_name, y = int(m.group(1)), m.group(2).lower(), int(m.group(3))
        mo = _MONTHS.get(month_name)
        if mo is None:
            return None
        try:
            return ParsedDate(date(y, mo, d), canonical=False)
        except ValueError:
            return None

    return None


def format_date(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def add_days(d: date, days: int) -> date:
    return d + timedelta(days=days)
