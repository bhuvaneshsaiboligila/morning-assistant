"""Turn a chat message into a calendar event with the local model."""
import datetime as dt
import html
import json
import re
from zoneinfo import ZoneInfo

import ollama

from assistant.summarize import MODEL

LOCAL = ZoneInfo("Europe/Berlin")
UTC = dt.timezone.utc

TIME_RE = re.compile(r"^\s*([01]?\d|2[0-3])[:.]([0-5]\d)\s*$")
OVERNIGHT_START_HOUR = 18  # end <= start is only treated as overnight if start is this late...
OVERNIGHT_END_HOUR = 6     # ...and end is this early, e.g. 22:00-01:00

SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "date": {"type": "string"},
        "start_time": {"type": ["string", "null"]},
        "end_time": {"type": ["string", "null"]},
        "location": {"type": ["string", "null"]},
        "remind_minutes_before": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["title", "date", "start_time", "end_time", "location", "remind_minutes_before"],
}

PROMPT = """Extract ONE calendar event from the message below.
Today is {today} ({weekday}). Time zone: Europe/Berlin.
Rules:
- date: YYYY-MM-DD. Resolve "tomorrow", "next Friday", "27th Jan" etc. relative to today.
- start_time / end_time: HH:MM in 24-hour format, or null if not given. No start time means an all-day event.
- title: short and clear, e.g. "Exam: Linear Algebra" or "Call Ausländerbehörde". Never put the date in the title.
- location: only if the message names a place, else null.
- remind_minutes_before: only reminders the user explicitly asked for, in minutes
  (1 hour = 60, 1 day = 1440, 1 week = 10080). Empty list if none were asked for.

Message: {text}"""

def _parse_time_field(s):
    """None stays None (all-day). Anything else must be strict HH:MM (":" or "."), else raise."""
    if s is None:
        return None
    m = TIME_RE.match(str(s))
    if not m:
        raise ValueError(f"invalid time value: {s!r} - expected HH:MM")
    return dt.time(int(m.group(1)), int(m.group(2)))

def _dst_flag(label, local_dt):
    """Flag a start/end datetime that falls in a Europe/Berlin DST gap or ambiguous fold."""
    naive = local_dt.replace(tzinfo=None)
    utc0 = local_dt.replace(fold=0).astimezone(UTC)
    utc1 = local_dt.replace(fold=1).astimezone(UTC)
    if utc0 == utc1:
        return None
    back0 = utc0.astimezone(LOCAL).replace(tzinfo=None)
    if back0 == naive:
        return (f"{label} time {naive:%H:%M} on {naive:%d %b} is ambiguous (clocks fall back "
                f"that night) - using the earlier occurrence")
    return (f"{label} time {naive:%H:%M} on {naive:%d %b} does not exist (clocks spring forward "
            f"that night) - treated as {back0:%H:%M}")

def human(minutes):
    if minutes == 0:
        return "at start"
    for unit, size in (("week", 10080), ("day", 1440), ("hour", 60)):
        if minutes % size == 0:
            n = minutes // size
            return f"{n} {unit}{'s' if n > 1 else ''} before"
    return f"{minutes} min before"

def build_event(d, now):
    """Pure post-processing: model JSON `d` -> event dict. Raises ValueError on bad input.

    Every automatic correction or default (missing end time, overnight end,
    default reminders) is recorded in ev["notes"] so it can be shown on the
    confirmation card.
    """
    notes = []
    date = dt.date.fromisoformat(str(d["date"]).strip())
    start_t = _parse_time_field(d.get("start_time"))
    end_t = _parse_time_field(d.get("end_time"))
    ev = {"title": (d.get("title") or "Event").strip()[:100],
          "location": (d.get("location") or "").strip()[:100]}

    if start_t:
        start = dt.datetime.combine(date, start_t, LOCAL)
        if end_t is None:
            end = start + dt.timedelta(hours=1)
            notes.append("no end time given - defaulted to 1 hour")
        else:
            end_date = date
            if end_t <= start_t:
                if start_t.hour >= OVERNIGHT_START_HOUR and end_t.hour <= OVERNIGHT_END_HOUR:
                    end_date = date + dt.timedelta(days=1)
                    notes.append("end time is before start time - treated as an overnight "
                                 "event ending the next day")
                else:
                    raise ValueError(f"end time {end_t:%H:%M} is not after start time {start_t:%H:%M}")
            end = dt.datetime.combine(end_date, end_t, LOCAL)
        for label, moment in (("start", start), ("end", end)):
            flag = _dst_flag(label, moment)
            if flag:
                notes.append(flag)
        ev.update(all_day=False, start=start, end=end)
    else:
        ev.update(all_day=True, start=date, end=date + dt.timedelta(days=1))

    asked = sorted({int(m) for m in (d.get("remind_minutes_before") or []) if 0 <= int(m) <= 40320},
                   reverse=True)[:5]
    if not asked:
        if ev["all_day"]:
            asked = [1440]
        else:
            asked = [1440, 60] if ev["start"] - now > dt.timedelta(days=1) else [0]
        notes.append("no reminder requested - used the default")
    ev["reminders"] = asked
    ev["notes"] = notes
    return ev

def parse(text):
    """Return an event dict, or raise if the message can't be understood."""
    now = dt.datetime.now(LOCAL)
    r = ollama.chat(
        model=MODEL,
        messages=[{"role": "user", "content": PROMPT.format(
            today=now.date().isoformat(), weekday=f"{now:%A}", text=text)}],
        format=SCHEMA, think=False, keep_alive="10m",
        options={"temperature": 0, "num_ctx": 2048},
    )
    d = json.loads(r.message.content)
    return build_event(d, now)

def google_minutes(ev):
    """Google counts all-day reminders from 00:00 - shift day-based ones to 09:00."""
    if ev["all_day"]:
        return [m - 540 if m >= 1440 else m for m in ev["reminders"]]
    return ev["reminders"]

def describe(ev, header="📅 <b>Add this to Google Calendar?</b>"):
    if ev["all_day"]:
        when = f"{ev['start']:%a %d %b %Y}, all day"
        rem = [human(m) + (" (09:00)" if m >= 1440 else "") for m in ev["reminders"]]
        day = ev["start"]
    else:
        when = f"{ev['start']:%a %d %b %Y}, {ev['start']:%H:%M}–{ev['end']:%H:%M}"
        rem = [human(m) for m in ev["reminders"]]
        day = ev["start"].date()
    lines = [header, "", f"<b>{html.escape(ev['title'])}</b>", f"🕒 {when}"]
    if ev["location"]:
        lines.append(f"📍 {html.escape(ev['location'])}")
    lines.append("🔔 " + ", ".join(rem))
    for note in ev.get("notes", []):
        lines.append(f"⚠️ {html.escape(note)}")
    if day < dt.datetime.now(LOCAL).date():
        lines.append("⚠️ This date is in the past - please check it!")
    return "\n".join(lines)
