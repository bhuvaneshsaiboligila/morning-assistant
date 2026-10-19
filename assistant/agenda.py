"""Events from Google Calendar via the secret iCal address (read-only)."""
import datetime as dt
import html
import os
from zoneinfo import ZoneInfo

import httpx
import icalendar
import recurring_ical_events

LOCAL = ZoneInfo("Europe/Berlin")

def _local(value):
    """Convert an iCal date/datetime to an aware Berlin datetime. Returns (datetime, all_day)."""
    if not isinstance(value, dt.datetime):
        return dt.datetime.combine(value, dt.time(), LOCAL), True
    if value.tzinfo is None:
        return value.replace(tzinfo=LOCAL), False
    return value.astimezone(LOCAL), False

def fetch_events(days=7):
    """All events from today 00:00 until `days` days later, recurring ones expanded."""
    url = os.environ.get("GOOGLE_ICS_URL")
    if not url:
        raise RuntimeError("GOOGLE_ICS_URL is not set in .env")
    try:
        r = httpx.get(url, timeout=30, follow_redirects=True)
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise RuntimeError(f"calendar download failed ({type(e).__name__})") from None  # never print the secret URL
    cal = icalendar.Calendar.from_ical(r.content)
    start = dt.datetime.now(LOCAL).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + dt.timedelta(days=days)
    events = []
    for ev in recurring_ical_events.of(cal).between(start, end):
        begin, all_day = _local(ev.get("DTSTART").dt)
        finish = _local(ev.get("DTEND").dt)[0] if ev.get("DTEND") else None
        events.append({
            "title": str(ev.get("SUMMARY") or "(no title)"),
            "start": begin, "end": finish, "all_day": all_day,
            "location": str(ev.get("LOCATION") or ""),
        })
    return sorted(events, key=lambda e: e["start"])

def _line(e, with_day):
    day = f"{e['start']:%a %d %b} " if with_day else ""
    if e["all_day"]:
        when = f"{day}all day"
    else:
        when = f"{day}{e['start']:%H:%M}" + (f"–{e['end']:%H:%M}" if e["end"] else "")
    place = f" <i>({html.escape(e['location'])})</i>" if e["location"] else ""
    return f"• {when}  {html.escape(e['title'])}{place}"

def format_agenda(events):
    """Telegram HTML block: today's events, then the rest of the week."""
    today = dt.datetime.now(LOCAL).date()
    todays = [e for e in events if e["start"].date() == today]
    later = [e for e in events if e["start"].date() > today]
    lines = ["<b>📅 Today</b>"]
    lines += [_line(e, False) for e in todays] or ["Nothing scheduled today."]
    if later:
        lines += ["", "<b>🗓 Next 7 days</b>"] + [_line(e, True) for e in later]
    return "\n".join(lines)

if __name__ == "__main__":
    events = fetch_events()
    print(f"{len(events)} events in the next 7 days\n")
    print(format_agenda(events))
