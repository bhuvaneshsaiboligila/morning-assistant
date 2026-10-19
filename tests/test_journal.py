"""Tests for assistant.journal - no model calls (model calls are mocked where needed).

Uses an in-memory SQLite database with journal.SCHEMA applied directly, so the
real data/assistant.db is never touched.
"""
import datetime as dt
import sqlite3

import pytest

from assistant import journal


@pytest.fixture
def con():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(journal.SCHEMA)
    return c


def _at(day, hour=12):
    """A UTC ISO timestamp safely inside `day`'s Europe/Berlin calendar day."""
    local_dt = dt.datetime.combine(day, dt.time(hour), journal.LOCAL)
    return local_dt.astimezone(journal.UTC).isoformat()


# --- FTS search + date filtering ------------------------------------------------

def test_search_entries_keyword(con):
    today = dt.date(2026, 10, 19)
    e1 = journal.add_entry(con, "working on the python project", now=_at(today - dt.timedelta(days=3)))
    journal.add_entry(con, "went to the gym", now=_at(today - dt.timedelta(days=1)))
    e3 = journal.add_entry(con, "python meeting with the team", now=_at(today))

    hits = journal.search_entries(con, ["python"])
    assert {h["id"] for h in hits} == {e1, e3}


def test_search_entries_date_range_falls_back_to_recent(con):
    today = dt.date(2026, 10, 19)
    journal.add_entry(con, "working on the python project", now=_at(today - dt.timedelta(days=3)))
    e2 = journal.add_entry(con, "went to the gym", now=_at(today - dt.timedelta(days=1)))
    e3 = journal.add_entry(con, "python meeting with the team", now=_at(today))

    recent = journal.search_entries(con, [], date_from=today - dt.timedelta(days=1), date_to=today)
    assert {r["id"] for r in recent} == {e2, e3}


def test_search_entries_keyword_and_date_range_combined(con):
    today = dt.date(2026, 10, 19)
    e1 = journal.add_entry(con, "python project kickoff", now=_at(today - dt.timedelta(days=3)))
    journal.add_entry(con, "python meeting today", now=_at(today))

    hits = journal.search_entries(con, ["python"], date_from=today - dt.timedelta(days=4),
                                   date_to=today - dt.timedelta(days=2))
    assert {h["id"] for h in hits} == {e1}


# --- memory add/update/supersede/expire/forget ----------------------------------

def test_memory_supersede_keeps_history(con):
    now = _at(dt.date(2026, 10, 19))
    old_id = journal.add_memory(con, "goal", "learn Rust", now=now)
    new_id = journal.supersede_memory(con, old_id, "goal", "learn Rust, finished the basics", now=now)

    old = con.execute("SELECT status, superseded_by FROM memories WHERE id=?", (old_id,)).fetchone()
    assert old["status"] == "superseded"
    assert old["superseded_by"] == new_id
    assert {m["id"] for m in journal.active_memories(con)} == {new_id}


def test_memory_expiry(con):
    created = _at(dt.date(2026, 10, 19))
    expires_at = _at(dt.date(2026, 10, 20))
    tmp_id = journal.add_memory(con, "commitment", "finish report by Friday",
                                expires_at=expires_at, now=created)

    # Not expired yet, relative to a "now" before expires_at.
    journal.expire_memories(con, now=_at(dt.date(2026, 10, 19), hour=23))
    assert tmp_id in {m["id"] for m in journal.active_memories(con)}

    # Expired once "now" has passed expires_at.
    expired = journal.expire_memories(con, now=_at(dt.date(2026, 10, 21)))
    assert expired == 1
    assert tmp_id not in {m["id"] for m in journal.active_memories(con)}


def test_forget_memory(con):
    mid = journal.add_memory(con, "habit", "goes running on Mondays")
    journal.forget_memory(con, mid)
    assert mid not in {m["id"] for m in journal.active_memories(con)}


# --- JSON validation dropping bad items ------------------------------------------

