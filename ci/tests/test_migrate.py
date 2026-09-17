"""Tests for `gtd_ci migrate` — see migrate.py.

Builds a fake pre-split vault: a copy of a real fixture tree (so migrate runs
against actual vault markdown, not an empty directory) plus the `.gtd/`,
`flake.*`, old workflows, `.gitlab-ci.yml` and `CLAUDE.md` a real vault would
have vendored before the split.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from gtd_ci.docs_sync import MARKER, doc_text
from gtd_ci.migrate import migrate

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures"

_DEPLOYMENT_NIX = '{ services.gtd-mcp.remoteUrl = "https://example.com/repo.git"; }\n'

_PRUNE_STORAGE_YML = (
    "name: prune storage\njobs:\n  prune:\n"
    "    uses: charlesbaynham/nix-proxmox-cattle/.github/workflows/prune.yml@v1\n"
)

_CLAUDE_WITH_SECTION_9 = """\
# CLAUDE.md

Some old hand-maintained body.

---

## 9. This vault

- Repo: `https://example.com/repo.git`
- Default branch: `main`
"""


def _build_vault(
    tmp_path: Path,
    *,
    claude_md: str,
    gitlab_mentions_rmapi: bool = True,
    with_deployment_nix: bool = True,
    with_gitlab_ci: bool = True,
    with_old_nightly: bool = True,
    with_old_lxc: bool = True,
) -> Path:
    vault = tmp_path / "vault"
    shutil.copytree(FIXTURES_DIR / "sort" / "input", vault)

    gtd_nix = vault / ".gtd" / "nix"
    gtd_nix.mkdir(parents=True)
    if with_deployment_nix:
        (gtd_nix / "deployment.nix").write_text(_DEPLOYMENT_NIX, encoding="utf-8")
    (vault / "flake.nix").write_text("{ }\n", encoding="utf-8")
    (vault / "flake.lock").write_text("{}\n", encoding="utf-8")

    workflows = vault / ".github" / "workflows"
    workflows.mkdir(parents=True)
    if with_old_nightly:
        (workflows / "nightly-maintenance.yml").write_text("name: old nightly\n", encoding="utf-8")
    if with_old_lxc:
        (workflows / "lxc-template.yml").write_text("name: old lxc\n", encoding="utf-8")
    (workflows / "prune-storage.yml").write_text(_PRUNE_STORAGE_YML, encoding="utf-8")

    if with_gitlab_ci:
        gitlab_body = "stages: [test]\n"
        if gitlab_mentions_rmapi:
            gitlab_body += "variables:\n  RMAPI_DEVICE_TOKEN: masked\n"
        (vault / ".gitlab-ci.yml").write_text(gitlab_body, encoding="utf-8")

    (vault / "CLAUDE.md").write_text(claude_md, encoding="utf-8")
    (vault / ".gitignore").write_text("old-entry\n", encoding="utf-8")
    return vault


def _snapshot(vault: Path) -> dict[Path, bytes]:
    return {p.relative_to(vault): p.read_bytes() for p in vault.rglob("*") if p.is_file()}


def test_migrate_moves_deployment_nix_and_deletes_old_files(tmp_path: Path) -> None:
    vault = _build_vault(tmp_path, claude_md=_CLAUDE_WITH_SECTION_9)
    migrate(vault)

    assert not (vault / ".gtd").exists()
    assert not (vault / "flake.nix").exists()
    assert not (vault / "flake.lock").exists()
    assert (vault / "deployment.nix").read_text(encoding="utf-8") == _DEPLOYMENT_NIX


def test_migrate_writes_thin_callers_and_keeps_prune_storage(tmp_path: Path) -> None:
    vault = _build_vault(tmp_path, claude_md=_CLAUDE_WITH_SECTION_9)
    migrate(vault)

    workflows = vault / ".github" / "workflows"
    nightly = (workflows / "nightly-maintenance.yml").read_text(encoding="utf-8")
    assert "uses: charlesbaynham/gtd-engine/.github/workflows/nightly-maintenance.yml@v1" in nightly
    lxc = (workflows / "lxc-template.yml").read_text(encoding="utf-8")
    assert "uses: charlesbaynham/gtd-engine/.github/workflows/lxc-template.yml@v1" in lxc
    remarkable = (workflows / "remarkable.yml").read_text(encoding="utf-8")
    assert "uses: charlesbaynham/gtd-engine/.github/workflows/remarkable.yml@v1" in remarkable
    # prune-storage.yml is not part of the split; migrate must not touch it.
    assert (workflows / "prune-storage.yml").read_text(encoding="utf-8") == _PRUNE_STORAGE_YML


def test_migrate_skips_remarkable_and_lxc_without_signal(tmp_path: Path) -> None:
    vault = _build_vault(
        tmp_path, claude_md=_CLAUDE_WITH_SECTION_9,
        gitlab_mentions_rmapi=False, with_deployment_nix=False, with_old_lxc=False,
    )
    migrate(vault)

    workflows = vault / ".github" / "workflows"
    assert not (workflows / "remarkable.yml").exists()
    assert not (workflows / "lxc-template.yml").exists()


def test_migrate_keeps_lxc_that_existed_without_deployment_nix(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    vault = _build_vault(tmp_path, claude_md=_CLAUDE_WITH_SECTION_9, with_deployment_nix=False)
    migrate(vault)
    assert "gtd-engine/.github/workflows/lxc-template.yml@v1" in (vault / ".github" / "workflows" / "lxc-template.yml").read_text(encoding="utf-8")
    assert "no deployment.nix found" in capsys.readouterr().err


def test_migrate_gitlab_hosted_vault_gets_no_github_nightly(tmp_path: Path) -> None:
    vault = _build_vault(tmp_path, claude_md=_CLAUDE_WITH_SECTION_9, with_old_nightly=False)
    migrate(vault)
    assert not (vault / ".github" / "workflows" / "nightly-maintenance.yml").exists()
    assert "include:" in (vault / ".gitlab-ci.yml").read_text(encoding="utf-8")


def test_migrate_vault_with_no_ci_gets_github_nightly(tmp_path: Path) -> None:
    vault = _build_vault(tmp_path, claude_md=_CLAUDE_WITH_SECTION_9, with_old_nightly=False, with_gitlab_ci=False)
    migrate(vault)
    assert "nightly-maintenance.yml@v1" in (vault / ".github" / "workflows" / "nightly-maintenance.yml").read_text(encoding="utf-8")


def test_migrate_remarkable_flag_forces_caller(tmp_path: Path) -> None:
    vault = _build_vault(tmp_path, claude_md=_CLAUDE_WITH_SECTION_9, gitlab_mentions_rmapi=False, with_deployment_nix=False)
    migrate(vault, remarkable=True)
    assert (vault / ".github" / "workflows" / "remarkable.yml").is_file()


def test_migrate_replaces_gitlab_ci(tmp_path: Path) -> None:
    vault = _build_vault(tmp_path, claude_md=_CLAUDE_WITH_SECTION_9)
    migrate(vault)
    gitlab_ci = (vault / ".gitlab-ci.yml").read_text(encoding="utf-8")
    assert "include:" in gitlab_ci
    assert "gtd-engine/v1/gitlab/vault.gitlab-ci.yml" in gitlab_ci


def test_migrate_claude_md_section_9_becomes_tail(tmp_path: Path) -> None:
    vault = _build_vault(tmp_path, claude_md=_CLAUDE_WITH_SECTION_9)
    migrate(vault)
    claude = (vault / "CLAUDE.md").read_text(encoding="utf-8")

    before_marker, tail = claude.split(MARKER, 1)
    assert before_marker.rstrip("\n") == doc_text("CLAUDE.md").rstrip("\n")
    assert "## 9. This vault" in tail
    assert "Repo:" in tail
    last_nonblank = [line for line in before_marker.splitlines() if line.strip()][-1]
    assert last_nonblank != "---"


def test_migrate_claude_md_with_marker_only_refreshes_body(tmp_path: Path) -> None:
    existing = f"stale body\n{MARKER}\n\n## This vault\n\nkeep me\n"
    vault = _build_vault(tmp_path, claude_md=existing)
    migrate(vault)
    claude = (vault / "CLAUDE.md").read_text(encoding="utf-8")
    assert claude == f"{doc_text('CLAUDE.md')}\n{MARKER}\n\n## This vault\n\nkeep me\n"


def test_migrate_claude_md_no_marker_no_section_left_untouched(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    original = "# A hand-written CLAUDE.md with no recognisable structure.\n"
    vault = _build_vault(tmp_path, claude_md=original)
    migrate(vault)
    assert (vault / "CLAUDE.md").read_text(encoding="utf-8") == original
    assert "no marker and no '## 9. This vault'" in capsys.readouterr().err


def test_migrate_ensures_gitignore_entries(tmp_path: Path) -> None:
    vault = _build_vault(tmp_path, claude_md=_CLAUDE_WITH_SECTION_9)
    migrate(vault)
    lines = (vault / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "remarkable-out" in lines
    assert ".pip-cache" in lines
    assert "old-entry" in lines


def test_migrate_dry_run_writes_nothing(tmp_path: Path) -> None:
    vault = _build_vault(tmp_path, claude_md=_CLAUDE_WITH_SECTION_9)
    before = _snapshot(vault)
    migrate(vault, dry_run=True)
    assert _snapshot(vault) == before
    assert not (vault / "deployment.nix").exists()


def test_migrate_idempotent(tmp_path: Path) -> None:
    vault = _build_vault(tmp_path, claude_md=_CLAUDE_WITH_SECTION_9)
    migrate(vault)
    before = _snapshot(vault)
    migrate(vault)
    assert _snapshot(vault) == before


def test_migrate_missing_vault_raises(tmp_path: Path) -> None:
    with pytest.raises(NotADirectoryError):
        migrate(tmp_path / "does-not-exist")
