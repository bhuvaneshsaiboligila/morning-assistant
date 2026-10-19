"""Turn a chat message into a calendar event with the local model."""
import datetime as dt
import html
import json
import re
from zoneinfo import ZoneInfo

import ollama

from assistant.summarize import MODEL

LOCAL = ZoneInfo("Europe/Berlin")

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

def _time(s):
    m = re.match(r"^\s*(\d{1,2})[:.](\d{2})", s or "")
    if not m:
        return None
    return dt.time(int(m.group(1)), int(m.group(2)))

def human(minutes):
    if minutes == 0:
        return "at start"
    for unit, size in (("week", 10080), ("day", 1440), ("hour", 60)):
        if minutes % size == 0:
            n = minutes // size
            return f"{n} {unit}{'s' if n > 1 else ''} before"
    return f"{minutes} min before"

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
    date = dt.date.fromisoformat(str(d["date"]).strip())
    start_t, end_t = _time(d.get("start_time")), _time(d.get("end_time"))
    ev = {"title": (d.get("title") or "Event").strip()[:100],
          "location": (d.get("location") or "").strip()[:100]}
    if start_t:
        start = dt.datetime.combine(date, start_t, LOCAL)
        end = dt.datetime.combine(date, end_t, LOCAL) if end_t else start + dt.timedelta(hours=1)
        if end <= start:
            end = start + dt.timedelta(hours=1)
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
    ev["reminders"] = asked
    return ev

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
    if day < dt.datetime.now(LOCAL).date():
        lines.append("⚠️ This date is in the past - please check it!")
    return "\n".join(lines)
