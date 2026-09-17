"""Runs every case under fixtures/ against the CLI job pipeline.

case.yml keys consumed here: `today`, `jobs` (list of job names, or
`[run]` for the full pipeline), `findings` (optional exact multiset of
{severity, code, file}, message not compared).

Two additional case.yml keys — `strip` (a list of {from, to} pairs) and
`rows` (a list of {cells, line} pairs) — are reserved for the plugin's own
test suite (fixture `plugin-strip`); this runner ignores them.
"""
from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import pytest
import yaml

from gtd_ci.cli import run_jobs
from gtd_ci.model import load_vault, save_vault

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures"


def _all_files(root: Path) -> set[Path]:
    return {p.relative_to(root) for p in root.rglob("*") if p.is_file()}


def _case_dirs() -> list[Path]:
    return sorted(p for p in FIXTURES_DIR.iterdir() if p.is_dir())


@pytest.mark.parametrize("case_dir", _case_dirs(), ids=lambda p: p.name)
def test_fixture(case_dir: Path, tmp_path: Path) -> None:
    case = yaml.safe_load((case_dir / "case.yml").read_text(encoding="utf-8"))
    today = date.fromisoformat(str(case["today"]))
    jobs = case["jobs"]

    work = tmp_path / "vault"
    shutil.copytree(case_dir / "input", work)

    vault = load_vault(work)
    report = run_jobs(vault, today, jobs)
    save_vault(vault)

    expected_dir = case_dir / "expected"
    expected_files = _all_files(expected_dir)
    actual_files = _all_files(work) - {Path("CI status.md")}
    expected_files -= {Path("CI status.md")}

    assert actual_files == expected_files, f"{case_dir.name}: file set mismatch"
    for rel in expected_files:
        expected_bytes = (expected_dir / rel).read_bytes()
        actual_bytes = (work / rel).read_bytes()
        assert actual_bytes == expected_bytes, f"{case_dir.name}: {rel} differs"

    if "findings" in case:
        actual = sorted((f.severity, f.code, f.file) for f in report.findings)
        expected = sorted((f["severity"], f["code"], f["file"]) for f in case["findings"])
        assert actual == expected, f"{case_dir.name}: findings mismatch"
