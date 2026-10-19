"""Fetch news from the RSS/Atom feeds listed in config/feeds.yaml."""
import calendar
import datetime as dt
import html
import re
from pathlib import Path
from zoneinfo import ZoneInfo

import feedparser
import httpx
import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "feeds.yaml"
HEADERS = {"User-Agent": "morning-assistant/0.1 (personal news briefing)"}
LOCAL = ZoneInfo("Europe/Berlin")

def _clean(text, limit=400):
    """Strip HTML tags and extra whitespace, cut to `limit` characters."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    return html.unescape(re.sub(r"\s+", " ", text)).strip()[:limit]

def _published(entry):
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    if not t:
        return None
    return dt.datetime.fromtimestamp(calendar.timegm(t), tz=dt.timezone.utc)

def fetch_feed(feed):
    r = httpx.get(feed["url"], headers=HEADERS, timeout=20, follow_redirects=True)
    r.raise_for_status()
    items = []
    for e in feedparser.parse(r.content).entries:
        if not e.get("title") or not e.get("link"):
            continue
        items.append({
            "title": _clean(e.title, 200),
            "link": e.link,
            "source": feed["name"],
            "category": feed["category"],
            "published": _published(e),
            "summary": _clean(e.get("summary", "")),
        })
    return items

def collect(max_age_hours=36):
    """Return (fresh items, per-source status report)."""
    feeds = yaml.safe_load(CONFIG.read_text())["feeds"]
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=max_age_hours)
    items, report = [], []
    for feed in feeds:
        try:
            got = fetch_feed(feed)
        except Exception as e:
            report.append((feed["name"], f"FAILED - {type(e).__name__}: {e}"))
            continue
        fresh = [i for i in got if i["published"] and i["published"] >= cutoff]
        undated = sum(1 for i in got if not i["published"])
        report.append((feed["name"], f"{len(got)} items, {len(fresh)} fresh, {undated} undated (dropped)"))
        items.extend(fresh)
    return items, report

if __name__ == "__main__":
    items, report = collect()
    print("=== Sources ===")
    for name, status in report:
        print(f"  {name}: {status}")
    print(f"\n=== Newest 3 per category ({len(items)} fresh total) ===")
    for cat in sorted({i["category"] for i in items}):
        print(f"\n[{cat}]")
        newest = sorted((i for i in items if i["category"] == cat), key=lambda i: i["published"], reverse=True)[:3]
        for i in newest:
            print(f"  {i['published'].astimezone(LOCAL):%a %H:%M}  {i['title']}")
            print(f"           {i['link']}")
