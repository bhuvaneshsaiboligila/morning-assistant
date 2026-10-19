"""Calendar events: Google Calendar API (instant), falling back to the secret iCal link."""
import datetime as dt
import html
import os
from zoneinfo import ZoneInfo

import httpx

LOCAL = ZoneInfo("Europe/Berlin")

def _parse_dt(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(LOCAL)

def _fetch_api(start, end):
    from assistant.gcal import service
    items = service().events().list(
        calendarId="primary", timeMin=start.isoformat(), timeMax=end.isoformat(),
        singleEvents=True, orderBy="startTime", maxResults=250).execute().get("items", [])
    events = []
    for it in items:
        s, e = it.get("start", {}), it.get("end", {})
        if "date" in s:
            begin, finish, all_day = dt.datetime.combine(dt.date.fromisoformat(s["date"]), dt.time(), LOCAL), None, True
        else:
            begin, all_day = _parse_dt(s["dateTime"]), False
            finish = _parse_dt(e["dateTime"]) if "dateTime" in e else None
        events.append({"title": it.get("summary") or "(no title)", "start": begin, "end": finish,
                       "all_day": all_day, "location": it.get("location") or ""})
    return events

def _ics_local(value):
    if not isinstance(value, dt.datetime):
        return dt.datetime.combine(value, dt.time(), LOCAL), True
    if value.tzinfo is None:
        return value.replace(tzinfo=LOCAL), False
    return value.astimezone(LOCAL), False

def _fetch_ics(start, end):
    import icalendar
    import recurring_ical_events
    url = os.environ.get("GOOGLE_ICS_URL")
    if not url:
        raise RuntimeError("GOOGLE_ICS_URL is not set in .env")
    try:
        r = httpx.get(url, timeout=30, follow_redirects=True)
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise RuntimeError(f"calendar download failed ({type(e).__name__})") from None  # never print the URL
    events = []
    for ev in recurring_ical_events.of(icalendar.Calendar.from_ical(r.content)).between(start, end):
        begin, all_day = _ics_local(ev.get("DTSTART").dt)
        finish = _ics_local(ev.get("DTEND").dt)[0] if ev.get("DTEND") else None
        events.append({"title": str(ev.get("SUMMARY") or "(no title)"), "start": begin, "end": finish,
                       "all_day": all_day, "location": str(ev.get("LOCATION") or "")})
    return events

def fetch_events(days=7):
    """Events from today 00:00 until `days` days later."""
    start = dt.datetime.now(LOCAL).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + dt.timedelta(days=days)
    try:
        events = _fetch_api(start, end)
    except Exception as e:
        print(f"Calendar API unavailable ({type(e).__name__}) - using the iCal link", flush=True)
        events = _fetch_ics(start, end)
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
