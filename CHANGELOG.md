# Changelog

Why each change exists is in `claude.md`; how to deploy is in `DEPLOYMENT.md`.

## v1.2.0

**Keyword matching — false positives.** A watchlist of `shoes` alerted on home
products. `difflib.SequenceMatcher` scores two 5-letter words sharing 4 characters
at exactly 0.800 and the threshold was 0.75, so `homes`, `hoses` and `shows` all
matched `shoes`. Fuzzy matching is now off by default; plurals are handled exactly
by `singularise()` instead of approximately by similarity.

**Synonym expansion leaked across categories.** `/watch "air cooler"` silently
expanded to 11 terms including `split ac` — the term is listed under both `ac` and
`cooler`, and the reverse lookup pulled in every owning category. Ambiguous terms
now match only themselves.

**Non-deterministic match reporting.** `get_synonyms` returned `list(set(...))`;
Python randomises string hashing per process, so `matched_term` changed between
restarts. Now sorted longest-first, which also makes the most specific term win.

**Added** `/testmatch <text>` and `/synonyms <keyword>` — a wrong match is
indistinguishable from a right one from the outside, which is why this went
unnoticed.

**Fixed** a stale `DATA_PATH` / `SESSION_PATH` crashing the app at import.

## v1.1.0

**Postgres storage.** State lived in a SQLite file on the container, which a free
Render instance wipes on every restart — the bot came back with an empty
watchlist. Storage now sits behind a two-backend abstraction selected by
`DATABASE_URL`: Postgres when set, SQLite otherwise. The public API is identical
either way.

The pool is tuned for serverless providers that suspend an idle database: holds
nothing open, recycles idle connections, and retries connection-level failures
while the database wakes.

**Added** `check_db.py` (verify a database before deploying) and
`migrate_to_postgres.py` (carry an existing SQLite file across).

## v1.0.0

**Link previews never ran.** `channel_monitor.py` called `asyncio.sleep()` without
importing `asyncio`. Telegram answers `WebPagePending` on first sight of any URL,
so that branch was hit constantly; it raised `NameError`, a bare `except`
swallowed it into an invisible debug log, and every message fell through to the
HTML scraper — which Amazon and Flipkart answer with a CAPTCHA.

**Deploys were reaped.** The web server bound its port *after* Telethon login, so
Render's port scan timed out. It now binds first, in ~30ms.

**Memory grew without bound.** A new SQLite connection (and thread) per call with
the watchlist re-read on every message; `cleanup_old_deals()` was never called by
anything; the scrape cache only evicted expired entries; a new
`aiohttp.ClientSession` per URL.

**Python 3.14 crash.** Render ignores `runtime.txt`, so it ran 3.14 where
python-telegram-bot 21.x fails on `asyncio.get_event_loop()`. Pinned via
`.python-version`.

**Added** `test_pipeline.py` — offline, no credentials or network.
