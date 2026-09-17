# gtd-ci

The nightly maintenance for a GTD vault, and the reference Python
implementation of [`FORMAT.md`](gtd_ci/docs/FORMAT.md). Stdlib only at runtime.

```bash
pip install "gtd-ci @ git+https://github.com/charlesbaynham/gtd-engine@v1#subdirectory=ci"

python -m gtd_ci run --vault ~/gtd --dry-run --today "$(date +%F)"   # plan and diffs, writes nothing
python -m gtd_ci run --vault ~/gtd --today "$(date +%F)"             # expire, stamp, promote, sort, lint
python -m gtd_ci lint --vault ~/gtd --dry-run                        # one job on its own
python -m gtd_ci refresh-docs --vault ~/gtd                          # FORMAT.md + the engine half of CLAUDE.md
python -m gtd_ci migrate --vault ~/gtd                               # one-shot: drop a vendored .gtd/, add the thin callers
```

`--vault` defaults to the current directory. Every command takes `--dry-run`.

The five jobs and their exact rules are FORMAT.md §7; `CI status.md` in the
vault is the report each run overwrites. Tests: `cd ci && pip install -e
'.[test]' && pytest -q` — the fixtures under `../fixtures/` are shared with
the Obsidian plugin.