def test_valid_items_drops_bad_entries():
    related_ids = {1, 2}
    raw = [
        {"action": "add", "memory_id": None, "kind": "goal", "text": "learn Rust",
         "temporary": False, "expires_in_days": None},
        {"action": "update", "memory_id": 1, "kind": "habit", "text": "runs every morning",
         "temporary": False, "expires_in_days": None},
        {"action": "update", "memory_id": 999, "kind": "habit", "text": "unknown id", "temporary": False},
        {"action": "add", "kind": "not_a_kind", "text": "bad kind", "temporary": False},
        {"action": "add", "memory_id": None, "kind": "goal", "text": "", "temporary": False},
        {"action": "bogus", "memory_id": None, "kind": "goal", "text": "bad action", "temporary": False},
        "not a dict",
        {"action": "add", "memory_id": None, "kind": "commitment", "text": "finish X",
         "temporary": True, "expires_in_days": -5},
    ]
    items = journal._valid_items(raw, related_ids)
    assert len(items) == 3
    assert items[0]["text"] == "learn Rust"
    assert items[1]["memory_id"] == 1
    assert items[2]["temporary"] is True
    assert items[2]["expires_in_days"] == journal.DEFAULT_EXPIRY_DAYS  # bad value -> default


def test_valid_items_empty_on_garbage():
    assert journal._valid_items("not a list", set()) == []
    assert journal._valid_items(None, set()) == []


# --- extraction pipeline (model call mocked) -------------------------------------

def test_extract_memories_applies_validated_items(con, monkeypatch):
    entry_id = journal.add_entry(con, "I started learning Rust this week", now=_at(dt.date(2026, 10, 19)))
    monkeypatch.setattr(journal, "_call_extract_model", lambda text, related: [
        {"action": "add", "memory_id": None, "kind": "goal", "text": "learning Rust",
         "temporary": False, "expires_in_days": None}])

    created = journal.extract_memories(con, entry_id, "I started learning Rust this week")
    assert len(created) == 1
    mem = con.execute("SELECT * FROM memories WHERE id=?", (created[0],)).fetchone()
    assert mem["text"] == "learning Rust"
    assert mem["source_entry_id"] == entry_id


def test_extract_memories_failure_does_not_raise(con, monkeypatch):
    entry_id = journal.add_entry(con, "entry text", now=_at(dt.date(2026, 10, 19)))

    def boom(text, related):
        raise RuntimeError("model down")
    monkeypatch.setattr(journal, "_call_extract_model", boom)

    assert journal.extract_memories(con, entry_id, "entry text") == []


# --- /ask date-range resolution post-processing ----------------------------------

def test_resolve_route_swaps_and_clamps_dates():
    now = dt.datetime(2026, 10, 19, 8, 0, tzinfo=journal.LOCAL)
    raw = {"keywords": ["gym", ""], "date_from": "2026-10-20", "date_to": "2026-10-10"}
    route = journal.resolve_route(raw, now)
    assert route["keywords"] == ["gym"]
    assert route["date_from"] == dt.date(2026, 10, 10)
    assert route["date_to"] == dt.date(2026, 10, 19)  # swapped, then clamped to "today"


def test_resolve_route_invalid_dates_become_none():
    now = dt.datetime(2026, 10, 19, 8, 0, tzinfo=journal.LOCAL)
    route = journal.resolve_route({"keywords": [], "date_from": "not-a-date", "date_to": None}, now)
    assert route["date_from"] is None
    assert route["date_to"] is None


# --- report building ---------------------------------------------------------------

def test_week_period():
    start, end = journal.week_period(dt.date(2026, 10, 25))  # a Sunday
    assert start == dt.date(2026, 10, 18)
    assert end == dt.date(2026, 10, 24)


def test_month_period():
    start, end = journal.month_period(dt.date(2026, 11, 1))
    assert start == dt.date(2026, 10, 1)
    assert end == dt.date(2026, 10, 31)


def test_build_report_zero_entries_skips_model(con):
    text = journal.build_report(con, "week", run_date=dt.date(2026, 10, 25))
    assert "No journal entries" in text
    saved = con.execute("SELECT * FROM reports").fetchone()
    assert saved is not None and saved["period"] == "week"


# --- export ------------------------------------------------------------------------

def test_export_markdown_contains_entries_and_memories(con):
    now = _at(dt.date(2026, 10, 19))
    journal.add_entry(con, "first entry", now=now)
    journal.add_memory(con, "goal", "learn Rust", now=now)

    text = journal.export_markdown(con)
    assert text.startswith("# Journal export")
    assert "first entry" in text
    assert "(goal) learn Rust" in text
