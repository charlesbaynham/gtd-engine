"""Decisions -> vault, exercised the way the CI job does it: build the tasks
document from the vault (as the sheet would carry it), then apply a
decisions document keyed by sheet ids."""
from __future__ import annotations

from datetime import date

import pytest

from gtd_ci.model import load_vault, save_vault
from gtd_remarkable.apply import apply_decisions, parse_handwritten_date
from gtd_remarkable.tasks import build_tasks

TODAY = date(2026, 9, 15)


def _tasks_doc(vault) -> dict:
    """What remarkable_gtd.common.embedded.tasks_document would embed: every
    printed row keyed by its sheet id, including the Inbox's blank capture
    rows (CP-*), the New Projects page's blank rows (NP-*), each project's
    open items (P01-02), its project row (P01-PJ) and its add-an-action
    lines (P01-C1)."""
    t = build_tasks(vault, TODAY)
    out = {}
    for i, it in enumerate(t["inbox"], 1):
        out[f"IN-{i:02d}"] = {**it, "bucket": "inbox"}
    for i, it in enumerate(t["next"], 1):
        out[f"NA-{i:02d}"] = {**it, "bucket": "next"}
    for i, it in enumerate(t["delegated"], 1):
        out[f"DG-{i:02d}"] = {**it, "bucket": "delegated"}
    n = 1
    for period in ("week", "month", "quarter"):
        for it in t["tickler"][period]:
            out[f"TK-{n:02d}"] = {**it, "bucket": "tickler", "period": period}
            n += 1
    for i in range(1, 7):
        out[f"CP-{i:02d}"] = {"act": "", "bucket": "capture"}
    for i in range(1, 7):
        out[f"NP-{i:02d}"] = {"act": "", "bucket": "newproj"}
    for pi, proj in enumerate(t["projects"], 1):
        ref = f"P{pi:02d}"
        out[f"{ref}-PJ"] = {"act": proj["name"], "bucket": "projhead", "proj": proj["name"], "goal": proj["goal"]}
        for pos, item in enumerate(proj["items"], 1):
            if item["done"]:
                continue  # printed struck through, no boxes
            out[f"{ref}-{pos:02d}"] = {
                "act": item["text"], "bucket": "project", "proj": proj["name"],
                "handle": item["handle"], "surfaced": item["surfaced"],
            }
        for c in range(1, 5):
            out[f"{ref}-C{c}"] = {"act": "", "bucket": "capture", "proj": proj["name"]}
    return {"schema": "gtd.tasks/1", "date": TODAY.isoformat(), "tasks": out}


def _decisions(*pages) -> dict:
    return {"schema": "gtd.decisions/2", "pages": list(pages)}


def _page(key, tasks=(), captures=(), **extra):
    return {"page_key": key, "tasks": list(tasks), "captures": list(captures), "warnings": [], **extra}


def _d(tid, action="none", **extra):
    return {"id": tid, "action": action, "ai": False, "qr_verified": True, **extra}


def _ai_d(tid, *ops_, suggestion=None, **kw):
    """A row as the scanner emits it when ✦ AI is ticked.

    The scanner demotes the gutter reading into ``suggestion`` and leaves
    ``action`` as ``"none"``, so the entry itself asks for nothing
    deterministic — see remarkable-gtd's ``decisions.resolve_task``.
    """
    out = {"id": tid, "action": "none", "ai": True, "new_project": False,
           "qr_verified": True, "ai_reading": _ai(*ops_, **kw)}
    if suggestion is not None:
        out["suggestion"] = suggestion
    return out


def _text(root, rel):
    return (root / rel).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2026-06-06", "2026-06-06"), ("6 Jun", "2027-06-06"), ("6th June", "2027-06-06"),
        ("Jun 6", "2027-06-06"), ("6/6", "2027-06-06"), ("6/6/27", "2027-06-06"), ("20 Aug", "2026-08-20"),
        ("20 Sep", "2026-09-20"), ("1 Jan", "2027-01-01"), ("Fri", "2026-09-18"),
        ("tomorrow", "2026-09-16"), ("garbage", None), ("", None), ("31 Feb", None),
    ],
)
def test_parse_handwritten_date(raw, expected):
    assert parse_handwritten_date(raw, TODAY) == expected


def test_done_removes_row_and_ticks_project(vault_root):
    vault = load_vault(vault_root)
    doc = _tasks_doc(vault)
    report = apply_decisions(vault, TODAY, _decisions(_page("GTD|next|2026-09-15", [_d("NA-01", "done")])), doc)
    save_vault(vault)
    assert "Read Ben's paper" not in _text(vault_root, "Next actions.md")
    assert "- [x] Read Ben's paper" in _text(vault_root, "Project details/Wedding 2026.md")
    assert len(report.applied) == 1 and not report.warnings and not report.skipped


def test_inbox_routing_with_slots(vault_root):
    vault = load_vault(vault_root)
    doc = _tasks_doc(vault)
    decisions = _decisions(
        _page(
            "GTD|inbox|2026-09-15",
            [
                _d("IN-01", "to_next", fields={"priority": {"text": "7"}, "due": {"text": "20 Sep"}, "project": {"text": "Wedding 2026"}}),
                _d("IN-02", "to_deleg", fields={"to": {"text": "Dave"}, "due": {"text": "Fri"}}),
            ],
            captures=[{"line": "N1", "inked": True, "text": "Buy milk"}, {"line": "N2", "inked": False, "text": ""}],
        )
    )
    report = apply_decisions(vault, TODAY, decisions, doc)
    save_vault(vault)
    inbox = _text(vault_root, "Inbox.md")
    assert "Claim for delayed train" not in inbox and "Chase the EOM quote" not in inbox
    assert inbox.endswith("Buy milk\n")
    assert "| Claim for delayed train | [[Wedding 2026]] | 2026-09-20 | 7 |" in _text(vault_root, "Next actions.md")
    assert "| Chase the EOM quote | Dave | 2026-09-18 |  |" in _text(vault_root, "Delegated.md")
    assert not report.warnings and not report.skipped and len(report.applied) == 3


