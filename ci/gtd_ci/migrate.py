"""One-shot conversion of a vault that vendors `.gtd/` into one that depends on
gtd-engine instead. Idempotent by construction — a second run touches nothing.
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

from . import docs_sync

_SECTION_9 = re.compile(r"^## 9\. This vault\s*$", re.MULTILINE)

_NIGHTLY_MAINTENANCE_YML = """\
name: Nightly GTD maintenance
on:
  schedule: [{ cron: "30 2 * * *" }]
  workflow_dispatch: {}
permissions:
  contents: write
jobs:
  maintain:
    uses: charlesbaynham/gtd-engine/.github/workflows/nightly-maintenance.yml@v1
    with:
      vault_tz: Europe/London
"""

_REMARKABLE_YML = """\
name: reMarkable round-trip
on:
  workflow_run:
    workflows: [Nightly GTD maintenance]
    types: [completed]
  workflow_dispatch: {}
permissions:
  contents: write
jobs:
  remarkable:
    if: github.event_name == 'workflow_dispatch' || github.event.workflow_run.conclusion == 'success'
    uses: charlesbaynham/gtd-engine/.github/workflows/remarkable.yml@v1
    with:
      vault_tz: Europe/London
    secrets:
      RMAPI_DEVICE_TOKEN: ${{ secrets.RMAPI_DEVICE_TOKEN }}
      OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}
"""

_GITLAB_CI_YML = """\
include:
  - remote: https://raw.githubusercontent.com/charlesbaynham/gtd-engine/v1/gitlab/vault.gitlab-ci.yml
variables:
  VAULT_TZ: Europe/London
"""

_GITIGNORE_ENTRIES = ("remarkable-out", ".pip-cache")


def _log(msg: str) -> None:
    print(msg)


def _delete(path: Path, vault: Path, dry_run: bool) -> None:
    if not path.exists():
        return
    _log(f"delete {path.relative_to(vault)}")
    if dry_run:
        return
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def _write(path: Path, content: str, vault: Path, dry_run: bool) -> None:
    if path.is_file() and path.read_text(encoding="utf-8") == content:
        return
    _log(f"write {path.relative_to(vault)}")
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _sync_workflow(path: Path, wanted: bool, content: str, vault: Path, dry_run: bool) -> None:
    """Ensure `path` holds `content` when wanted, or is absent when not — whether
    it started out missing, as the old vendored version, or already migrated."""
    if wanted:
        _write(path, content, vault, dry_run)
    else:
        _delete(path, vault, dry_run)


def _drop_lxc_build(vault: Path, dry_run: bool) -> None:
    """The engine builds and releases the template itself now, from a generic
    image a deployment configures from /data/config.env. A vault's own build
    only ever existed to bake deployment.nix in, so both go."""
    workflow = vault / ".github" / "workflows" / "lxc-template.yml"
    deployment = vault / ".gtd" / "nix" / "deployment.nix"
    if not deployment.is_file():
        deployment = vault / "deployment.nix"
    settings = deployment.read_text(encoding="utf-8") if deployment.is_file() else ""

    _delete(workflow, vault, dry_run)
    _delete(deployment, vault, dry_run)
    if settings:
        print(
            "deployment.nix is gone: its settings now belong in /data/config.env on "
            "the container, as GTD_REMOTE_URL / GTD_BRANCH / GTD_ALLOWED_USERS. "
            "See gtd-engine's nix/config.env.example. It held:\n" + settings,
            file=sys.stderr,
        )


def _mentions_rmapi(path: Path) -> bool:
    return path.is_file() and "RMAPI_DEVICE_TOKEN" in path.read_text(encoding="utf-8")


def _wants_remarkable(vault: Path, flag: bool) -> bool:
    workflows = vault / ".github" / "workflows"
    # Once migrated, the thin caller carries no RMAPI_DEVICE_TOKEN mention of
    # its own — its own presence is what has to keep a re-run idempotent.
    if flag or (workflows / "remarkable.yml").is_file():
        return True
    return any(
        _mentions_rmapi(p)
        for p in (vault / ".gitlab-ci.yml", workflows / "nightly-maintenance.yml")
    )


def _migrate_claude_md(vault: Path, dry_run: bool) -> None:
    dst = vault / "CLAUDE.md"
    if not dst.is_file():
        docs_sync.refresh_claude(vault, dry_run)
        return

    old = dst.read_text(encoding="utf-8")
    if docs_sync.MARKER in old:
        docs_sync.refresh_claude(vault, dry_run)
        return

    match = _SECTION_9.search(old)
    if match is None:
        print(
            "CLAUDE.md has no marker and no '## 9. This vault' section; "
            "left as is — add the marker by hand to have it refreshed",
            file=sys.stderr,
        )
        _log("unchanged CLAUDE.md")
        return

    tail = old[match.start():]
    new = f"{docs_sync.doc_text('CLAUDE.md')}\n{docs_sync.MARKER}\n\n{tail}"
    if new == old:
        _log("unchanged CLAUDE.md")
        return
    _log("refreshed CLAUDE.md")
    if not dry_run:
        dst.write_text(new, encoding="utf-8")


def _ensure_gitignore(vault: Path, dry_run: bool) -> None:
    path = vault / ".gitignore"
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    missing = [e for e in _GITIGNORE_ENTRIES if e not in lines]
    if not missing:
        return
    _log("write .gitignore" if not path.is_file() else "update .gitignore")
    if dry_run:
        return
    path.write_text("\n".join(lines + missing) + "\n", encoding="utf-8")


def migrate(vault: Path, dry_run: bool = False, remarkable: bool = False) -> None:
    if not vault.is_dir():
        raise NotADirectoryError(vault)

    workflows = vault / ".github" / "workflows"
    # A vault that maintains itself from GitLab must not gain a second nightly,
    # and the GitHub reMarkable caller only makes sense after a GitHub nightly.
    wants_nightly = (workflows / "nightly-maintenance.yml").is_file() or not (vault / ".gitlab-ci.yml").is_file()
    wants_remarkable = wants_nightly and _wants_remarkable(vault, remarkable)

    _drop_lxc_build(vault, dry_run)
    _delete(vault / ".gtd", vault, dry_run)
    _delete(vault / "flake.nix", vault, dry_run)
    _delete(vault / "flake.lock", vault, dry_run)

    # Old thick versions (vendored) and stale thin callers (no longer wanted)
    # are both handled by _sync_workflow, so an already-migrated vault sees no
    # further changes and one whose reMarkable setup was removed does.
    # prune-storage.yml is untouched: it already calls nix-proxmox-cattle
    # directly and owns nothing this migration changes.
    _sync_workflow(workflows / "nightly-maintenance.yml", wants_nightly, _NIGHTLY_MAINTENANCE_YML, vault, dry_run)
    _sync_workflow(workflows / "remarkable.yml", wants_remarkable, _REMARKABLE_YML, vault, dry_run)

    gitlab_ci = vault / ".gitlab-ci.yml"
    if gitlab_ci.is_file():
        _write(gitlab_ci, _GITLAB_CI_YML, vault, dry_run)

    _migrate_claude_md(vault, dry_run)
    docs_sync.refresh_format(vault, dry_run)
    _ensure_gitignore(vault, dry_run)
