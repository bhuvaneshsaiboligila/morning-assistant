"""Two-way Telegram chat with the local AI. Only answers TELEGRAM_CHAT_ID (me).

Commands: /help /new /today /briefing /add /j /ask /memories /forget /journal /export /week
Messages starting with "remind me", "add" or "schedule" become a Google Calendar
event - but only after I tap ✅ on the confirmation card.
"""
import datetime as dt
import html
import os
import re
import sys
import tempfile
import threading
import time
import traceback
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo

import ollama

from assistant.notify import _call, redact, send, send_document
from assistant.summarize import MODEL

LOCAL = ZoneInfo("Europe/Berlin")
OWNER = int(os.environ["TELEGRAM_CHAT_ID"])
MAX_HISTORY = 12
EVENT_RE = re.compile(r"^\s*(/add\b|add\b|remind me\b|schedule\b)", re.I)

HELP = ("Hi! I'm your local assistant, running on your laptop.\n\n"
        "Just write to chat with me.\n\n"
        "📅 To add something to Google Calendar, start your message with "
        "'remind me', 'add' or 'schedule', for example:\n"
        "remind me about my exam on 27 Jan 2027 at 10:00, 1 week before\n\n"
        "📝 Private journal (never logged, stays local):\n"
        "/j <text> – save a journal entry\n"
        "/ask <question> – ask about your journal and memories\n"
        "/memories [kind] – list active memories\n"
        "/forget <id> – forget a memory (see its id in /memories)\n"
        "/journal [YYYY-MM-DD|today|yesterday] – show a day's entries\n"
        "/export – export entries + memories as a Markdown file\n"
        "/week – build the weekly report now\n\n"
        "/today – my calendar for the next 7 days\n"
        "/briefing – get a news briefing now\n"
        "/new – start a fresh conversation\n"
        "/help – show this message")

history = []
pending = {}  # confirmation-card id -> event waiting for ✅ / ❌

def system_prompt():
    now = dt.datetime.now(LOCAL)
    return (f"You are Bhuvanesh's personal assistant, running locally on his laptop. "
            f"Today is {now:%A, %d %B %Y}, the time is {now:%H:%M} (Europe/Berlin). "
            "Answer concisely and clearly. You have NO internet access: never invent current news, "
            "prices, sports results or dates - say you don't know instead. "
            "Use plain text, no markdown tables. "
            "You cannot create calendar events or reminders yourself in this chat. If he asks for one, "
            "tell him to send a message starting with 'remind me' or 'add', e.g. "
            "'remind me about the exam on 27 Jan 2027 at 10:00'. "
            "He also keeps a private journal: /j saves an entry, /ask answers questions about it. "
            "In THIS normal chat you do NOT have access to the journal or any long-term memory of "
            "past conversations - never claim to remember something he told you before unless it "
            "is in the messages above; if he asks about the journal or something from the past, "
            "tell him to use /ask.")

def model_loaded():
    try:
        return any(m.model.startswith(MODEL) for m in ollama.ps().models)
    except Exception:
        return False

class typing:
    """Show 'typing…' in Telegram until the block finishes (Telegram drops it after 5 s)."""
    def __enter__(self):
        self.stop = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()
    def _loop(self):
        while not self.stop.is_set():
            try:
                _call("sendChatAction", http="POST", json={"chat_id": OWNER, "action": "typing"})
            except Exception:
                pass
            self.stop.wait(4)
    def __exit__(self, *exc):
        self.stop.set()

def wake_notice():
    if not model_loaded():
        send("⏳ Waking up the model (about 2–3 min the first time)…")

def chat(text):
    wake_notice()
    history.append({"role": "user", "content": text})
    del history[:-MAX_HISTORY]
    with typing():
        r = ollama.chat(model=MODEL,
                        messages=[{"role": "system", "content": system_prompt()}] + history,
                        think=False, keep_alive="10m",
                        options={"temperature": 0.6, "num_ctx": 4096})
    answer = r.message.content.strip() or "(empty answer)"
    history.append({"role": "assistant", "content": answer})
    for i in range(0, len(answer), 4000):
        send(answer[i:i + 4000])

def run_briefing():
    from assistant.briefing import deliver_briefing
    wake_notice()
    with typing():
        result = deliver_briefing(kind="manual", log=lambda *_: None)
    if result["status"] == "locked":
        send("A briefing is already being built - it will arrive shortly.")

