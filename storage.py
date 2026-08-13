"""
Storage backends for NDT.

Two backends behind one tiny interface, chosen at runtime by whether DATABASE_URL
is set:

* SQLite  (default)   — a file on local disk. Fine for development and for hosts with
                        a mounted persistent disk. On an ephemeral container it is
                        wiped on every restart, which is why the second backend exists.
* Postgres (DATABASE_URL) — an external server, so the data outlives the container.

The SQL in database.py is written once, in SQLite's `?` placeholder style, and
translated to Postgres' `$1, $2` form here. Every statement it issues is deliberately
kept to syntax both engines accept — notably `ON CONFLICT ... DO NOTHING/DO UPDATE`,
which SQLite has supported since 3.24 (Python 3.11 bundles far newer). That keeps the
two backends from drifting into subtly different behaviour.
"""

import asyncio
import logging
import re
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)


def to_pg_placeholders(sql):
    """Rewrite `?` placeholders to Postgres' positional `$1, $2, ...` form."""
    out = []
    index = 0
    for char in sql:
        if char == '?':
            index += 1
            out.append(f'${index}')
        else:
            out.append(char)
    return ''.join(out)


def normalise_dsn(url):
    """
    Normalise a Postgres connection URL for asyncpg.

    Heroku-style providers hand out `postgres://`, which asyncpg's URL parser does
    accept, but several tools emit the deprecated form inconsistently — normalise so
    logs and comparisons are predictable. Also strips whitespace, which is the single
    most common thing wrong with a connection string pasted into a dashboard field.
    """
    url = (url or '').strip()
    if url.startswith('postgres://'):
        url = 'postgresql://' + url[len('postgres://'):]
    return url


def redact_dsn(url):
    """Hide the password so a connection string can safely be logged."""
    return re.sub(r'://([^:/@]+):([^@]*)@', r'://\1:***@', url or '')


class SqliteBackend:
    """
    One shared aiosqlite connection guarded by a lock.

    A single aiosqlite connection must not be driven by two coroutines at once, and
    opening one per call spawns a thread per call — see the note in database.py.
    """

    placeholder_style = 'qmark'
    name = 'sqlite'

    def __init__(self, db_path):
        self.db_path = db_path
        self._conn = None
        self._lock = asyncio.Lock()

    async def _ensure_conn(self):
        if self._conn is None:
            import aiosqlite
            self._conn = await aiosqlite.connect(self.db_path)
            self._conn.row_factory = aiosqlite.Row
            # WAL keeps readers from blocking the writer; NORMAL sync is durable
            # enough for a deal cache and avoids an fsync on every insert.
            await self._conn.execute('PRAGMA journal_mode=WAL')
            await self._conn.execute('PRAGMA synchronous=NORMAL')
            await self._conn.execute('PRAGMA cache_size=-4000')  # ~4 MB page cache
            await self._conn.commit()
        return self._conn

    async def connect(self):
        await self._ensure_conn()

    async def close(self):
        async with self._lock:
            if self._conn is not None:
                await self._conn.close()
                self._conn = None

    async def execute(self, sql, params=()):
        """Run a statement, returning the number of affected rows."""
        async with self._lock:
            conn = await self._ensure_conn()
            cursor = await conn.execute(sql, params)
            await conn.commit()
            return cursor.rowcount

    async def execute_many_ddl(self, statements):
        async with self._lock:
            conn = await self._ensure_conn()
            for statement in statements:
                await conn.execute(statement)
            await conn.commit()

    async def fetch_all(self, sql, params=()):
        async with self._lock:
            conn = await self._ensure_conn()
            cursor = await conn.execute(sql, params)
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def fetch_one(self, sql, params=()):
        async with self._lock:
            conn = await self._ensure_conn()
            cursor = await conn.execute(sql, params)
            row = await cursor.fetchone()
            return dict(row) if row is not None else None

    async def maintenance(self):
        """Return emptied pages to the OS instead of holding the high-water mark."""
        async with self._lock:
            conn = await self._ensure_conn()
            await conn.execute('PRAGMA incremental_vacuum')
            await conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            await conn.commit()

    def schema(self):
        return [
            '''CREATE TABLE IF NOT EXISTS watchlist (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   keyword TEXT NOT NULL UNIQUE,
                   custom_synonyms TEXT DEFAULT '',
                   added_date REAL NOT NULL
               )''',
            '''CREATE TABLE IF NOT EXISTS channels (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   channel_id INTEGER NOT NULL UNIQUE,
                   channel_name TEXT NOT NULL,
                   channel_username TEXT DEFAULT '',
                   active INTEGER DEFAULT 1,
                   added_date REAL NOT NULL
               )''',
            '''CREATE TABLE IF NOT EXISTS matched_deals (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   deal_hash TEXT NOT NULL UNIQUE,
                   keyword_matched TEXT NOT NULL,
                   product_name TEXT DEFAULT '',
                   price TEXT DEFAULT '',
                   channel_name TEXT DEFAULT '',
                   message_link TEXT DEFAULT '',
                   matched_date REAL NOT NULL
               )''',
            'CREATE INDEX IF NOT EXISTS idx_deals_matched_date ON matched_deals(matched_date)',
            'CREATE INDEX IF NOT EXISTS idx_channels_active ON channels(active)',
        ]


