import aiosqlite
import os
import time
from config import DB_PATH, DEDUP_HOURS


class Database:
    """Async SQLite database for NDT."""

    def __init__(self):
        self.db_path = DB_PATH

    async def init(self):
        """Create tables if they don't exist."""
        async with aiosqlite.connect(self.db_path) as db:
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

            await db.commit()

    # ── Watchlist Operations ──

    async def add_keyword(self, keyword, custom_synonyms=''):
        """Add a keyword to the watchlist."""
        async with aiosqlite.connect(self.db_path) as db:
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
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                'UPDATE watchlist SET custom_synonyms = ? WHERE keyword = ?',
                (custom_synonyms.lower().strip(), keyword.lower().strip())
            )
            await db.commit()
            return cursor.rowcount > 0

    async def remove_keyword(self, keyword):
        """Remove a keyword from the watchlist."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                'DELETE FROM watchlist WHERE keyword = ?',
                (keyword.lower().strip(),)
            )
            await db.commit()
            return cursor.rowcount > 0

    async def get_watchlist(self):
        """Get all watchlist keywords."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute('SELECT * FROM watchlist ORDER BY added_date DESC')
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_all_keywords(self):
        """Get just the keyword strings."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('SELECT keyword, custom_synonyms FROM watchlist')
            rows = await cursor.fetchall()
            return [(row[0], row[1]) for row in rows]

    # ── Channel Operations ──

    async def add_channel(self, channel_id, channel_name, channel_username=''):
        """Add a channel to monitor."""
        async with aiosqlite.connect(self.db_path) as db:
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
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                'DELETE FROM channels WHERE channel_id = ?',
                (channel_id,)
            )
            await db.commit()
            return cursor.rowcount > 0

    async def toggle_channel(self, channel_id):
        """Toggle channel active/inactive."""
        async with aiosqlite.connect(self.db_path) as db:
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
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('UPDATE channels SET active = 0')
            await db.commit()
            return cursor.rowcount

    async def untrack_specific_channel(self, identifier):
        """Set active = 0 for a specific channel by username or exact name."""
        async with aiosqlite.connect(self.db_path) as db:
            # Try matching username or channel_name
            cursor = await db.execute(
                'UPDATE channels SET active = 0 WHERE channel_username LIKE ? OR channel_name LIKE ?',
                (f"%{identifier}%", f"%{identifier}%")
            )
            await db.commit()
            return cursor.rowcount > 0

    async def get_active_channels(self):
        """Get all active channel IDs."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                'SELECT channel_id, channel_name, channel_username FROM channels WHERE active = 1'
            )
            rows = await cursor.fetchall()
            return [{'channel_id': r[0], 'channel_name': r[1], 'channel_username': r[2]} for r in rows]

    async def get_all_channels(self):
        """Get all channels (active and inactive)."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute('SELECT * FROM channels ORDER BY channel_name')
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    # ── Deal Deduplication ──

    async def is_deal_seen(self, deal_hash):
        """Check if a deal has already been notified."""
        async with aiosqlite.connect(self.db_path) as db:
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
        async with aiosqlite.connect(self.db_path) as db:
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
                return False

    async def get_recent_deals(self, hours=24):
        """Get deals matched in the last N hours."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cutoff = time.time() - (hours * 3600)
            cursor = await db.execute(
                'SELECT * FROM matched_deals WHERE matched_date > ? ORDER BY matched_date DESC',
                (cutoff,)
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def cleanup_old_deals(self, days=7):
        """Remove deals older than N days."""
        async with aiosqlite.connect(self.db_path) as db:
            cutoff = time.time() - (days * 86400)
            await db.execute(
                'DELETE FROM matched_deals WHERE matched_date < ?',
                (cutoff,)
            )
            await db.commit()

    # ── Stats ──

    async def get_stats(self):
        """Get bot statistics."""
        async with aiosqlite.connect(self.db_path) as db:
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