def test_defer_and_delegate_from_next(vault_root):
    vault = load_vault(vault_root)
    doc = _tasks_doc(vault)
    decisions = _decisions(
        _page(
            "GTD|next|2026-09-15",
            [
                _d("NA-02", "defer", defer_period="1m"),
                _d("NA-03", "to_deleg"),  # no name written
            ],
        )
    )
    report = apply_decisions(vault, TODAY, decisions, doc)
    save_vault(vault)
    assert "Deal with ![[Re_ Newsletter.msg]] comments" in _text(vault_root, "Tickler/Next month.md")
    assert "| Buy an ashtray | ? | 2026-09-22 |  |" in _text(vault_root, "Delegated.md")
    nxt = _text(vault_root, "Next actions.md")
    assert "Newsletter" not in nxt and "ashtray" not in nxt and "Read Ben's paper" in nxt
    assert any("person set to '?'" in w for w in report.warnings)
    assert any("chase-by defaulted" in w for w in report.warnings)


def test_delegated_and_tickler_actions(vault_root):
    vault = load_vault(vault_root)
    doc = _tasks_doc(vault)
    decisions = _decisions(
        _page("GTD|delegated|2026-09-15", [_d("DG-01", "to_me")]),
        _page("GTD|tickler|2026-09-15", [_d("TK-01", "activate"), _d("TK-02", "defer", defer_period="1q")]),
    )
    report = apply_decisions(vault, TODAY, decisions, doc)
    save_vault(vault)
    nxt = _text(vault_root, "Next actions.md")
    assert "| Send the invoice |  |  |  |" in nxt
    assert "| Consider dental coatings |  |  |  |" in nxt
    assert _text(vault_root, "Delegated.md").count("|") == 10  # header + separator only
    assert "Consider dental coatings" not in _text(vault_root, "Tickler/Next week.md")
    assert "Sample item" not in _text(vault_root, "Tickler/Next month.md")
    assert _text(vault_root, "Tickler/Next quarter.md").endswith("Sample item\n")  # undated: CI stamps it
    assert not report.warnings


def test_slot_writing_updates_a_row_in_place(vault_root):
    """The labelled slots are the deterministic in-place edit.

    Re-wording the printed action needs a model reading the row, which is
    the ✦ AI path; the slots do not.
    """
    vault = load_vault(vault_root)
    doc = _tasks_doc(vault)
    decisions = _decisions(
        _page("GTD|next|2026-09-15", [_d("NA-03", fields={"priority": {"text": "5"}})]),
        _page("GTD|delegated|2026-09-15", [_d("DG-01", fields={"to": {"text": "Sarah"}, "due": {"text": "1 Oct"}})]),
    )
    report = apply_decisions(vault, TODAY, decisions, doc)
    save_vault(vault)
    assert "| Buy an ashtray |  |  | 5 |" in _text(vault_root, "Next actions.md")
    assert "| Send the invoice | Sarah | 2026-10-01 |  |" in _text(vault_root, "Delegated.md")
    assert not report.warnings and len(report.applied) == 2


def test_stale_handle_refound_by_text_or_skipped(vault_root):
    vault = load_vault(vault_root)
    doc = _tasks_doc(vault)
    # Between print and scan, Obsidian changed NA-03's priority (hash moves)
    # and finished NA-01 (row gone).
    path = vault_root / "Next actions.md"
    text = path.read_text(encoding="utf-8")
    text = text.replace("| Buy an ashtray |  |  |  |", "| Buy an ashtray |  |  | 9 |")
    text = text.replace("| Read Ben's paper | [[Wedding 2026]] |  | 3 |\n", "")
    path.write_text(text, encoding="utf-8")
    vault = load_vault(vault_root)

    decisions = _decisions(_page("GTD|next|2026-09-15", [_d("NA-01", "done"), _d("NA-03", "done")]))
    report = apply_decisions(vault, TODAY, decisions, doc)
    save_vault(vault)
    assert "ashtray" not in _text(vault_root, "Next actions.md")
    assert len(report.applied) == 1
    assert len(report.skipped) == 1 and "NA-01" in report.skipped[0]


def test_unknown_project_and_illegible_capture_are_warned(vault_root):
    vault = load_vault(vault_root)
    doc = _tasks_doc(vault)
    decisions = _decisions(
        _page(
            "GTD|inbox|2026-09-15",
            [_d("IN-01", "to_next", fields={"project": {"text": "Quantum leap"}})],
            captures=[{"line": "N3", "inked": True, "text": ""}],
        )
    )
    report = apply_decisions(vault, TODAY, decisions, doc)
    save_vault(vault)
    assert "| Claim for delayed train |  |  |  |" in _text(vault_root, "Next actions.md")
    assert any("Quantum leap" in w and "no project of that name" in w for w in report.warnings)
    assert any("capture N3" in w for w in report.warnings)


