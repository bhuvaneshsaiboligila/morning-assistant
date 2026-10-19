"""SQLite storage: remembers every article so nothing is reported twice."""
import datetime as dt
import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "assistant.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    link        TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    source      TEXT NOT NULL,
    category    TEXT NOT NULL,
    published   TEXT NOT NULL,
    summary     TEXT,
    first_seen  TEXT NOT NULL,
    briefed_at  TEXT
);
"""

def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat()

def connect():
    DB.parent.mkdir(exist_ok=True)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con

def save_new(con, items):
    """Store items; return only the ones never seen before."""
    new, now = [], _now()
    with con:
        for i in items:
            cur = con.execute(
                "INSERT OR IGNORE INTO articles "
                "(link, title, source, category, published, summary, first_seen) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (i["link"], i["title"], i["source"], i["category"],
                 i["published"].isoformat(), i["summary"], now))
            if cur.rowcount:
                new.append(i)
    return new

def unbriefed(con):
    """Articles that have not been in a briefing yet, newest first."""
    rows = con.execute("SELECT * FROM articles WHERE briefed_at IS NULL ORDER BY published DESC")
    return [dict(r) for r in rows]

def mark_briefed(con, links):
    now = _now()
    with con:
        con.executemany("UPDATE articles SET briefed_at = ? WHERE link = ?", [(now, l) for l in links])

if __name__ == "__main__":
    from assistant.collect import collect
    items, report = collect()
    for name, status in report:
        print(f"  {name}: {status}")
    con = connect()
    new = save_new(con, items)
    total = con.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    print(f"\nCollected {len(items)} | NEW {len(new)} | already known {len(items) - len(new)} | in database {total}")
