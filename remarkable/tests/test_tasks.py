from __future__ import annotations

from datetime import date

from gtd_remarkable.tasks import build_tasks, display_text


def test_display_text():
    assert display_text("Deal with ![[Re_ Newsletter.msg]] comments") == "Deal with (Re_ Newsletter.msg) comments"
    assert display_text("See [[Wedding 2026|the wedding]] and [[Other#h]]") == "See the wedding and Other"
    assert display_text("a<br>b   c") == "a b c"
    assert display_text("x \\| y") == "x | y"


def test_build_tasks_shapes(vault):
    tasks = build_tasks(vault, date(2026, 9, 15))
    assert tasks["date"] == "2026-09-15"
    assert [t["act"] for t in tasks["inbox"]] == ["Claim for delayed train", "Chase the EOM quote"]
    assert all(t["handle"].startswith("inbox:") for t in tasks["inbox"])

    nxt = tasks["next"]
    assert [t["act"] for t in nxt] == ["Read Ben's paper", "Deal with (Re_ Newsletter.msg) comments", "Buy an ashtray"]
    assert nxt[0] == {"act": "Read Ben's paper", "pri": "3", "due": "", "proj": "Wedding 2026", "handle": nxt[0]["handle"]}
    assert nxt[1]["due"] == "2026-09-20" and nxt[1]["pri"] == ""

    assert tasks["delegated"] == [
        {"act": "Send the invoice", "to": "Dave", "due": "2026-09-22", "pri": "", "proj": "", "handle": tasks["delegated"][0]["handle"]}
    ]
    assert tasks["delegated"][0]["handle"].startswith("delegated:")

    tick = tasks["tickler"]
    assert [t["act"] for t in tick["week"]] == ["Consider dental coatings"]
    assert tick["week"][0]["tickler"] == "Next week" and tick["week"][0]["due"] == "2026-09-22"
    assert [t["act"] for t in tick["month"]] == ["Sample item"]
    assert tick["quarter"] == []

    assert tasks["context"] == {"projects": ["Boiler service", "Wedding 2026"], "people": ["Dave"]}


def test_build_tasks_projects(vault):
    projects = build_tasks(vault, date(2026, 9, 15))["projects"]
    assert [p["name"] for p in projects] == ["Boiler service", "Wedding 2026"]

    boiler, wedding = projects
    assert wedding["goal"] == "Get married" and wedding["status"] == ["\u2705 Venue booked"]
    assert boiler["starred"] is False and wedding["starred"] is False
    assert wedding["items"] == [
        {"text": "Read Ben's paper", "done": False, "handle": wedding["items"][0]["handle"], "surfaced": "next"},
        {"text": "Pay Sandra", "done": False, "handle": wedding["items"][1]["handle"], "surfaced": None},
    ]
    assert all(h.startswith("project:Wedding 2026:") for h in (i["handle"] for i in wedding["items"]))
    assert wedding["stalled"] is False  # its first action has a Next actions row

    assert boiler["items"][0] == {
        "text": "Find the manual", "done": True, "handle": boiler["items"][0]["handle"], "surfaced": None,
    }
    assert boiler["stalled"] is True  # unchecked items, none of them surfaced anywhere


def test_delegated_row_carries_its_project(tmp_path):
    from gtd_ci.model import load_vault

    from .conftest import write_vault

    root = tmp_path / "v"
    write_vault(
        root,
        delegated_rows="| Bleed the radiators | Plumber | 2026-09-25 |  | [[Boiler service]] |\n",
        delegated_columns=("Thing", "Person", "Chase by", "Priority", "Project"),
        projects={"Boiler service": "# Goal\n\nHeat\n\n## Next Actions\n\n- [ ] Bleed the radiators\n"},
    )
    tasks = build_tasks(load_vault(root), date(2026, 9, 15))
    assert tasks["delegated"][0]["proj"] == "Boiler service"
    assert tasks["projects"][0]["items"][0]["surfaced"] == "delegated"
    assert tasks["projects"][0]["stalled"] is False


def test_archive_to_delete():
    from datetime import datetime, timezone

    from gtd_remarkable.cli import archive_to_delete, sheet_date, sheet_time

    assert sheet_date("20260915Z0330_gtd_sheet") == date(2026, 9, 15)          # minute stamp, pre-2026-09-27
    assert sheet_time("20260915T033012Z_gtd_sheet") == datetime(2026, 9, 15, 3, 30, 12, tzinfo=timezone.utc)
    assert sheet_date("notes") is None and sheet_date("20261399Z0330_gtd_sheet") is None
    assert sheet_date("20260915Z0330_gtd_sheet_applied") is None

    today = date(2026, 9, 15)
    names = ["20260907T100000Z_gtd_sheet", "20260908Z1000_gtd_sheet", "20260913T100000Z_gtd_sheet",
             "20260914T090000Z_gtd_sheet", "20260914T100000Z_gtd_sheet", "notes"]
    # Nothing applied yet: only age counts.
    assert archive_to_delete(names, None, today, 7) == ["20260907T100000Z_gtd_sheet"]
    assert archive_to_delete(names, None, today, 0) == []
    # A later sheet came back inked: everything rendered before it goes; it
    # and anything newer stay.
    applied = datetime(2026, 9, 14, 9, 0, tzinfo=timezone.utc)
    assert archive_to_delete(names, applied, today, 7) == [
        "20260907T100000Z_gtd_sheet", "20260908Z1000_gtd_sheet", "20260913T100000Z_gtd_sheet",
    ]
    # ...and the newest applied sheet goes too once it is a week old.
    old = datetime(2026, 9, 7, 10, 0, tzinfo=timezone.utc)
    assert archive_to_delete(["20260907T100000Z_gtd_sheet"], old, today, 7) == ["20260907T100000Z_gtd_sheet"]
