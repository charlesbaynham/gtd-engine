# CLAUDE.md — developing gtd-engine

This is the engine repo itself, not a vault. If you're looking for how to
work *inside* a GTD vault, that's `ci/gtd_ci/docs/CLAUDE.md` — it ships as
package data and becomes the vault's own `CLAUDE.md`.

## Layout

```
ci/          gtd_ci        — parsing, rewriting, the five nightly jobs
mcp/         gtd_mcp        — MCP server, built on gtd_ci
remarkable/  gtd_remarkable — reMarkable round-trip, built on gtd_ci + gtd_mcp
fixtures/    shared input/expected cases, read by ci/tests and the Obsidian plugin
nix/         gtd-mcp.nix, cattle.nix, the Proxmox LXC template
.github/workflows/  the engine's own CI, plus the reusable workflows vaults call
gitlab/      the GitLab equivalent of the reusable workflows
```

Inter-package deps (`gtd-mcp` → `gtd-ci`, `gtd-remarkable` → both) are bare
names in `pyproject.toml`, never direct URLs — that's what lets an editable
local install and a one-shot `pip install ... @ git+...` both work without
pip's conflicting-URL error.

## Running the tests

Each package's suite runs from its own directory — **do not** `pytest` from
the repo root, the three `tests/` directories collide under one collection:

```bash
cd ci && pip install -e '.[test]' && pytest -q
cd mcp && pip install -e '../ci[test]' -e '.[test]' && pytest -q
cd remarkable && pip install -e '../ci[test]' -e '../mcp[test]' -e '.[test]' && pytest -q
```

Or from the repo root: `pip install -e ci -e 'mcp[test]' -e 'remarkable[test]'`,
then `cd`-and-`pytest` per package as above.

## Nix

`mcp>=1.26,<2` is pinned deliberately, to match what `nixpkgs` (`nixos-26.05`)
ships. Don't float it in `mcp/pyproject.toml` or a test venv's `mcp` install —
a newer SDK is a known source of silent breakage, and CI must build against
the same version the Nix package does. `nix build .#gtd-mcp` proves the
flake; run it after touching `flake.nix`, `ci/` or `mcp/`.

## FORMAT.md

`ci/gtd_ci/docs/FORMAT.md` is the spec — the single source of truth for every
vault file format. It ships as `gtd_ci` package data and is copied into a
vault's root by `refresh-docs`/`migrate`. Don't edit any other copy; there
isn't meant to be one. `ci/gtd_ci/docs/CLAUDE.md` is the engine-owned half of
a vault's `CLAUDE.md` the same way — see the marker line in
`gtd_ci/docs_sync.py` for where the vault-owned tail begins.
