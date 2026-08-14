# Deployment

Live on a **Render free web service** backed by **Neon Postgres**.

Free instances have no persistent disk, so state cannot live on the container —
it is wiped on every restart. Neon holds the watchlist, channels and deal history
instead. `claude.md` explains the reasoning; this file is the runbook.

## Environment variables

Set in Render → your service → **Environment**.

| Key | Value | Notes |
|---|---|---|
| `BOT_TOKEN` | from @BotFather | |
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

## Failure signatures

| Symptom | Cause |
|---|---|
| Stops a few minutes after deploying | Free-tier idle spin-down. Self-ping needs `KEEPALIVE_INTERVAL`; an external pinger on `/health` is the reliable fix |
| `RuntimeError: There is no current event loop` | Running on Python 3.14. Pin via `.python-version` / `PYTHON_VERSION` |
| `Port scan timeout reached` | Something slow ran before the web server bound. It must bind first |
| Watchlist empty after a restart | On SQLite, not Postgres. Check for `backend=postgres` in the logs |
| Bot answers but never alerts | `telethon_connected: false`, or no channels selected — run `/channels` |
| A channel you joined is missing from `/channels` | Its name has no `deal`/`sale` in it. Find it with `/searchchannel <text>`, or widen `CHANNEL_NAME_FILTERS` |
| `retry 1/2` then success in logs | Normal. Neon suspends when idle and briefly refuses the first connection while waking |
| `👋 NDT shut down cleanly` | Normal. Every deploy and restart logs this. A real failure shows a traceback |

## Free-tier limits

- ~750 instance hours/month **per account**. One always-awake service uses ~730,
  so a second free service will exhaust the pool and stop both.
- No disk. This is why Neon exists in the stack.
- Neon suspends an idle database; the pool is configured for it
  (`DB_POOL_MIN_SIZE=0`), so it sleeps rather than burning compute hours.
