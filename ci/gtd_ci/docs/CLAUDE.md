# CLAUDE.md — working in this GTD vault

> Read this before editing anything here. It is the house style for humans and
> agents alike. Where it disagrees with [`FORMAT.md`](FORMAT.md), `FORMAT.md`
> wins and this file needs fixing.

---

## 1. Philosophy

A markdown-native GTD system, edited in Obsidian, versioned in git. Plain text,
no database, no proprietary format. The design principle is extreme simplicity.

> **"80% of projects are just an outcome and a next action."**
> — the project template

Do not over-structure. A project page can be a one-line goal and a single
unchecked checkbox. Add sections only when the project actually demands them.
An agent's most common failure here is elaborating: adding status tables,
sub-projects and headings nobody asked for.

---

## 2. Where things go

```
Inbox.md              capture, unprocessed, one item per line
Next actions.md       the table you work from
Delegated.md          waiting on other people
Scheduled.md          date-tied reminders (the calendar is elsewhere)
Tickler/              Next week / two weeks / month / quarter
Project details/      one page per project
Project details/Done/ finished projects, moved not deleted
Reference/            kept, never actioned
attachments/          images and .msg files embedded with ![[...]]
```

### The rule people break most often

**Project pages always go in `Project details/`.** Never at the repo root.

```
✅ Project details/Kitchen refit.md
❌ Kitchen refit.md
```

---

## 3. Formats

`FORMAT.md` is the specification. This section is the short version.

### `Next actions.md`

A markdown table, exactly these columns:

```markdown
| Action | Project | Deadline | Priority |
| ------ | ------- | -------- | -------- |
| Ring the plumber |  | 2026-10-01 | 5 |
| Draft the agenda | [[Team offsite]] |  |  |
```

- **Priority** is an integer, higher is more urgent, blank is unprioritised.
  It is *relative*: to make something top priority, look at the current
  maximum and go above it. Roughly: 1–2 low, 3–4 medium, 5–6 high, 7+ urgent.
- **Project** is an Obsidian wiki-link whose text matches the project
  filename without `.md`.
- **Deadline** is `YYYY-MM-DD`. Other forms are read but flagged; a day and
  month with no year is treated as blank.
- **Attachments** embed inline: `![[some email.msg]]`, `![[photo.png]]`.
- Line breaks inside a cell are `<br>`.

### Project pages

```markdown
# Goal

One line: what "done" looks like.

## Next Actions

- [ ] The next thing to do
- [x] Something already done
```

- The heading must be `## Next Actions` — automation finds the list by that
  heading, and a page without one is invisible to it.
- Actions are top-level checkboxes. Indented lines are notes, never actions.
- The first unchecked item is the project's current next action, and it
  should have a row in `Next actions.md` whose Action text matches **exactly**.
- `## Context`, `## Status`, `## Notes`, `## References` are optional extras.

### `Delegated.md` and `Scheduled.md`

```markdown
| Thing | Person | Chase by | Priority |
| Thing | Date | Status | Event |
```

Both may carry an optional trailing `Project` column linking the row to a
project page. **Chase by** is the column that matters in `Delegated.md`: a row
without one never resurfaces. `Status` in `Scheduled.md` is blank,
`to-schedule`, `find-existing` or `linked`.

### `Tickler/`

Plain lines, one item each, optionally stamped `[YYYY-MM-DD] `. No tables.

### Front matter

`Inbox.md`, `Next actions.md`, `Delegated.md`, `Scheduled.md` and the four
tickler files each start with a `gtd:` key (`gtd: inbox`, `gtd: next-actions`,
…). It identifies the file to automation. Do not remove it.

---

## 4. Common tasks

**Add an action.** Add a row to `Next actions.md`. If it belongs to a project,
add the same text as a checkbox on the project page and link the row to it.

**Create a project.** Copy `Project details/-Project template.md` to
`Project details/<Name>.md`, write the goal, add the first action as a
checkbox, then add the matching row to `Next actions.md`.

**Record project context.** Notes about a project — a decision, a design, what
a conversation settled — go under `## Notes` on its page, after the
`## Next Actions` section, with a `### <YYYY-MM-DD>` subheading per day. Never
inside `## Next Actions`: everything there is an action. Automation only ever
appends to prose, so a note is safe from the nightly job.

