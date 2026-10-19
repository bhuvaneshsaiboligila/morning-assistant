"""Telegram delivery: send messages to my phone."""
import os
import sys
import httpx

API = "https://api.telegram.org/bot{token}/{name}"

def _call(name, http="GET", **kwargs):
    url = API.format(token=os.environ["TELEGRAM_BOT_TOKEN"], name=name)
    try:
        r = httpx.request(http, url, timeout=15, **kwargs)
        data = r.json()
    except httpx.HTTPError as e:
        raise RuntimeError(f"Telegram {name}: network error ({type(e).__name__})") from None
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {name} failed: {data.get('description', 'unknown error')}")
    return data["result"]

def find_chat_ids():
    """Chat IDs of everyone who has messaged the bot."""
    return {u["message"]["chat"]["id"] for u in _call("getUpdates") if "message" in u}

def send(text, chat_id=None):
    chat_id = chat_id or os.environ["TELEGRAM_CHAT_ID"]
    _call("sendMessage", http="POST", json={
        "chat_id": chat_id, "text": text, "disable_web_page_preview": True})
    return True

if __name__ == "__main__":
    if sys.argv[1:] == ["chat-id"]:
        print(find_chat_ids())
    else:
        print(send(" ".join(sys.argv[1:]) or "Test from morning-assistant"))