def test_none_actions_touch_nothing(vault_root):
    vault = load_vault(vault_root)
    doc = _tasks_doc(vault)
    before = {p: _text(vault_root, p) for p in ("Inbox.md", "Next actions.md", "Delegated.md")}
    report = apply_decisions(vault, TODAY, _decisions(_page("GTD|next|2026-09-15", [_d("NA-01"), _d("NA-02")])), doc)
    save_vault(vault)
    assert not vault.dirty and not report.applied
    assert {p: _text(vault_root, p) for p in before} == before


def _op(op, **kw):
    """One gtd.ai/3 operation: every key present, the inapplicable ones null."""
    out = {k: None for k in ("text", "priority", "due", "project", "person", "to", "period", "name", "goal")}
    out.update(kw)
    out["op"] = op
    return out


def _ai(*ops_, understood=True, confidence=0.9, handwriting="", note=""):
    return {
        "handwriting": handwriting, "understood": understood, "confidence": confidence,
        "note": note, "operations": list(ops_),
    }


def test_ai_update_rewords_row(vault_root):
    vault = load_vault(vault_root)
    doc = _tasks_doc(vault)
    decision = _ai_d("NA-03", _op("update", text="Buy a nice ashtray"), handwriting="Buy a nice ashtray")
    report = apply_decisions(vault, TODAY, _decisions(_page("GTD|next|2026-09-15", [decision])), doc)
    save_vault(vault)
    assert "| Buy a nice ashtray |  |  |  |" in _text(vault_root, "Next actions.md")
    assert len(report.applied) == 1 and not report.warnings


def test_ai_move_to_delegated_with_person_and_due(vault_root):
    vault = load_vault(vault_root)
    doc = _tasks_doc(vault)
    decision = _ai_d("NA-03", _op("move", to="delegated", due="Fri", person="Dave"), handwriting="-> Dave, Fri")
    report = apply_decisions(vault, TODAY, _decisions(_page("GTD|next|2026-09-15", [decision])), doc)
    save_vault(vault)
    assert "| Buy an ashtray | Dave | 2026-09-18 |  |" in _text(vault_root, "Delegated.md")
    assert "ashtray" not in _text(vault_root, "Next actions.md")
    assert len(report.applied) == 1


def test_ai_move_to_tickler_1m(vault_root):
    vault = load_vault(vault_root)
    doc = _tasks_doc(vault)
    decision = _ai_d("NA-02", _op("move", to="tickler", period="1m"), handwriting="defer 1m")
    report = apply_decisions(vault, TODAY, _decisions(_page("GTD|next|2026-09-15", [decision])), doc)
    save_vault(vault)
    assert "Newsletter" in _text(vault_root, "Tickler/Next month.md")
    assert "Newsletter" not in _text(vault_root, "Next actions.md")
    assert len(report.applied) == 1


def test_ai_update_sets_project(vault_root):
    vault = load_vault(vault_root)
    doc = _tasks_doc(vault)
    decision = _ai_d("NA-03", _op("update", project="Wedding 2026"), handwriting="Wedding 2026")
    report = apply_decisions(vault, TODAY, _decisions(_page("GTD|next|2026-09-15", [decision])), doc)
    save_vault(vault)
    assert "| Buy an ashtray | [[Wedding 2026]] |  |  |" in _text(vault_root, "Next actions.md")
    assert not report.warnings


def test_ai_not_understood_is_warned_and_unapplied(vault_root):
    vault = load_vault(vault_root)
    doc = _tasks_doc(vault)
    before = _text(vault_root, "Next actions.md")
    decision = _ai_d("NA-03", understood=False, handwriting="???", note="scribble")
    report = apply_decisions(vault, TODAY, _decisions(_page("GTD|next|2026-09-15", [decision])), doc)
    save_vault(vault)
    assert any("???" in w for w in report.warnings)
    assert not report.applied
    assert _text(vault_root, "Next actions.md") == before


def test_ai_low_confidence_is_warned_and_unapplied(vault_root):
    vault = load_vault(vault_root)
    doc = _tasks_doc(vault)
    before = _text(vault_root, "Next actions.md")
    decision = _ai_d("NA-03", confidence=0.3, handwriting="Buy stuff", note="uncertain")
    report = apply_decisions(vault, TODAY, _decisions(_page("GTD|next|2026-09-15", [decision])), doc)
    save_vault(vault)
    assert any("Buy stuff" in w for w in report.warnings)
    assert not report.applied
    assert _text(vault_root, "Next actions.md") == before


def test_ai_ticked_with_no_reading_at_all_is_warned(vault_root):
    vault = load_vault(vault_root)
    doc = _tasks_doc(vault)
    before = _text(vault_root, "Next actions.md")
    report = apply_decisions(
        vault, TODAY, _decisions(_page("GTD|next|2026-09-15", [_d("NA-03", ai=True)])), doc
    )
    save_vault(vault)
    assert any("no reading of the row" in w for w in report.warnings)
    assert not report.applied
    assert _text(vault_root, "Next actions.md") == before


