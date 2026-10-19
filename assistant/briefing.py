"""Build the morning briefing and send it to Telegram.

    python -m assistant.briefing            # real run: send + mark items as sent
    python -m assistant.briefing --no-mark  # test: send, but items can appear again
    python -m assistant.briefing --print    # test: print only, send nothing
    python -m assistant.briefing --force    # send even if already sent today

Delivery is "atomic enough": Telegram and SQLite cannot share a transaction, so
every run is built once and saved to data/outbox/<run_id>.json with a per-message
"sent" flag. Only messages not yet marked sent are sent; each is marked sent right
after Telegram confirms it. Articles are only marked briefed (and, for scheduled
runs, the daily stamp written) once every message in the run has been sent. A
scheduled run resumes an incomplete outbox from today instead of building a new
one, so a crash mid-send never drops or repeats a message.

deliver_briefing() is the single coordinator used by both the scheduled run and
the bot's /briefing command, serialized with a file lock (data/briefing.lock) so
two briefings never build or send at the same time.
"""
import argparse
import datetime as dt
import fcntl
import html
import json
import uuid
from zoneinfo import ZoneInfo

from assistant.agenda import fetch_events, format_agenda
from assistant.collect import collect
from assistant.notify import redact, send
from assistant.rank import select
from assistant.store import DB, connect, mark_briefed, save_new, unbriefed
from assistant.summarize import summarize

LOCAL = ZoneInfo("Europe/Berlin")
LIMIT = 3800  # Telegram allows 4096 characters per message
TITLES = {
    "jobs": "💼 Jobs &amp; internships",
    "research": "🔬 Research",
    "tech": "💻 Tech",
    "world": "🌍 World",
    "germany": "🇩🇪 Germany",
    "india": "🇮🇳 India",
}
NO_AI = {"jobs"}  # sections shown without an AI summary

STAMP = DB.parent / "last_briefing.txt"
LOCK_FILE = DB.parent / "briefing.lock"
OUTBOX_DIR = DB.parent / "outbox"
OUTBOX_MAX_AGE_DAYS = 14

def esc(s):
    return html.escape(s or "", quote=True)

def fmt_item(a, summary):
    t = dt.datetime.fromisoformat(a["published"]).astimezone(LOCAL)
    when = f"posted {t:%d %b}" if a["category"] in NO_AI else f"{t:%a %H:%M}"
    lines = [f'• <a href="{esc(a["link"])}">{esc(a["title"])}</a>']
    if summary:
        lines.append(f"   {esc(summary)}")
    lines.append(f"   <i>{esc(a['source'])} · {when}</i>")
    return "\n".join(lines)

def build(log=print):
    """Return (list of text blocks, list of links included)."""
    con = connect()
    items, report = collect()
    save_new(con, items)
    failed = [name for name, status in report if status.startswith("FAILED")]
    picked = select(unbriefed(con))
    agenda, cal_error = None, None
    try:
        agenda = format_agenda(fetch_events())
    except Exception as e:
        log(f"  CALENDAR FAILED: {redact(f'{type(e).__name__}: {e}')}")
        cal_error = True

    blocks, model_ok, count = [], True, 0
    for section, articles in picked.items():
        if not articles:
            continue
        lines = [f"<b>{TITLES.get(section, esc(section.title()))}</b>"]
        for a in articles:
            if section in NO_AI:
                summary = a.get("summary")
            else:
                summary = None
                if model_ok:
                    try:
                        summary = summarize(a)
                    except Exception as e:
                        model_ok = False
                        log(f"  MODEL FAILED: {type(e).__name__}: {e}")
                if summary is None:
                    summary = (a.get("summary") or "")[:200] or None
            lines.append(fmt_item(a, summary))
            count += 1
            log(f"  [{section}] {a['title'][:70]}")
        blocks.append("\n".join(lines))

    header = f"☀️ <b>Good morning! Briefing for {dt.datetime.now(LOCAL):%A, %d %B}</b>\n{count} items"
    if failed:
        header += "\n⚠️ Sources unavailable: " + esc(", ".join(failed))
    if not model_ok:
        header += "\n⚠️ Local AI unavailable: showing original snippets instead of summaries."
    if cal_error:
        header += "\n⚠️ Calendar unavailable - details are in the laptop log."
    try:
        from assistant.gcal import _creds
        _creds()
    except Exception:
        header += "\n🔑 Google Calendar login expired - on the laptop run: python -m assistant.gcal auth"
    links = [a["link"] for articles in picked.values() for a in articles]
    return [header] + ([agenda] if agenda else []) + blocks, links

def pack(blocks):
    """Join blocks into as few Telegram messages as possible."""
    messages, current = [], ""
    for b in blocks:
        if current and len(current) + len(b) + 2 > LIMIT:
            messages.append(current)
            current = b
        else:
            current = f"{current}\n\n{b}" if current else b
    if current:
        messages.append(current)
    return messages

# ---------------------------------------------------------------------------
# Outbox: one JSON file per run, so a crash mid-send never drops or repeats
# a message, and completed runs can be told apart from interrupted ones.
# ---------------------------------------------------------------------------

