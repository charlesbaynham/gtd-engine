"""Unit tests for `gtd_mcp.ops.nearest_project_stem` (near-name project
resolution used by callers that want suggestions rather than a hard error)."""
from __future__ import annotations

from pathlib import Path

from gtd_ci.model import Vault
from gtd_mcp.ops import nearest_project_stem


def _vault(stems: list[str], root: Path = Path("/vault")) -> Vault:
    vault = Vault(root=root)
    index: dict[str, list[Path]] = {}
    for stem in stems:
        index.setdefault(stem.lower(), []).append(root / "Project details" / f"{stem}.md")
    vault.project_index = index
    return vault


def test_exact_match_returns_stem_and_no_candidates():
    vault = _vault(["Sample", "Widget"])
    stem, candidates = nearest_project_stem(vault, "Sample")
    assert (stem, candidates) == ("Sample", [])


def test_exact_match_is_case_insensitive():
    vault = _vault(["Sample Project"])
    stem, candidates = nearest_project_stem(vault, "sample project")
    assert (stem, candidates) == ("Sample Project", [])


def test_done_only_project_is_not_an_exact_match():
    vault = _vault([])
    vault.project_index["retired"] = [vault.root / "Project details" / "Done" / "Retired.md"]
    stem, candidates = nearest_project_stem(vault, "Retired")
    # No active match: falls through to near-name search, which finds
    # nothing else either (the only entry is the Done/ one, excluded from
    # `all_stems`).
    assert stem is None
    assert candidates == []


def test_single_close_typo_resolves_uniquely():
    vault = _vault(["Wedding 2026", "Finances"])
    stem, candidates = nearest_project_stem(vault, "Weddign 2026")
    assert (stem, candidates) == ("Wedding 2026", ["Wedding 2026"])


def test_prefix_match_for_longer_names():
    vault = _vault(["GTD MCP connector", "Finances"])
    stem, candidates = nearest_project_stem(vault, "GTD MCP")
    assert (stem, candidates) == ("GTD MCP connector", ["GTD MCP connector"])


def test_substring_match_for_longer_names():
    vault = _vault(["Great Exhibition Road Festival", "Finances"])
    stem, candidates = nearest_project_stem(vault, "Exhibition Road")
    assert (stem, candidates) == ("Great Exhibition Road Festival", ["Great Exhibition Road Festival"])


def test_multiple_candidates_returns_none_and_the_list():
    vault = _vault(["Alpha Project", "Alpha-Project", "Beta Project"])
    stem, candidates = nearest_project_stem(vault, "Alph Project")
    assert stem is None
    assert candidates == ["Alpha Project", "Alpha-Project"]


def test_no_candidates_when_nothing_is_close():
    vault = _vault(["Sample", "Widget"])
    stem, candidates = nearest_project_stem(vault, "Completely Unrelated Name")
    assert stem is None
    assert candidates == []
