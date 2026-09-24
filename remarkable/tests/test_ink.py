"""``ink()`` counts live strokes and unreadable layers — and nothing else.

Needs the remarkable-gtd package but no browser, unlike test_cli_roundtrip.
"""
from __future__ import annotations

import json
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from gtd_remarkable.cli import ink

pytest.importorskip("remarkable_gtd")


def _rmdoc(out: Path, *layers: bytes | None) -> Path:
    """An .rmdoc with one page per layer; ``None`` means no ``.rm`` at all."""
    with zipfile.ZipFile(out, "w") as z:
        pages = [{"id": f"page-{i}", "redir": {"value": i}} for i, _ in enumerate(layers)]
        z.writestr("doc-uuid.content", json.dumps({"cPages": {"pages": pages}}))
        z.writestr("doc-uuid.pdf", b"%PDF-1.4\n")
        for i, blob in enumerate(layers):
            if blob is not None:
                z.writestr(f"doc-uuid/page-{i}.rm", blob)
    return out


def _stroke(*, erased: bool) -> bytes:
    from rmscene import CrdtId, CrdtSequenceItem, SceneLineItemBlock, write_blocks
    from rmscene.scene_items import Line, Pen, PenColor, Point

    line = Line(
        color=PenColor.BLACK,
        tool=Pen.FINELINER_1,
        points=[Point(x=x, y=0.0, speed=1, direction=0, width=10, pressure=100) for x in (0.0, 20.0)],
        thickness_scale=1.0,
        starting_length=0.0,
    )
    item = CrdtSequenceItem(
        item_id=CrdtId(1, 10), left_id=CrdtId(0, 0), right_id=CrdtId(0, 0),
        deleted_length=len(line.points) if erased else 0,
        value=None if erased else line,
    )
    buf = BytesIO()
    write_blocks(buf, [SceneLineItemBlock(parent_id=CrdtId(0, 1), item=item)])
    return buf.getvalue()


def test_untouched_sheet_has_no_layers(tmp_path):
    assert ink(_rmdoc(tmp_path / "a.rmdoc", None)) == (0, 0)


def test_written_on_sheet_counts_its_strokes(tmp_path):
    assert ink(_rmdoc(tmp_path / "b.rmdoc", _stroke(erased=False))) == (1, 0)


def test_erased_strokes_are_readable_and_empty(tmp_path):
    """The wedging case: three erased layers must look blank, not unreadable."""
    erased = _stroke(erased=True)
    assert ink(_rmdoc(tmp_path / "c.rmdoc", erased, erased, erased)) == (0, 0)


def test_unparseable_layer_is_counted_as_unreadable(tmp_path):
    assert ink(_rmdoc(tmp_path / "d.rmdoc", b"not a v6 stroke file at all")) == (0, 1)


def test_readable_ink_alongside_an_unreadable_layer_still_scans(tmp_path):
    """Unchanged behaviour: only a sheet with *no* readable ink is a failure."""
    assert ink(_rmdoc(tmp_path / "e.rmdoc", _stroke(erased=False), b"garbage")) == (1, 1)
