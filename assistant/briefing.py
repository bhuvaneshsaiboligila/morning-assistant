"""Build the morning briefing and send it to Telegram.

    python -m assistant.briefing            # real run: send + mark items as sent
    python -m assistant.briefing --no-mark  # test: send, but items can appear again
    python -m assistant.briefing --print    # test: print only, send nothing
"""
import argparse
import datetime as dt
import html
from zoneinfo import ZoneInfo

from assistant.agenda import fetch_events, format_agenda
from assistant.collect import collect
from assistant.notify import send
from assistant.rank import select
from assistant.store import connect, mark_briefed, save_new, unbriefed
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
        cal_error = str(e)

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
        header += "\n⚠️ Calendar unavailable: " + esc(cal_error)
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

STAMP = connect.__globals__["DB"].parent / "last_briefing.txt"

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-mark", action="store_true", help="test run: do not mark items as sent")
    ap.add_argument("--print", action="store_true", help="print instead of sending")
    ap.add_argument("--force", action="store_true", help="send even if a briefing was already sent today")
    args = ap.parse_args()

    today = dt.datetime.now(LOCAL).date().isoformat()
    real_run = not (args.no_mark or args.print)
    if real_run and not args.force and STAMP.exists() and STAMP.read_text().strip() == today:
        print(f"Briefing for {today} was already sent - skipping (use --force to send again).")
        raise SystemExit(0)

    blocks, links = build()
    messages = pack(blocks)
    for m in messages:
        if args.print:
            print(m, "\n" + "-" * 60)
        else:
            send(m, html=True)
    if real_run:
        mark_briefed(connect(), links)
        STAMP.write_text(today)
    print(f"\n{len(messages)} message(s), {len(links)} items, sent={not args.print}, marked as sent={real_run}")
