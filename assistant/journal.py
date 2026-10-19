"""Private journal + long-term memory, backed by the same SQLite file as the rest
of the assistant (data/assistant.db) and the same local model (Ollama).

- journal: every entry saved verbatim with a timestamp, searchable via FTS5.
- memories: durable facts extracted from entries by the model (preference, goal,
  project, decision, challenge, commitment, skill, life_event, habit). Updating a
  memory keeps history: the old row is marked superseded, never deleted.
- reports: weekly/monthly summaries built map-reduce style (per-day summaries,
  cached in day_summaries, then reduced into one report) so everything fits the
  4096-token context window.

Privacy: journal and memory TEXT is never logged and never put into an exception
message - only exception types are logged. The DB file is chmod 600.

All English-only prompts live together near the top of this file so German
variants can be added later without hunting through the module.
"""
import datetime as dt
import json
import re
from zoneinfo import ZoneInfo

import ollama

from assistant.store import connect as _db_connect
from assistant.summarize import MODEL

LOCAL = ZoneInfo("Europe/Berlin")
UTC = dt.timezone.utc

MEMORY_KINDS = ["preference", "goal", "project", "decision", "challenge",
                "commitment", "skill", "life_event", "habit"]
DEFAULT_EXPIRY_DAYS = 14
CONTEXT_CHAR_BUDGET = 9000  # ~2500 tokens of context for /ask, at a safe ~3.5 chars/token

# ---------------------------------------------------------------------------
# Prompts (English only for now - keep them together here to add German later)
# ---------------------------------------------------------------------------

EXTRACT_PROMPT = """You maintain a long-term memory about the user from their private journal.
Existing related memories (id, kind, text):
{memories}

New journal entry:
{entry}

Decide which of these existing memories (if any) this entry should UPDATE, and what NEW memories
(if any) it should ADD. Valid kinds: {kinds}.
Rules:
- Only include facts the user actually stated. Never guess, infer psychology, or add anything that
  is not directly in the entry.
- Moods, feelings and one-off daily details are NOT memories - leave them out, they stay in the
  journal entry itself.
- A short-term plan (e.g. "doing X this week") is temporary: set temporary=true and
  expires_in_days (use {default_expiry} if unsure). Durable facts are temporary=false with
  expires_in_days=null.
- Only use action="update" with the memory_id of one of the existing memories listed above when
  this entry clearly changes or replaces it; otherwise use action="add" with memory_id=null.
- If (and only if) there is truly nothing worth adding or updating, leave items empty - but check
  carefully first: durable facts about plans, decisions, projects, skills and commitments usually
  belong here even from a short entry.

Respond with JSON matching this shape: {{"items": [{{"action": "add"|"update", "memory_id":
int|null, "kind": str, "text": str, "temporary": bool, "expires_in_days": int|null}}, ...]}}"""

ROUTE_PROMPT = """Today is {today} ({weekday}). Turn the question below into a JSON search query
over a personal journal.
- keywords: important words/phrases to search for. Empty list if the question is about a time
  period in general (e.g. "what happened last week") rather than a topic.
- date_from / date_to: YYYY-MM-DD date range to search, or null if there is no date restriction.
  Resolve relative ranges like "last week" (the previous Monday-Sunday), "in September",
  "last 3 months" relative to today. date_to is inclusive.

Question: {question}"""

ASK_PROMPT = """You answer questions about the user's private journal, using ONLY the context
below. Never invent or assume anything that is not stated in it.

{context}

Question: {question}

Rules:
- Answer only from the context above. If nothing relevant is there, say so clearly instead of
  guessing.
- When you cite something from a journal entry, cite its date like [Tue 14 Oct].
- Clearly separate "what you wrote" (direct facts from entries/memories) from any inference you
  make connecting them - label inference explicitly, e.g. "It seems that..."."""

DAY_SUMMARY_PROMPT = """Summarize this journal day in 2-4 short factual bullet points - only what
was actually written, no interpretation. If nothing significant happened, say so in one line.

Entries:
{entries}"""