def _outbox_path(run_id):
    return OUTBOX_DIR / f"{run_id}.json"

def _save_outbox(data):
    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    path = _outbox_path(data["run_id"])
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)

def _load_outbox(path):
    return json.loads(path.read_text())

def _todays_incomplete_outbox(kind, today):
    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    candidates = []
    for path in OUTBOX_DIR.glob("*.json"):
        try:
            data = _load_outbox(path)
        except (OSError, ValueError):
            continue
        if (not data.get("complete") and data.get("kind") == kind
                and data.get("created_at", "")[:10] == today):
            candidates.append(data)
    candidates.sort(key=lambda d: d["created_at"])
    return candidates[-1] if candidates else None

def _build_outbox(kind, log):
    blocks, links = build(log=log)
    data = {
        "run_id": uuid.uuid4().hex[:12],
        "created_at": dt.datetime.now(LOCAL).isoformat(),
        "kind": kind,
        "messages": [{"text": m, "html": True, "sent": False} for m in pack(blocks)],
        "links": links,
        "complete": False,
    }
    _save_outbox(data)
    return data

def _send_outbox(data, log):
    """Send only the messages not yet marked sent; mark each one right after Telegram confirms it."""
    for m in data["messages"]:
        if m["sent"]:
            continue
        send(m["text"], html=m.get("html", False))
        m["sent"] = True
        _save_outbox(data)
        log(f"  sent message ({len(m['text'])} chars)")

def _finalize(data, kind, today, mark):
    """Only once every message is sent: mark articles briefed, stamp the day, close the outbox."""
    if mark:
        mark_briefed(connect(), data["links"])
        if kind == "scheduled":
            STAMP.write_text(today)
    data["complete"] = True
    _save_outbox(data)

def _cleanup_outbox():
    cutoff = dt.datetime.now(LOCAL) - dt.timedelta(days=OUTBOX_MAX_AGE_DAYS)
    for path in OUTBOX_DIR.glob("*.json"):
        try:
            data = _load_outbox(path)
        except (OSError, ValueError):
            continue
        if not data.get("complete"):
            continue
        try:
            created = dt.datetime.fromisoformat(data["created_at"])
        except ValueError:
            continue
        if created < cutoff:
            path.unlink(missing_ok=True)

# ---------------------------------------------------------------------------
# Lock: only one briefing may build/send at a time.
# ---------------------------------------------------------------------------

def _acquire_lock(blocking):
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    f = open(LOCK_FILE, "w")
    flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
    try:
        fcntl.flock(f, flags)
    except BlockingIOError:
        f.close()
        return None
    return f

def _release_lock(f):
    fcntl.flock(f, fcntl.LOCK_UN)
    f.close()

def _already_sent_today(today):
    return STAMP.exists() and STAMP.read_text().strip() == today

def deliver_briefing(kind="scheduled", mark=True, force=False, log=print):
    """Single entry point for both the scheduled run and the bot's /briefing.

    kind: "scheduled" sends at most once per day (daily stamp) and resumes an
    incomplete outbox from today. "manual" is always allowed, never sets the
    daily stamp, but still marks its articles as briefed so the morning
    briefing doesn't repeat them.

    Returns a dict: {"status": "sent"|"skipped"|"locked", ...}.
    """
    today = dt.datetime.now(LOCAL).date().isoformat()
    if kind == "scheduled" and not force and _already_sent_today(today):
        return {"status": "skipped", "reason": "already sent today"}

    lock_f = _acquire_lock(blocking=(kind == "scheduled"))
    if lock_f is None:
        return {"status": "locked"}
    try:
        if kind == "scheduled" and not force and _already_sent_today(today):
            return {"status": "skipped", "reason": "already sent today"}

        data = _todays_incomplete_outbox(kind, today) if kind == "scheduled" else None
        if data is None:
            data = _build_outbox(kind, log)
        else:
            left = sum(1 for m in data["messages"] if not m["sent"])
            log(f"  resuming outbox {data['run_id']} ({left} message(s) left to send)")

        _send_outbox(data, log)
        _finalize(data, kind, today, mark=mark)
        _cleanup_outbox()
        return {"status": "sent", "messages": len(data["messages"]), "links": len(data["links"])}
    finally:
        _release_lock(lock_f)

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-mark", action="store_true", help="test run: do not mark items as sent")
    ap.add_argument("--print", action="store_true", help="print instead of sending")
    ap.add_argument("--force", action="store_true", help="send even if a briefing was already sent today")
    args = ap.parse_args()

    if args.print:
        blocks, links = build()
        messages = pack(blocks)
        for m in messages:
            print(m, "\n" + "-" * 60)
        print(f"\n{len(messages)} message(s), {len(links)} items, sent=False, marked as sent=False")
        raise SystemExit(0)

    result = deliver_briefing(kind="scheduled", mark=not args.no_mark, force=args.force)
    if result["status"] == "skipped":
        print("Briefing for today was already sent - skipping (use --force to send again).")
    elif result["status"] == "locked":
        print("Another briefing is already being built/sent - skipping.")
    else:
        print(f"\n{result['messages']} message(s), {result['links']} items, "
              f"sent=True, marked as sent={not args.no_mark}")