**Mark something done.** Tick the checkbox on the project page *and* remove the
row from `Next actions.md`. A standalone action with no project: just remove
the row. When a project is finished, **move** the file to
`Project details/Done/` — never delete it.

**Process the inbox.** Each item goes to exactly one of: next actions, a
project, scheduled, tickler, delegated, reference, or the bin. An empty inbox
is the goal.

**In Obsidian, prefer the plugin buttons** over hand-editing: every eligible
line and row shows ✓ done, → Next actions / Delegated / Tickler / Scheduled /
Inbox, → Project, and a priority control. They get the format right by
construction.

---

## 5. Git

```bash
git pull origin <branch>     # always pull first
# ... edit ...
git add <files>
git commit -m "Descriptive message"
git push origin <branch>
```

Obsidian's git plugin auto-commits in the background with messages like
`vault backup: 2026-06-11 15:02:33`, and the nightly job commits too, so
divergence is normal and expected:

```bash
git pull --rebase origin <branch>   # prefer rebase, keep the history linear
```

For hand-made commits, describe the change: `Add: book train tickets
(priority 6)`, `Update: mark paper tasks complete`, `Move: studentship to
Done/`.

Work in a local clone with ordinary file tools. Editing through a web API
invites encoding corruption and gives you nothing to diff.

---

## 6. Automation

The nightly job lives in [`gtd-engine`](https://github.com/charlesbaynham/gtd-engine),
not in this vault. This vault's `.github/workflows/nightly-maintenance.yml` (or
`.gitlab-ci.yml`) is a thin caller that pulls the engine at branch `v2` and runs
it here. It does five things in order:

1. **expire** — dated tickler lines, scheduled rows and delegated chase-bys
   whose date has arrived go back into `Inbox.md` with a `[from …]` prefix.
2. **stamp** — undated tickler lines get today + the bucket's offset.
3. **promote** — a project with unchecked actions and no row anywhere gets one
   in `Next actions.md`.
4. **sort** — next actions by priority then deadline, delegated by chase-by.
5. **lint** — writes `CI status.md`: what changed, and what looks wrong.

Run it yourself:

```bash
pip install "gtd-ci @ git+https://github.com/charlesbaynham/gtd-engine@v2#subdirectory=ci"
python -m gtd_ci run --dry-run --today "$(date +%F)"   # writes nothing
python -m gtd_ci run --today "$(date +%F)"
```

`--vault PATH` overrides the vault root (default: the current directory).

`CI status.md` and `reMarkable status.md` are outputs. Never edit them; they
are overwritten on every run.

`FORMAT.md`, and everything above the marker line in this file, are refreshed
by that nightly job from `gtd-engine` and overwritten on every run. Put
vault-specific notes below the marker, not above it.

---

## 7. Hard-earned lessons

**UTF-8 or death.** Every `.md` file must stay valid UTF-8, LF line endings,
no BOM. A file with a BOM or a `\r` is a parse failure: automation leaves it
untouched and reports it. If something corrupts a file:

```bash
file <filename>
iconv -f UTF-8 -t UTF-8 < corrupted.md > fixed.md
```

**Done means moved.** Completed projects go to `Project details/Done/`. Keeping
them preserves the record of what was decided and why.

**Scheduled is not a calendar.** `Scheduled.md` holds *reminders about* dated
things. Real events live in a real calendar.

**Match the action text exactly.** A `Next actions.md` row whose Action text
differs from the project's checkbox by even a word is reported as a mismatch
and the project looks stalled.

**Check the path before saving a project page.** See §2.

---

## 8. Troubleshooting

| Problem | Cause | Fix |
|---|---|---|
| `git push` rejected, non-fast-forward | Obsidian or CI committed while you edited | `git pull --rebase` then push |
| File looks binary or corrupted | Encoding damage | `iconv`, or recreate the file |
| Project link does not resolve in Obsidian | Wrong folder or wrong link text | File must be `Project details/<Name>.md` and the link must match the stem |
| Project reported stalled | No unchecked item, or none surfaced in any table | Add an action, or add a row linking the project |
| Priority sorting looks wrong | Priorities blank or tied | Use distinct integers for the things that matter |
| Nightly job ran but nothing was committed | Push permission | GitHub: workflow permissions must be read/write. GitLab: `GTD_PUSH_TOKEN` must exist and be unexpired |
