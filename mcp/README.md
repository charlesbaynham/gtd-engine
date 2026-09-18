# gtd_mcp

An MCP server that exposes the GTD vault as structured tools (next actions,
delegated, scheduled, tickler, inbox, projects) instead of raw markdown. It
imports [`gtd_ci`](../ci/) for every bit of parsing, rendering, sorting and
date logic — this package only adds handle-based addressing, semantic write
operations, read views and the git-backed store around them.

## Running locally

```bash
cd /path/to/gtd-engine
pip install -e 'ci[test]' -e 'mcp[test]'

# stdio, for a local Claude Code session, against a scratch vault directory
# (no git remote — the store just commits locally, or writes plainly if the
# directory isn't a git repo at all):
gtd-mcp stdio --vault /path/to/scratch-vault

# streamable-HTTP on :8000, against the real clone:
GTD_VAULT_DIR=/path/to/gtd gtd-mcp serve
```

`--today YYYY-MM-DD` fixes "today" for every call — useful for testing what
tomorrow's brief or maintenance run will do.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `GTD_VAULT_DIR` | `/data/vault` | Where the vault clone lives. |
| `GTD_REMOTE_URL` | unset | Git HTTPS URL. Unset = local-only: no fetch/push, just commits (or plain writes if `GTD_VAULT_DIR` isn't a git repo at all). |
| `GTD_PUSH_TOKEN` | unset | Password for HTTPS fetch/push, handed to git via a credential helper reading the environment. Never logged. |
| `GTD_PUSH_USER` | `oauth2` | Username paired with the token: a deploy token's own username, or anything for a project access token. |
| `GTD_WEBHOOK_SECRET` | unset | Compared against `X-Gitlab-Token` with `hmac.compare_digest`. Unset ⇒ `/hooks/gitlab` returns 404. |
| `GTD_ALLOWED_USERS` | unset | Comma-separated logins. When set, every tool call must carry `X-Auth-User` matching one of them, re-checked per call (not just at the mcp-auth border). Unset ⇒ no check (local mode). |
| `GTD_MCP_HOST` / `GTD_MCP_PORT` | `0.0.0.0` / `8000` | Bind address for `serve`. |
| `GTD_POLL_SECONDS` | `900` | Backstop sync interval; `0` disables it (the webhook is still the fast path). |
| `GTD_GIT_NAME` / `GTD_GIT_EMAIL` | `GTD MCP` / `gtd-mcp@noreply` | Commit identity. |
| `GTD_BRANCH` | `master` | Branch to sync and push. |

## Tools

**The brief:** `get_brief`.

**Read:** `list_next_actions`, `list_inbox`, `list_delegated`,
`list_scheduled`, `list_tickler`, `list_projects`, `get_project`, `search`,
`lint`.

**Write:** `capture`, `add_next_action`, `delegate`, `schedule`,
`add_to_tickler`, `triage`, `complete`, `update`, `delete`, `create_project`,
`add_project_action`, `append_project_note`, `set_project_goal`,
`tick_project_action`, `archive_project`.

**Housekeeping:** `run_maintenance`.

Anything that writes a Delegated.md row (`delegate`, `triage to delegated`)
always sets a Chase by: a row without one never resurfaces, so an omitted
`chase_by` defaults to today + 7 days, and `update` cannot blank it. When the
default is used the payload carries `chase_by_defaulted` and a `warning`, so
the caller can replace it with a date that actually fits.

Project pages hold prose as well as actions. `append_project_note` writes it
— a decision, the shape of a design, whatever a conversation settled — to a
named level-2 section (`## Notes` by default), dated with a `### <today>`
subheading unless `dated=False`, and never inside `## Next Actions`;
`set_project_goal` rewrites the paragraph under `# Goal`; and
`get_project(include_body=True)` returns the page verbatim, so an edit is
made against the real text rather than a guess at it. Together they close
the gap that used to send project context around the MCP and straight at the
git remote.

Every write tool takes `dry_run: bool = False` and returns
`{ok, summary, commit, diff, stale, error, ...payload}` — `diff` is populated
only for `dry_run`, `stale` only when a handle no longer resolves
unambiguously (see `handles.py`), `error` only for a validation failure.

Every read tool returns items carrying a `handle` (`handles.py`): a
content-hashed address like `next-actions:3:a1b2c3d4` that survives the
nightly `sort` reordering rows underneath it, and that write tools accept in
place of a line number.

## Testing

```bash
cd /path/to/gtd-engine
pip install 'mcp==1.26.0'   # pin exactly: floating to a newer SDK mid-run
                            # is a known source of silent breakage
pip install -e 'ci[test]' -e 'mcp[test]'
cd mcp && pytest
```

Fixtures live in `mcp/fixtures/mcp-<name>/`, in the same `input/` /
`expected/` / `case.yml` shape as `../fixtures/`, but `case.yml` carries
`op: <name>` and `args: {...}` instead of `jobs: [...]`.
