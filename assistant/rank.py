"""Score articles by my interests and pick the best ones per section."""
import datetime as dt
import re
from pathlib import Path

import yaml

CONFIG = Path(__file__).resolve().parent.parent / "config" / "interests.yaml"

def load():
    return yaml.safe_load(CONFIG.read_text())

def score(article, cfg, now):
    """Return (score, matched keywords) for one article."""
    text = f"{article['title']} {article.get('summary') or ''}".lower()
    s = cfg["sections"].get(article["category"], {}).get("priority", 1)
    hits = []
    for kw, weight in cfg.get("keywords", {}).items():
        if re.search(r"\b" + re.escape(str(kw).lower()), text):
            s += weight
            hits.append(str(kw))
    age_hours = (now - dt.datetime.fromisoformat(article["published"])).total_seconds() / 3600
    s += max(0.0, 2 * (1 - age_hours / 36))          # up to +2 for very recent news
    return round(s, 2), hits

def select(articles, cfg=None):
    """Return {section: [best articles]} following config/interests.yaml."""
    cfg = cfg or load()
    now = dt.datetime.now(dt.timezone.utc)
    for a in articles:
        a["score"], a["hits"] = score(a, cfg, now)
    picked = {}
    for name, sec in cfg["sections"].items():
        group = sorted((a for a in articles if a["category"] == name),
                       key=lambda a: a["score"], reverse=True)
        picked[name] = group[: sec.get("max_items", 3)]
    return picked

if __name__ == "__main__":
    from assistant.store import connect, unbriefed
    articles = unbriefed(connect())
    picked = select(articles)
    total = sum(len(v) for v in picked.values())
    print(f"{len(articles)} unbriefed articles -> {total} selected\n")
    for section, items in picked.items():
        print(f"[{section}]")
        for a in items:
            hits = f"  ({', '.join(a['hits'])})" if a["hits"] else ""
            print(f"  {a['score']:5.1f}  {a['title'][:90]}{hits}")
            print(f"         {a['link']}")
        print()
