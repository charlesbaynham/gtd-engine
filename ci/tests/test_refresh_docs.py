"""Tests for `gtd_ci refresh-docs` — see docs_sync.py."""
from __future__ import annotations

from pathlib import Path

import pytest

from gtd_ci.docs_sync import MARKER, doc_text, refresh_docs


def test_refresh_docs_creates_missing_files(tmp_path: Path) -> None:
    refresh_docs(tmp_path)
    assert (tmp_path / "FORMAT.md").read_text(encoding="utf-8") == doc_text("FORMAT.md")
    claude = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    assert claude.startswith(doc_text("CLAUDE.md"))
    assert MARKER in claude


def test_refresh_docs_replaces_body_keeps_tail(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text(f"stale body\n{MARKER}\n\nvault notes stay\n", encoding="utf-8")
    refresh_docs(tmp_path)
    claude = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    assert claude == f"{doc_text('CLAUDE.md')}\n{MARKER}\n\nvault notes stay\n"


def test_refresh_docs_no_marker_leaves_untouched(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    original = "# hand-written CLAUDE.md, no marker\n"
    (tmp_path / "CLAUDE.md").write_text(original, encoding="utf-8")
    refresh_docs(tmp_path)
    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8") == original
    assert "no marker" in capsys.readouterr().err


def test_refresh_docs_dry_run_writes_nothing(tmp_path: Path) -> None:
    refresh_docs(tmp_path, dry_run=True)
    assert not (tmp_path / "FORMAT.md").exists()
    assert not (tmp_path / "CLAUDE.md").exists()


def test_refresh_docs_idempotent(tmp_path: Path) -> None:
    refresh_docs(tmp_path)
    assert refresh_docs(tmp_path) == ["unchanged FORMAT.md", "unchanged CLAUDE.md"]


def test_refresh_docs_missing_vault_raises(tmp_path: Path) -> None:
    with pytest.raises(NotADirectoryError):
        refresh_docs(tmp_path / "does-not-exist")