def test_ai_must_ask_for_the_move_itself(vault_root):
    """The gutter no longer completes an AI row's intent for it.

    Under the old ✎ semantics an `update` operation was merged into the
    gutter's routing tick. Now the agent is the only writer, so a row that
    should end up in Delegated has to say `move` — `update` alone amends it
    where it is, and that is the whole answer.
    """
    vault = load_vault(vault_root)
    doc = _tasks_doc(vault)
    decision = _ai_d(
        "NA-03",
        _op("move", to="delegated", text="Buy two ashtrays", person="Dave"),
        handwriting="Buy two ashtrays -> Dave",
        suggestion={"action": "to_deleg", "new_project": False},
    )
    report = apply_decisions(vault, TODAY, _decisions(_page("GTD|next|2026-09-15", [decision])), doc)
    save_vault(vault)
    assert "| Buy two ashtrays | Dave |" in _text(vault_root, "Delegated.md")
    assert "ashtray" not in _text(vault_root, "Next actions.md")
    # The gutter's own reading is reported as context, never as a second write.
    assert any("passed to the agent as context" in n for n in report.notes)


# --- capture rows ------------------------------------------------------------


def test_capture_row_to_inbox(vault_root):
    vault = load_vault(vault_root)
    doc = _tasks_doc(vault)
    page = _page("GTD|inbox|2026-09-15", [
        {**_d("CP-01"), "inked": True, "act_text": " Buy milk "},
        {**_d("CP-02"), "inked": False, "act_text": None},  # untouched line
    ])
    report = apply_decisions(vault, TODAY, _decisions(page), doc)
    save_vault(vault)
    assert _text(vault_root, "Inbox.md").endswith("Buy milk\n")
    assert len(report.applied) == 1 and not report.warnings


def test_capture_row_to_next_matches_a_near_project_name(vault_root):
    vault = load_vault(vault_root)
    doc = _tasks_doc(vault)
    decision = {**_d("CP-01", "to_next"), "inked": True, "act_text": "Book the band",
                "fields": {"project": {"text": "Weding 2026"}, "priority": {"text": "8"}}}
    report = apply_decisions(vault, TODAY, _decisions(_page("GTD|inbox|2026-09-15", [decision])), doc)
    save_vault(vault)
    assert "| Book the band | [[Wedding 2026]] |  | 8 |" in _text(vault_root, "Next actions.md")
    assert report.notes == ["CP-01: project 'Weding 2026' matched to 'Wedding 2026'"]
    assert not report.warnings


def test_capture_row_to_delegated_and_defer(vault_root):
    vault = load_vault(vault_root)
    doc = _tasks_doc(vault)
    page = _page("GTD|inbox|2026-09-15", [
        {**_d("CP-01", "to_deleg"), "inked": True, "act_text": "Order the cake",
         "fields": {"to": {"text": "Sam"}, "due": {"text": "1 Oct"}}},
        {**_d("CP-02", "defer"), "inked": True, "act_text": "Think about a honeymoon", "defer_period": "1m"},
    ])
    report = apply_decisions(vault, TODAY, _decisions(page), doc)
    save_vault(vault)
    assert "| Order the cake | Sam | 2026-10-01 |  |" in _text(vault_root, "Delegated.md")
    assert "Think about a honeymoon" in _text(vault_root, "Tickler/Next month.md")
    assert len(report.applied) == 2 and not report.warnings


def test_capture_row_on_a_project_page_adds_an_action(vault_root):
    vault = load_vault(vault_root)
    doc = _tasks_doc(vault)
    assert doc["tasks"]["P02-C1"]["proj"] == "Wedding 2026"
    decision = {**_d("P02-C1"), "inked": True, "act_text": "Book the registrar"}
    report = apply_decisions(vault, TODAY, _decisions(_page("GTD|project-02|2026-09-15", [decision])), doc)
    save_vault(vault)
    assert "- [ ] Book the registrar" in _text(vault_root, "Project details/Wedding 2026.md")
    assert len(report.applied) == 1 and not report.warnings


def test_capture_row_ink_without_text_is_warned(vault_root):
    vault = load_vault(vault_root)
    doc = _tasks_doc(vault)
    page = _page("GTD|inbox|2026-09-15", [
        {**_d("CP-01"), "inked": True, "act_text": "  "},
        {**_d("CP-02", "to_next"), "inked": False, "act_text": None},
    ])
    report = apply_decisions(vault, TODAY, _decisions(page), doc)
    save_vault(vault)
    assert not report.applied and not vault.dirty
    assert any("CP-01" in w and "nothing legible" in w for w in report.warnings)
    assert any("CP-02" in w and "nothing was written" in w for w in report.warnings)


# --- the NEW box ---------------------------------------------------------------


def test_new_project_from_a_next_action_row(vault_root):
    vault = load_vault(vault_root)
    doc = _tasks_doc(vault)
    decision = {**_d("NA-03"), "new_project": True, "fields": {"project": {"text": "Ashtray hunt"}}}
    report = apply_decisions(vault, TODAY, _decisions(_page("GTD|next|2026-09-15", [decision])), doc)
    save_vault(vault)
    page = _text(vault_root, "Project details/Ashtray hunt.md")
    # The row's own text is the Goal AND the first action — no placeholder.
    assert "Buy an ashtray" in page
    assert "- [ ] Buy an ashtray" in page
    assert "Plan project" not in page
    nxt = _text(vault_root, "Next actions.md")
    # The project's own row carries the action; the source row is consumed.
    assert "| Buy an ashtray | [[Ashtray hunt]] |" in nxt
    assert "| Buy an ashtray |  |  |  |" not in nxt
    assert "Plan project" not in nxt
    assert len(report.applied) == 2 and not report.warnings


