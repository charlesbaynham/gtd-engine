# gtd-engine

The shared engine behind every vault created from
[`gtd-template`](https://github.com/charlesbaynham/gtd-template). Vaults used
to vendor a frozen copy of this code under `.gtd/`; now they depend on a tag
of this repo instead, so a fix or a feature ships once and every vault picks
it up on its next nightly run.

## The three packages

| Package | Import name | What it does |
|---|---|---|
| [`ci/`](ci/) | `gtd_ci` | Parses and rewrites the vault's markdown: expire, stamp, promote, sort, lint. `FORMAT.md` and the engine-owned half of `CLAUDE.md` ship as its package data (`gtd_ci/docs/`). |
| [`mcp/`](mcp/) | `gtd_mcp` | An MCP server exposing the vault as structured tools, built on `gtd_ci`. |
| [`remarkable/`](remarkable/) | `gtd_remarkable` | The vault side of the reMarkable paper round-trip, built on `gtd_ci` and `gtd_mcp`. The device/vision half lives in the separate [`remarkable-gtd`](https://github.com/charlesbaynham/remarkable-gtd) repo and is an optional extra. |

[`nix/`](nix/) packages `gtd-mcp` as a NixOS module and a Proxmox LXC
template. `fixtures/` holds the shared test fixtures both `ci` and the
Obsidian plugin are checked against.

## How a vault consumes this repo

A vault installs the packages it needs from a release branch, never `main`:

```bash
pip install "gtd-ci @ git+https://github.com/charlesbaynham/gtd-engine@v2#subdirectory=ci"
```

Its GitHub Actions workflows are thin callers of the reusable workflows here
(`.github/workflows/nightly-maintenance.yml@v2` etc.), and its GitLab CI
includes [`gitlab/vault.gitlab-ci.yml`](gitlab/vault.gitlab-ci.yml) the same
way. `python -m gtd_ci migrate --vault .` converts a vault that still vendors
`.gtd/` into this shape in one commit; `python -m gtd_ci refresh-docs --vault .`
is what the nightly job uses to keep `FORMAT.md` and the engine-owned half of
`CLAUDE.md` in sync afterwards.

`v2` is a release **branch**: it moves forward — fast-forward only — with every
compatible release, and a vault pinned to it needs no action to pick up a new
one. Version tags (`vX.Y.Z`) are immutable and mark each release on it. (`v1`
was a floating *tag*; it is frozen at the last v1 release and no longer moves.)

## Releasing

```bash
git tag vX.Y.Z origin/main
git push origin vX.Y.Z
git push origin origin/main:refs/heads/v2   # fast-forward; never --force
```

A new major is a search-and-replace of the branch name: `GTD_ENGINE_REF` in the
reusable workflows and in `gitlab/vault.gitlab-ci.yml`, the callers
`gtd_ci migrate` writes, and the install lines in the docs (so the `v3` files
install `v3`); then create the `v3` branch and move each vault's callers from
`@v2` to `@v3`.

Bump the **major** version only for a lockstep-breaking change — the kind
where an old vault would misbehave against the new engine, like the
`gtd.decisions/1` → `/2` schema change that prompted splitting the engine out
in the first place. A vault stays on `@v2` until its owner deliberately moves
it to `@v3`; a floating `main`, by contrast, would break every vault the
moment such a change merged. Everything else — a bug fix, a new job, a new
MCP tool — is a minor or patch release on the same major branch.
