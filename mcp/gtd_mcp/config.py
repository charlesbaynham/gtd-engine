"""Environment configuration. Names are fixed — the deployment sets these."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    vault_dir: str
    remote_url: str | None
    push_token: str | None
    push_user: str
    webhook_secret: str | None
    allowed_users: frozenset[str]
    host: str
    port: int
    poll_seconds: int
    git_name: str
    git_email: str
    branch: str

    @property
    def local_only(self) -> bool:
        return not self.remote_url


def load_config() -> Config:
    allowed = os.environ.get("GTD_ALLOWED_USERS", "")
    return Config(
        vault_dir=os.environ.get("GTD_VAULT_DIR", "/data/vault"),
        remote_url=os.environ.get("GTD_REMOTE_URL") or None,
        push_token=os.environ.get("GTD_PUSH_TOKEN") or None,
        push_user=os.environ.get("GTD_PUSH_USER", "oauth2"),
        webhook_secret=os.environ.get("GTD_WEBHOOK_SECRET") or None,
        allowed_users=frozenset(u.strip() for u in allowed.split(",") if u.strip()),
        host=os.environ.get("GTD_MCP_HOST", "0.0.0.0"),
        port=int(os.environ.get("GTD_MCP_PORT", "8000")),
        poll_seconds=int(os.environ.get("GTD_POLL_SECONDS", "900")),
        git_name=os.environ.get("GTD_GIT_NAME", "GTD MCP"),
        git_email=os.environ.get("GTD_GIT_EMAIL", "gtd-mcp@noreply"),
        branch=os.environ.get("GTD_BRANCH", "master"),
    )
