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

    async def add_column_if_missing(self, table, column, definition):
        """
        Additive migration for a table that already exists in a deployed database.

        SQLite has no `ADD COLUMN IF NOT EXISTS`, so the column list has to be read
        first — re-running ALTER TABLE otherwise errors and takes the whole boot with
        it. Returns True if the column was actually added.
        """
        async with self._lock:
            conn = await self._ensure_conn()
            cursor = await conn.execute(f'PRAGMA table_info({table})')
            rows = await cursor.fetchall()
            if any(row['name'] == column for row in rows):
                return False
            await conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')
            await conn.commit()
            return True

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
            # Counters only — never one row per alert. See the note on the Postgres
            # copy of this table for why.
            '''CREATE TABLE IF NOT EXISTS channel_stats (
                   channel_id INTEGER PRIMARY KEY,
                   alerts INTEGER NOT NULL DEFAULT 0,
                   up INTEGER NOT NULL DEFAULT 0,
                   down INTEGER NOT NULL DEFAULT 0,
                   alerts_at_report INTEGER NOT NULL DEFAULT 0,
                   up_at_report INTEGER NOT NULL DEFAULT 0,
                   down_at_report INTEGER NOT NULL DEFAULT 0,
                   last_alert_at REAL DEFAULT 0,
                   last_report_at REAL DEFAULT 0
               )''',
            'CREATE INDEX IF NOT EXISTS idx_deals_matched_date ON matched_deals(matched_date)',
            'CREATE INDEX IF NOT EXISTS idx_channels_active ON channels(active)',
        ]


