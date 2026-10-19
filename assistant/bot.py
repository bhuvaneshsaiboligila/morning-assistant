"""Two-way Telegram chat with the local AI. Only answers TELEGRAM_CHAT_ID (me).

Commands: /help  /new  /briefing
"""
import datetime as dt
import os
import threading
import time
import traceback
from zoneinfo import ZoneInfo

import ollama

from assistant.notify import _call, send
from assistant.summarize import MODEL

LOCAL = ZoneInfo("Europe/Berlin")
OWNER = int(os.environ["TELEGRAM_CHAT_ID"])
MAX_HISTORY = 12  # messages remembered in the current conversation

HELP = ("Hi! I'm your local assistant, running on your laptop.\n\n"
        "Just write to chat with me.\n"
        "/new – start a fresh conversation\n"
        "/today – my calendar for the next 7 days\n"
        "/briefing – get a news briefing now\n"
        "/help – show this message")

history = []

def system_prompt():
    now = dt.datetime.now(LOCAL)
    return (f"You are Bhuvanesh's personal assistant, running locally on his laptop. "
            f"Today is {now:%A, %d %B %Y}, the time is {now:%H:%M} (Europe/Berlin). "
            "Answer concisely and clearly. You have NO internet access: never invent current news, "
            "prices, sports results or dates - say you don't know instead. "
            "Use plain text, no markdown tables.")

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

def reply_chat(text):
    if not model_loaded():
        send("⏳ Waking up the model (about 2–3 min the first time)…")
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
    from assistant.briefing import build, pack
    from assistant.store import connect, mark_briefed
    send("📰 Building your briefing (about 4 min)…")
    with typing():
        blocks, links = build(log=lambda *_: None)
    for m in pack(blocks):
        send(m, html=True)
    mark_briefed(connect(), links)

def handle(text):
    cmd = text.strip().split()[0].lower() if text.strip() else ""
    if cmd in ("/start", "/help"):
        send(HELP)
    elif cmd == "/new":
        history.clear()
        send("🧹 New conversation started.")
    elif cmd == "/today":
        from assistant.agenda import fetch_events, format_agenda
        send(format_agenda(fetch_events()), html=True)
    elif cmd == "/briefing":
        run_briefing()
    else:
        reply_chat(text)

def main():
    _call("setMyCommands", http="POST", json={"commands": [
        {"command": "new", "description": "Start a fresh conversation"},
        {"command": "today", "description": "My calendar for the next 7 days"},
        {"command": "briefing", "description": "Get a news briefing now"},
        {"command": "help", "description": "Show commands"}]})
    # Skip messages that arrived while the bot was off
    pending = _call("getUpdates", params={"timeout": 0})
    offset = pending[-1]["update_id"] + 1 if pending else None
    print("bot started", flush=True)

    while True:
        try:
            params = {"timeout": 50, "allowed_updates": '["message"]'}
            if offset:
                params["offset"] = offset
            updates = _call("getUpdates", params=params, timeout=60)
        except Exception as e:
            print(f"poll error: {e}", flush=True)
            time.sleep(10)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            msg = u.get("message") or {}
            if msg.get("chat", {}).get("id") != OWNER:
                continue  # ignore everyone except me
            text = msg.get("text")
            if not text:
                send("I can only read text messages for now.")
                continue
            try:
                handle(text)
            except Exception as e:
                traceback.print_exc()
                send(f"⚠️ Something went wrong: {type(e).__name__}: {e}"[:500])

if __name__ == "__main__":
    main()