def test_new_project_with_an_existing_name_is_warned(vault_root):
    vault = load_vault(vault_root)
    doc = _tasks_doc(vault)
    before = _text(vault_root, "Next actions.md")
    decision = {**_d("NA-03"), "new_project": True, "fields": {"project": {"text": "Wedding 2026"}}}
    report = apply_decisions(vault, TODAY, _decisions(_page("GTD|next|2026-09-15", [decision])), doc)
    save_vault(vault)
    assert not report.applied and _text(vault_root, "Next actions.md") == before
    assert any("already exists" in w for w in report.warnings)


def test_new_project_ignores_a_routing_tick_and_a_blank_name(vault_root):
    vault = load_vault(vault_root)
    doc = _tasks_doc(vault)
    page = _page("GTD|next|2026-09-15", [
        {**_d("NA-03", "to_deleg"), "new_project": True, "fields": {"project": {"text": "Ashtray hunt"}}},
        {**_d("NA-02"), "new_project": True},
    ])
    report = apply_decisions(vault, TODAY, _decisions(page), doc)
    save_vault(vault)
    assert "Ashtray hunt" in _text(vault_root, "Next actions.md")
    assert "ashtray" not in _text(vault_root, "Delegated.md")  # the → Deleg tick was ignored
    assert any("NA-03" in w and "ignored" in w for w in report.warnings)
    assert any("NA-02" in w and "PROJECT box is empty" in w for w in report.warnings)
    assert "Newsletter" in _text(vault_root, "Next actions.md")  # NA-02 untouched


# --- project pages --------------------------------------------------------------


def test_project_item_done_ticks_it_and_drops_its_next_actions_row(vault_root):
    vault = load_vault(vault_root)
    doc = _tasks_doc(vault)
    assert doc["tasks"]["P02-01"]["act"] == "Read Ben's paper"
    report = apply_decisions(
        vault, TODAY, _decisions(_page("GTD|project-02|2026-09-15", [_d("P02-01", "done")], bucket="project")), doc
    )
    save_vault(vault)
    assert "- [x] Read Ben's paper" in _text(vault_root, "Project details/Wedding 2026.md")
    assert "Read Ben's paper" not in _text(vault_root, "Next actions.md")
    assert len(report.applied) == 1 and not report.warnings


def test_project_item_stale_handle_is_refound_by_its_printed_text(vault_root):
    vault = load_vault(vault_root)
    doc = _tasks_doc(vault)
    # Obsidian added an item above it between print and scan: the index moves.
    path = vault_root / "Project details" / "Wedding 2026.md"
    path.write_text(path.read_text(encoding="utf-8").replace(
        "- [ ] Read Ben's paper", "- [ ] Choose a caterer\n- [ ] Read Ben's paper"), encoding="utf-8")
    vault = load_vault(vault_root)
    report = apply_decisions(
        vault, TODAY, _decisions(_page("GTD|project-02|2026-09-15", [_d("P02-02", "done")], bucket="project")), doc
    )
    save_vault(vault)
    assert "- [x] Pay Sandra" in _text(vault_root, "Project details/Wedding 2026.md")
    assert len(report.applied) == 1 and not report.skipped


WEDDING = "GTD|project-02|2026-09-15"


def _proj_page(*rows):
    return _decisions(_page(WEDDING, list(rows), bucket="project"))


def _slots(**texts):
    return {k: {"text": v, "fill": 0.2} for k, v in texts.items()}


def test_project_step_delegated_stays_on_its_page(vault_root):
    vault = load_vault(vault_root)
    doc = _tasks_doc(vault)
    report = apply_decisions(
        vault, TODAY, _proj_page(_d("P02-01", "to_deleg", fields=_slots(to="Ben", due="1 Oct"))), doc
    )
    save_vault(vault)
    assert "- [ ] Read Ben's paper" in _text(vault_root, "Project details/Wedding 2026.md")
    assert "Read Ben's paper" not in _text(vault_root, "Next actions.md")
    # its priority moved with it, and the row links back to the project
    assert "| Read Ben's paper | Ben | 2026-10-01 | 3 | [[Wedding 2026]] |" in _text(vault_root, "Delegated.md")
    assert len(report.applied) == 1 and not report.warnings

    # Ticking the Delegated row off tomorrow ticks the step.
    vault = load_vault(vault_root)
    doc = _tasks_doc(vault)
    dg = next(k for k, v in doc["tasks"].items() if v["bucket"] == "delegated" and v["act"] == "Read Ben's paper")
    apply_decisions(vault, TODAY, _decisions(_page("GTD|delegated|2026-09-15", [_d(dg, "done")])), doc)
    save_vault(vault)
    assert "- [x] Read Ben's paper" in _text(vault_root, "Project details/Wedding 2026.md")


def test_project_step_deferred_and_dropped(vault_root):
    vault = load_vault(vault_root)
    doc = _tasks_doc(vault)
    report = apply_decisions(
        vault, TODAY, _proj_page(_d("P02-01", "drop"), _d("P02-02", "defer", defer_period="1m")), doc
    )
    save_vault(vault)
    page = _text(vault_root, "Project details/Wedding 2026.md")
    assert "Read Ben's paper" not in page
    assert "Read Ben's paper" not in _text(vault_root, "Next actions.md")
    assert "- [ ] Pay Sandra" in page
    assert "Pay Sandra [[Wedding 2026]]" in _text(vault_root, "Tickler/Next month.md")
    assert len(report.applied) == 2 and not report.warnings


