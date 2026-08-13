import logging
import time

from config import DATABASE_URL, DB_PATH, DEAL_RETENTION_DAYS, DEDUP_HOURS
from storage import build_backend

logger = logging.getLogger(__name__)


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
        """Create tables and indexes if they don't exist."""
        await self.backend.execute_many_ddl(self.backend.schema())
        logger.info(f"Database ready (backend={self.backend.name})")

    # ── Watchlist Operations ──

    async def add_keyword(self, keyword, custom_synonyms=''):
        """Add a keyword to the watchlist. False if it was already there."""
        affected = await self.backend.execute(
            '''INSERT INTO watchlist (keyword, custom_synonyms, added_date)
               VALUES (?, ?, ?)
               ON CONFLICT (keyword) DO NOTHING''',
            (keyword.lower().strip(), custom_synonyms.lower().strip(), time.time())
        )
        return affected > 0

    async def update_synonyms(self, keyword, custom_synonyms):
        """Update the custom synonyms for an existing keyword."""
        affected = await self.backend.execute(
            'UPDATE watchlist SET custom_synonyms = ? WHERE keyword = ?',
            (custom_synonyms.lower().strip(), keyword.lower().strip())
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
        """Get just the keyword strings, as (keyword, custom_synonyms) tuples."""
        rows = await self.backend.fetch_all(
            'SELECT keyword, custom_synonyms FROM watchlist'
        )
        return [(row['keyword'], row['custom_synonyms']) for row in rows]

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
        """Remove a channel from monitoring."""
        affected = await self.backend.execute(
            'DELETE FROM channels WHERE channel_id = ?',
            (channel_id,)
        )
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

    # ── Stats ──

    async def get_stats(self):
        """Get bot statistics."""
        # COUNT(*) is aliased because the implicit column name differs between
        # engines ('COUNT(*)' in SQLite, 'count' in Postgres).
        watchlist_count = (await self.backend.fetch_one(
            'SELECT COUNT(*) AS cnt FROM watchlist'))['cnt']
        active_channels = (await self.backend.fetch_one(
            'SELECT COUNT(*) AS cnt FROM channels WHERE active = 1'))['cnt']
        total_channels = (await self.backend.fetch_one(
            'SELECT COUNT(*) AS cnt FROM channels'))['cnt']
        deals_24h = (await self.backend.fetch_one(
            'SELECT COUNT(*) AS cnt FROM matched_deals WHERE matched_date > ?',
            (time.time() - 86400,)))['cnt']
        total_deals = (await self.backend.fetch_one(
            'SELECT COUNT(*) AS cnt FROM matched_deals'))['cnt']

        return {
            'watchlist_count': watchlist_count,
            'active_channels': active_channels,
            'total_channels': total_channels,
            'deals_24h': deals_24h,
            'total_deals': total_deals,
        }