REPORT_PROMPT = """Here are daily summaries from the user's private journal for {period_label}
({start} to {end}). Write a report using ONLY what's in these summaries - no guessing.

Daily summaries:
{days}

New memories recorded in this period (id - kind - text):
{new_memories}

Write these sections, each a short paragraph or a few bullet points:
- What happened
- What you learned
- Progress
- Decisions
- Recurring difficulties
- Goals for next {period_word}

Then a final section "New memories this {period_word}" listing each new memory with its id so
wrong ones can be removed with /forget <id>. If there is nothing for a section, write
"Nothing noted." for that section."""

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS journal (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    text        TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS journal_fts USING fts5(
    text, content='journal', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS journal_ai AFTER INSERT ON journal BEGIN
    INSERT INTO journal_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS journal_ad AFTER DELETE ON journal BEGIN
    INSERT INTO journal_fts(journal_fts, rowid, text) VALUES('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS journal_au AFTER UPDATE ON journal BEGIN
    INSERT INTO journal_fts(journal_fts, rowid, text) VALUES('delete', old.id, old.text);
    INSERT INTO journal_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TABLE IF NOT EXISTS memories (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL,
    text            TEXT NOT NULL,
    source_entry_id INTEGER,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',
    superseded_by   INTEGER,
    expires_at      TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    text, content='memories', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, text) VALUES('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, text) VALUES('delete', old.id, old.text);
    INSERT INTO memories_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TABLE IF NOT EXISTS reports (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    period       TEXT NOT NULL,
    period_start TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    text         TEXT NOT NULL
);

-- Per-day entry summaries, cached so weekly/monthly reports don't re-summarize.
CREATE TABLE IF NOT EXISTS day_summaries (
    day         TEXT PRIMARY KEY,
    text        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""

def connect():
    con = _db_connect()  # applies the articles schema and chmod 600 on data/assistant.db
    con.executescript(SCHEMA)
    return con

def _now():
    return dt.datetime.now(UTC).isoformat()

def _day_bounds_utc(day):
    """UTC ISO bounds [start, end) of a Europe/Berlin calendar day."""
    start_local = dt.datetime.combine(day, dt.time(), LOCAL)
    end_local = start_local + dt.timedelta(days=1)
    return start_local.astimezone(UTC).isoformat(), end_local.astimezone(UTC).isoformat()

def _fts_query(terms):
    """Build an FTS5 MATCH query (phrases OR'd together) from a list of terms, or None."""
    terms = [str(t).strip() for t in (terms or []) if str(t).strip()]
    if not terms:
        return None
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)

def _tokenize(text, min_len=3, max_terms=24):
    words = re.findall(r"[A-Za-z0-9']+", text.lower())
    seen, terms = set(), []
    for w in words:
        if len(w) >= min_len and w not in seen:
            seen.add(w)
            terms.append(w)
        if len(terms) >= max_terms:
            break
    return terms

# ---------------------------------------------------------------------------
# Journal entries
# ---------------------------------------------------------------------------

def add_entry(con, text, now=None):
    now = now or _now()
    cur = con.execute("INSERT INTO journal (created_at, text) VALUES (?, ?)", (now, text))
    con.commit()
    return cur.lastrowid

def entries_on_day(con, day):
    start, end = _day_bounds_utc(day)
    rows = con.execute("SELECT * FROM journal WHERE created_at >= ? AND created_at < ? "
                       "ORDER BY created_at", (start, end)).fetchall()
    return [dict(r) for r in rows]

def search_entries(con, keywords, date_from=None, date_to=None, limit=10):
    """FTS5 bm25 search within an optional [date_from, date_to] date range. If keywords give no
    query but a date range is set, fall back to the most recent entries in that range."""
    where, params = [], []
    if date_from:
        where.append("j.created_at >= ?")
        params.append(_day_bounds_utc(date_from)[0])
    if date_to:
        where.append("j.created_at < ?")
        params.append(_day_bounds_utc(date_to)[1])
    where_sql = (" AND " + " AND ".join(where)) if where else ""

    query = _fts_query(keywords)
    if query:
        sql = (f"SELECT j.id, j.created_at, j.text FROM journal_fts "
               f"JOIN journal j ON j.id = journal_fts.rowid "
               f"WHERE journal_fts MATCH ?{where_sql} ORDER BY bm25(journal_fts) LIMIT ?")
        rows = con.execute(sql, [query] + params + [limit]).fetchall()
    else:
        sql = f"SELECT j.id, j.created_at, j.text FROM journal j WHERE 1=1{where_sql} ORDER BY j.created_at DESC LIMIT ?"
        rows = con.execute(sql, params + [limit]).fetchall()
    return [dict(r) for r in rows]

# ---------------------------------------------------------------------------
# Memories
# ---------------------------------------------------------------------------

def add_memory(con, kind, text, source_entry_id=None, expires_at=None, now=None):
    now = now or _now()
    cur = con.execute(
        "INSERT INTO memories (kind, text, source_entry_id, created_at, updated_at, status, expires_at) "
        "VALUES (?, ?, ?, ?, ?, 'active', ?)",
        (kind, text, source_entry_id, now, now, expires_at))
    con.commit()
    return cur.lastrowid

def supersede_memory(con, old_id, kind, text, source_entry_id=None, expires_at=None, now=None):
    now = now or _now()
    new_id = add_memory(con, kind, text, source_entry_id, expires_at, now)
    con.execute("UPDATE memories SET status='superseded', superseded_by=?, updated_at=? WHERE id=?",
               (new_id, now, old_id))
    con.commit()
    return new_id

def forget_memory(con, memory_id, now=None):
    con.execute("UPDATE memories SET status='forgotten', updated_at=? WHERE id=?",
               (now or _now(), memory_id))
    con.commit()

def expire_memories(con, now=None):
    """Mark temporary memories past their expiry as forgotten. Returns how many were expired."""
    now = now or _now()
    cur = con.execute("UPDATE memories SET status='forgotten', updated_at=? "
                       "WHERE status='active' AND expires_at IS NOT NULL AND expires_at <= ?",
                       (now, now))
    con.commit()
    return cur.rowcount

def active_memories(con, kind=None):
    expire_memories(con)
    if kind:
        rows = con.execute("SELECT * FROM memories WHERE status='active' AND kind=? "
                           "ORDER BY updated_at DESC", (kind,)).fetchall()
    else:
        rows = con.execute("SELECT * FROM memories WHERE status='active' "
                           "ORDER BY kind, updated_at DESC").fetchall()
    return [dict(r) for r in rows]

def search_memories(con, keywords, limit=8):
    expire_memories(con)
    query = _fts_query(keywords)
    if not query:
        rows = con.execute("SELECT * FROM memories WHERE status='active' "
                           "ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    sql = ("SELECT m.* FROM memories_fts JOIN memories m ON m.id = memories_fts.rowid "
           "WHERE memories_fts MATCH ? AND m.status='active' ORDER BY bm25(memories_fts) LIMIT ?")
    return [dict(r) for r in con.execute(sql, (query, limit)).fetchall()]

def related_memories(con, text, limit=8):
    """The ~`limit` most similar active memories to free-form `text` (FTS over its tokens)."""
    rows = search_memories(con, _tokenize(text), limit=limit)
    if rows:
        return rows
    expire_memories(con)
    rows = con.execute("SELECT * FROM memories WHERE status='active' "
                       "ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]

# ---------------------------------------------------------------------------
# Memory extraction from a journal entry
# ---------------------------------------------------------------------------

def _call_extract_model(entry_text, related):
    schema = {
        "type": "object",
        "properties": {"items": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "memory_id": {"type": ["integer", "null"]},
                "kind": {"type": "string"},
                "text": {"type": "string"},
                "temporary": {"type": "boolean"},
                "expires_in_days": {"type": ["integer", "null"]},
            },
            "required": ["action", "memory_id", "kind", "text", "temporary", "expires_in_days"],
        }}},
        "required": ["items"],
    }
    context = "\n".join(f"[{m['id']}] ({m['kind']}) {m['text']}" for m in related) or "(none yet)"
    r = ollama.chat(
        model=MODEL,
        messages=[{"role": "user", "content": EXTRACT_PROMPT.format(
            memories=context, entry=entry_text, kinds=", ".join(MEMORY_KINDS),
            default_expiry=DEFAULT_EXPIRY_DAYS)}],
        format=schema, think=False, keep_alive="10m",
        options={"temperature": 0, "num_ctx": 4096},
    )
    return json.loads(r.message.content)["items"]

def _valid_items(raw, related_ids):
    """Drop anything that isn't a well-formed add/update item referencing a real related memory."""
    items = []
    if not isinstance(raw, list):
        return items
    for item in raw:
        if not isinstance(item, dict):
            continue
        action = item.get("action")
        kind = item.get("kind")
        text = (item.get("text") or "").strip()
        if action not in ("add", "update") or kind not in MEMORY_KINDS or not text or len(text) > 500:
            continue
        memory_id = item.get("memory_id")
        if action == "update" and (not isinstance(memory_id, int) or memory_id not in related_ids):
            continue
        temporary = bool(item.get("temporary"))
        expires_in_days = item.get("expires_in_days")
        if temporary and not (isinstance(expires_in_days, int) and expires_in_days > 0):
            expires_in_days = DEFAULT_EXPIRY_DAYS
        items.append({"action": action, "memory_id": memory_id if action == "update" else None,
                      "kind": kind, "text": text[:500], "temporary": temporary,
                      "expires_in_days": expires_in_days if temporary else None})
    return items

def apply_extraction(con, entry_id, items, now=None):
    """Write validated add/update items to `memories`. Returns the list of new/updated row ids."""
    now = now or _now()
    created = []
    for item in items:
        expires_at = None
        if item["temporary"] and item["expires_in_days"]:
            expires_at = (dt.datetime.fromisoformat(now) + dt.timedelta(days=item["expires_in_days"])).isoformat()
        if item["action"] == "update" and item["memory_id"]:
            new_id = supersede_memory(con, item["memory_id"], item["kind"], item["text"],
                                      source_entry_id=entry_id, expires_at=expires_at, now=now)
        else:
            new_id = add_memory(con, item["kind"], item["text"], source_entry_id=entry_id,
                                expires_at=expires_at, now=now)
        created.append(new_id)
    return created

def extract_memories(con, entry_id, entry_text, log=lambda *_: None):
    """Extract memories from a journal entry. Never raises - a failure here must never lose the
    entry, which is already saved by the time this runs. Only exception TYPES are logged, never
    any entry/memory text."""
    try:
        related = related_memories(con, entry_text)
        raw = _call_extract_model(entry_text, related)
    except Exception as e:
        log(f"  MEMORY EXTRACTION FAILED (model): {type(e).__name__}")
        return []
    try:
        items = _valid_items(raw, {m["id"] for m in related})
        return apply_extraction(con, entry_id, items)
    except Exception as e:
        log(f"  MEMORY EXTRACTION FAILED (apply): {type(e).__name__}")
        return []

# ---------------------------------------------------------------------------
# /ask
# ---------------------------------------------------------------------------

def _safe_date(s):
    if not s:
        return None
    try:
        return dt.date.fromisoformat(str(s).strip())
    except ValueError:
        return None

def resolve_route(raw, now):
    """Validate/normalize the model's {keywords, date_from, date_to} JSON. Pure and testable."""
    keywords = [str(k).strip() for k in (raw.get("keywords") or []) if str(k).strip()][:10]
    date_from, date_to = _safe_date(raw.get("date_from")), _safe_date(raw.get("date_to"))
    if date_from and date_to and date_from > date_to:
        date_from, date_to = date_to, date_from
    today = now.date() if isinstance(now, dt.datetime) else now
    if date_to and date_to > today:
        date_to = today
    return {"keywords": keywords, "date_from": date_from, "date_to": date_to}

def _call_route_model(question, now):
    schema = {
        "type": "object",
        "properties": {
            "keywords": {"type": "array", "items": {"type": "string"}},
            "date_from": {"type": ["string", "null"]},
            "date_to": {"type": ["string", "null"]},
        },
        "required": ["keywords", "date_from", "date_to"],
    }
    r = ollama.chat(
        model=MODEL,
        messages=[{"role": "user", "content": ROUTE_PROMPT.format(
            today=now.date().isoformat(), weekday=f"{now:%A}", question=question)}],
        format=schema, think=False, keep_alive="10m",
        options={"temperature": 0, "num_ctx": 2048},
    )
    return json.loads(r.message.content)

def format_context(entries, memories, budget=CONTEXT_CHAR_BUDGET):
    lines, used = ["MEMORIES:"], 0
    for m in memories:
        line = f"- ({m['kind']}) {m['text']}"
        if used + len(line) > budget:
            break
        lines.append(line)
        used += len(line)
    lines.append("\nENTRIES:")
    for e in entries:
        day = dt.datetime.fromisoformat(e["created_at"]).astimezone(LOCAL)
        line = f"[{day:%a %d %b}] {e['text']}"
        if used + len(line) > budget:
            break
        lines.append(line)
        used += len(line)
    return "\n".join(lines)

def _call_answer_model(context, question):
    r = ollama.chat(
        model=MODEL,
        messages=[{"role": "user", "content": ASK_PROMPT.format(context=context, question=question)}],
        think=False, keep_alive="10m", options={"temperature": 0.2, "num_ctx": 4096},
    )
    return r.message.content.strip()

def ask(con, question, now=None):
    now = now or dt.datetime.now(LOCAL)
    route = resolve_route(_call_route_model(question, now), now)
    entries = search_entries(con, route["keywords"], route["date_from"], route["date_to"])
    memories = search_memories(con, route["keywords"])
    if not entries and not memories:
        return "🤷 I couldn't find anything relevant in the journal."
    return _call_answer_model(format_context(entries, memories), question)

# ---------------------------------------------------------------------------
# Weekly / monthly reports (map-reduce: per-day summary, cached, then reduced)
# ---------------------------------------------------------------------------

def week_period(run_date):
    """The 7 days ending the day before run_date (so a Sunday 09:00 run covers Sun-Sat)."""
    end = run_date - dt.timedelta(days=1)
    return end - dt.timedelta(days=6), end

def month_period(run_date):
    """The previous calendar month, relative to run_date."""
    end = run_date.replace(day=1) - dt.timedelta(days=1)
    return end.replace(day=1), end

def _call_day_summary_model(entries_text):
    r = ollama.chat(model=MODEL, messages=[{"role": "user", "content": DAY_SUMMARY_PROMPT.format(entries=entries_text)}],
                    think=False, keep_alive="10m", options={"temperature": 0.2, "num_ctx": 4096})
    return r.message.content.strip()

def day_summary(con, day, now=None):
    """Cached per-day summary of that day's entries, or None if there were none."""
    cached = con.execute("SELECT text FROM day_summaries WHERE day=?", (day.isoformat(),)).fetchone()
    if cached:
        return cached["text"]
    entries = entries_on_day(con, day)
    if not entries:
        return None
    summary = _call_day_summary_model("\n".join(e["text"] for e in entries))
    con.execute("INSERT OR REPLACE INTO day_summaries (day, text, created_at) VALUES (?, ?, ?)",
               (day.isoformat(), summary, now or _now()))
    con.commit()
    return summary

def _call_report_model(prompt_text):
    r = ollama.chat(model=MODEL, messages=[{"role": "user", "content": prompt_text}],
                    think=False, keep_alive="10m", options={"temperature": 0.3, "num_ctx": 4096})
    return r.message.content.strip()

def save_report(con, period, period_start, text, now=None):
    con.execute("INSERT INTO reports (period, period_start, created_at, text) VALUES (?, ?, ?, ?)",
               (period, period_start.isoformat(), now or _now(), text))
    con.commit()

def build_report(con, period, run_date=None):
    """period: 'week' or 'month'. Builds (and stores) the report text. Never calls the model
    if there are no entries in the period."""
    run_date = run_date or dt.datetime.now(LOCAL).date()
    start, end = week_period(run_date) if period == "week" else month_period(run_date)
    period_word = "week" if period == "week" else "month"

    days_text, day = [], start
    while day <= end:
        summary = day_summary(con, day)
        if summary:
            days_text.append(f"{day:%a %d %b}: {summary}")
        day += dt.timedelta(days=1)

    if not days_text:
        text = f"📓 No journal entries this {period_word} ({start:%d %b}-{end:%d %b})."
        save_report(con, period, start, text)
        return text

    start_utc, _ = _day_bounds_utc(start)
    _, end_utc = _day_bounds_utc(end)
    new_memories = con.execute(
        "SELECT id, kind, text FROM memories WHERE created_at >= ? AND created_at < ? AND status != 'forgotten' "
        "ORDER BY created_at", (start_utc, end_utc)).fetchall()
    mem_text = "\n".join(f"[{m['id']}] ({m['kind']}) {m['text']}" for m in new_memories) or "(none)"

    prompt_text = REPORT_PROMPT.format(
        period_label=f"the past {period_word}", start=f"{start:%d %b}", end=f"{end:%d %b}",
        days="\n".join(days_text), new_memories=mem_text, period_word=period_word)
    text = _call_report_model(prompt_text)
    save_report(con, period, start, text)
    return text

def deliver_report(period, run_date=None, kind="scheduled", log=print):
    """Build & send the weekly/monthly report, serialized with the same lock as the briefing."""
    from assistant.briefing import _acquire_lock, _release_lock
    from assistant.notify import send
    lock_f = _acquire_lock(blocking=(kind == "scheduled"))
    if lock_f is None:
        return {"status": "locked"}
    try:
        con = connect()
        text = build_report(con, period, run_date)
        for i in range(0, len(text), 3800):
            send(text[i:i + 3800])
        log(f"  {period} report sent ({len(text)} chars)")
        return {"status": "sent"}
    finally:
        _release_lock(lock_f)

# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def export_markdown(con):
    """A Markdown export of every journal entry and every active memory."""
    entries = con.execute("SELECT * FROM journal ORDER BY created_at").fetchall()
    memories = active_memories(con)
    lines = ["# Journal export", "", "## Entries"]
    for e in entries:
        t = dt.datetime.fromisoformat(e["created_at"]).astimezone(LOCAL)
        lines.append(f"\n### {t:%Y-%m-%d %H:%M}\n{e['text']}")
    lines.append("\n## Active memories")
    for m in memories:
        lines.append(f"- [{m['id']}] ({m['kind']}) {m['text']}")
    return "\n".join(lines)

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Build and send the weekly/monthly journal report.")
    ap.add_argument("period", nargs="?", choices=["week", "month"],
                    help="force a period; default: auto-detect from today (Sunday=week, 1st=month)")
    args = ap.parse_args()

    today = dt.datetime.now(LOCAL).date()
    period = args.period
    if period is None:
        if today.day == 1:
            period = "month"
        elif today.weekday() == 6:
            period = "week"
        else:
            print(f"{today} is neither the 1st of the month nor a Sunday - nothing to do.")
            raise SystemExit(0)
    result = deliver_report(period, run_date=today, kind="scheduled")
    print(f"{period} report: {result['status']}")