def show_today():
    from assistant.agenda import fetch_events, format_agenda
    send(format_agenda(fetch_events()), html=True)

def save_journal_entry(text):
    if not text:
        send("Usage: /j <what's on your mind>")
        return
    from assistant.journal import add_entry, connect, extract_memories
    con = connect()
    entry_id = add_entry(con, text)
    send("📝 Saved")
    with typing():
        extract_memories(con, entry_id, text, log=lambda m: print(m, flush=True))

def answer_question(question):
    if not question:
        send("Usage: /ask <question>")
        return
    from assistant.journal import ask, connect
    wake_notice()
    with typing():
        try:
            answer = ask(connect(), question)
        except Exception:
            print(redact(traceback.format_exc()), file=sys.stderr, flush=True)
            send("⚠️ Something went wrong - details are in the laptop log.")
            return
    send(answer)

def show_memories(kind_arg):
    from assistant.journal import MEMORY_KINDS, active_memories, connect
    kind = kind_arg.strip().lower() if kind_arg.strip().lower() in MEMORY_KINDS else None
    mems = active_memories(connect(), kind)
    if not mems:
        send("🧠 No active memories" + (f" of kind '{kind}'." if kind else "."))
        return
    groups = {}
    for m in mems:
        groups.setdefault(m["kind"], []).append(m)
    lines = ["🧠 <b>Active memories</b>"]
    for k in MEMORY_KINDS:
        if k not in groups:
            continue
        lines.append(f"\n<b>{k}</b>")
        lines += [f"  [{m['id']}] {html.escape(m['text'])}" for m in groups[k]]
    send("\n".join(lines), html=True)

def forget(arg):
    from assistant.journal import connect, forget_memory
    try:
        memory_id = int(arg.strip())
    except ValueError:
        send("Usage: /forget <id> (see the id in /memories)")
        return
    con = connect()
    if not con.execute("SELECT 1 FROM memories WHERE id=? AND status='active'", (memory_id,)).fetchone():
        send("⚠️ No active memory with that id.")
        return
    forget_memory(con, memory_id)
    send(f"🗑 Forgot memory [{memory_id}].")

def show_journal(arg):
    from assistant.journal import connect, entries_on_day
    today = dt.datetime.now(LOCAL).date()
    arg = (arg or "today").strip().lower()
    if arg == "today":
        day = today
    elif arg == "yesterday":
        day = today - dt.timedelta(days=1)
    else:
        try:
            day = dt.date.fromisoformat(arg)
        except ValueError:
            send("Usage: /journal [YYYY-MM-DD|today|yesterday]")
            return
    entries = entries_on_day(connect(), day)
    if not entries:
        send(f"📓 No journal entries for {day:%a %d %b}.")
        return
    lines = [f"📓 <b>{day:%A, %d %B}</b>"]
    for e in entries:
        t = dt.datetime.fromisoformat(e["created_at"]).astimezone(LOCAL)
        lines.append(f"\n<i>{t:%H:%M}</i>\n{html.escape(e['text'])}")
    send("\n".join(lines), html=True)

def export_journal():
    from assistant.journal import connect, export_markdown
    text = export_markdown(connect())
    path = Path(tempfile.gettempdir()) / f"journal-export-{uuid.uuid4().hex[:8]}.md"
    path.write_text(text)
    try:
        send_document(str(path), caption="📤 Journal export")
    finally:
        path.unlink(missing_ok=True)

def send_report_now():
    from assistant.journal import deliver_report
    wake_notice()
    with typing():
        result = deliver_report("week", kind="manual", log=lambda *_: None)
    if result["status"] == "locked":
        send("A briefing is already being built - it will arrive shortly.")

def handle_event(text):
    from assistant.events import describe, parse
    wake_notice()
    with typing():
        try:
            ev = parse(re.sub(r"^\s*/add\s*", "", text))
        except Exception:
            print(redact(traceback.format_exc()), file=sys.stderr, flush=True)
            send("🤔 I couldn't turn that into an event. Try for example:\n"
                 "remind me about my Linear Algebra exam on 27 Jan 2027 at 10:00")
            return
    pid = uuid.uuid4().hex[:8]
    pending[pid] = ev
    day = ev["start"] if ev["all_day"] else ev["start"].date()
    add_label = "✅ Add anyway" if day < dt.datetime.now(LOCAL).date() else "✅ Add"
    send(describe(ev), html=True, reply_markup={"inline_keyboard": [[
        {"text": add_label, "callback_data": f"add:{pid}"},
        {"text": "❌ Cancel", "callback_data": f"no:{pid}"}]]})

