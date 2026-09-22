# gtd-remarkable — the vault side of the reMarkable round-trip

Turns the vault into the tasks JSON that [`remarkable-gtd`](https://github.com/charlesbaynham/remarkable-gtd)
prints, and turns the decisions JSON its scanner produces back into vault
edits through `gtd_mcp.ops` (so FORMAT.md is honoured by construction).

```
gtd-remarkable tasks | render | scan | apply | process | publish
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

So a sheet that comes back with no strokes on it is treated as *pending*:

- it is **not scanned** (no OCR, no vision calls, nothing to apply),
- it is **not archived** and never reaches the rotation that deletes old
  sheets, so it cannot be deleted out from under the tablet,
- and `publish` **does not upload another sheet on top of it**
  (`--max-pending` / `REMARKABLE_MAX_PENDING`, default 1).

Turn WiFi back on, let the tablet sync, and the next run reads the sheet you
actually wrote on, applies it, archives it, and publishes a fresh one.

A blank sheet is retired — archived, and eventually rotated away — only once a
**newer** sheet comes back with ink on it. That is proof the tablet has synced
since the blank one was uploaded, so its blankness is real. Rotation also
never deletes a sheet in the same run that archived it: the `--keep-days`
grace period is keyed on the upload date in the name, and a sheet rescued
after a long offline spell is already "old" by then.

If sheets were archived by an earlier version while your tablet was offline,
move them back into `GTD Daily` on the device (or in the reMarkable app) and
the next run will pick them up. Don't re-run a sheet that has already been
applied — the vault edits are not idempotent.

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
  and the done ones struck through. An action row there takes ✓ Done (which
  ticks it off on the page and drops the Next actions row for it) and ✦ AI.
- **Tick ✦ AI and write anything in the row**: reword it, "→ Louise", "due
  Fri", "priority 8", "defer 1m", "drop", "done", "move to <project>",
  "back to inbox", "also add: ring the venue", "split this into three",
  "rename this project". The whole row goes to the agent, a vision model
  that is told how GTD works and what every vault operation means; it
  returns a `gtd.ai/3` reading with a list of operations (`update`,
  `complete`, `delete`, `move`, `capture`, `add_next_action`, `delegate`,
  `schedule`, `add_to_tickler`, `create_project`, `add_project_action`),
  applied in order. `OPENROUTER_AI_MODEL` picks the model for this call
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