class PostgresBackend:
    """
    asyncpg connection pool against an external Postgres.

    The pool is deliberately small. This is one bot with a light query load, and free
    Postgres tiers cap concurrent connections aggressively — Neon's free plan and
    Supabase's pooler both punish a chatty client far more than a slow one.
    """

    placeholder_style = 'numeric'
    name = 'postgres'

    def __init__(self, dsn, min_size=1, max_size=3, command_timeout=30):
        self.dsn = normalise_dsn(dsn)
        self.min_size = min_size
        self.max_size = max_size
        self.command_timeout = command_timeout
        self._pool = None
        self._lock = asyncio.Lock()

    async def _ensure_pool(self):
        if self._pool is None:
            async with self._lock:
                if self._pool is None:
                    try:
                        import asyncpg
                    except ImportError as e:
                        raise RuntimeError(
                            "DATABASE_URL is set but asyncpg is not installed. "
                            "Add asyncpg to requirements.txt / pip install asyncpg."
                        ) from e
                    logger.info(f"Connecting to Postgres at {redact_dsn(self.dsn)}")
                    self._pool = await asyncpg.create_pool(
                        self.dsn,
                        min_size=self.min_size,
                        max_size=self.max_size,
                        command_timeout=self.command_timeout,
                    )
                    logger.info("✅ Postgres pool ready")
        return self._pool

    async def connect(self):
        await self._ensure_pool()

    async def close(self):
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @staticmethod
    def _rowcount(status):
        """
        asyncpg returns a status string such as 'INSERT 0 1', 'UPDATE 3', 'DELETE 2'.
        The affected-row count is the final token.
        """
        try:
            return int(str(status).split()[-1])
        except (ValueError, IndexError):
            return 0

    async def execute(self, sql, params=()):
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            status = await conn.execute(to_pg_placeholders(sql), *params)
            return self._rowcount(status)

    async def execute_many_ddl(self, statements):
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            for statement in statements:
                await conn.execute(statement)

    async def fetch_all(self, sql, params=()):
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(to_pg_placeholders(sql), *params)
            return [dict(row) for row in rows]

    async def fetch_one(self, sql, params=()):
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(to_pg_placeholders(sql), *params)
            return dict(row) if row is not None else None

    async def maintenance(self):
        """No-op — Postgres autovacuum handles reclamation."""
        return

    def schema(self):
        # BIGINT for channel_id is not optional: Telegram channel IDs routinely exceed
        # the 32-bit range, and INTEGER in Postgres really is 32-bit (unlike SQLite's,
        # which is 64-bit). Getting this wrong fails only for large IDs, which is the
        # worst kind of bug to ship.
        return [
            '''CREATE TABLE IF NOT EXISTS watchlist (
                   id BIGSERIAL PRIMARY KEY,
                   keyword TEXT NOT NULL UNIQUE,
                   custom_synonyms TEXT DEFAULT '',
                   added_date DOUBLE PRECISION NOT NULL
               )''',
            '''CREATE TABLE IF NOT EXISTS channels (
                   id BIGSERIAL PRIMARY KEY,
                   channel_id BIGINT NOT NULL UNIQUE,
                   channel_name TEXT NOT NULL,
                   channel_username TEXT DEFAULT '',
                   active INTEGER DEFAULT 1,
                   added_date DOUBLE PRECISION NOT NULL
               )''',
            '''CREATE TABLE IF NOT EXISTS matched_deals (
                   id BIGSERIAL PRIMARY KEY,
                   deal_hash TEXT NOT NULL UNIQUE,
                   keyword_matched TEXT NOT NULL,
                   product_name TEXT DEFAULT '',
                   price TEXT DEFAULT '',
                   channel_name TEXT DEFAULT '',
                   message_link TEXT DEFAULT '',
                   matched_date DOUBLE PRECISION NOT NULL
               )''',
            'CREATE INDEX IF NOT EXISTS idx_deals_matched_date ON matched_deals(matched_date)',
            'CREATE INDEX IF NOT EXISTS idx_channels_active ON channels(active)',
        ]


def build_backend(database_url, sqlite_path):
    """Pick a backend: Postgres when DATABASE_URL is set, otherwise SQLite."""
    if database_url and database_url.strip():
        return PostgresBackend(database_url)
    return SqliteBackend(sqlite_path)