def handle_button(cq):
    _call("answerCallbackQuery", http="POST", json={"callback_query_id": cq["id"]})
    action, _, pid = (cq.get("data") or "").partition(":")
    msg = cq["message"]
    ev = pending.pop(pid, None)
    if ev is None:
        text = "⌛ This request expired (the bot was restarted). Please send it again."
    elif action == "no":
        text = "❌ Cancelled - nothing was added."
    else:
        from assistant.events import describe, google_minutes
        from assistant.gcal import create_event
        created = create_event(ev["title"], ev["start"], ev["end"], all_day=ev["all_day"],
                               location=ev["location"], reminders=google_minutes(ev),
                               description="Added via morning-assistant")
        text = (describe(ev, header="✅ <b>Added to Google Calendar</b>")
                + f'\n\n<a href="{html.escape(created["htmlLink"])}">Open in Google Calendar</a>')
    _call("editMessageText", http="POST", json={
        "chat_id": msg["chat"]["id"], "message_id": msg["message_id"], "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": True})

def handle(text):
    stripped = text.strip()
    cmd = stripped.split()[0].lower() if stripped else ""
    arg = stripped[len(cmd):].strip()
    if cmd in ("/start", "/help"):
        send(HELP)
    elif cmd == "/new":
        history.clear()
        send("🧹 New conversation started.")
    elif cmd == "/today":
        show_today()
    elif cmd == "/briefing":
        run_briefing()
    elif cmd == "/j":
        save_journal_entry(arg)
    elif cmd == "/ask":
        answer_question(arg)
    elif cmd == "/memories":
        show_memories(arg)
    elif cmd == "/forget":
        forget(arg)
    elif cmd == "/journal":
        show_journal(arg)
    elif cmd == "/export":
        export_journal()
    elif cmd == "/week":
        send_report_now()
    elif EVENT_RE.match(text):
        handle_event(text)
    else:
        chat(text)

def report(e):
    print(redact(traceback.format_exc()), file=sys.stderr, flush=True)
    s = str(e)
    if "invalid_grant" in s or type(e).__name__ == "RefreshError" or "not authorized" in s:
        send("🔑 Google Calendar login expired. On the laptop run:\n"
             "cd ~/morning-assistant && .venv/bin/python -m assistant.gcal auth")
    else:
        send("⚠️ Something went wrong - details are in the laptop log.")

def main():
    _call("setMyCommands", http="POST", json={"commands": [
        {"command": "today", "description": "My calendar for the next 7 days"},
        {"command": "add", "description": "Add an event, e.g. /add exam 27 Jan 2027 10:00"},
        {"command": "briefing", "description": "Get a news briefing now"},
        {"command": "j", "description": "Save a journal entry"},
        {"command": "ask", "description": "Ask about your journal and memories"},
        {"command": "memories", "description": "List active memories"},
        {"command": "forget", "description": "Forget a memory by id"},
        {"command": "journal", "description": "Show a day's journal entries"},
        {"command": "export", "description": "Export journal as Markdown"},
        {"command": "week", "description": "Build the weekly report now"},
        {"command": "new", "description": "Start a fresh conversation"},
        {"command": "help", "description": "Show commands"}]})
    pending_updates = _call("getUpdates", params={"timeout": 0})
    offset = pending_updates[-1]["update_id"] + 1 if pending_updates else None
    print("bot started", flush=True)

    while True:
        try:
            params = {"timeout": 50, "allowed_updates": '["message","callback_query"]'}
            if offset:
                params["offset"] = offset
            updates = _call("getUpdates", params=params, timeout=60)
        except Exception as e:
            print(f"poll error: {redact(str(e))}", flush=True)
            time.sleep(10)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            try:
                if "callback_query" in u:
                    cq = u["callback_query"]
                    if cq.get("from", {}).get("id") == OWNER:
                        handle_button(cq)
                    continue
                msg = u.get("message") or {}
                if msg.get("chat", {}).get("id") != OWNER:
                    continue  # ignore everyone except me
                text = msg.get("text")
                if not text:
                    send("I can only read text messages for now.")
                    continue
                handle(text)
            except Exception as e:
                report(e)

if __name__ == "__main__":
    main()
