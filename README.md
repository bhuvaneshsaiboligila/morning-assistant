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
- **Google Calendar** — today's events and the next 7 days at the top of the briefing
- **Telegram bot** — chat with the local model; `/today`, `/briefing`, `/new`
- **Automation** — systemd timer wakes the laptop from sleep, retries on network
  failure, never sends twice a day, catches up after the laptop was off

## Architecture
RSS feeds ─┐
Job API ───┼─► collect ─► SQLite (dedup) ─► rank ─► local LLM summaries ─┐
Calendar ──┘ ├─► Telegram
Telegram messages ─► bot ─► local LLM ┘


| Module | Role |
|---|---|
| `assistant/collect.py` | Fetch RSS + job API, normalize links, drop stale/undated/excluded items |
| `assistant/store.py` | SQLite store — every item is reported only once |
| `assistant/rank.py` | Keyword + priority + recency scoring per section |
| `assistant/summarize.py` | One-line summaries with the local model (Ollama) |
| `assistant/agenda.py` | Google Calendar events (private iCal link), recurring events expanded |
| `assistant/briefing.py` | Builds the briefing, splits it into Telegram messages, marks items as sent |
| `assistant/bot.py` | Two-way Telegram chat, owner-only |
| `assistant/notify.py` | Telegram API, errors never contain the token |

## Hardware constraints this was built around
- GTX 1650 (4 GB VRAM), 5.7 GB RAM → 4B model, mostly on GPU, ~15 tokens/s
- Model stored on a USB 2.0 stick mounted at boot → ~2.5 min cold load, so the
  briefing job starts at 05:25 for 05:30 delivery
- The model unloads after idle time so the laptop stays usable

## Setup
```bash
git clone <this repo> && cd morning-assistant
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # fill in your own values
ollama pull qwen3.5:4b
python -m assistant.briefing --print   # dry run, sends nothing
```
systemd units for the timer and the bot are in `deploy/`.

## Configuration
- `config/feeds.yaml` — sources, time windows, exclusion rules, job searches
- `config/interests.yaml` — sections, priorities, keyword weights

## Privacy
Secrets live only in `.env` (git-ignored). Calendar data and chats stay on the laptop;
only the finished briefing is sent to Telegram.

## Roadmap
- Reminders via chat with confirmation buttons
- Writing events to Google Calendar
- Sports (F1, cricket, football) with local start times
- Grouping the same story from different outlets
