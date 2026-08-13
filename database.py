import asyncio
import logging
import time
from contextlib import asynccontextmanager

import aiosqlite

from config import DB_PATH, DEDUP_HOURS, DEAL_RETENTION_DAYS

logger = logging.getLogger(__name__)


class Database:
    """
    Async SQLite database for NDT.

    Connection model — this used to call ``aiosqlite.connect()`` inside every single
    method. Each of those spawns a dedicated OS thread plus a fresh page cache, and
    ``get_all_keywords()`` runs on *every* incoming channel message, so a busy set of
    deal channels churned through thousands of short-lived connections and threads.
    That was the main source of the runaway memory growth in the deployed service.

    Now: one process-wide connection (the class is a singleton, so ``bot.py`` and
    ``ChannelMonitor`` share it), opened lazily and guarded by an ``asyncio.Lock``
    because a single aiosqlite connection must not be driven by two coroutines at once.
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
        self._conn = None
        self._lock = asyncio.Lock()
        self._ready = True

    async def _ensure_conn(self):
        if self._conn is None:
            self._conn = await aiosqlite.connect(self.db_path)
            self._conn.row_factory = aiosqlite.Row
            # WAL keeps readers from blocking the writer; NORMAL sync is durable
            # enough for a deal cache and avoids an fsync on every insert.
            await self._conn.execute('PRAGMA journal_mode=WAL')
            await self._conn.execute('PRAGMA synchronous=NORMAL')
            # Cap SQLite's page cache (negative value = KiB, so this is 4 MB).
            await self._conn.execute('PRAGMA cache_size=-4000')
            await self._conn.commit()
        return self._conn

    @asynccontextmanager
    async def _session(self):
        async with self._lock:
            yield await self._ensure_conn()

    async def close(self):
        """Close the shared connection (called on shutdown)."""
        async with self._lock:
            if self._conn is not None:
                await self._conn.close()
                self._conn = None

    async def init(self):
        """Create tables if they don't exist."""
        async with self._session() as db:
            # Watchlist — keywords user is tracking
            await db.execute('''
                CREATE TABLE IF NOT EXISTS watchlist (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    keyword TEXT NOT NULL UNIQUE,
                    custom_synonyms TEXT DEFAULT '',
                    added_date REAL NOT NULL
                )
            ''')

            # Channels — channels selected for monitoring
            await db.execute('''
                CREATE TABLE IF NOT EXISTS channels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id INTEGER NOT NULL UNIQUE,
                    channel_name TEXT NOT NULL,
                    channel_username TEXT DEFAULT '',
                    active INTEGER DEFAULT 1,
                    added_date REAL NOT NULL
                )
            ''')

            # Matched deals — for deduplication
            await db.execute('''
                CREATE TABLE IF NOT EXISTS matched_deals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    deal_hash TEXT NOT NULL UNIQUE,
                    keyword_matched TEXT NOT NULL,
                    product_name TEXT DEFAULT '',
                    price TEXT DEFAULT '',
                    channel_name TEXT DEFAULT '',
                    message_link TEXT DEFAULT '',
                    matched_date REAL NOT NULL
                )
            ''')

            # is_deal_seen() and the retention sweep both filter on matched_date.
            await db.execute(
                'CREATE INDEX IF NOT EXISTS idx_deals_matched_date ON matched_deals(matched_date)'
            )
            await db.execute(
                'CREATE INDEX IF NOT EXISTS idx_channels_active ON channels(active)'
            )

            await db.commit()

    # ── Watchlist Operations ──

    async def add_keyword(self, keyword, custom_synonyms=''):
        """Add a keyword to the watchlist."""
        async with self._session() as db:
            try:
                await db.execute(
                    'INSERT INTO watchlist (keyword, custom_synonyms, added_date) VALUES (?, ?, ?)',
                    (keyword.lower().strip(), custom_synonyms.lower().strip(), time.time())
                )
                await db.commit()
                return True
            except aiosqlite.IntegrityError:
                return False  # Already exists

    async def update_synonyms(self, keyword, custom_synonyms):
        """Update the custom synonyms for an existing keyword."""
        async with self._session() as db:
            cursor = await db.execute(
                'UPDATE watchlist SET custom_synonyms = ? WHERE keyword = ?',
                (custom_synonyms.lower().strip(), keyword.lower().strip())
            )
            await db.commit()
            return cursor.rowcount > 0

    async def remove_keyword(self, keyword):
        """Remove a keyword from the watchlist."""
        async with self._session() as db:
            cursor = await db.execute(
                'DELETE FROM watchlist WHERE keyword = ?',
                (keyword.lower().strip(),)
            )
            await db.commit()
            return cursor.rowcount > 0

    async def get_watchlist(self):
        """Get all watchlist keywords."""
        async with self._session() as db:
            cursor = await db.execute('SELECT * FROM watchlist ORDER BY added_date DESC')
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_all_keywords(self):
        """Get just the keyword strings."""
        async with self._session() as db:
            cursor = await db.execute('SELECT keyword, custom_synonyms FROM watchlist')
            rows = await cursor.fetchall()
            return [(row[0], row[1]) for row in rows]

    # ── Channel Operations ──

    async def add_channel(self, channel_id, channel_name, channel_username=''):
        """Add a channel to monitor."""
        async with self._session() as db:
            try:
                await db.execute(
                    'INSERT INTO channels (channel_id, channel_name, channel_username, active, added_date) VALUES (?, ?, ?, 0, ?)',
                    (channel_id, channel_name, channel_username, time.time())
                )
                await db.commit()
                return True
            except aiosqlite.IntegrityError:
                return False

    async def remove_channel(self, channel_id):
        """Remove a channel from monitoring."""
        async with self._session() as db:
            cursor = await db.execute(
                'DELETE FROM channels WHERE channel_id = ?',
                (channel_id,)
            )
            await db.commit()
            return cursor.rowcount > 0

    async def toggle_channel(self, channel_id):
        """Toggle channel active/inactive."""
        async with self._session() as db:
            cursor = await db.execute(
                'SELECT active FROM channels WHERE channel_id = ?',
                (channel_id,)
            )
            row = await cursor.fetchone()
            if row:
                new_state = 0 if row[0] == 1 else 1
                await db.execute(
                    'UPDATE channels SET active = ? WHERE channel_id = ?',
                    (new_state, channel_id)
                )
                await db.commit()
                return new_state
            return None

    async def untrack_all_channels(self):
        """Set active = 0 for all channels."""
        async with self._session() as db:
            cursor = await db.execute('UPDATE channels SET active = 0')
            await db.commit()
            return cursor.rowcount

    async def untrack_specific_channel(self, identifier):
        """Set active = 0 for a specific channel by username or exact name."""
        async with self._session() as db:
            # Try matching username or channel_name
            cursor = await db.execute(
                'UPDATE channels SET active = 0 WHERE channel_username LIKE ? OR channel_name LIKE ?',
                (f"%{identifier}%", f"%{identifier}%")
            )
            await db.commit()
            return cursor.rowcount > 0

    async def get_active_channels(self):
        """Get all active channel IDs."""
        async with self._session() as db:
            cursor = await db.execute(
                'SELECT channel_id, channel_name, channel_username FROM channels WHERE active = 1'
            )
            rows = await cursor.fetchall()
            return [{'channel_id': r[0], 'channel_name': r[1], 'channel_username': r[2]} for r in rows]

    async def get_all_channels(self):
        """Get all channels (active and inactive)."""
        async with self._session() as db:
            cursor = await db.execute('SELECT * FROM channels ORDER BY channel_name')
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    # ── Deal Deduplication ──

    async def is_deal_seen(self, deal_hash):
        """Check if a deal has already been notified."""
        async with self._session() as db:
            cutoff = time.time() - (DEDUP_HOURS * 3600)
            cursor = await db.execute(
                'SELECT id FROM matched_deals WHERE deal_hash = ? AND matched_date > ?',
                (deal_hash, cutoff)
            )
            row = await cursor.fetchone()
            return row is not None

    async def save_deal(self, deal_hash, keyword_matched, product_name='',
                        price='', channel_name='', message_link=''):
        """Save a matched deal for deduplication."""
        async with self._session() as db:
            try:
                await db.execute(
                    '''INSERT INTO matched_deals
                       (deal_hash, keyword_matched, product_name, price, channel_name, message_link, matched_date)
                       VALUES (?, ?, ?, ?, ?, ?, ?)''',
                    (deal_hash, keyword_matched, product_name, price, channel_name, message_link, time.time())
                )
                await db.commit()
                return True
            except aiosqlite.IntegrityError:
                # Same hash already stored — refresh its timestamp so the dedup
                # window slides forward instead of letting a stale row expire.
                await db.execute(
                    'UPDATE matched_deals SET matched_date = ? WHERE deal_hash = ?',
                    (time.time(), deal_hash)
                )
                await db.commit()
                return False

    async def get_recent_deals(self, hours=24):
        """Get deals matched in the last N hours."""
        async with self._session() as db:
            cutoff = time.time() - (hours * 3600)
            cursor = await db.execute(
                'SELECT * FROM matched_deals WHERE matched_date > ? ORDER BY matched_date DESC',
                (cutoff,)
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def cleanup_old_deals(self, days=None):
        """
        Remove deals older than N days and reclaim the freed pages.

        This existed before but was never wired to anything, so matched_deals grew
        without bound for the lifetime of the deployment. bot.py now runs it on a
        timer (see MAINTENANCE_INTERVAL_HOURS).
        """
        days = DEAL_RETENTION_DAYS if days is None else days
        async with self._session() as db:
            cutoff = time.time() - (days * 86400)
            cursor = await db.execute(
                'DELETE FROM matched_deals WHERE matched_date < ?',
                (cutoff,)
            )
            deleted = cursor.rowcount
            await db.commit()
            if deleted > 0:
                # Return the emptied pages to the OS rather than holding the
                # high-water mark for the life of the process.
                await db.execute('PRAGMA incremental_vacuum')
                await db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                await db.commit()
            return deleted

    # ── Stats ──

    async def get_stats(self):
        """Get bot statistics."""
        async with self._session() as db:
            # Watchlist count
            cursor = await db.execute('SELECT COUNT(*) FROM watchlist')
            watchlist_count = (await cursor.fetchone())[0]

            # Active channels count
            cursor = await db.execute('SELECT COUNT(*) FROM channels WHERE active = 1')
            active_channels = (await cursor.fetchone())[0]

            # Total channels count
            cursor = await db.execute('SELECT COUNT(*) FROM channels')
            total_channels = (await cursor.fetchone())[0]

            # Deals in last 24h
            cutoff = time.time() - 86400
            cursor = await db.execute(
                'SELECT COUNT(*) FROM matched_deals WHERE matched_date > ?',
                (cutoff,)
            )
            deals_24h = (await cursor.fetchone())[0]

            # Total deals ever
            cursor = await db.execute('SELECT COUNT(*) FROM matched_deals')
            total_deals = (await cursor.fetchone())[0]

            return {
                'watchlist_count': watchlist_count,
                'active_channels': active_channels,
                'total_channels': total_channels,
                'deals_24h': deals_24h,
                'total_deals': total_deals,
            }