def test_project_step_slots_without_a_tick_are_reported(vault_root):
    vault = load_vault(vault_root)
    doc = _tasks_doc(vault)
    report = apply_decisions(vault, TODAY, _proj_page(_d("P02-01", fields=_slots(to="Ben"))), doc)
    assert not report.applied and not vault.dirty
    assert any("no box ticked" in w for w in report.warnings)


def test_project_row_regoals_renames_and_finishes_after_its_steps(vault_root):
    """Every box on the project row at once, with a step ticked on the same
    page: the step is applied first, then goal, rename, archive."""
    vault = load_vault(vault_root)
    doc = _tasks_doc(vault)
    head = _d("P02-PJ", "done", fields=_slots(goal="Married, and still solvent", name="Wedding"))
    report = apply_decisions(vault, TODAY, _proj_page(head, _d("P02-01", "done")), doc)
    save_vault(vault)
    assert not (vault_root / "Project details" / "Wedding 2026.md").exists()
    assert not (vault_root / "Project details" / "Wedding.md").exists()
    done = _text(vault_root, "Project details/Done/Wedding.md")
    assert "Married, and still solvent" in done
    assert "- [x] Read Ben's paper" in done
    assert "Wedding" not in _text(vault_root, "Next actions.md")
    assert [a.split(":")[0] for a in report.applied] == ["P02-01 \"Read Ben's paper\"", "P02-PJ project 'Wedding 2026'",
                                                         "P02-PJ project 'Wedding 2026'", "P02-PJ project 'Wedding'"]
    assert not report.warnings


def test_project_row_rename_rewrites_links(vault_root):
    vault = load_vault(vault_root)
    doc = _tasks_doc(vault)
    report = apply_decisions(vault, TODAY, _proj_page(_d("P02-PJ", fields=_slots(name="Our wedding"))), doc)
    save_vault(vault)
    assert (vault_root / "Project details" / "Our wedding.md").exists()
    assert "| Read Ben's paper | [[Our wedding]] |" in _text(vault_root, "Next actions.md")
    assert len(report.applied) == 1 and not report.warnings


def test_project_row_rename_clash_is_reported(vault_root):
    vault = load_vault(vault_root)
    doc = _tasks_doc(vault)
    report = apply_decisions(vault, TODAY, _proj_page(_d("P02-PJ", fields=_slots(name="Boiler service"))), doc)
    assert not report.applied
    assert any("already exists" in w for w in report.warnings)
    assert (vault_root / "Project details" / "Wedding 2026.md").exists()


def test_ai_on_the_project_row_uses_project_ops(vault_root):
    vault = load_vault(vault_root)
    doc = _tasks_doc(vault)
    row = _ai_d(
        "P02-PJ",
        _op("set_project_goal", goal="Married by June"),
        _op("rename_project", text="June wedding"),
    )
    report = apply_decisions(vault, TODAY, _proj_page(row), doc)
    save_vault(vault)
    assert "Married by June" in _text(vault_root, "Project details/June wedding.md")
    assert len(report.applied) == 2 and not report.warnings

    vault = load_vault(vault_root)
    doc = _tasks_doc(vault)
    report = apply_decisions(vault, TODAY, _proj_page(_ai_d("P02-PJ", _op("complete"))), doc)
    save_vault(vault)
    assert (vault_root / "Project details" / "Done" / "June wedding.md").exists()


def test_ai_move_of_a_project_step_keeps_it_on_the_page(vault_root):
    vault = load_vault(vault_root)
    doc = _tasks_doc(vault)
    row = _ai_d("P02-01", _op("move", to="delegated", person="Ben", due="2026-10-02"))
    report = apply_decisions(vault, TODAY, _proj_page(row), doc)
    save_vault(vault)
    assert "- [ ] Read Ben's paper" in _text(vault_root, "Project details/Wedding 2026.md")
    assert "| Read Ben's paper | Ben | 2026-10-02 |" in _text(vault_root, "Delegated.md")
    assert len(report.applied) == 1 and not report.warnings


def test_summary_page_is_skipped(vault_root):
    vault = load_vault(vault_root)
    doc = _tasks_doc(vault)
    decisions = _decisions({"page_key": "GTD|projects|2026-09-15", "page_no": 5, "skipped": True})
    report = apply_decisions(vault, TODAY, decisions, doc)
    assert not report.applied and not report.warnings and not vault.dirty


# --- fuzzy project matching -------------------------------------------------------


def test_ambiguous_project_name_is_warned_not_guessed(tmp_path):
    from .conftest import write_vault

    root = tmp_path / "v"
    write_vault(
        root,
        inbox="Order a new thermostat\n",
        projects={
            "Boiler service": "# Goal\n\nA\n\n## Next Actions\n\n- [ ] x\n",
            "Boiler repair": "# Goal\n\nB\n\n## Next Actions\n\n- [ ] y\n",
        },
    )
    vault = load_vault(root)
    doc = _tasks_doc(vault)
    decision = _d("IN-01", "to_next", fields={"project": {"text": "Boiler"}})
    report = apply_decisions(vault, TODAY, _decisions(_page("GTD|inbox|2026-09-15", [decision])), doc)
    save_vault(vault)
    assert "| Order a new thermostat |  |  |  |" in _text(root, "Next actions.md")  # moved, project dropped
    assert any("did you mean" in w and "Boiler repair" in w and "Boiler service" in w for w in report.warnings)
    assert not report.notes


# --- the ✦ AI agent's operations ---------------------------------------------


