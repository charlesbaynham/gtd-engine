# GTD vault format contract

This file is the single source of truth for the file formats that automation
touches. Two implementations read it: the Python CI in `.gtd/ci/` and the
Obsidian plugin (`gtd-tools`, repo
[`charlesbaynham/obsidian-gtd-plugin`](https://github.com/charlesbaynham/obsidian-gtd-plugin)). Both
are tested against the shared fixtures in `.gtd/fixtures/`. A rule that is not
written here does not exist: add it here first, then implement it.

Everything in the vault stays plain markdown that renders in stock Obsidian.

## 1. Encoding and lines

- UTF-8, no BOM, LF line endings. A file containing a BOM or any `\r` is a
  **parse failure** (§9): it is left byte-identical and reported.
- A file is split into lines on `\n`. Whether the file ends with a trailing
  newline is recorded and reproduced on rewrite.
- "Trimmed" means stripped of leading and trailing ASCII whitespace.

## 2. Front matter and file roles

A managed file starts with a YAML front-matter block: line 1 is exactly `---`,
the block ends at the next line that is exactly `---`. The block is preserved
verbatim; the only key automation reads is `gtd`.

| File | `gtd:` | Kind |
|---|---|---|
| `Inbox.md` | `inbox` | line file |
| `Next actions.md` | `next-actions` | table |
| `Delegated.md` | `delegated` | table |
| `Scheduled.md` | `scheduled` | table |
| `Tickler/Next week.md` | `tickler` | line file, offset +7 days |
| `Tickler/Next two weeks.md` | `tickler` | line file, offset +14 days |
| `Tickler/Next month.md` | `tickler` | line file, offset +30 days |
| `Tickler/Next quarter.md` | `tickler` | line file, offset +91 days |
| `Project details/**/*.md` | `project` (optional) | project page |
| `CI status.md` | `ci-status` | written by CI, never read |
| `reMarkable status.md` | `remarkable-status` | written by the reMarkable job, never read |

CI finds managed files by these fixed paths and checks the `gtd` value; a
mismatch or missing front matter is a parse failure for that file. The plugin
decides a file's kind from the front matter alone (any path). Tickler offsets
are keyed by filename, not front matter.

Project pages are resolved by wiki-link (§6), never by front matter. Files
under `Project details/Done/` are retired projects.

The plugin's "Initialise vault" command creates any of the files above that
are missing (plus `Project details/-Project template.md` and
`Project details/Done/`) in exactly the shapes below — front matter and an
empty body for line files, front matter, header, separator and one
placeholder row for tables — and never touches a file that already exists.

## 3. Dates

Canonical form: `YYYY-MM-DD`, optionally followed by one space and `HH:MM`
(24 h). This is the only form ever **written**.

For **reading**, these additional forms are accepted where the date part is
unambiguous, and each produces a `bad-date` lint warning:

- `YYYY/MM/DD`, `YYYY.MM.DD`
- `D Mon YYYY` and `D Month YYYY` (English month names, case-insensitive,
  e.g. `7 Mar 2026`, `7 March 2026`)

Anything else (including a day and month with no year, such as `7 Mar`) is
**unparseable**: it also produces `bad-date`, and every job treats the cell as
if it were blank (never expires, sorts last).

Comparisons are on the date part only; a time never affects ordering or expiry.

## 4. Table files

A table file contains prose or blank lines, then exactly one table, then
optional trailing lines. The table is located as the first line starting with
`|` that is immediately followed by a **separator row**: a line matching
`^\|(\s*:?-+:?\s*\|)+\s*$`. The line before the separator is the **header
row**; the separator must have the same number of cells as the header, or
the file is a parse failure. **Data rows** are the consecutive lines after the separator that start
with `|`; the table ends at the first line that does not. Everything outside
the data rows (front matter, prose, header, separator, blank lines, trailing
content) is preserved verbatim.

### 4.1 Cells

A row is split into cells on every `|` that is not inside `[[...]]` or
`![[...]]` and is not escaped as `\|`. The first and last split pieces are the
empty strings outside the outer pipes and are discarded. A row must have the
same number of cells as the header; otherwise the file is a parse failure.

Cell content is opaque text (it may contain `<br>`, `![[embeds]]`, links).
Cells are compared **trimmed** and written back **verbatim**.

A **placeholder row** is a data row whose cells are all blank. It is kept and
ignored by every job; when sorting it is treated as having blank keys.

### 4.2 Columns

Header cell names are matched trimmed and case-insensitively; any other header
is a parse failure, except that a table with a declared **optional column**
may also be read with that column present. A file is well-formed with its
header equal to either its base column list or the base list plus its
optional columns, in order; anything else is a parse failure naming both
accepted forms.

**`Next actions.md`** — `| Action | Project | Deadline | Priority |`

- Priority: an integer (`^-?\d+$` after trimming); higher is more urgent;
  blank is unprioritised. Any other content counts as blank and is a
  `bad-priority` lint warning.
- Project: blank, or a wiki-link to a project page (§6).
- Deadline: a date (§3) or blank.

**`Delegated.md`** — `| Thing | Person | Chase by | Priority |`, with an
**optional trailing `Project` column**: `| Thing | Person | Chase by |
Priority | Project |`.

- Chase by: a date or blank. Priority as above. Blank is legal but rots: the MCP server always writes a Chase by, defaulting to today + 7 days.
- Project: when the column is present, blank or a wiki-link to a project
  page (§6), same as Next actions.md's Project column. See §6 "surfaced".

**`Scheduled.md`** — `| Thing | Date | Status | Event |`, with an
**optional trailing `Project` column**: `| Thing | Date | Status | Event |
Project |`.

- Date: a date, optionally with time.
- Status: exactly one of blank, `to-schedule`, `find-existing`, `linked`
  (lowercase). Anything else is a `bad-status` lint warning and is treated as
  blank.
- Event: blank, a markdown link `[text](url)`, or a bare URL. Populated only
  when Status is `linked` (not enforced; the URL is used when present).
- Project: as for Delegated.md's optional Project column.

### 4.3 Rewriting

- **Reordering** emits each data row's original source line verbatim.
- **Adding** a row appends it after the last data row (placeholder rows
  included), formatted `| a | b | c | d |`: each cell is written as a space,
  the cell text, a space; a blank cell is therefore written as two spaces
  (`|  |`). A bare `|` inside cell text is written as `\|`. Existing rows
  are never re-aligned.
- **Removing** a row deletes exactly that line.
- **Editing** one cell (plugin only: Priority, Status) rewrites that row as
  above with the other cells' trimmed text; other rows are untouched.
- **Upgrading** a table that has an optional column declared but absent: a
  table is written back with the column count it was read with unless a
  write needs to put a value in that column, in which case the header and
  separator gain the column (appending ` <Name> |` / ` ---- |`) and every
  existing data row is padded with one blank cell, verbatim rows included
  (their source line gains a trailing `  |`, so the padding is
  indistinguishable from a row that always had the column). This is the
  only way a managed table's column count changes.

## 5. Line files (Inbox and Tickler)

The body after the front matter is a list of lines. A **blank line** (empty or
whitespace only) is a separator and is preserved. Any other line is an
**item**, consisting of:

```
<marker><stamp><text>
```

- `marker`: optional list marker `- ` or `* ` (hyphen or asterisk, one space).
  Preserved where present; never written by automation.
- `stamp` (tickler only): `[YYYY-MM-DD] ` — a bracketed canonical date and one
  space. A line is **dated** if, after the optional marker, it starts with `[`
  followed by an ASCII digit and the bracket closes with a date parseable per
  §3 (`bad-date` if non-canonical). A bracket whose content does not parse
  as any date (`[garbage] …`, `[2026-13-45] …`) is not a stamp: the line is
  undated and will be stamped in front of it. `[[wiki links]]` start with
  `[[`, which is not a stamp.
- `text`: the rest of the line, opaque.

**Stamping** an undated tickler line inserts `[<date>] ` between the marker
and the text. A dated line is never re-stamped.

**Trailing project link (Inbox and Tickler):** a line's `text` may end with
one `[[target]]` (or `[[target|alias]]`/`[[target#heading]]`) matching
`^(.*?)\s*(\[\[[^\]]+\]\])\s*$`; the match is the whole rest of the line
after the marker/stamp, so at most one trailing link is ever recognised.
This is the same project-link syntax as a table's Project cell (§6), used to
**surface** the line's body against that project (§6). It is written by
appending ` [[stem]]` to the line's text; it is never required, and a line
without one behaves exactly as before.

### 5.1 Provenance prefix (Inbox)

Automation that returns an item to the Inbox writes:

```
[from <source>{, <detail>}] <text>
```

- `source`: `Tickler/Next week`, `Tickler/Next two weeks`, `Tickler/Next
  month`, `Tickler/Next quarter`, `Scheduled`, or `Delegated`.
- details, by source:
  - Tickler: `due <date>`
  - Scheduled: `<Date cell, trimmed>`; then `cal: <url>` if Event is
    populated (the URL of the markdown link, or the bare URL).
  - Delegated: `<Person>`, then `chase by <date>` (omitted if Chase by is
    blank).
- `text`: the source item's text (line text after marker and stamp; or the
  Action/Thing cell, trimmed). A Delegated or Scheduled row's optional
  Project cell, when populated, is carried onto the end of `text` as a
  trailing ` [[name]]` link (the raw link name, unresolved), same as a
  Tickler line already carries its own trailing link along with it (it is
  part of `text` there, not added separately).

Examples:

```
[from Tickler/Next month, due 2026-10-08] Read David's talk: ![[Re_ u3a Talks.msg]]
[from Scheduled, 2026-09-21 14:00, cal: https://calendar.google.com/...] Dentist appointment
[from Delegated, Xander, chase by 2026-09-01] Ensure I don't miss the UROP statement deadline
[from Delegated, Bob, chase by 2026-09-01] Chase Alice for review [[Grant Application]]
```

Returned lines are **prepended**: inserted as a block immediately after the
front matter (or at line 1 if there is none), before whatever is already
there, with no separator line added. Within one run the block is in processing
order: tickler files in offset order, then Scheduled rows, then Delegated rows,
each in source order.

**Stripping** (plugin, when moving an item onward): remove one leading
list marker (`- ` or `* `), then one leading provenance prefix matching
`^\[from [^\]]*\]\s*`, then one leading stamp matching
`^\[\d{4}-\d{2}-\d{2}\]\s*`, then trim. The marker is never carried into a
table cell or another line file.

## 6. Project pages and links

A **project link** in a Project cell is `[[target]]`, `[[target|alias]]`,
`[[target#heading]]` or a combination. Inside a table Obsidian writes the
alias separator as `\|`; both `|` and `\|` end the target. The **name** is the last `/`-separated
segment of `target`, trimmed. It resolves to the file `Project details/**/<name>.md`
compared case-insensitively on the filename stem. If several files match, the
one not under `Done/` wins, then the shallowest path. A link that matches no
file is **dangling**. A link resolving only to a file under `Done/` is
`links-done-project` (lint).

Written links use the filename stem as the target: `[[<stem>]]`.

A **project page** is any `.md` under `Project details/` that is not under
`Done/`, is not `-Project template.md`, and contains the heading `## Next
Actions` (level-2 heading, text matched trimmed and case-insensitively).
Files without that heading are ignored by automation.

The **Next Actions section** runs from that heading to the next heading of
level 1 or 2, or end of file. Its **items** are the lines in that section
matching `^[-*] \[( |x|X)\] (.*)$` — top-level only; indented lines are notes
and never items. `[ ]` is unchecked; `[x]`/`[X]` is checked. Item text is
group 2, trimmed. Item order is priority order; the first unchecked item is
the project's current next action.

A project page's next action **should** have a row in `Next actions.md` whose
Project cell resolves to it. The row's Action text should equal an unchecked
item's text exactly (trimmed).

An unchecked item is **surfaced** when some row of Next actions.md,
Delegated.md or Scheduled.md, or some Tickler line, links the item's project
page (§6) and that row's Action/Thing cell, or that line's body (its text
with the trailing link itself, if any, removed), equals the item's text
exactly (trimmed). A project **should** have at least one surfaced unchecked
item; a link that resolves to the right page but matches no unchecked item
is `row-mismatch` (lint), same whether it is a table row or a Tickler line.
Inbox is never a surfaced view: an item just expired into Inbox with a
carried project link, and a freshly promoted row for the same project, can
coexist for one night without either being wrong.

Writes to project pages:

- **Ticking** an item rewrites `- [ ]` as `- [x]` (or `* [ ]` as `* [x]`) on
  that line only.
- **Appending** an item adds `- [ ] <text>` as the last line of the Next
  Actions section (after its last non-blank line). If the heading is absent,
  `## Next Actions` is appended at end of file preceded by one blank line.

- **Promoting** an item to a project copies
  `Project details/-Project template.md` (basename configurable in the
  plugin) to `<template's folder>/<name>.md`, writes the item text as the
  paragraph under `# Goal`, replaces the empty `- [ ] ` placeholder in
  `## Next Actions` with `- [ ] <item text>` (appending it if there is no
  placeholder, creating the section if absent), appends
  `| <item text> | [[<name>]] |  | <priority> |` to `Next actions.md`
  (priority carried per §4.2 when the source row has a Priority column),
  and finally removes the source item. Targets are written before the
  source is removed, so a failure never loses the item.

  The promoted item's own text is therefore both the Goal and the first
  action: no placeholder action is invented. A project may also be created
  with **no** first action at all, in which case `## Next Actions` keeps
  the template's empty `- [ ] ` placeholder and no row is added to
  `Next actions.md`; such a project reads as STALLED, which is accurate.

`## Sub-projects` sections are out of scope.

## 7. Jobs (CI)

All jobs take a fixed `today` (`YYYY-MM-DD`). Order: expire, stamp, promote,
sort, lint.

**expire** — an item is expired when its date is on or before `today`.
Expired items are removed from the source and prepended to the Inbox (§5.1).
Sources: every dated tickler line; every Scheduled row with a parseable Date,
regardless of Status (a `to-schedule` row whose date is strictly before
`today` — i.e. one that expires having already passed, not on the day
itself — is additionally reported as `missed-schedule`, emitted by expire
since lint never sees the row); every Delegated row with a parseable Chase
by.

**stamp** — every undated tickler item gets `[today + offset]`.

**promote** — for every project page with at least one unchecked item and no
surfaced unchecked item (§6) anywhere (Next actions.md, Delegated.md,
Scheduled.md or any Tickler line) and no `row-mismatch` against it either:
add the row `| <first unchecked text> | [[<stem>]] |  |  |` to
`Next actions.md`. Either a surfaced item or a mismatched link suppresses
promotion, so a project already represented — correctly or not — never gets
a second row.

**sort** — `Next actions.md`: Priority descending (blank last), then Deadline
ascending (blank/unparseable last), then original order. `Delegated.md`:
Chase by ascending (blank last), then original order. Stable; rows verbatim.

**lint** — report only. Findings, each with a code, severity, file and
message:

| severity | code | meaning |
|---|---|---|
| error | `parse-failure` | file skipped (§9) |
| warn | `dangling-link` | Project link (any table row or Tickler line) resolves to no file |
| warn | `links-done-project` | Project link (any table row or Tickler line) resolves only under `Done/` |
| warn | `bad-date` | non-canonical or unparseable date in a date column or stamp |
| warn | `bad-priority` | non-integer Priority |
| warn | `bad-status` | Status not in the allowed set |
| warn | `missed-schedule` | Scheduled row past its date with Status `to-schedule` |
| warn | `project-stalled` | project page with no unchecked item, or with unchecked item(s) but none surfaced (§6) |
| warn | `row-mismatch` | a table row or Tickler line links a project but its Action/Thing/body matches no unchecked item |
| info | `similar-names` | two files under `Project details/` (Done included) whose names, lowercased and reduced to `[a-z0-9]`, are within Levenshtein distance 3 |

## 8. `CI status.md`

Overwritten by every CI run:

```
---
gtd: ci-status
---
# CI status

Run: <YYYY-MM-DD HH:MM> (today = <YYYY-MM-DD>)

## Changes

- <one line per change, or "none">

## Errors

- <file>: <reason>

## Warnings

- <file>: <code> — <message>

## Info

- <code> — <message>
```

Empty sections contain the single line `- none`. The run time is local
Europe/London time; with a fixed `--today` it is written as `<today> 00:00`
so fixtures are reproducible. The file is rewritten only when some other file
changed or the content below the `Run:` line differs from the previous run,
so a quiet night produces no commit.

### 8.1 `reMarkable status.md`

Overwritten by the nightly reMarkable job (`.gtd/remarkable/`) whenever at
least one sheet came back from the device; left alone otherwise. Same shape
as `CI status.md`: front matter, a `Run:` line, then per sheet `# Sheet
<name>` with `## Applied`, `## Skipped` and `## Warnings` lists (`- none`
when empty). The job writes the vault only through the MCP operations
(`gtd_mcp.ops`), so every edit it makes obeys §4–§6 by construction.

## 9. Parse failures and safety

Whenever a file cannot be parsed under this contract (bad encoding, missing or
wrong front matter, no table, malformed separator, ragged row, unknown header)
the file is skipped in its entirety: no job reads from or writes to it, its
bytes are unchanged, and a `parse-failure` error is reported. Jobs that move
items between files skip a move whenever either end failed to parse. No job
ever deletes a line except as the source side of a move or a Done action.

Round-trip is a hard requirement: parsing any well-formed managed file and
emitting it with no changes produces identical bytes.

## 10. Fixtures

`.gtd/fixtures/<case>/` contains:

- `input/` — a vault snapshot (only the files the case needs)
- `expected/` — the same snapshot after the jobs ran; `CI status.md` is not
  compared byte-for-byte
- `case.yml`:

```yaml
today: 2026-09-10
jobs: [expire, stamp, promote, sort, lint]   # or: [run]
findings:                                   # optional, exact multiset
  - {severity: warn, code: dangling-link, file: Next actions.md}
```

A fixture passes when every file in `expected/` matches the file the jobs
produced byte-for-byte, no unexpected file was created or changed, and the
findings (if listed) match exactly. The plugin's test suite runs the
round-trip check over every `input/` and `expected/` file, and checks its
stripping and row-formatting rules against the `plugin-*` cases.

`Delegated.md` and `Scheduled.md` fixtures may use either their base
4-column header or the 5-column form with a trailing `Project` column
(§4.2/§4.3); both must round-trip byte-for-byte, and a fixture that adds a
Project value to a 4-column table should assert the upgraded header,
separator and padded rows in `expected/`.
