"""Fetch news from the RSS/Atom feeds listed in config/feeds.yaml."""
import calendar
import datetime as dt
import html
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
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

def normalize_link(link):
    """Remove tracking parameters so the same article always has the same URL."""
    p = urlsplit(link.strip())
    query = [(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith(("utm_", "at_"))]
    return urlunsplit((p.scheme, p.netloc.lower(), p.path, urlencode(query), ""))

def fetch_feed(feed):
    r = httpx.get(feed["url"], headers=HEADERS, timeout=20, follow_redirects=True)
    r.raise_for_status()
    items = []
    for e in feedparser.parse(r.content).entries:
        if not e.get("title") or not e.get("link"):
            continue
        items.append({
            "title": _clean(e.title, 200),
            "link": normalize_link(e.link),
            "source": feed["name"],
            "category": feed["category"],
            "published": _published(e),
            "summary": _clean(e.get("summary", "")),
        })
    return items

def _excluded(item, cfg):
    return (any(re.search(p, item["title"], re.I) for p in cfg.get("exclude_title", []))
            or any(re.search(p, item["link"], re.I) for p in cfg.get("exclude_link", [])))

def collect():
    """Return (fresh, non-excluded items, per-source status report)."""
    cfg = yaml.safe_load(CONFIG.read_text())
    now = dt.datetime.now(dt.timezone.utc)
    items, report = [], []
    for feed in cfg["feeds"]:
        cutoff = now - dt.timedelta(hours=feed.get("max_age_hours", cfg.get("max_age_hours", 36)))
        try:
            got = fetch_feed(feed)
        except Exception as e:
            report.append((feed["name"], f"FAILED - {type(e).__name__}: {e}"))
            continue
        fresh = [i for i in got if i["published"] and i["published"] >= cutoff]
        kept = [i for i in fresh if not _excluded(i, cfg)]
        undated = sum(1 for i in got if not i["published"])
        report.append((feed["name"], f"{len(got)} items, {len(fresh)} fresh, "
                                     f"{len(fresh) - len(kept)} excluded, {undated} undated"))
        items.extend(kept)
    return items, report

if __name__ == "__main__":
    items, report = collect()
    for name, status in report:
        print(f"  {name}: {status}")
    print(f"{len(items)} items kept")
