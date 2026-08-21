import logging
import time

from config import (
    DATABASE_URL,
    DB_PATH,
    DEAL_RETENTION_DAYS,
    DEDUP_HOURS,
    MIRROR_DEDUP_HOURS,
)
from storage import build_backend

logger = logging.getLogger(__name__)

# Columns added to tables that already exist in deployed databases. CREATE TABLE IF
# NOT EXISTS silently does nothing for a table that is already there, so a new column
# needs its own additive step — see backend.add_column_if_missing().
_COLUMN_MIGRATIONS = [
    ('watchlist', 'exclusions', "TEXT DEFAULT ''"),
]


class Database:
    """
    NDT's persistence layer.

    Storage is delegated to a backend (see storage.py) chosen by whether DATABASE_URL
    is set: Postgres when it is, SQLite otherwise. This class does not care which — the
    SQL below is written once in a dialect both accept, and the backend translates
    placeholders. Every method's signature and return type is identical either way, so
    nothing else in the codebase knows or needs to know where the data lives.

    Why Postgres exists as an option: on a host without a mounted disk (Render's free
    tier, for one) the container filesystem is wiped on every restart, so a SQLite file
    loses the watchlist, tracked channels and deal history each time the service
    redeploys or wakes. An external database survives that.

    Connection model — this used to call ``aiosqlite.connect()`` inside every single
    method. Each of those spawns a dedicated OS thread plus a fresh page cache, and
    ``get_all_keywords()`` runs on every incoming channel message, so a busy set of
    deal channels churned through thousands of short-lived connections and threads.
    The backend now holds one connection (SQLite) or a small pool (Postgres), and this
    class is a singleton so bot.py and ChannelMonitor share it.
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._ready = False
        return cls._instance

    def __init__(self):
        if self._ready:
            return
        self.db_path = DB_PATH
        self.backend = build_backend(DATABASE_URL, DB_PATH)
        # Cleared if the exclusions migration did not take, so the matcher falls back
        # to plain matching instead of erroring on every message.
        self._has_exclusions = True
        self._ready = True

    @classmethod
    def reset_for_tests(cls):
        """Drop the singleton so a test can build a differently-configured instance."""
        cls._instance = None

    @property
    def backend_name(self):
        return self.backend.name

    async def close(self):
        """Release the connection or pool (called on shutdown)."""
        await self.backend.close()

    async def init(self):
        """Create tables and indexes if they don't exist, then apply additive migrations."""
        await self.backend.execute_many_ddl(self.backend.schema())
        for table, column, definition in _COLUMN_MIGRATIONS:
            try:
                if await self.backend.add_column_if_missing(table, column, definition):
                    logger.info(f"Migration: added {table}.{column}")
            except Exception as e:
                # A failed migration must not stop the bot from booting — the feature
                # that needs the column degrades, everything else keeps working.
                logger.error(f"Migration for {table}.{column} failed: {e}")
        logger.info(f"Database ready (backend={self.backend.name})")

    # ── Watchlist Operations ──

    async def add_keyword(self, keyword, custom_synonyms='', exclusions=''):
        """Add a keyword to the watchlist. False if it was already there."""
        affected = await self.backend.execute(
            '''INSERT INTO watchlist (keyword, custom_synonyms, exclusions, added_date)
               VALUES (?, ?, ?, ?)
               ON CONFLICT (keyword) DO NOTHING''',
            (keyword.lower().strip(), custom_synonyms.lower().strip(),
             (exclusions or '').lower().strip(), time.time())
        )
        return affected > 0

    async def update_synonyms(self, keyword, custom_synonyms):
        """Update the custom synonyms for an existing keyword."""
        affected = await self.backend.execute(
            'UPDATE watchlist SET custom_synonyms = ? WHERE keyword = ?',
            (custom_synonyms.lower().strip(), keyword.lower().strip())
        )
        return affected > 0

    async def update_exclusions(self, keyword, exclusions):
        """Set the negative keywords for an existing keyword. '' clears them."""
        affected = await self.backend.execute(
            'UPDATE watchlist SET exclusions = ? WHERE keyword = ?',
            ((exclusions or '').lower().strip(), keyword.lower().strip())
        )
        return affected > 0

    async def remove_keyword(self, keyword):
        """Remove a keyword from the watchlist."""
        affected = await self.backend.execute(
            'DELETE FROM watchlist WHERE keyword = ?',
            (keyword.lower().strip(),)
        )
        return affected > 0

    async def get_watchlist(self):
        """Get all watchlist keywords."""
        return await self.backend.fetch_all(
            'SELECT * FROM watchlist ORDER BY added_date DESC'
        )

    async def get_all_keywords(self):
        """
        The watchlist as (keyword, custom_synonyms, exclusions) tuples.

        This is what the matcher consumes on every incoming message. It grew a third
        element for negative keywords; KeywordMatcher.match() unpacks defensively so
        a plain 2-tuple still works.
        """
        if self._has_exclusions:
            try:
                rows = await self.backend.fetch_all(
                    'SELECT keyword, custom_synonyms, exclusions FROM watchlist')
                return [(row['keyword'], row['custom_synonyms'] or '',
                         row['exclusions'] or '') for row in rows]
            except Exception as e:
                # The migration is non-fatal by design, so the column can genuinely be
                # absent. Degrade to matching without exclusions rather than throwing
                # on every single incoming message.
                logger.error(f"exclusions column unavailable, disabling it: {e}")
                self._has_exclusions = False

        rows = await self.backend.fetch_all(
            'SELECT keyword, custom_synonyms FROM watchlist')
        return [(row['keyword'], row['custom_synonyms'] or '', '') for row in rows]

    # ── Channel Operations ──

    async def add_channel(self, channel_id, channel_name, channel_username=''):
        """Add a channel (inactive by default). False if already known."""
        affected = await self.backend.execute(
            '''INSERT INTO channels (channel_id, channel_name, channel_username, active, added_date)
               VALUES (?, ?, ?, 0, ?)
               ON CONFLICT (channel_id) DO NOTHING''',
            (channel_id, channel_name, channel_username, time.time())
        )
        return affected > 0

    async def remove_channel(self, channel_id):
        """Remove a channel from monitoring, and its counters with it."""
        affected = await self.backend.execute(
            'DELETE FROM channels WHERE channel_id = ?',
            (channel_id,)
        )
        await self.backend.execute(
            'DELETE FROM channel_stats WHERE channel_id = ?', (channel_id,))
        return affected > 0

    async def toggle_channel(self, channel_id):
        """Toggle channel active/inactive. Returns the new state, or None if unknown."""
        row = await self.backend.fetch_one(
            'SELECT active FROM channels WHERE channel_id = ?',
            (channel_id,)
        )
        if row is None:
            return None
        new_state = 0 if row['active'] == 1 else 1
        await self.backend.execute(
            'UPDATE channels SET active = ? WHERE channel_id = ?',
            (new_state, channel_id)
        )
        return new_state

    async def untrack_all_channels(self):
        """Set active = 0 for all channels."""
        return await self.backend.execute('UPDATE channels SET active = 0')

    async def untrack_specific_channel(self, identifier):
        """Set active = 0 for a specific channel by username or name."""
        affected = await self.backend.execute(
            'UPDATE channels SET active = 0 WHERE channel_username LIKE ? OR channel_name LIKE ?',
            (f"%{identifier}%", f"%{identifier}%")
        )
        return affected > 0

    async def get_active_channels(self):
        """Get all active channels."""
        return await self.backend.fetch_all(
            'SELECT channel_id, channel_name, channel_username FROM channels WHERE active = 1'
        )

    async def get_all_channels(self):
        """Get all channels (active and inactive)."""
        return await self.backend.fetch_all(
            'SELECT * FROM channels ORDER BY channel_name'
        )

    # ── Deal Deduplication ──

    async def is_deal_seen(self, deal_hash):
        """Check if a deal has already been notified inside the dedup window."""
        cutoff = time.time() - (DEDUP_HOURS * 3600)
        row = await self.backend.fetch_one(
            'SELECT id FROM matched_deals WHERE deal_hash = ? AND matched_date > ?',
            (deal_hash, cutoff)
        )
        return row is not None

    async def save_deal(self, deal_hash, keyword_matched, product_name='',
                        price='', channel_name='', message_link=''):
        """
        Record a matched deal.

        On a repeat hash the timestamp is refreshed so the dedup window slides forward
        rather than letting a stale row age out and re-alert. `excluded` is the
        conflicting row in both SQLite and Postgres, so one statement covers both.
        """
        await self.backend.execute(
            '''INSERT INTO matched_deals
                   (deal_hash, keyword_matched, product_name, price, channel_name,
                    message_link, matched_date)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (deal_hash) DO UPDATE SET matched_date = excluded.matched_date''',
            (deal_hash, keyword_matched, product_name, price, channel_name,
             message_link, time.time())
        )
        return True

    async def get_recent_deals(self, hours=24):
        """Get deals matched in the last N hours."""
        cutoff = time.time() - (hours * 3600)
        return await self.backend.fetch_all(
            'SELECT * FROM matched_deals WHERE matched_date > ? ORDER BY matched_date DESC',
            (cutoff,)
        )

    async def cleanup_old_deals(self, days=None):
        """
        Remove deals older than N days and reclaim the freed space.

        This existed before but was never wired to anything, so matched_deals grew
        without bound for the lifetime of the deployment. bot.py now runs it on a
        timer (see MAINTENANCE_INTERVAL_HOURS) and once at boot.
        """
        days = DEAL_RETENTION_DAYS if days is None else days
        cutoff = time.time() - (days * 86400)
        deleted = await self.backend.execute(
            'DELETE FROM matched_deals WHERE matched_date < ?',
            (cutoff,)
        )
        if deleted > 0:
            await self.backend.maintenance()
        return deleted

    # ── Channel counters ──
    #
    # NDT keeps no history of alerts: matched_deals is a dedup ledger pruned after
    # DEAL_RETENTION_DAYS, so it cannot answer "how has this channel done for me".
    # These are running totals instead — one row per channel, forever, regardless of
    # how many alerts pass through. Nothing here grows with alert volume.

    async def record_alert(self, channel_id, when=None):
        """Count one alert actually delivered from this channel."""
        if channel_id is None:
            return False
        now = time.time() if when is None else when
        await self.backend.execute(
            '''INSERT INTO channel_stats (channel_id, alerts, last_alert_at)
               VALUES (?, 1, ?)
               ON CONFLICT (channel_id) DO UPDATE
                   SET alerts = alerts + 1, last_alert_at = excluded.last_alert_at''',
            (channel_id, now)
        )
        return True

    async def record_feedback(self, channel_id, is_good):
        """
        Count one 👍 or 👎 against the channel the alert came from.

        Deliberately attributed to the CHANNEL, not the alert. The vote arrives with
        the channel id carried in the button's callback data, so nothing about the
        individual alert needs to have been stored for this to work.
        """
        if channel_id is None:
            return False
        column = 'up' if is_good else 'down'
        await self.backend.execute(
            f'''INSERT INTO channel_stats (channel_id, {column})
                VALUES (?, 1)
                ON CONFLICT (channel_id) DO UPDATE SET {column} = {column} + 1''',
            (channel_id,)
        )
        return True

    async def get_channel_report(self):
        """
        Per-channel totals for active channels, plus the delta since the last report.

        Deltas come from the *_at_report snapshot columns, which is what lets the
        8-hourly report say "since last time" without keeping any per-alert rows.
        """
        rows = await self.backend.fetch_all(
            '''SELECT c.channel_id       AS channel_id,
                      c.channel_name     AS channel_name,
                      c.channel_username AS channel_username,
                      COALESCE(s.alerts, 0)           AS alerts,
                      COALESCE(s.up, 0)               AS up,
                      COALESCE(s.down, 0)             AS down,
                      COALESCE(s.alerts_at_report, 0) AS alerts_at_report,
                      COALESCE(s.up_at_report, 0)     AS up_at_report,
                      COALESCE(s.down_at_report, 0)   AS down_at_report,
                      COALESCE(s.last_alert_at, 0)    AS last_alert_at,
                      COALESCE(s.last_report_at, 0)   AS last_report_at
               FROM channels c
               LEFT JOIN channel_stats s ON s.channel_id = c.channel_id
               WHERE c.active = 1'''
        )
        for row in rows:
            row['new_alerts'] = row['alerts'] - row['alerts_at_report']
            row['new_up'] = row['up'] - row['up_at_report']
            row['new_down'] = row['down'] - row['down_at_report']
        rows.sort(key=lambda r: (-r['new_alerts'], -r['alerts']))
        return rows

    async def snapshot_channel_report(self, when=None):
        """Mark the current totals as reported, so the next report shows a fresh delta."""
        now = time.time() if when is None else when
        return await self.backend.execute(
            '''UPDATE channel_stats
                  SET alerts_at_report = alerts,
                      up_at_report = up,
                      down_at_report = down,
                      last_report_at = ?''',
            (now,)
        )

    # ── Bot 2: muted products and terms ──
    #
    # Bot 1 filters IN (only watchlist matches are sent). Bot 2 filters OUT
    # (everything is sent except what you muted), so this is its entire config.
    # Both are small, permanent lists — a mute is a decision, not history, and it
    # is never pruned on a timer.

    async def mute_product(self, product_key, label=''):
        """Mute one product by canonical key. False if it was already muted."""
        if not product_key:
            return False
        affected = await self.backend.execute(
            '''INSERT INTO muted_products (product_key, label, muted_at)
               VALUES (?, ?, ?)
               ON CONFLICT (product_key) DO NOTHING''',
            (product_key, (label or '')[:120], time.time())
        )
        return affected > 0

    async def unmute_product(self, product_key):
        affected = await self.backend.execute(
            'DELETE FROM muted_products WHERE product_key = ?', (product_key,))
        return affected > 0

    async def muted_product_keys(self, keys):
        """Which of `keys` are muted. Empty list in, empty set out — no query run."""
        keys = [k for k in (keys or []) if k]
        if not keys:
            return set()
        placeholders = ','.join('?' for _ in keys)
        rows = await self.backend.fetch_all(
            f'SELECT product_key FROM muted_products WHERE product_key IN ({placeholders})',
            tuple(keys)
        )
        return {row['product_key'] for row in rows}

    async def get_muted_products(self, limit=50):
        return await self.backend.fetch_all(
            'SELECT product_key, label, muted_at FROM muted_products '
            'ORDER BY muted_at DESC LIMIT ?',
            (limit,)
        )

    async def mute_term(self, term):
        """Mute a word or phrase from the mirror feed. False if already muted."""
        term = (term or '').strip().lower()
        if not term:
            return False
        affected = await self.backend.execute(
            '''INSERT INTO muted_terms (term, muted_at) VALUES (?, ?)
               ON CONFLICT (term) DO NOTHING''',
            (term, time.time())
        )
        return affected > 0

    async def unmute_term(self, term):
        affected = await self.backend.execute(
            'DELETE FROM muted_terms WHERE term = ?', ((term or '').strip().lower(),))
        return affected > 0

    async def get_muted_terms(self):
        """Every muted word, as a plain list of strings."""
        rows = await self.backend.fetch_all(
            'SELECT term FROM muted_terms ORDER BY term')
        return [row['term'] for row in rows]

    async def clear_mutes(self):
        """Drop every mute — both products and terms. Returns the counts removed."""
        products = await self.backend.execute('DELETE FROM muted_products')
        terms = await self.backend.execute('DELETE FROM muted_terms')
        return {'products': products, 'terms': terms}

    # ── Bot 2: mirror deduplication ──
    #
    # Same shape and lifetime as matched_deals: a key, a timestamp, pruned after
    # DEAL_RETENTION_DAYS. It exists so the same product posted by both tracked
    # channels within minutes reaches you once, and it survives a restart, which an
    # in-memory cache cannot.

    async def is_mirror_seen(self, dedup_key, hours=None):
        if not dedup_key:
            return False
        window = MIRROR_DEDUP_HOURS if hours is None else hours
        cutoff = time.time() - (window * 3600)
        row = await self.backend.fetch_one(
            'SELECT dedup_key FROM mirror_seen WHERE dedup_key = ? AND seen_at > ?',
            (dedup_key, cutoff)
        )
        return row is not None

    async def mark_mirror_seen(self, dedup_key):
        """Record that the mirror sent this. Refreshes the timestamp on a repeat."""
        if not dedup_key:
            return False
        await self.backend.execute(
            '''INSERT INTO mirror_seen (dedup_key, seen_at) VALUES (?, ?)
               ON CONFLICT (dedup_key) DO UPDATE SET seen_at = excluded.seen_at''',
            (dedup_key, time.time())
        )
        return True

    async def cleanup_mirror_seen(self, days=None):
        days = DEAL_RETENTION_DAYS if days is None else days
        cutoff = time.time() - (days * 86400)
        return await self.backend.execute(
            'DELETE FROM mirror_seen WHERE seen_at < ?', (cutoff,))

    # ── Stats ──

    async def get_stats(self):
        """
        Numbers for the status screen, each with its own error boundary.

        Every count is fetched independently and a failure is returned as None rather
        than raised. The reason is a real debugging dead end this hit: one broken
        query used to abort the whole screen, and because the send never happened the
        symptom was a status command that did nothing at all. A count that reads None
        renders as an explicit error next to the metric, so a missing table or a
        sleeping database names itself instead of hiding behind a zero.

        `backend` and `location` ride along for the same reason — a zero means
        something very different depending on whether it came from Postgres or from a
        SQLite file on a disk the host wipes on restart.
        """
        async def count(sql, params=()):
            try:
                # COUNT(*) is aliased because the implicit column name differs
                # between engines ('COUNT(*)' in SQLite, 'count' in Postgres).
                row = await self.backend.fetch_one(sql, params)
                return row['cnt'] if row else 0
            except Exception as e:
                logger.error(f"Stats query failed [{sql.split('FROM')[-1].strip()}]: {e}")
                return None

        day_ago = time.time() - 86400
        stats = {
            'backend': self.backend.name,
            'location': getattr(self.backend, 'location', '?'),
            'watchlist_count': await count('SELECT COUNT(*) AS cnt FROM watchlist'),
            'active_channels': await count(
                'SELECT COUNT(*) AS cnt FROM channels WHERE active = 1'),
            'total_channels': await count('SELECT COUNT(*) AS cnt FROM channels'),
            'deals_24h': await count(
                'SELECT COUNT(*) AS cnt FROM matched_deals WHERE matched_date > ?',
                (day_ago,)),
            'total_deals': await count('SELECT COUNT(*) AS cnt FROM matched_deals'),
            'muted_products': await count('SELECT COUNT(*) AS cnt FROM muted_products'),
            'muted_terms': await count('SELECT COUNT(*) AS cnt FROM muted_terms'),
            'mirrored_24h': await count(
                'SELECT COUNT(*) AS cnt FROM mirror_seen WHERE seen_at > ?', (day_ago,)),
        }

        # Lifetime alert totals come from the counters, not from matched_deals — that
        # table is a 7-day dedup ledger, so counting it understates a channel's real
        # history the moment the first prune runs.
        try:
            row = await self.backend.fetch_one(
                'SELECT COALESCE(SUM(alerts), 0) AS cnt FROM channel_stats')
            stats['total_alerts'] = row['cnt'] if row else 0
        except Exception as e:
            logger.error(f"Stats query failed [channel_stats]: {e}")
            stats['total_alerts'] = None

        return stats