class PostgresBackend:
    """
    asyncpg connection pool against an external Postgres.

    The pool is deliberately small and holds nothing open by default. This is one bot
    with a light query load, and free tiers cap concurrent connections aggressively —
    Neon's free plan and Supabase's pooler both punish a chatty client far more than a
    slow one.

    Serverless Postgres needs two accommodations, and without them the bot appears to
    work and then quietly stops recording deals after an idle period:

    * The provider SUSPENDS an idle database and drops its connections. Holding a
      connection open both fights that and burns the free tier's compute budget, so
      min_size defaults to 0 and idle connections are recycled after 60s — comfortably
      inside Neon's ~5 minute suspend window.
    * A suspended database takes a moment to WAKE, and refuses connections while it
      does. Every query is therefore retried a couple of times on connection-level
      failures. Query errors (bad SQL, constraint violations) are never retried.
    """

    placeholder_style = 'numeric'
    name = 'postgres'

    def __init__(self, dsn, min_size=None, max_size=None, command_timeout=None,
                 max_idle=None, max_retries=None, retry_delay=None):
        from config import (
            DB_COMMAND_TIMEOUT,
            DB_MAX_RETRIES,
            DB_POOL_MAX_IDLE,
            DB_POOL_MAX_SIZE,
            DB_POOL_MIN_SIZE,
            DB_RETRY_DELAY,
        )
        self.dsn = normalise_dsn(dsn)
        self.min_size = DB_POOL_MIN_SIZE if min_size is None else min_size
        self.max_size = DB_POOL_MAX_SIZE if max_size is None else max_size
        self.command_timeout = DB_COMMAND_TIMEOUT if command_timeout is None else command_timeout
        self.max_idle = DB_POOL_MAX_IDLE if max_idle is None else max_idle
        self.max_retries = DB_MAX_RETRIES if max_retries is None else max_retries
        self.retry_delay = DB_RETRY_DELAY if retry_delay is None else retry_delay
        self._pool = None
        self._lock = asyncio.Lock()
        self._retryable = None

    def _retryable_errors(self):
        """
        Exception types worth retrying: the database is asleep, waking, or dropped a
        socket. Built lazily and defensively — asyncpg's exception names have shifted
        between releases, so a missing one must not break the whole backend.
        """
        if self._retryable is None:
            types = [ConnectionError, OSError, asyncio.TimeoutError]
            try:
                import asyncpg.exceptions as pgerr
                for name in (
                    'ConnectionDoesNotExistError',   # server closed the connection
                    'ConnectionFailureError',
                    'CannotConnectNowError',         # database is starting up / waking
                    'InterfaceError',                # client-side: connection is closed
                    'TooManyConnectionsError',
                    'ConnectionRejectionError',
                    'PostgresConnectionError',
                ):
                    exc = getattr(pgerr, name, None)
                    if exc is not None:
                        types.append(exc)
            except ImportError:
                pass
            self._retryable = tuple(types)
        return self._retryable

    def is_retryable(self, error):
        return isinstance(error, self._retryable_errors())

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
                        max_inactive_connection_lifetime=self.max_idle,
                    )
                    logger.info(
                        f"✅ Postgres pool ready "
                        f"(min={self.min_size} max={self.max_size} idle={self.max_idle}s)"
                    )
        return self._pool

    async def _run(self, operation):
        """
        Run `operation(connection)` with retries on connection-level failures.

        A suspended serverless database refuses the first connection while it wakes,
        and a socket can go stale between pool checkout and use. Both are transient
        and both look like a hard failure without this.
        """
        attempt = 0
        while True:
            attempt += 1
            try:
                pool = await self._ensure_pool()
                async with pool.acquire() as conn:
                    return await operation(conn)
            except Exception as e:
                if not self.is_retryable(e) or attempt >= self.max_retries:
                    raise
                logger.warning(
                    f"Postgres call failed ({type(e).__name__}: {e}) — "
                    f"retry {attempt}/{self.max_retries - 1}"
                )
                # The pool may be holding dead connections after a suspend; drop it
                # so the next attempt dials fresh.
                await self._discard_pool()
                await asyncio.sleep(self.retry_delay * attempt)

    async def _discard_pool(self):
        """Throw away the pool without waiting for in-flight work."""
        pool, self._pool = self._pool, None
        if pool is not None:
            try:
                pool.terminate()
            except Exception as e:
                logger.debug(f"Error terminating Postgres pool: {e}")

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
        statement = to_pg_placeholders(sql)

        async def op(conn):
            return self._rowcount(await conn.execute(statement, *params))

        return await self._run(op)

    async def execute_many_ddl(self, statements):
        async def op(conn):
            for statement in statements:
                await conn.execute(statement)

        return await self._run(op)

    async def fetch_all(self, sql, params=()):
        statement = to_pg_placeholders(sql)

        async def op(conn):
            rows = await conn.fetch(statement, *params)
            return [dict(row) for row in rows]

        return await self._run(op)

    async def fetch_one(self, sql, params=()):
        statement = to_pg_placeholders(sql)

        async def op(conn):
            row = await conn.fetchrow(statement, *params)
            return dict(row) if row is not None else None

        return await self._run(op)

    async def maintenance(self):
        """No-op — Postgres autovacuum handles reclamation."""
        return

    async def add_column_if_missing(self, table, column, definition):
        """Additive migration. Postgres has this natively, so it is a one-liner."""
        async def op(conn):
            await conn.execute(
                f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {definition}')
            return True

        return await self._run(op)

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
            # Per-channel COUNTERS, not an alert log. NDT deliberately keeps no
            # history of alerts — matched_deals is a 7-day dedup ledger that gets
            # pruned, so anything that has to survive longer (how a channel has
            # performed, how its alerts were rated) is kept as running totals. This
            # table therefore stays at one row per channel no matter how many alerts
            # are sent, which is also what makes it free to keep forever.
            #   *_at_report are snapshots taken when a report is sent, so the next
            #   report can show the delta without storing anything per alert.
            '''CREATE TABLE IF NOT EXISTS channel_stats (
                   channel_id BIGINT PRIMARY KEY,
                   alerts INTEGER NOT NULL DEFAULT 0,
                   up INTEGER NOT NULL DEFAULT 0,
                   down INTEGER NOT NULL DEFAULT 0,
                   alerts_at_report INTEGER NOT NULL DEFAULT 0,
                   up_at_report INTEGER NOT NULL DEFAULT 0,
                   down_at_report INTEGER NOT NULL DEFAULT 0,
                   last_alert_at DOUBLE PRECISION DEFAULT 0,
                   last_report_at DOUBLE PRECISION DEFAULT 0
               )''',
            'CREATE INDEX IF NOT EXISTS idx_deals_matched_date ON matched_deals(matched_date)',
            'CREATE INDEX IF NOT EXISTS idx_channels_active ON channels(active)',
        ]


def build_backend(database_url, sqlite_path):
    """Pick a backend: Postgres when DATABASE_URL is set, otherwise SQLite."""
    if database_url and database_url.strip():
        return PostgresBackend(database_url)
    return SqliteBackend(sqlite_path)
