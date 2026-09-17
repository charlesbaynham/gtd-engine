"""The git-backed vault store: sync, load, apply-op-with-replay, commit, push.

One `threading.Lock` serialises every git operation and every vault load, so
concurrent tool calls never race on the working tree. A write is: sync, load,
apply the semantic op, write dirty files, commit, push; on a non-fast-forward
push it resets to the new origin head and replays the op from scratch (the
op is a function of fresh state, not a patch, so this is always well-defined)
up to three times before giving up.
"""
from __future__ import annotations

import difflib
import os
import subprocess
import threading
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

from gtd_ci.model import Vault, load_vault, save_vault

from .config import Config
from .handles import Stale
from .ops import OpError, OpResult

_MAX_RETRIES = 3


class GitError(RuntimeError):
    pass


def _redact(text: str, token: str | None) -> str:
    return text.replace(token, "***") if token else text


# The token reaches git through a credential helper reading the environment,
# never on the command line or in .git/config where `ps` and a clone would
# keep it. A deploy token authenticates as its own username; a project access
# token as any name (`oauth2` by convention).
_CREDENTIAL_HELPER = (
    "!f() { [ \"$1\" = get ] && printf 'username=%s\\npassword=%s\\n' \"$GTD_PUSH_USER\" \"$GTD_PUSH_TOKEN\"; }; f"
)


@dataclass
class WriteOutcome:
    ok: bool
    summary: str = ""
    commit: str | None = None
    diff: str | None = None
    stale: dict[str, str] | None = None
    error: str | None = None
    payload: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        out = {
            "ok": self.ok,
            "summary": self.summary,
            "commit": self.commit,
            "diff": self.diff,
            "stale": self.stale,
            "error": self.error,
        }
        out.update(self.payload or {})
        return out


class Store:
    def __init__(self, config: Config):
        self.config = config
        self.root = Path(config.vault_dir)
        self._lock = threading.Lock()
        self._last_pushed_sha: str | None = None
        self._last_sync: datetime | None = None
        self._ensure_clone()

    @property
    def last_sync(self) -> datetime | None:
        return self._last_sync

    @property
    def last_pushed_sha(self) -> str | None:
        return self._last_pushed_sha

    # --- git plumbing ---

    def _git(self, *args: str, cwd: Path | None = None) -> str:
        proc = subprocess.run(self._git_cmd(*args), cwd=cwd or self.root, capture_output=True, text=True, env=self._git_env())
        if proc.returncode != 0:
            raise GitError(_redact(f"git {' '.join(args)} failed: {proc.stderr}", self.config.push_token))
        return proc.stdout.strip()

    def _git_cmd(self, *args: str) -> list[str]:
        cred = ["-c", f"credential.helper={_CREDENTIAL_HELPER}"] if self.config.push_token else []
        return ["git", *cred, *args]

    def _git_env(self) -> dict[str, str]:
        env = dict(os.environ)
        if self.config.push_token:
            env["GTD_PUSH_TOKEN"] = self.config.push_token
            env["GTD_PUSH_USER"] = self.config.push_user
        return env

    def _is_repo(self) -> bool:
        return (self.root / ".git").exists()

    def _ensure_clone(self) -> None:
        if self._is_repo() or not self.config.remote_url:
            return
        self.root.parent.mkdir(parents=True, exist_ok=True)
        self._git("clone", "--branch", self.config.branch, self.config.remote_url, str(self.root), cwd=self.root.parent)

    def head_sha(self) -> str | None:
        if not self._is_repo():
            return None
        try:
            return self._git("rev-parse", "HEAD")
        except GitError:
            return None

    def _sync_locked(self) -> None:
        if self.config.remote_url and self._is_repo():
            self._git("fetch", self.config.remote_url, self.config.branch)
            self._git("reset", "--hard", "FETCH_HEAD")
        self._last_sync = datetime.now(timezone.utc)

    def sync(self) -> None:
        with self._lock:
            self._sync_locked()

    # --- reads ---

    def read(self, fn: Callable[[Vault], Any]) -> Any:
        with self._lock:
            self._sync_locked()
            vault = load_vault(self.root)
            return fn(vault)

    # --- writes ---

    def write(self, op: Callable[..., OpResult], today: date, *, dry_run: bool = False, **kwargs) -> WriteOutcome:
        with self._lock:
            self._sync_locked()
            for attempt in range(_MAX_RETRIES):
                vault = load_vault(self.root)
                try:
                    result = op(vault, today, **kwargs)
                except Stale as exc:
                    return WriteOutcome(ok=False, stale={"handle": exc.handle, "reason": exc.reason})
                except OpError as exc:
                    return WriteOutcome(ok=False, error=str(exc))

                save_vault(vault)

                if dry_run:
                    diff = self._diff(sorted(result.changed_files))
                    self._discard()
                    return WriteOutcome(ok=True, summary=result.summary, diff=diff, payload=result.payload)

                if not self._is_repo():
                    return WriteOutcome(ok=True, summary=result.summary, payload=result.payload)

                self._git("add", "-A")
                if not self._git("status", "--porcelain"):
                    return WriteOutcome(ok=True, summary=result.summary, payload=result.payload)
                self._git(
                    "-c", f"user.name={self.config.git_name}",
                    "-c", f"user.email={self.config.git_email}",
                    "commit", "-m", f"mcp: {result.summary}",
                )
                sha = self._git("rev-parse", "HEAD")

                if not self.config.remote_url:
                    return WriteOutcome(ok=True, summary=result.summary, commit=sha, payload=result.payload)

                push = subprocess.run(
                    self._git_cmd("push", self.config.remote_url, f"HEAD:{self.config.branch}"),
                    cwd=self.root, capture_output=True, text=True, env=self._git_env(),
                )
                if push.returncode == 0:
                    self._last_pushed_sha = sha
                    return WriteOutcome(ok=True, summary=result.summary, commit=sha, payload=result.payload)

                # Non-fast-forward: someone else pushed between our sync and our
                # push. The op is data, not a patch, so replaying it against
                # fresh state is well-defined -- reset and loop.
                self._sync_locked()
                if attempt == _MAX_RETRIES - 1:
                    return WriteOutcome(
                        ok=False,
                        error=f"push rejected after {_MAX_RETRIES} attempts: {_redact(push.stderr, self.config.push_token)}",
                    )
            return WriteOutcome(ok=False, error="push retries exhausted")  # pragma: no cover

    def _diff(self, changed: list[str]) -> str:
        parts: list[str] = []
        for relpath in changed:
            before = ""
            if self._is_repo():
                proc = subprocess.run(["git", "show", f"HEAD:{relpath}"], cwd=self.root, capture_output=True, text=True)
                if proc.returncode == 0:
                    before = proc.stdout
            path = self.root / relpath
            after = path.read_text(encoding="utf-8") if path.is_file() else ""
            if before == after:
                continue
            parts.extend(
                difflib.unified_diff(
                    before.splitlines(keepends=True), after.splitlines(keepends=True),
                    fromfile=relpath, tofile=relpath,
                )
            )
        return "".join(parts)

    def _discard(self) -> None:
        if self._is_repo():
            self._git("reset", "--hard", "HEAD")
            self._git("clean", "-fd")
