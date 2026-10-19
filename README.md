# Morning Assistant — a private AI briefing that runs on my own laptop

Every morning at 5:30 my phone gets a Telegram message with the news, research,
jobs and calendar events that matter to me — selected and summarized by a
**local LLM** (Qwen3.5 4B via Ollama) on a GTX 1650 laptop. No cloud AI, no paid APIs.
I can also chat with the same local model from Telegram.

## What it does
- **News & research** from RSS (BBC, Tagesschau, The Hindu, NVIDIA, arXiv cs.DC)
- **Jobs & internships** in HPC, simulation, CFD and quantum computing from the
  Bundesagentur für Arbeit job search API
- **Ranking** by configurable keywords and section priorities (`config/interests.yaml`)
- **AI summaries** — one sentence per story, translated to English, restricted to facts in the source
- **Google Calendar** — today's events and the next 7 days at the top of the briefing (Calendar API, iCal link as fallback)
- **Add events by chat** — "remind me about my exam on 27 Jan 2027 at 10:00, 1 week before" → the local model
  extracts the event as structured JSON → confirmation card with ✅/❌ buttons → Google Calendar with reminders
- **Telegram bot** — chat with the local model; `/today`, `/add`, `/briefing`, `/new`
- **Automation** — systemd timer wakes the laptop from sleep, retries on network
  failure, never sends twice a day, catches up after the laptop was off
- **Crash-safe delivery** — every briefing run is saved to an outbox file before
  anything is sent, so a crash mid-send never drops or repeats a message

## Architecture
```text
RSS feeds ──┐
Job API  ───┼──► collect ──► SQLite (dedup) ──► rank ──► local LLM summaries ──┐
Calendar ───┘                                                                  ├──► Telegram (phone)
Telegram messages ──► bot ──► local LLM ───────────────────────────────────────┘
```

| Module | Role |
|---|---|
| `assistant/collect.py` | Fetch RSS + job API, normalize links, drop stale/undated/excluded items |
| `assistant/store.py` | SQLite store — every item is reported only once |
| `assistant/rank.py` | Keyword + priority + recency scoring per section |
| `assistant/summarize.py` | One-line summaries with the local model (Ollama) |
| `assistant/agenda.py` | Calendar events via Google Calendar API, iCal link as fallback |
| `assistant/gcal.py` | Google Calendar OAuth, create/delete events with reminders |
| `assistant/events.py` | Chat message → event via the local model with a JSON schema, validated in Python |
| `assistant/briefing.py` | Builds the briefing, delivers it via the outbox, marks items as sent |
| `assistant/bot.py` | Two-way Telegram chat, owner-only |
| `assistant/notify.py` | Telegram API; `redact()` strips secrets before anything is logged |

## Delivery design: outbox, retries, and the lock

Telegram delivery and the SQLite "already briefed" state can't share one
transaction, so the design accepts partial delivery and makes it resumable
instead of pretending it can't happen:

- Every run (scheduled or manual) is built once and written to
  `data/outbox/<run_id>.json` before anything is sent: the message list with a
  per-message `sent` flag, the article links, and whether the run is
  `scheduled` or `manual`.
- Only messages not yet marked `sent` are sent; each is marked `sent` in the
  outbox file immediately after Telegram confirms it, so a crash mid-send
  resumes instead of re-sending or dropping messages.
- Articles are only marked briefed - and, for scheduled runs, the daily stamp
  written - once **every** message in the run has been sent. A scheduled run
  that starts up looks for an incomplete outbox from today first and resumes
  it instead of building a new briefing.
- Completed outbox files older than 14 days are deleted automatically.
- `assistant.briefing.deliver_briefing()` is the single coordinator both the
  systemd timer and the bot's `/briefing` command call into. A file lock
  (`data/briefing.lock`, via `fcntl.flock`) means only one briefing can be
  built or sent at a time: a scheduled run waits for the lock, `/briefing`
  does not and replies "A briefing is already being built - it will arrive
  shortly." if it finds the lock held.
- A scheduled run sends at most once per day (`data/last_briefing.txt`
  stamp). `/briefing` is always allowed, never sets that stamp, but still
  marks its articles as briefed so the morning briefing doesn't repeat them.

## Calendar event validation

Chat messages are turned into events by the local model, then validated in
plain Python (`assistant/events.py`, pure function `build_event`, tested in
`tests/test_events.py` without calling the model):

- `start_time` / `end_time` must be `null` or a strict `HH:MM` (also accepts
  `H:MM` and `HH.MM`) - an invalid time string is rejected outright, never
  silently turned into an all-day event.
- If the end time is not after the start time, it's only accepted as an
  overnight event (e.g. 22:00-01:00, ending the next day) when the times look
  like one; otherwise it's rejected rather than silently changing the
  duration.
- Every automatic correction or default - missing end time, overnight end,
  default reminders - is recorded and shown on the confirmation card as
  `⚠️ ...` lines, along with a `⚠️ This date is in the past` warning (and the
  button becomes "✅ Add anyway") for events in the past.
- Europe/Berlin DST is checked with `zoneinfo`, `datetime.fold`, and a
  round-trip through UTC: a start/end time that doesn't exist (spring-forward
  gap) or is ambiguous (fall-back) is flagged on the card instead of silently
  picking one interpretation.

## Hardware constraints this was built around
- GTX 1650 (4 GB VRAM), 5.7 GB RAM → 4B model, mostly on GPU, ~15 tokens/s
- Model stored on a USB 2.0 stick mounted at boot → ~2.5 min cold load, so the
  briefing job starts at 05:25 for 05:30 delivery
- The model unloads after idle time so the laptop stays usable

## Setup
```bash
git clone https://github.com/bhuvaneshsaiboligila/morning-assistant.git
cd morning-assistant
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                    # fill in your own values
ollama pull qwen3.5:4b
python -m assistant.briefing --print    # dry run, sends nothing
```
systemd units for the timer and the bot are in `deploy/`.

For development, `pip install -r requirements-dev.txt` adds `pytest`, then run
the tests with `python -m pytest -q`.

## Configuration
- `config/feeds.yaml` — sources, time windows, exclusion rules, job searches
- `config/interests.yaml` — sections, priorities, keyword weights

## Privacy
Secrets live only in `.env` (git-ignored). Calendar data and chats stay on the laptop;
only the finished briefing is sent to Telegram. Errors sent to Telegram are always
generic (e.g. "details are in the laptop log") - full details, redacted of any
secrets by `assistant.notify.redact()`, go to the local log only.

## Known limitations
- The laptop must be on or asleep (the timer uses `WakeSystem=true` to wake it) -
  if it's fully off or disconnected, nothing runs until it's back.
- The Google OAuth app is in Testing mode, so the Calendar login expires every 7
  days; the bot and briefing header both tell you to run
  `python -m assistant.gcal auth` again when that happens.
- Telegram chats are not end-to-end encrypted: briefing content, including
  calendar event titles, passes through Telegram's servers.

## Roadmap
- Reminders via chat with confirmation buttons
- Sports (F1, cricket, football) with local start times
- Grouping the same story from different outlets
