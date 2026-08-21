# Deployment

Live on a **Render free web service** backed by **Neon Postgres**.

Free instances have no persistent disk, so state cannot live on the container —
it is wiped on every restart. Neon holds the watchlist, channels and deal history
instead. `claude.md` explains the reasoning; this file is the runbook.

## Environment variables

Set in Render → your service → **Environment**.

| Key | Value | Notes |
|---|---|---|
| `BOT_TOKEN` | from @BotFather | bot 1 — watchlist alerts |
| `BOT2_TOKEN` | a second @BotFather token | optional. Bot 2, the channel mirror: everything the channels post except what you muted. Leave unset to run bot 1 alone. **Press Start on bot 2 once**, or Telegram blocks it from messaging you |
| `API_ID` / `API_HASH` | from my.telegram.org | |
| `OWNER_ID` | your numeric Telegram ID | only this user can command the bot |
| `SESSION_STRING` | `python generate_session.py` | **required** — a container has no terminal to type a login code into. Without it the bot runs and monitors nothing |
| `DATABASE_URL` | Neon pooled connection string | keep `?sslmode=require`. Without it, storage silently falls back to SQLite and is wiped on restart |
| `KEEPALIVE_INTERVAL` | `300` | free services sleep after ~15 min without inbound traffic |
| `PYTHON_VERSION` | `3.11.9` | Render ignores `runtime.txt`; `.python-version` covers this too |
| `PYTHONUNBUFFERED` | `1` | otherwise logs sit in a buffer |

Everything else has a working default — see `.env.example`. `DATA_PATH` and
`SESSION_PATH` are unused once `DATABASE_URL` and `SESSION_STRING` are set.

Health check path: `/health`.

## Deploy

1. Push to the release branch; Render auto-deploys it.
2. Confirm the branch in **Settings → Build & Deploy → Branch**.
3. Watch **Logs** for `Database ready (backend=postgres)`. If it says
   `backend=sqlite`, `DATABASE_URL` did not take and state will not survive.

Verify a database before pointing Render at it:

```bash
export DATABASE_URL="postgresql://..."
python check_db.py              # connects, creates schema, round-trips a row
python migrate_to_postgres.py   # only to carry an existing SQLite file across
```

## Verify a deploy

In order — this isolates failures instead of guessing:

1. `GET /health` → `telethon_connected: true`. If false, `SESSION_STRING` is wrong.
2. `/stats` in Telegram → the bot answers.
3. `/watch shoes`, then **Manual Deploy → Restart**, then `/watchlist`. If the
   keyword survives, persistence works.
4. `/testmatch Home decor for homes Rs 499` → must say **WOULD NOT ALERT**.
5. `/channelreport` → answers (empty until alerts have been sent).

A schema change ships with this release. Watch the logs for
`Migration: added watchlist.exclusions` on first boot — it is additive and keeps
existing rows. `Migration for … failed` means negative keywords are inert while
everything else keeps working.

## Failure signatures

| Symptom | Cause |
|---|---|
| Stops a few minutes after deploying | Free-tier idle spin-down. Self-ping needs `KEEPALIVE_INTERVAL`; an external pinger on `/health` is the reliable fix |
| `RuntimeError: There is no current event loop` | Running on Python 3.14. Pin via `.python-version` / `PYTHON_VERSION` |
| `Port scan timeout reached` | Something slow ran before the web server bound. It must bind first |
| Watchlist empty after a restart | On SQLite, not Postgres. Check for `backend=postgres` in the logs |
| Bot answers but never alerts | `telethon_connected: false`, or no channels selected — run `/channels` |
| A channel you joined is missing from `/channels` | Its name has no `deal`/`sale` in it. Find it with `/searchchannel <text>`, or widen `CHANNEL_NAME_FILTERS` |
| A keyword stopped alerting | Check `/watchlist` for a 🚫 line, then `/testmatch <message>` — it prints `BLOCKED keyword:` when a negative keyword vetoed the match |
| A command replies "❌ specify a…" | Fixed in v1.5.0 — bare commands ask instead. If you still see it, the deploy is on an older branch |
| Buttons do nothing / a screen never opens | Telegram rejected the message. Check the logs for `Menu render failed` or a BadRequest about parse entities |
| Channel report never arrives | `CHANNEL_REPORT_ENABLED=false`, or no channel is active. The schedule is stored in the database, so restarts do not reset it. `/channelreport` works on demand regardless |
| `retry 1/2` then success in logs | Normal. Neon suspends when idle and briefly refuses the first connection while waking |
| `👋 NDT shut down cleanly` | Normal. Every deploy and restart logs this. A real failure shows a traceback |
| Bot 2 never sends anything | You haven't pressed Start on it, or `BOT2_TOKEN` is unset. `/stats` shows the mirror as `⚪ Off (reason)` or `🔴 Failed to start` |
| Bot 2's 👎 does nothing | Bot 2 isn't polling — a button's callback goes to the bot that sent the message. Look for `🪞 Mirror bot started as @…` in the logs |
| Bot 2 mirrors nothing but bot 1 works | Every post was muted, deduped, or had no link (`MIRROR_REQUIRE_LINK=true`). Check `/muted` |
| `/stats` shows all zeros | It now says why. `sqlite` as the storage backend means the data is wiped on restart — set `DATABASE_URL`. Otherwise nothing is set up yet: `/watch` and `/channels` |
| `/stats` shows `⚠️ error` on a line | That one query failed; the rest are still real. The log line names the table |
| The same deal still arrives twice | Neither post's link could be reduced to a product id, so dedup fell back to the text. Check the link is a real product URL, not a search page, and that `RESOLVE_SHORT_LINKS=true` |

## Free-tier limits

- ~750 instance hours/month **per account**. One always-awake service uses ~730,
  so a second free service will exhaust the pool and stop both.
- No disk. This is why Neon exists in the stack.
- Neon suspends an idle database; the pool is configured for it
  (`DB_POOL_MIN_SIZE=0`), so it sleeps rather than burning compute hours.
