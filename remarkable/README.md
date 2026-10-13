# gtd-remarkable — the vault side of the reMarkable round-trip

Turns the vault into the tasks JSON that [`remarkable-gtd`](https://github.com/charlesbaynham/remarkable-gtd)
prints, and turns the decisions JSON its scanner produces back into vault
edits through `gtd_mcp.ops` (so FORMAT.md is honoured by construction).

```
gtd-remarkable tasks | render | scan | apply | process | publish | reference-sync
```

`SETUP.md` § 10 turns it on; `.gitlab-ci.yml` is the nightly job itself.

## One-time setup

1. **reMarkable token.** On any machine: install [rmapi](https://github.com/ddvk/rmapi),
   run `rmapi ls` once and follow the one-time code login. Copy `devicetoken`
   from `~/.config/rmapi/rmapi.conf` (or `~/.rmapi`) into the masked GitLab CI
   variable `RMAPI_DEVICE_TOKEN`. The short-lived user token is refreshed by
   rmapi from the device token on every run.
2. **OpenRouter.** Create a key at openrouter.ai and store it as the masked CI
   variable `OPENROUTER_API_KEY`. `OPENROUTER_MODEL` (optional) picks the
   model; the default is Google Gemini Flash (`remarkable_gtd.scan.ocr.DEFAULT_OPENROUTER_MODEL`).
   `OPENROUTER_AI_MODEL` (optional) overrides it for the ✦ AI agent alone.
   Only inked write-in boxes are sent, as small PNG crops, plus one crop of
   each ✦ AI-ticked row — a sheet with a few handwritten fields costs a
   fraction of a cent.
3. Trigger the pipeline once from the GitLab UI ("Run pipeline") to get the
   first sheet onto the device; from then on the 03:30 schedule does it.

`SETUP.md` § 10 walks through all three with the exact settings pages.

## Running it without CI

The same two values are read from the environment, so the round-trip works
from a laptop too — useful for a first try, or for a vault that is not on
GitLab at all:

```bash
pip install -e ci -e mcp -e 'remarkable[device]'
export OPENROUTER_API_KEY=sk-or-...        # required by `scan` and `process`
export RMAPI_DEVICE_TOKEN=...              # omit if this machine already ran
                                           # `rmapi ls` and has ~/.config/rmapi
export TODAY="$(date +%F)"

gtd-remarkable process --vault . --work-dir remarkable-out --today "$TODAY"
git add -A && git commit -m "reMarkable: apply handwritten decisions"
gtd-remarkable publish --vault . --work-dir remarkable-out --today "$TODAY"
```

`process` is safe to re-run: nothing is archived on the device until
`publish`, so a failed commit just means the sheet is picked up again.

## When the tablet is offline

A sheet is only finished with **once the ink on it has reached the cloud**.
While your reMarkable is offline it keeps your ticks on its own copy, and the
cloud copy stays byte-identical to the one that was uploaded — so from CI's
side, "you have been offline for a week with a full sheet" and "you have not
written on it yet" look exactly the same.

So nothing is ever decided from a blank sheet. Instead, **every run sweeps
every sheet not yet applied**, in `GTD Daily` and in `GTD Daily/Archive`, and
applies the ink on any of them, however old the sheet is.

That lets the device stay current, so the pipeline can run hourly:

- **The sheet follows the vault.** When the vault no longer matches what the
  current sheet prints (a new item, a change, or simply a new day), `publish`
  moves the sheet to the archive and uploads a fresh one. If nothing changed,
  nothing is uploaded.
- **Late ink still lands.** Moving a document in the cloud while the tablet
  holds unsynced strokes for it is safe. The tablet syncs by document id, not
  by folder, so when WiFi comes back the ink arrives on the document in the
  archive, with no conflict copy. This was tested on a real device on
  2026-09-27. The next run applies it against the tasks that sheet was
  printed with. A row whose item has since changed in the vault is re-found
  by its text, or reported.
- **Applied sheets are never applied twice.** Every sheet is named for the
  moment it was rendered, to the second (`20260927T173012Z_gtd_sheet`, UTC).
  The vault records the stamp of the newest sheet applied so far in
  `.remarkable.json`, committed together with the edits it produced. A sheet
  stamped at or before it is never read again. Nothing on the device is
  renamed.

The archive is cleared of:

- every sheet rendered **before a later sheet came back with ink**, since that
  ink proves the tablet synced after they were replaced, and
- every sheet rendered more than **a week** ago
  (`--keep-days` / `REMARKABLE_KEEP_DAYS`, default 7, 0 keeps them all).

Both rules can delete ink that has not reached the cloud: writing on an old
sheet while offline, after a later sheet has already come back inked or after
a week has passed. So can a sheet that failed to read, once a later one is
applied or it is a week old; the failure is reported in `reMarkable status.md`
on every run until then.

**Upgrading from an earlier version:** the first run finds no
`.remarkable.json` and takes every sheet already in the archive as applied,
since an earlier version only archived sheets it had applied or found blank.
It records that and clears them, so nothing is applied twice.

## Writing on the sheet

The sheet is Inbox, Next Actions, Delegated and Tickler, then a read-only
Projects summary, one page per project, and a New Projects page.

- **Tick a gutter box** to act on a row; one box per row (if several are
  ticked, Done/Next win and the rest is reported as a warning).
- **Write inside the metadata boxes**: PRIORITY (an integer), DUE (`6 Jun`,
  `2026-06-06`, `Fri`, `tomorrow`…), PROJECT (a project name — a near miss
  like "Weding 2026" is matched and noted; something that matches two
  projects, or none, is reported and left off), TO (who you delegated to —
  needed for → Deleg).
- **New items** go on the Inbox page's six capture rows: write the item and,
  if you like, tick the same gutter it would get anywhere else — blank drops
  it in `Inbox.md`, → Next files it straight into Next actions (with the
  PRIORITY/DUE/PROJECT you wrote), → Deleg into Delegated, Defer into a
  tickler. **New actions for a project** go on that project page's four
  add-an-action lines; they need no tick.
- **NEW** turns a row into a project, the same way the Obsidian plugin's
  "→ Project" button does: write the project name in the PROJECT box, and
  the page is created with the row's own text as both its Goal and its
  first (and only) action; the row itself is consumed. A routing tick on
  the same row is ignored (and reported). Nothing is seeded on your behalf
  — the thing that made you want a project is the thing you want to do
  about it, so there is no "Plan project" placeholder to delete.
- **New projects** go on the **New Projects** page, the sheet's last: six
  blank rows, one project each. Write the project's first action on the
  line and its name in the PROJECT box — no NEW tick needed, the page says
  so. A routing tick on such a row means you changed your mind: the text is
  filed like any Inbox item and no project is created (reported as a note).
  ✦ AI hands the row to the agent instead, which is how you say more than a
  name and a line will carry.
- **Project pages** print the goal, the status lines, every open action with
  a badge for the view it is surfaced in (NA/DG/SC/TK, or STALLED if none),
  and the done ones struck through. An action (a step) is an ordinary
  action that lives on its page: ✓ Done ticks it off and drops every row
  surfacing it; → Deleg (TO = the person, DUE = chase-by) and Defer
  1w/1m/1q move *where it is surfaced* — to Delegated or a tickler, linked
  back to the project — while the checkbox stays on the page, so ticking
  the Delegated row off later ticks the step; ✗ Drop deletes the step and
  its rows; ✦ AI as anywhere else.
- **The project row** at the top of each project page stands for the
  project itself. Write a new name in RENAME TO (the page and every link to
  it are renamed), a new goal in NEW GOAL, tick ★ Star (☆ Unstar on a
  starred project) to pin it, and tick ✓ Finish when the whole
  project is done (its page moves to `Project details/Done/` and every row
  surfacing it goes). They apply star, goal, name, finish in that order and after every other row
  on the sheet, so a step ticked on the same page lands before the page
  moves. ✦ AI on it hands the project to the agent (`rename_project`,
  `set_project_goal`, `archive_project`, `star_project`, `unstar_project`).
- **Starred projects** (`starred: true` in the page's front matter, FORMAT.md
  §6) print first, with a ★, and every page of the sheet carries a hotbar
  under its header linking straight to each one.
- **Tick ✦ AI and write anything in the row**: reword it, "→ Louise", "due
  Fri", "priority 8", "defer 1m", "drop", "done", "move to <project>",
  "back to inbox", "also add: ring the venue", "split this into three",
  "rename this project". The whole row goes to the agent, a vision model
  that is told how GTD works and what every vault operation means; it
  returns a `gtd.ai/3` reading with a list of operations (`update`,
  `complete`, `delete`, `move`, `capture`, `add_next_action`, `delegate`,
  `schedule`, `add_to_tickler`, `create_project`, `add_project_action`,
  `rename_project`, `set_project_goal`, `archive_project`, `star_project`,
  `unstar_project`), applied in order. `OPENROUTER_AI_MODEL` picks the model for this call
  alone.

  **✦ AI switches the deterministic rules off for that row.** It is an
  escape hatch, not an annotation: the row's gutter ticks and slots are
  still read, but only to build a suggestion the agent is given as a
  labelled hint, and nothing here applies them. The agent's operations are
  the row's single write — so a routing box that should still take effect
  has to come back as an operation, and a row the agent could not read is
  reported and left alone rather than falling back to the boxes. "The agent
  could not read it" and "apply the boxes instead" are different answers,
  and only the first one is honest.
- **Nothing is guessed.** Unreadable handwriting, an ambiguous project name
  or an operation that cannot be carried out is reported in
  `reMarkable status.md` (Applied / Notes / Skipped / Warnings) and the row
  is left as it was.

## Reference notes on the device

`gtd-remarkable reference-sync` mirrors `Reference/` onto the tablet, one
device-sized PDF per note, in `GTD Reference/` (`REMARKABLE_REFERENCE_FOLDER`)
with the note's subfolders as device subfolders. Every page has a ✦ AI box;
the last page is a notes page of six blank lines.

- **Changed notes are re-rendered.** The vault records each uploaded note's
  sha256 in `.remarkable-reference.json` (committed with the run); a note that
  differs is rendered and replaced. Unchanged notes cost no device calls.
- **A replaced copy is archived, never deleted.** It is moved to
  `GTD Reference/Archive/<run stamp>/` (`--reference-archive-folder` /
  `REMARKABLE_REFERENCE_ARCHIVE_FOLDER`), after its strokes, if any, have been
  scanned and applied: lines on the notes page are appended to the note under
  today's date, and a ✦ AI page is interpreted into appends, exact-text edits
  or Inbox captures. The new copy is then rendered from the updated note. Ink
  anywhere else without ✦ AI is reported, not read. An edit whose quoted text
  is missing or ambiguous is a warning, never a guess; the warning names the
  archive path the ink is still on. Ink that will not parse leaves the copy
  alone and fails the run (the other notes are still synced and committed).
- **`--read-ink`** also downloads every unchanged note's copy, and every
  archived copy not yet applied (the tablet syncs late). That is a download
  each, so the hourly run leaves it off and a daily schedule turns it on
  (`remarkable_reference_read_ink` / `REMARKABLE_REFERENCE_READ_INK`); until
  then ink on an unchanged note waits on the device. Ink that changed nothing
  (margin writing without ✦ AI) is warned about once and the copy stays put,
  recorded by a fingerprint of its strokes.
- **Self-healing.** An archived copy is deleted only once the *committed*
  `.remarkable-reference.json` says it was applied, or after `--keep-days`
  (`REMARKABLE_KEEP_DAYS`, default 7; 0 keeps all). So a failed push, a late
  tablet or a page the agent could not read costs nothing: archive folders the
  state does not know of are found again by listing on the next `--read-ink`
  run.
- **Deleted notes** are removed from the device, or archived with a warning if
  they were written on. A note moved between folders takes its copy with it.
  A note that cannot be rendered (body overflow, a `|` in the name) is recorded
  with its error and skipped until it changes.
- Results go in a `# Reference notes` section of `reMarkable status.md`.
  `process` rewrites that file when it handles a sheet, so the section is back
  only after the next sync that does something.
- `gtd-remarkable scan` on a reference `.rmdoc` writes its decisions JSON, and
  `gtd-remarkable apply` applies it.

Unsynced ink on a blank-looking copy lives on in the archive, not on the
replacement, until the tablet syncs and a `--read-ink` run reads it.
