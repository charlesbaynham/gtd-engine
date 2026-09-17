"""Store git plumbing: dry_run leaves the tree clean, a parse failure refuses
the write, and a genuine push race is replayed against the new head."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from datetime import date

from gtd_mcp import ops as opsmod
from gtd_mcp.config import Config
from gtd_mcp.store import Store

FIXTURE_VAULT = Path(__file__).resolve().parents[1].parent / "fixtures" / "round-trip" / "input"
TODAY = date(2026, 9, 10)


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _init_local_repo(path: Path) -> None:
    shutil.copytree(FIXTURE_VAULT, path)
    _git(path, "init", "-q", "-b", "master")
    _git(path, "config", "user.email", "test@test")
    _git(path, "config", "user.name", "test")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "seed")


def _config(vault_dir: Path, remote_url: str | None = None) -> Config:
    return Config(
        vault_dir=str(vault_dir), remote_url=remote_url, push_token=None, push_user="oauth2", webhook_secret=None,
        allowed_users=frozenset(), host="127.0.0.1", port=8000, poll_seconds=0,
        git_name="Test", git_email="test@test", branch="master",
    )


def test_dry_run_leaves_tree_clean(tmp_path: Path) -> None:
    vault_dir = tmp_path / "vault"
    _init_local_repo(vault_dir)
    store = Store(_config(vault_dir))

    before_status = _git(vault_dir, "status", "--porcelain")
    before_head = _git(vault_dir, "rev-parse", "HEAD").strip()

    outcome = store.write(opsmod.capture, TODAY, dry_run=True, text="should not persist")
    assert outcome.ok
    assert "should not persist" in outcome.diff

    assert _git(vault_dir, "status", "--porcelain") == before_status
    assert _git(vault_dir, "rev-parse", "HEAD").strip() == before_head
    assert "should not persist" not in (vault_dir / "Inbox.md").read_text(encoding="utf-8")


def test_parse_failure_refuses_and_changes_nothing(tmp_path: Path) -> None:
    vault_dir = tmp_path / "vault"
    _init_local_repo(vault_dir)
    # Break Next actions.md's header so gtd_ci raises ParseFailure on load.
    na = vault_dir / "Next actions.md"
    text = na.read_text(encoding="utf-8").replace("| Action | Project | Deadline | Priority |", "| Nope |")
    na.write_text(text, encoding="utf-8")
    _git(vault_dir, "add", "-A")
    _git(vault_dir, "commit", "-q", "-m", "break header")
    before_head = _git(vault_dir, "rev-parse", "HEAD").strip()

    store = Store(_config(vault_dir))
    outcome = store.write(opsmod.add_next_action, TODAY, action="Should not be added")
    assert not outcome.ok
    assert "Next actions.md" in outcome.error
    assert _git(vault_dir, "rev-parse", "HEAD").strip() == before_head
    assert _git(vault_dir, "status", "--porcelain") == ""


def test_push_conflict_is_replayed_against_new_head(tmp_path: Path) -> None:
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)

    seed = tmp_path / "seed"
    _init_local_repo(seed)
    _git(seed, "remote", "add", "origin", str(bare))
    subprocess.run(["git", "push", "-q", str(bare), "master"], cwd=seed, capture_output=True)

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(bare), str(clone)], check=True)
    attacker = tmp_path / "attacker"
    subprocess.run(["git", "clone", "-q", str(bare), str(attacker)], check=True)
    _git(attacker, "config", "user.email", "attacker@test")
    _git(attacker, "config", "user.name", "attacker")

    store = Store(_config(clone, remote_url=str(bare)))

    calls = {"n": 0}

    def racy_capture(vault, today, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            # A concurrent writer pushes between our sync and our push.
            (attacker / "Inbox.md").write_text(
                (attacker / "Inbox.md").read_text(encoding="utf-8") + "concurrent line\n", encoding="utf-8"
            )
            _git(attacker, "add", "-A")
            _git(attacker, "commit", "-q", "-m", "concurrent push")
            push = subprocess.run(["git", "push", str(bare), "master"], cwd=attacker, capture_output=True, text=True)
            assert push.returncode == 0, push.stderr
        return opsmod.capture(vault, today, text=f"attempt {calls['n']}")

    outcome = store.write(racy_capture, TODAY)

    assert outcome.ok, outcome.error
    assert calls["n"] == 2, "the op should have been replayed exactly once after the conflict"

    log = _git(Path(bare), "log", "--format=%s", "master")
    assert "concurrent push" in log
    assert "mcp: Captured \"attempt 2\" to Inbox.md" in log
    assert "attempt 1" not in _git(bare, "show", "master:Inbox.md")
    assert "attempt 2" in _git(bare, "show", "master:Inbox.md")
    assert "concurrent line" in _git(bare, "show", "master:Inbox.md")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
