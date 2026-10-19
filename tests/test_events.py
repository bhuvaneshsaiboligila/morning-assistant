"""Tests for assistant.events.build_event - the model-JSON -> event post-processing,
tested as a pure function so no model call is needed."""
import datetime as dt

import pytest

from assistant.events import LOCAL, build_event, google_minutes


def _last_sunday(year, month):
    next_month = dt.date(year + 1, 1, 1) if month == 12 else dt.date(year, month + 1, 1)
    last_day = next_month - dt.timedelta(days=1)
    return last_day - dt.timedelta(days=(last_day.weekday() - 6) % 7)


def _d(**over):
    base = {"title": "Meeting", "date": "2026-11-02", "start_time": None, "end_time": None,
            "location": None, "remind_minutes_before": []}
    base.update(over)
    return base


NOW = dt.datetime(2026, 10, 19, 8, 0, tzinfo=LOCAL)


def test_normal_timed_event():
    ev = build_event(_d(start_time="09:00", end_time="10:00", remind_minutes_before=[30]), NOW)
    assert ev["all_day"] is False
    assert ev["start"] == dt.datetime(2026, 11, 2, 9, 0, tzinfo=LOCAL)
    assert ev["end"] == dt.datetime(2026, 11, 2, 10, 0, tzinfo=LOCAL)
    assert ev["reminders"] == [30]
    assert ev["notes"] == []


def test_all_day_event():
    ev = build_event(_d(date="2026-12-25"), NOW)
    assert ev["all_day"] is True
    assert ev["start"] == dt.date(2026, 12, 25)
    assert ev["end"] == dt.date(2026, 12, 26)
    assert ev["reminders"] == [1440]
    assert any("default" in n for n in ev["notes"])


@pytest.mark.parametrize("bad", ["25:99", "abc", "9", "9:0", "24:00", "09-00"])
def test_invalid_time_raises(bad):
    with pytest.raises(ValueError):
        build_event(_d(start_time=bad), NOW)


@pytest.mark.parametrize("ok", ["9:00", "09:00", "09.00"])
def test_time_formats_accepted(ok):
    ev = build_event(_d(start_time=ok, end_time="10:00"), NOW)
    assert ev["start"].hour == 9


def test_overnight_event():
    ev = build_event(_d(start_time="22:00", end_time="01:00"), NOW)
    assert ev["all_day"] is False
    assert ev["start"] == dt.datetime(2026, 11, 2, 22, 0, tzinfo=LOCAL)
    assert ev["end"] == dt.datetime(2026, 11, 3, 1, 0, tzinfo=LOCAL)
    assert any("overnight" in n for n in ev["notes"])


def test_end_before_start_rejected():
    with pytest.raises(ValueError):
        build_event(_d(start_time="14:00", end_time="10:00"), NOW)


def test_past_date_warned():
    from assistant.events import describe
    ev = build_event(_d(date="2020-01-01", start_time="09:00", end_time="10:00"), NOW)
    assert "in the past" in describe(ev)


def test_default_reminders():
    ev = build_event(_d(date="2026-12-01", start_time="15:00", end_time="16:00",
                         remind_minutes_before=[]), NOW)
    assert ev["reminders"] == [1440, 60]
    assert any("default" in n for n in ev["notes"])


def test_all_day_reminder_shift_to_0900():
    ev = build_event(_d(date="2026-12-25", remind_minutes_before=[1440]), NOW)
    assert google_minutes(ev) == [900]  # 900 min before 00:00 = 09:00 the day before


def test_dst_gap_day():
    gap_day = _last_sunday(2026, 3)
    ev = build_event(_d(date=gap_day.isoformat(), start_time="02:30"), NOW)
    assert any("does not exist" in n for n in ev["notes"])


def test_dst_overlap_day():
    overlap_day = _last_sunday(2026, 10)
    ev = build_event(_d(date=overlap_day.isoformat(), start_time="02:30"), NOW)
    assert any("ambiguous" in n for n in ev["notes"])
