"""FastMCP app: tool registration, /health, /hooks/gitlab, per-call allow-list."""
from __future__ import annotations

import hmac
import os
import threading
from datetime import date
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from . import brief as briefmod
from . import ops as opsmod
from . import views
from .config import Config
from .store import Store, WriteOutcome

_FIXED_TODAY_ENV = "GTD_MCP_FIXED_TODAY"


def _today() -> date:
    fixed = os.environ.get(_FIXED_TODAY_ENV)
    return date.fromisoformat(fixed) if fixed else date.today()


def _auth_user(ctx: Context) -> str | None:
    request: Request | None = getattr(ctx.request_context, "request", None)
    return request.headers.get("x-auth-user") if request is not None else None


def _check_auth(config: Config, ctx: Context) -> None:
    """The Secret Server discipline: re-checked on every call, not just at the
    border, so a login dropped from GTD_ALLOWED_USERS loses access immediately
    rather than riding out mcp-auth's up-to-30-day refresh token."""
    if not config.allowed_users:
        return
    user = _auth_user(ctx)
    if user not in config.allowed_users:
        raise PermissionError(f"user {user!r} is not on GTD_ALLOWED_USERS")


def create_server(config: Config) -> tuple[FastMCP, Store]:
    store = Store(config)
    mcp = FastMCP("gtd-mcp", host=config.host, port=config.port)

    def do_read(ctx: Context, fn) -> Any:
        _check_auth(config, ctx)
        return store.read(fn)

    def do_write(ctx: Context, op, dry_run: bool, **kwargs) -> dict[str, Any]:
        _check_auth(config, ctx)
        outcome: WriteOutcome = store.write(op, _today(), dry_run=dry_run, **kwargs)
        return outcome.as_dict()

    # --- the brief ---

    @mcp.tool()
    def get_brief(ctx: Context, today: str | None = None) -> dict:
        """The morning brief in one call: top 10 next actions by priority then
        deadline; everything overdue, due today, or due this week across next
        actions/delegated/scheduled; inbox depth and items recently returned
        from expiry; tickler items landing this week; stalled projects; and
        the current lint findings. `today` overrides the server's date
        (YYYY-MM-DD), for testing what tomorrow's brief will look like."""
        d = date.fromisoformat(today) if today else _today()
        return do_read(ctx, lambda vault: briefmod.get_brief(vault, d))

    # --- reads ---

    @mcp.tool()
    def list_next_actions(
        ctx: Context,
        project: str | None = None,
        min_priority: int | None = None,
        due_before: str | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """Next actions.md rows, each with a handle, sorted priority
        descending then deadline ascending (blank last) -- the same order the
        nightly `sort` job leaves them in. Filter by project stem, a minimum
        priority, or a deadline on/before `due_before` (YYYY-MM-DD)."""

        def fn(vault):
            items = views.next_actions(vault)
            if project:
                items = [i for i in items if i["project"] and i["project"].lower() == project.lower()]
            if min_priority is not None:
                items = [i for i in items if (i["priority"] or 0) >= min_priority]
            if due_before:
                cutoff = date.fromisoformat(due_before)
                items = [
                    i for i in items if i["deadline"]["value"] and date.fromisoformat(i["deadline"]["value"]) <= cutoff
                ]
            items.sort(key=lambda i: -(i["priority"] if i["priority"] is not None else -(10**9)))
            return items[:limit] if limit else items

        return do_read(ctx, fn)

    @mcp.tool()
    def list_inbox(ctx: Context) -> list[dict]:
        """Inbox.md items, each with a handle and, when it was returned by
        `expire`, the `from: {source, detail}` provenance it carries."""
        return do_read(ctx, views.inbox)

    @mcp.tool()
    def list_delegated(ctx: Context, chase_before: str | None = None) -> list[dict]:
        """Delegated.md rows, each with a handle. `chase_before` (YYYY-MM-DD)
        keeps only rows whose Chase by is on or before that date. Each row
        also carries `project`/`project_link_status` (ok/done/dangling/None)
        when the row's optional Project column links a project page."""

        def fn(vault):
            items = views.delegated(vault)
            if chase_before:
                cutoff = date.fromisoformat(chase_before)
                items = [
                    i for i in items if i["chase_by"]["value"] and date.fromisoformat(i["chase_by"]["value"]) <= cutoff
                ]
            return items

        return do_read(ctx, fn)

    @mcp.tool()
    def list_scheduled(
        ctx: Context, from_date: str | None = None, to_date: str | None = None, status: str | None = None
    ) -> list[dict]:
        """Scheduled.md rows, each with a handle. Filter by a date window
        (YYYY-MM-DD, inclusive) and/or Status (to-schedule/find-existing/linked).
        Each row also carries `project`/`project_link_status`
        (ok/done/dangling/None) when the row's optional Project column links
        a project page."""

        def fn(vault):
            items = views.scheduled(vault)
            if from_date:
                lo = date.fromisoformat(from_date)
                items = [i for i in items if i["date"]["value"] and date.fromisoformat(i["date"]["value"]) >= lo]
            if to_date:
                hi = date.fromisoformat(to_date)
                items = [i for i in items if i["date"]["value"] and date.fromisoformat(i["date"]["value"]) <= hi]
            if status is not None:
                items = [i for i in items if i["status"] == status]
            return items

        return do_read(ctx, fn)

    @mcp.tool()
    def list_tickler(ctx: Context, bucket: str | None = None) -> list[dict]:
        """Tickler items across all four buckets (or just `bucket`, one of
        Next week/Next two weeks/Next month/Next quarter), each with a handle
        and its stamped due date, if any."""
        return do_read(ctx, lambda vault: views.tickler(vault, bucket))

    @mcp.tool()
    def list_projects(ctx: Context, include_done: bool = False) -> list[dict]:
        """Every project page carrying a '## Next Actions' heading: goal,
        status bullets, checkbox items with handles, and the linked
        Next actions.md row if any. Set include_done to also list
        Project details/Done/."""
        return do_read(ctx, lambda vault: views.projects(vault, include_done))

    @mcp.tool()
    def get_project(ctx: Context, name: str, include_body: bool = False) -> dict | None:
        """One project page by stem (filename without .md), active or done.
        Set `include_body` to also get `body`: the page's full text verbatim,
        including everything outside '## Next Actions' — the notes, the
        status bullets, whatever else the page carries. Ask for it before
        editing prose, so an edit is made against the real text."""
        return do_read(ctx, lambda vault: views.get_project(vault, name, include_body))

    @mcp.tool()
    def search(ctx: Context, query: str, kinds: list[str] | None = None) -> dict:
        """Case-insensitive substring search over typed vault content (never
        raw grep). `kinds` restricts to any of: next-actions, delegated,
        scheduled, inbox, tickler, projects; omit for all of them."""

        def fn(vault):
            q = query.lower()
            want = set(kinds) if kinds else {"next-actions", "delegated", "scheduled", "inbox", "tickler", "projects"}
            out: dict[str, list[dict]] = {}
            if "next-actions" in want:
                out["next_actions"] = [i for i in views.next_actions(vault) if q in i["action"].lower()]
            if "delegated" in want:
                out["delegated"] = [i for i in views.delegated(vault) if q in i["thing"].lower()]
            if "scheduled" in want:
                out["scheduled"] = [i for i in views.scheduled(vault) if q in i["thing"].lower()]
            if "inbox" in want:
                out["inbox"] = [i for i in views.inbox(vault) if q in i["text"].lower()]
            if "tickler" in want:
                out["tickler"] = [i for i in views.tickler(vault) if q in i["text"].lower()]
            if "projects" in want:
                out["projects"] = [
                    p for p in views.projects(vault, include_done=True)
                    if q in p["stem"].lower() or (p["goal"] and q in p["goal"].lower())
                ]
            return out

        return do_read(ctx, fn)

    @mcp.tool()
    def lint(ctx: Context) -> list[dict]:
        """Current gtd_ci lint findings (the same checks behind CI status.md),
        computed fresh, on demand -- report-only, changes nothing."""

        def fn(vault):
            from gtd_ci.jobs import lint as lint_job
            from gtd_ci.report import Report

            report = Report()
            lint_job.run(vault, _today(), report)
            return [{"severity": f.severity, "code": f.code, "file": f.file, "message": f.message} for f in report.findings]

        return do_read(ctx, fn)

    # --- write: capture and triage ---

    @mcp.tool()
    def capture(ctx: Context, text: str, dry_run: bool = False) -> dict:
        """Append `text` to Inbox.md as a new last line. The one-second
        capture path; everything else can wait for triage. Set dry_run to see
        the diff without committing."""
        return do_write(ctx, opsmod.capture, dry_run, text=text)

    @mcp.tool()
    def add_next_action(
        ctx: Context,
        action: str,
        project: str | None = None,
        deadline: str | None = None,
        priority: int | None = None,
        dry_run: bool = False,
    ) -> dict:
        """Append a row to Next actions.md. `project` is a project stem
        (filename without .md); it must already resolve to a project page
        (FORMAT.md §6) or the call fails with near-name suggestions. `deadline`
        is a canonical date (YYYY-MM-DD[ HH:MM])."""
        return do_write(ctx, opsmod.add_next_action, dry_run, action=action, project=project, deadline=deadline, priority=priority)

    @mcp.tool()
    def delegate(
        ctx: Context,
        thing: str,
        person: str,
        chase_by: str | None = None,
        priority: int | None = None,
        project: str | None = None,
        dry_run: bool = False,
    ) -> dict:
        """Append a row to Delegated.md. Every delegated row carries a
        chase-by date, or it just rots: omitting `chase_by` defaults it to
        today + 7 days and returns `chase_by_defaulted` plus a `warning` in
        the payload, so pass an explicit date when a week is wrong. `project`
        (a project stem) surfaces this row against that project (FORMAT.md
        §6); it must already resolve to a project page or the call fails
        with near-name suggestions. Giving it adds a Project column to
        Delegated.md the first time it is used."""
        return do_write(ctx, opsmod.delegate, dry_run, thing=thing, person=person, chase_by=chase_by, priority=priority, project=project)

    @mcp.tool()
    def schedule(
        ctx: Context,
        thing: str,
        date: str,
        status: str | None = None,
        event: str | None = None,
        project: str | None = None,
        dry_run: bool = False,
    ) -> dict:
        """Append a row to Scheduled.md. `status` is one of blank,
        to-schedule, find-existing, linked. `event` is the Google Calendar
        link once Status is linked. `project` (a project stem) surfaces this
        row against that project (FORMAT.md §6); it must already resolve to
        a project page. Giving it adds a Project column to Scheduled.md the
        first time it is used."""
        return do_write(ctx, opsmod.schedule, dry_run, thing=thing, date=date, status=status, event=event, project=project)

    @mcp.tool()
    def add_to_tickler(
        ctx: Context, bucket: str, text: str, project: str | None = None, dry_run: bool = False
    ) -> dict:
        """Append an undated line to a Tickler bucket (Next week/Next two
        weeks/Next month/Next quarter) -- the nightly `stamp` job dates it.
        `project` (a project stem) appends a trailing `[[stem]]` link that
        surfaces this line against that project (FORMAT.md §6)."""
        return do_write(ctx, opsmod.add_to_tickler, dry_run, bucket=bucket, text=text, project=project)

    @mcp.tool()
    def triage(
        ctx: Context,
        handle: str,
        to: str,
        project: str | None = None,
        deadline: str | None = None,
        priority: int | None = None,
        person: str | None = None,
        chase_by: str | None = None,
        date: str | None = None,
        status: str | None = None,
        event: str | None = None,
        text: str | None = None,
        dry_run: bool = False,
    ) -> dict:
        """Move the item a handle addresses (Inbox, Tickler, or any table row)
        to `to`: next-actions, delegated, scheduled, tickler:<bucket>,
        project:<stem>, inbox, or trash. Strips a returned item's provenance
        prefix and stamp first (FORMAT.md §5.1). Extra fields are the
        target's own columns; a source Priority carries over automatically
        when the target also has one and none is given here, and likewise a
        source Project link (a Delegated/Scheduled row's Project column or a
        Tickler line's trailing `[[link]]`) carries into a next-actions/
        delegated/scheduled/tickler:<bucket> target unless `project` is given
        explicitly here -- triage to inbox or to a project page never
        carries a project. Triage to delegated always sets a Chase by --
        today + 7 days when none is given, reported as `chase_by_defaulted`
        and a `warning` in the payload. `text` rewrites the item as it is
        moved. The target is written before the source is removed, so a
        failure never loses the item."""
        fields = {
            k: v
            for k, v in dict(
                project=project, deadline=deadline, priority=priority, person=person,
                chase_by=chase_by, date=date, status=status, event=event,
            ).items()
            if v is not None
        }
        return do_write(ctx, opsmod.triage, dry_run, handle=handle, to=to, text=text, **fields)

    # --- write: complete, update, delete ---

    @mcp.tool()
    def complete(ctx: Context, handle: str, dry_run: bool = False) -> dict:
        """Complete the item a handle addresses. Any table row or Tickler/
        Inbox line carrying a project link (Next actions' Project column,
        Delegated/Scheduled's Project column when present, or a Tickler
        line's trailing `[[link]]`) ticks the matching unchecked item on
        that project page (by exact trimmed text) and removes the row/line;
        a project checkbox item ticks it and removes any Next actions.md row
        that pointed at it; anything else is just removed. Payload notes
        `no_matching_item` when a linked project had no unchecked item
        matching the row/line's text."""
        return do_write(ctx, opsmod.complete, dry_run, handle=handle)

    @mcp.tool()
    def update(
        ctx: Context,
        handle: str,
        action: str | None = None,
        project: str | None = None,
        deadline: str | None = None,
        priority: int | None = None,
        thing: str | None = None,
        person: str | None = None,
        chase_by: str | None = None,
        date: str | None = None,
        status: str | None = None,
        event: str | None = None,
        text: str | None = None,
        dry_run: bool = False,
    ) -> dict:
        """Rewrite fields of the row/item a handle addresses (FORMAT.md §4.3
        Editing). Table rows accept their own columns (Next actions:
        action/project/deadline/priority; Delegated: thing/person/chase_by/
        priority/project; Scheduled: thing/date/status/event/project);
        Inbox, Tickler and project checkbox items accept only `text`. Fields
        left as None are unchanged; a Delegated row's chase_by cannot be
        blanked (clearing it resets it to today + 7 days and warns). Setting
        `project` on a Delegated/Scheduled row that has no Project column
        yet adds one (padding every other row with a blank cell); clearing
        `project` on a row with no Project column is a no-op."""
        fields = {
            k: v
            for k, v in dict(
                action=action, project=project, deadline=deadline, priority=priority, thing=thing,
                person=person, chase_by=chase_by, date=date, status=status, event=event, text=text,
            ).items()
            if v is not None
        }
        return do_write(ctx, opsmod.update, dry_run, handle=handle, **fields)

    @mcp.tool()
    def delete(ctx: Context, handle: str, dry_run: bool = False) -> dict:
        """Remove the row/line/item a handle addresses. For a project
        checkbox item this deletes the line outright (use `complete` to tick
        it instead)."""
        return do_write(ctx, opsmod.delete, dry_run, handle=handle)

    # --- write: projects ---

    @mcp.tool()
    def create_project(
        ctx: Context,
        name: str,
        goal: str,
        first_action: str = "",
        priority: int | None = None,
        dry_run: bool = False,
    ) -> dict:
        """Create Project details/<name>.md from the template, with `goal`
        under '# Goal' and `first_action` as its first unchecked item, plus a
        matching row in Next actions.md. Refuses if a project with that name
        already exists anywhere under Project details/ (Done/ included).

        `first_action` is optional and has no stock default: pass the text
        of the thing that made this a project. Left empty, the project is
        created with no action and reads as STALLED — better than an
        invented placeholder the user has to notice and delete."""
        return do_write(ctx, opsmod.create_project, dry_run, name=name, goal=goal, first_action=first_action, priority=priority)

    @mcp.tool()
    def add_project_action(ctx: Context, project: str, text: str, dry_run: bool = False) -> dict:
        """Append `- [ ] text` to a project's Next Actions section (FORMAT.md
        §6 Appending): fills an empty placeholder item if one is present,
        otherwise adds it as the section's last line."""
        return do_write(ctx, opsmod.add_project_action, dry_run, project=project, text=text)

    @mcp.tool()
    def append_project_note(
        ctx: Context,
        project: str,
        text: str,
        heading: str = opsmod.DEFAULT_NOTE_HEADING,
        dated: bool = True,
        dry_run: bool = False,
    ) -> dict:
        """Append free prose to a project page — a design decision, what a
        conversation settled, context that is not a task. `text` lands at the
        end of the level-2 section named `heading` ('Notes' by default,
        FORMAT.md §6), which is created at end of file if the page has none;
        `## Next Actions` and everything in it are never touched (use
        `add_project_action` for work). `dated` prefixes the note with a
        `### <today>` subheading, at most one per day per section. Markdown
        in `text` is written verbatim, except that a level-1/2 heading is
        refused: it would end the section the note is written into."""
        return do_write(ctx, opsmod.append_project_note, dry_run, project=project, text=text, heading=heading, dated=dated)

    @mcp.tool()
    def set_project_goal(ctx: Context, project: str, goal: str, dry_run: bool = False) -> dict:
        """Rewrite the paragraph under a project page's '# Goal' heading, so
        a goal given at creation can be corrected later. The previous text
        comes back in the payload as `previous_goal`."""
        return do_write(ctx, opsmod.set_project_goal, dry_run, project=project, goal=goal)

    @mcp.tool()
    def tick_project_action(ctx: Context, handle: str, dry_run: bool = False) -> dict:
        """Tick a project checkbox item without touching Next actions.md.
        Use `complete` instead when the item also has a row there."""
        return do_write(ctx, opsmod.tick_project_action, dry_run, handle=handle)

    @mcp.tool()
    def archive_project(ctx: Context, name: str, dry_run: bool = False) -> dict:
        """Move a project page to Project details/Done/<name>.md and remove
        any Next actions.md rows that linked to it."""
        return do_write(ctx, opsmod.archive_project, dry_run, name=name)

    # --- housekeeping ---

    @mcp.tool()
    def run_maintenance(ctx: Context, today: str | None = None, dry_run: bool = False) -> dict:
        """Run the nightly CI pipeline on demand (expire, stamp, promote,
        sort, lint) -- the same gtd_ci code the 03:30 job runs, useful when
        the brief wants today's expiries before that job has run. Unlike the
        CLI, this never writes CI status.md; lint findings come back in the
        payload instead."""

        def op(vault, today_arg):
            return opsmod.run_maintenance(vault, today_arg)

        d = date.fromisoformat(today) if today else None
        outcome = store.write(op, d or _today(), dry_run=dry_run)
        return outcome.as_dict()

    return mcp, store


def build_app(config: Config) -> Starlette:
    mcp, store = create_server(config)
    app = mcp.streamable_http_app()

    async def health(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "head": store.head_sha(),
                "last_sync": store.last_sync.isoformat() if store.last_sync else None,
            }
        )

    debounce_lock = threading.Lock()
    debounce_timer: list[threading.Timer | None] = [None]

    async def gitlab_hook(request: Request):
        if not config.webhook_secret:
            return PlainTextResponse("not found", status_code=404)
        token = request.headers.get("x-gitlab-token", "")
        if not hmac.compare_digest(token, config.webhook_secret):
            return PlainTextResponse("forbidden", status_code=403)
        try:
            body = await request.json()
        except Exception:
            body = {}
        ref = body.get("ref", "")
        after = body.get("after", "")
        if ref and ref != f"refs/heads/{config.branch}":
            return JSONResponse({"ignored": "different branch"})
        if after and after == store.last_pushed_sha:
            return JSONResponse({"ignored": "own push"})

        with debounce_lock:
            if debounce_timer[0] is not None:
                debounce_timer[0].cancel()
            timer = threading.Timer(2.0, store.sync)
            timer.daemon = True
            debounce_timer[0] = timer
            timer.start()
        return JSONResponse({"ok": True})

    app.router.routes.append(Route("/health", health, methods=["GET"]))
    app.router.routes.append(Route("/hooks/gitlab", gitlab_hook, methods=["POST"]))

    if config.poll_seconds > 0:
        stop = threading.Event()

        def poll_loop() -> None:
            while not stop.wait(config.poll_seconds):
                try:
                    store.sync()
                except Exception:
                    pass  # backstop: a bad sync must not kill the poller, next tick retries

        threading.Thread(target=poll_loop, daemon=True).start()

    return app


def run_stdio(config: Config) -> None:
    mcp, _store = create_server(config)
    mcp.run(transport="stdio")