def test_ai_runs_several_operations_in_order(vault_root):
    vault = load_vault(vault_root)
    doc = _tasks_doc(vault)
    decision = _ai_d("NA-03", 
        _op("update", text="Buy a nice ashtray", priority=5),
        _op("add_next_action", text="Buy matches", project="Wedding 2026"),
        handwriting="Buy a nice ashtray p5 + Buy matches",
    )
    report = apply_decisions(vault, TODAY, _decisions(_page("GTD|next|2026-09-15", [decision])), doc)
    save_vault(vault)
    nxt = _text(vault_root, "Next actions.md")
    assert "| Buy a nice ashtray |  |  | 5 |" in nxt
    assert "| Buy matches | [[Wedding 2026]] |  |  |" in nxt
    assert len(report.applied) == 2 and not report.warnings


def test_ai_operation_failure_does_not_stop_the_rest(vault_root):
    vault = load_vault(vault_root)
    doc = _tasks_doc(vault)
    decision = _ai_d("NA-03", 
        _op("schedule"),  # no date: reported, then the next op still runs
        _op("nonsense"),
        _op("capture", text="Ring the venue"),
    )
    report = apply_decisions(vault, TODAY, _decisions(_page("GTD|next|2026-09-15", [decision])), doc)
    save_vault(vault)
    assert _text(vault_root, "Inbox.md").endswith("Ring the venue\n")
    assert len(report.applied) == 1
    assert any("schedule without a date" in w for w in report.warnings)
    assert any("unknown operation" in w for w in report.warnings)


def test_ai_moves_an_item_onto_a_project_page(vault_root):
    vault = load_vault(vault_root)
    doc = _tasks_doc(vault)
    decision = _ai_d("NA-03", _op("move", to="project", project="Wedding 2026"))
    report = apply_decisions(vault, TODAY, _decisions(_page("GTD|next|2026-09-15", [decision])), doc)
    save_vault(vault)
    assert "- [ ] Buy an ashtray" in _text(vault_root, "Project details/Wedding 2026.md")
    assert "ashtray" not in _text(vault_root, "Next actions.md")
    assert len(report.applied) == 1


def test_ai_with_no_operations_is_warned(vault_root):
    vault = load_vault(vault_root)
    doc = _tasks_doc(vault)
    decision = _ai_d("NA-03", handwriting="hmm")
    report = apply_decisions(vault, TODAY, _decisions(_page("GTD|next|2026-09-15", [decision])), doc)
    save_vault(vault)
    assert not report.applied and not vault.dirty
    assert any("no change was asked for" in w for w in report.warnings)


def test_legacy_edit_route_is_reported_not_applied(vault_root):
    """A decisions file from before the rename: ``edited`` + ``edit``."""
    vault = load_vault(vault_root)
    doc = _tasks_doc(vault)
    legacy = {"handwriting": "-> Dave", "understood": True, "confidence": 0.9, "note": "",
              "route": "delegated", "person": "Dave"}
    decision = {"id": "NA-03", "action": "none", "edited": True,
                "qr_verified": True, "edit": legacy}
    report = apply_decisions(
        vault, TODAY, _decisions(_page("GTD|next|2026-09-15", [decision])), doc
    )
    save_vault(vault)
    assert not report.applied and not vault.dirty
    assert any("old scanner" in w for w in report.warnings)


def test_a_pre_rename_decisions_file_still_applies(vault_root):
    """``edited: true`` + ``edit: {...}`` is read as the AI path."""
    vault = load_vault(vault_root)
    doc = _tasks_doc(vault)
    decision = {"id": "NA-03", "action": "none", "edited": True, "qr_verified": True,
                "edit": _ai(_op("update", text="Buy a nice ashtray"))}
    report = apply_decisions(vault, TODAY, _decisions(_page("GTD|next|2026-09-15", [decision])), doc)
    save_vault(vault)
    assert "| Buy a nice ashtray |  |  |  |" in _text(vault_root, "Next actions.md")
    assert len(report.applied) == 1


def test_ai_beats_a_gutter_tick_on_the_same_row(vault_root):
    """The AI flag short-circuits the deterministic path — by test, not by eye.

    The scanner already refuses to emit an ``action`` on an AI row, so this
    decision (both set) can only come from an older scanner or a hand-edited
    file. Either way the agent's operation is the single write and the ✓ Done
    tick does nothing.
    """
    vault = load_vault(vault_root)
    doc = _tasks_doc(vault)
    decision = {**_ai_d("NA-03", _op("move", to="inbox")), "action": "done"}
    report = apply_decisions(vault, TODAY, _decisions(_page("GTD|next|2026-09-15", [decision])), doc)
    save_vault(vault)
    assert _text(vault_root, "Inbox.md").endswith("Buy an ashtray\n")   # the agent's move ran
    assert "ashtray" not in _text(vault_root, "Next actions.md")
    assert not any("Done" in a for a in report.applied)


def test_ai_with_new_project_ticked_creates_nothing_on_its_own(vault_root):
    """Not even the NEW box writes behind the agent."""
    vault = load_vault(vault_root)
    doc = _tasks_doc(vault)
    before = _text(vault_root, "Next actions.md")
    decision = {**_ai_d("NA-03", understood=False, note="scribble"),
                "new_project": True, "fields": {"project": {"text": "Ashtray hunt"}}}
    report = apply_decisions(vault, TODAY, _decisions(_page("GTD|next|2026-09-15", [decision])), doc)
    save_vault(vault)
    assert not (vault_root / "Project details" / "Ashtray hunt.md").exists()
    assert _text(vault_root, "Next actions.md") == before
    assert not report.applied and not vault.dirty


