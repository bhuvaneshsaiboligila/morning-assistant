"""Fetch news (RSS/Atom) and jobs (Arbeitsagentur) listed in config/feeds.yaml."""
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

BA_URL = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v6/jobs"
BA_HEADERS = {**HEADERS, "X-API-Key": "jobboerse-jobsuche"}   # public client id, not a secret

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

def fetch_rss(feed):
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

def fetch_arbeitsagentur(feed):
    """Job offers from the Bundesagentur für Arbeit job search (API v6).
    Each search runs on its own: one failing search does not stop the others."""
    jobs, errors = {}, []
    for search in feed["searches"]:
        params = {"was": search["was"], "veroeffentlichtseit": feed.get("days", 7),
                  "size": search.get("size", 25)}
        if "angebotsart" in search:
            params["angebotsart"] = search["angebotsart"]
        try:
            r = httpx.get(BA_URL, params=params, headers=BA_HEADERS, timeout=30)
            r.raise_for_status()
            results = r.json().get("ergebnisliste") or []
        except Exception as e:
            errors.append(f"'{search['was']}': {type(e).__name__} {getattr(e, 'response', None) and e.response.status_code}")
            continue
        for job in results:
            refnr = job.get("referenznummer")
            if not refnr or refnr in jobs:
                continue
            places = job.get("stellenlokationen") or [{}]
            place = (places[0].get("adresse") or {}).get("ort", "")
            date = job.get("datumErsteVeroeffentlichung")
            homeoffice = " | home office possible" if job.get("homeofficemoeglich") else ""
            jobs[refnr] = {
                "title": _clean(f"{job.get('stellenangebotsTitel', '')} – {job.get('firma', '')}", 200),
                "link": f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{refnr}",
                "source": feed["name"],
                "category": feed["category"],
                "published": dt.datetime.fromisoformat(date).replace(tzinfo=dt.timezone.utc) if date else None,
                "summary": _clean(f"{job.get('hauptberuf', '')} | {place}{homeoffice}"),
            }
    if errors and not jobs:
        raise RuntimeError("all searches failed: " + "; ".join(errors))
    if errors:
        print(f"  WARNING {feed['name']}: some searches failed: " + "; ".join(errors))
    return list(jobs.values())

FETCHERS = {"rss": fetch_rss, "arbeitsagentur": fetch_arbeitsagentur}

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
            got = FETCHERS[feed.get("type", "rss")](feed)
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
