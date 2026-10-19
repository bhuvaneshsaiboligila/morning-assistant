"""Google Calendar API: create and delete events (OAuth token stored locally).

    python -m assistant.gcal auth   # one-time login in the browser
    python -m assistant.gcal test   # create a test event, then delete it
"""
import datetime as dt
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

ROOT = Path(__file__).resolve().parent.parent
CLIENT_FILE = ROOT / "credentials.json"
TOKEN_FILE = ROOT / "data" / "google_token.json"
SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
TZ = "Europe/Berlin"
LOCAL = ZoneInfo(TZ)

def _creds(interactive=False):
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES) if TOKEN_FILE.exists() else None
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    elif interactive:
        flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_FILE), SCOPES)
        creds = flow.run_local_server(port=0)
    else:
        raise RuntimeError("Google Calendar is not authorized - run: python -m assistant.gcal auth")
    TOKEN_FILE.parent.mkdir(exist_ok=True)
    TOKEN_FILE.write_text(creds.to_json())
    TOKEN_FILE.chmod(0o600)
    return creds

def service(interactive=False):
    return build("calendar", "v3", credentials=_creds(interactive), cache_discovery=False)

def create_event(title, start, end=None, all_day=False, location="", description=""):
    """Timed event: start/end are datetimes. All-day event: start/end are dates."""
    if all_day:
        end = end or start + dt.timedelta(days=1)
        times = {"start": {"date": start.isoformat()}, "end": {"date": end.isoformat()}}
    else:
        end = end or start + dt.timedelta(hours=1)
        times = {"start": {"dateTime": start.isoformat(), "timeZone": TZ},
                 "end": {"dateTime": end.isoformat(), "timeZone": TZ}}
    body = {"summary": title, "location": location, "description": description, **times}
    return service().events().insert(calendarId="primary", body=body).execute()

def delete_event(event_id):
    service().events().delete(calendarId="primary", eventId=event_id).execute()

if __name__ == "__main__":
    if sys.argv[1:] == ["auth"]:
        service(interactive=True)
        print("Authorized - token saved to data/google_token.json")
    elif sys.argv[1:] == ["test"]:
        start = (dt.datetime.now(LOCAL) + dt.timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
        ev = create_event("Assistant test event", start, start + dt.timedelta(minutes=30),
                          description="Created by morning-assistant")
        print("Created:", ev["summary"], "at", ev["start"]["dateTime"])
        input("Check Google Calendar on your phone (tomorrow 12:00), then press Enter to delete it... ")
        delete_event(ev["id"])
        print("Deleted - writing works.")
    else:
        print(__doc__)