def test_project_item_done_sweeps_the_delegated_row(tmp_path):
    from .conftest import write_vault

    root = tmp_path / "v"
    write_vault(
        root,
        delegated_rows="| Bleed the radiators | Plumber | 2026-09-25 |  | [[Boiler service]] |\n",
        delegated_columns=("Thing", "Person", "Chase by", "Priority", "Project"),
        projects={"Boiler service": "# Goal\n\nHeat\n\n## Next Actions\n\n- [ ] Bleed the radiators\n"},
    )
    vault = load_vault(root)
    doc = _tasks_doc(vault)
    report = apply_decisions(
        vault, TODAY, _decisions(_page("GTD|project-01|2026-09-15", [_d("P01-01", "done")], bucket="project")), doc
    )
    save_vault(vault)
    assert "- [x] Bleed the radiators" in _text(root, "Project details/Boiler service.md")
    assert "Bleed the radiators" not in _text(root, "Delegated.md")
    assert not any("surfaced in" in w for w in report.warnings)


# --- the New Projects page ---------------------------------------------------------


def test_new_projects_row_creates_a_project_seeded_with_its_own_text(vault_root):
    """The page is the NEW box: no tick needed, and nothing is invented."""
    vault = load_vault(vault_root)
    doc = _tasks_doc(vault)
    page = _page("GTD|new-projects|2026-09-15", [
        {**_d("NP-01"), "inked": True, "act_text": " Order a new transformer ",
         "fields": {"project": {"text": "Rewire the PSU"}}},
        {**_d("NP-02"), "inked": False, "act_text": None},
    ])
    report = apply_decisions(vault, TODAY, _decisions(page), doc)
    save_vault(vault)

    proj = _text(vault_root, "Project details/Rewire the PSU.md")
    assert "- [ ] Order a new transformer" in proj
    assert "Plan project" not in proj
    assert "| Order a new transformer | [[Rewire the PSU]] |" in _text(vault_root, "Next actions.md")
    # A blank write-in line has no source row to consume, so nothing is
    # skipped for want of a handle either.
    assert len(report.applied) == 1
    assert not report.warnings and not report.skipped


def test_new_projects_row_falls_back_to_the_written_text_as_the_name(vault_root):
    """Nothing in the PROJECT box: the line names the project as well."""
    vault = load_vault(vault_root)
    doc = _tasks_doc(vault)
    page = _page("GTD|new-projects|2026-09-15", [
        {**_d("NP-01"), "inked": True, "act_text": "Rewire the PSU"},
    ])
    report = apply_decisions(vault, TODAY, _decisions(page), doc)
    save_vault(vault)
    assert any("PROJECT box is empty" in w for w in report.warnings)
    assert not report.applied


def test_new_projects_row_routed_instead_creates_no_project(vault_root):
    """A routing tick means "on reflection this is not a project"."""
    vault = load_vault(vault_root)
    doc = _tasks_doc(vault)
    page = _page("GTD|new-projects|2026-09-15", [
        {**_d("NP-01", "to_deleg"), "inked": True, "act_text": "Get the PSU quote",
         "fields": {"to": {"text": "Dave"}, "project": {"text": "Rewire the PSU"}}},
    ])
    report = apply_decisions(vault, TODAY, _decisions(page), doc)
    save_vault(vault)
    assert not (vault_root / "Project details" / "Rewire the PSU.md").exists()
    assert "| Get the PSU quote | Dave |" in _text(vault_root, "Delegated.md")
    assert any("no project was created" in n for n in report.notes)


def test_new_projects_row_with_ai_ticked_goes_to_the_agent_only(vault_root):
    """✦ AI on a New Projects row: no project is created behind the agent."""
    vault = load_vault(vault_root)
    doc = _tasks_doc(vault)
    decision = {
        **_ai_d("NP-01",
                _op("create_project", name="Lab move", goal="Move the lab to B12",
                    text="Book the removals firm"),
                _op("add_project_action", name="Lab move", text="Label every crate"),
                handwriting="Lab move: book removals, label crates",
                suggestion={"action": "none", "new_project": True,
                            "fields": {"project": "Lab move"},
                            "text": "Book the removals firm"}),
        "inked": True, "act_text": "Book the removals firm",
        "fields": {"project": {"text": "Lab move"}},
    }
    report = apply_decisions(vault, TODAY, _decisions(_page("GTD|new-projects|2026-09-15", [decision])), doc)
    save_vault(vault)

    proj = _text(vault_root, "Project details/Lab move.md")
    assert "Move the lab to B12" in proj
    assert "- [ ] Book the removals firm" in proj
    assert "- [ ] Label every crate" in proj
    assert "Plan project" not in proj
    # Exactly the agent's two operations — the page's own "create a project
    # from this line" rule did not also fire.
    assert len(report.applied) == 2
    assert len([a for a in report.applied if "create_project" in a]) == 1


def test_untouched_new_projects_page_changes_nothing(vault_root):
    vault = load_vault(vault_root)
    doc = _tasks_doc(vault)
    page = _page("GTD|new-projects|2026-09-15",
                 [{**_d(f"NP-{i:02d}"), "inked": False} for i in range(1, 7)])
    report = apply_decisions(vault, TODAY, _decisions(page), doc)
    assert not report.applied and not report.warnings and not vault.dirty
