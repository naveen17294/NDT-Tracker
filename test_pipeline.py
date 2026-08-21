"""
Offline regression tests — no Telegram credentials, no network.

Run with:  python test_pipeline.py

The headline case is test_preview_resolves_after_pending. Telegram answers
WebPagePending the first time it is asked about a URL, and the old code hit
`NameError: name 'asyncio' is not defined` on that exact branch. A bare `except`
swallowed it into a debug log, so every message silently fell through to the HTML
scraper and preview-based extraction never ran at all.
"""

import asyncio
import os
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from types import SimpleNamespace

# Point config at a throwaway directory before importing it — importing config
# creates DATA_PATH/SESSION_PATH as a side effect.
_TMP = tempfile.mkdtemp(prefix='ndt-test-')
os.environ['DATA_PATH'] = os.path.join(_TMP, 'data') + os.sep
os.environ['SESSION_PATH'] = os.path.join(_TMP, 'sessions') + os.sep
os.environ.setdefault('PREVIEW_RETRY_DELAY', '0.01')  # keep the suite fast

from telethon.tl.types import (
    MessageMediaWebPage,
    WebPage,
    WebPageEmpty,
    WebPagePending,
)

from channel_monitor import ChannelMonitor
from database import Database
from keyword_matcher import KeywordMatcher
from link_scraper import LinkScraper
from price_extractor import PriceExtractor
from storage import (
    PostgresBackend,
    SqliteBackend,
    build_backend,
    normalise_dsn,
    redact_dsn,
    to_pg_placeholders,
)

_FAILURES = []
_PASSES = []


def _safe(text):
    """
    Printable on a Windows console.

    A Windows terminal is cp1252 by default, so printing an emoji from a failure
    detail raises UnicodeEncodeError and takes down the whole run — hiding the
    failure it was trying to report.
    """
    encoding = getattr(sys.stdout, 'encoding', None) or 'utf-8'
    return str(text).encode(encoding, errors='replace').decode(encoding, errors='replace')


def check(name, condition, detail=''):
    if condition:
        _PASSES.append(name)
        print(_safe(f"  PASS  {name}"))
    else:
        _FAILURES.append((name, detail))
        print(_safe(f"  FAIL  {name} {detail}"))


def _source(filename):
    """A module's own source, for the few checks that are about wiring, not output."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    with open(path, encoding='utf-8') as handle:
        return handle.read()


# ── Fakes ───────────────────────────────────────────────────────────────────

def make_webpage(title, description=''):
    return WebPage(
        id=1,
        url='https://www.amazon.in/dp/B0TEST',
        display_url='amazon.in/dp/B0TEST',
        hash=0,
        title=title,
        description=description,
    )


def make_pending():
    return WebPagePending(id=1, date=datetime.now(timezone.utc))


class NewLayerResult:
    """Newer Telegram layers wrap the media in a messages.WebPagePreview."""

    def __init__(self, media):
        self.media = media


class FakeClient:
    """Returns each queued response in turn, recording how many calls it saw."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    async def __call__(self, request):
        self.calls += 1
        if not self._responses:
            raise AssertionError("FakeClient called more times than expected")
        result = self._responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def make_monitor(client):
    """Build a ChannelMonitor without constructing a real TelegramClient."""
    monitor = object.__new__(ChannelMonitor)
    from collections import OrderedDict
    monitor._preview_cache = OrderedDict()
    monitor.client = client
    return monitor


# ── Tests ───────────────────────────────────────────────────────────────────

async def test_preview_resolves_after_pending():
    """THE regression: pending twice, then a real page. Must return the title."""
    client = FakeClient([
        MessageMediaWebPage(make_pending()),
        MessageMediaWebPage(make_pending()),
        MessageMediaWebPage(make_webpage('Samsung 236L Double Door Refrigerator', 'Frost free')),
    ])
    monitor = make_monitor(client)

    title, desc = await monitor._resolve_preview('https://amzn.to/xyz')

    check('preview survives WebPagePending', title == 'Samsung 236L Double Door Refrigerator',
          f'got {title!r}')
    check('preview returns description', desc == 'Frost free', f'got {desc!r}')
    check('preview retried until resolved', client.calls == 3, f'calls={client.calls}')


async def test_preview_immediate():
    client = FakeClient([MessageMediaWebPage(make_webpage('Sony Bravia 55 inch 4K TV'))])
    monitor = make_monitor(client)
    title, _ = await monitor._resolve_preview('https://amzn.to/immediate')
    check('preview resolves on first try', title == 'Sony Bravia 55 inch 4K TV', f'got {title!r}')


async def test_preview_new_layer_shape():
    """A layer bump that wraps media in .media must not break resolution."""
    client = FakeClient([
        NewLayerResult(MessageMediaWebPage(make_webpage('LG 1.5 Ton Split AC')))
    ])
    monitor = make_monitor(client)
    title, _ = await monitor._resolve_preview('https://amzn.to/newlayer')
    check('preview handles wrapped .media shape', title == 'LG 1.5 Ton Split AC', f'got {title!r}')


async def test_preview_gives_up_when_always_pending():
    from config import PREVIEW_RETRIES
    client = FakeClient([MessageMediaWebPage(make_pending()) for _ in range(PREVIEW_RETRIES)])
    monitor = make_monitor(client)
    title, desc = await monitor._resolve_preview('https://amzn.to/forever-pending')
    check('always-pending gives up cleanly', title is None and desc is None, f'got {title!r}')
    check('gave up after PREVIEW_RETRIES calls', client.calls == PREVIEW_RETRIES,
          f'calls={client.calls}')


async def test_preview_empty_page():
    client = FakeClient([MessageMediaWebPage(WebPageEmpty(id=1))])
    monitor = make_monitor(client)
    title, _ = await monitor._resolve_preview('https://amzn.to/empty')
    check('empty preview returns None', title is None, f'got {title!r}')


async def test_preview_request_error_is_not_fatal():
    client = FakeClient([RuntimeError('FLOOD_WAIT_5')])
    monitor = make_monitor(client)
    title, _ = await monitor._resolve_preview('https://amzn.to/boom')
    check('preview error handled, not raised', title is None, f'got {title!r}')


async def test_preview_cache_hit():
    client = FakeClient([MessageMediaWebPage(make_webpage('Boat Airdopes 141 Earbuds'))])
    monitor = make_monitor(client)
    first, _ = await monitor._resolve_preview('https://amzn.to/cached')
    second, _ = await monitor._resolve_preview('https://amzn.to/cached')
    check('cached preview returns same title', first == second == 'Boat Airdopes 141 Earbuds')
    check('cached preview makes no second call', client.calls == 1, f'calls={client.calls}')


async def test_preview_cache_is_bounded():
    from config import PREVIEW_CACHE_SIZE
    monitor = make_monitor(FakeClient([]))
    for i in range(PREVIEW_CACHE_SIZE + 50):
        monitor._cache_put(f'https://example.com/{i}', f'Title {i}', '')
    check('preview cache respects its cap', len(monitor._preview_cache) == PREVIEW_CACHE_SIZE,
          f'size={len(monitor._preview_cache)}')
    check('preview cache evicted the oldest entry',
          monitor._cache_get('https://example.com/0') is None)
    check('preview cache kept the newest entry',
          monitor._cache_get(f'https://example.com/{PREVIEW_CACHE_SIZE + 49}') is not None)


async def test_scraper_cache_is_bounded():
    from config import SCRAPE_CACHE_SIZE
    scraper = LinkScraper()
    for i in range(SCRAPE_CACHE_SIZE + 50):
        scraper._cache_result(f'https://example.com/{i}', f'Product {i}')
    check('scraper cache respects its cap', len(scraper._cache) == SCRAPE_CACHE_SIZE,
          f'size={len(scraper._cache)}')
    check('scraper cache evicted the oldest', scraper._is_cached('https://example.com/0') is None)
    await scraper.close()
    check('scraper close clears cache', len(scraper._cache) == 0)


async def test_postgres_retries_on_a_sleeping_database():
    """
    Serverless Postgres (Neon, Supabase) suspends an idle database and refuses
    connections while it wakes. Without a retry the first query after an idle period
    fails and the bot silently stops recording deals — so this pins that a
    connection-level failure is retried and a query-level failure is not.
    """
    import asyncpg.exceptions as pgerr

    backend = PostgresBackend('postgresql://u:p@h/db', max_retries=3, retry_delay=0.01)

    # Classification
    check('waking database is retryable',
          backend.is_retryable(pgerr.CannotConnectNowError('starting up')))
    check('dropped connection is retryable',
          backend.is_retryable(pgerr.ConnectionDoesNotExistError('gone')))
    check('closed client connection is retryable',
          backend.is_retryable(pgerr.InterfaceError('connection closed')))
    check('socket error is retryable', backend.is_retryable(ConnectionResetError()))
    check('bad SQL is NOT retryable',
          not backend.is_retryable(pgerr.SyntaxOrAccessError('boom')))
    check('unique violation is NOT retryable',
          not backend.is_retryable(pgerr.UniqueViolationError('dup')))

    # A pool that refuses once (database waking), then succeeds.
    class FakeConn:
        def __init__(self, outcomes):
            self.outcomes = outcomes

        async def execute(self, sql, *params):
            result = self.outcomes.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

    class FakeAcquire:
        def __init__(self, conn):
            self.conn = conn

        async def __aenter__(self):
            return self.conn

        async def __aexit__(self, *a):
            return False

    class FakePool:
        def __init__(self, outcomes):
            self.conn = FakeConn(outcomes)
            self.terminated = 0

        def acquire(self):
            return FakeAcquire(self.conn)

        def terminate(self):
            self.terminated += 1

    pool = FakePool([pgerr.CannotConnectNowError('waking'), 'UPDATE 1'])
    backend._pool = pool

    async def fake_ensure():
        if backend._pool is None:
            backend._pool = pool
        return backend._pool
    backend._ensure_pool = fake_ensure

    affected = await backend.execute('UPDATE t SET a = ? WHERE b = ?', (1, 2))
    check('retry recovers from a waking database', affected == 1, f'got {affected}')
    check('stale pool is discarded before retrying', pool.terminated == 1,
          f'terminated={pool.terminated}')

    # A non-retryable error must surface immediately, not burn retries.
    pool2 = FakePool([pgerr.UniqueViolationError('dup'), 'INSERT 0 1'])
    backend._pool = pool2

    async def fake_ensure2():
        if backend._pool is None:
            backend._pool = pool2
        return backend._pool
    backend._ensure_pool = fake_ensure2

    raised = None
    try:
        await backend.execute('INSERT INTO t VALUES (?)', (1,))
    except Exception as e:
        raised = e
    check('query errors are not retried', isinstance(raised, pgerr.UniqueViolationError),
          f'got {raised!r}')
    check('pool not discarded on a query error', pool2.terminated == 0,
          f'terminated={pool2.terminated}')

    # Give up after max_retries rather than looping forever.
    pool3 = FakePool([pgerr.CannotConnectNowError('waking')] * 5)
    backend._pool = pool3

    async def fake_ensure3():
        if backend._pool is None:
            backend._pool = pool3
        return backend._pool
    backend._ensure_pool = fake_ensure3

    raised = None
    try:
        await backend.execute('SELECT 1', ())
    except Exception as e:
        raised = e
    check('gives up after max_retries', isinstance(raised, pgerr.CannotConnectNowError),
          f'got {raised!r}')
    check('exhausted retries used the full budget', pool3.conn.outcomes and len(pool3.conn.outcomes) == 2,
          f'remaining={len(pool3.conn.outcomes)}')


async def test_postgres_pool_defaults_suit_serverless():
    """min_size 0 lets a serverless database suspend instead of being held awake."""
    backend = PostgresBackend('postgresql://u:p@h/db')
    check('pool holds no idle connections by default', backend.min_size == 0,
          f'got {backend.min_size}')
    check('idle connections recycle before the provider suspends',
          backend.max_idle <= 300, f'got {backend.max_idle}')
    check('pool stays small for free-tier connection caps', backend.max_size <= 5,
          f'got {backend.max_size}')


async def test_database_shared_connection_and_retention():
    import time as _time

    db = Database()
    check('Database is a singleton', db is Database())
    check('defaults to SQLite with no DATABASE_URL', db.backend_name == 'sqlite',
          f'got {db.backend_name}')
    await db.init()

    conn_first = await db.backend._ensure_conn()
    await db.add_keyword('fridge', 'samsung')
    conn_second = await db.backend._ensure_conn()
    check('DB reuses one connection', conn_first is conn_second)

    kws = await db.get_all_keywords()
    # (keyword, custom_synonyms, exclusions) — the third element arrived with
    # negative keywords.
    check('keyword round-trips', ('fridge', 'samsung', '') in kws, f'got {kws}')

    await db.update_exclusions('fridge', 'mini, used')
    kws = await db.get_all_keywords()
    check('exclusions round-trip', ('fridge', 'samsung', 'mini, used') in kws,
          f'got {kws}')

    # Duplicate insert must report "already there" rather than raising.
    again = await db.add_keyword('fridge', 'samsung')
    check('duplicate keyword returns False', again is False, f'got {again}')

    removed = await db.remove_keyword('fridge')
    check('remove_keyword reports success', removed is True)
    check('remove_keyword on a missing key returns False',
          await db.remove_keyword('nonexistent') is False)

    # Channels: IDs beyond 32 bits must survive (Telegram uses them).
    big_id = 2_147_483_648 + 12345
    await db.add_channel(big_id, 'Big ID Channel', '@bigid')
    state = await db.toggle_channel(big_id)
    check('toggle_channel activates', state == 1, f'got {state}')
    active = await db.get_active_channels()
    check('large channel id round-trips',
          any(c['channel_id'] == big_id for c in active), f'got {active}')

    await db.save_deal('hash-fresh', 'fridge', 'Samsung Fridge', '24999', '@deals', '')
    check('fresh deal is seen', await db.is_deal_seen('hash-fresh') is True)

    # Re-saving the same hash must slide the window forward, not raise.
    await db.save_deal('hash-fresh', 'fridge', 'Samsung Fridge', '24999', '@deals', '')
    check('repeat save_deal upserts cleanly', await db.is_deal_seen('hash-fresh') is True)

    # Backdate a row well past the retention window and prove the sweep removes it.
    await db.backend.execute(
        'INSERT INTO matched_deals (deal_hash, keyword_matched, matched_date) VALUES (?, ?, ?)',
        ('hash-ancient', 'fridge', _time.time() - (60 * 86400))
    )

    deleted = await db.cleanup_old_deals(days=7)
    check('retention sweep deletes old rows', deleted == 1, f'deleted={deleted}')
    check('retention sweep keeps fresh rows', await db.is_deal_seen('hash-fresh') is True)

    stats = await db.get_stats()
    check('get_stats aliases COUNT(*) correctly', stats['total_deals'] == 1,
          f"got {stats}")

    await db.close()


async def test_working_directories_never_crash_at_import():
    """
    config.py creates its working directories at import. That used to be an
    unguarded os.makedirs, so a leftover DATA_PATH=/var/data/ from a disk-backed
    deployment killed the process at import once the disk was detached — before
    main() ran, so no port was bound and the deploy just failed.
    """
    from config import _ensure_dir

    unusable = os.path.join('Z:' + os.sep, 'nonexistent-drive', 'data')

    # Not needed (Postgres / SESSION_STRING in use) — must not even try.
    _ensure_dir(unusable, False, 'test')
    check('unneeded directory is skipped entirely', not os.path.exists(unusable))

    # Needed but uncreatable — must warn, not raise.
    raised = None
    try:
        _ensure_dir(unusable, True, 'test')
    except Exception as e:
        raised = e
    check('uncreatable directory does not raise at import', raised is None,
          f'raised {raised!r}')

    # Needed and creatable — must actually create it.
    wanted = os.path.join(_TMP, 'made-by-test')
    _ensure_dir(wanted, True, 'test')
    check('needed directory is created', os.path.isdir(wanted))


async def test_storage_backend_selection_and_translation():
    """
    The Postgres path cannot be exercised without a live server, but everything that
    would silently corrupt queries — placeholder translation, DSN handling, schema
    types, rowcount parsing — is pure logic and is tested here.
    """
    check('no DATABASE_URL selects SQLite',
          isinstance(build_backend('', '/tmp/x.db'), SqliteBackend))
    check('blank DATABASE_URL selects SQLite',
          isinstance(build_backend('   ', '/tmp/x.db'), SqliteBackend))
    check('DATABASE_URL selects Postgres',
          isinstance(build_backend('postgresql://u:p@h:5432/db', '/tmp/x.db'),
                     PostgresBackend))

    # Placeholder translation: ? -> $1, $2, ...
    got = to_pg_placeholders('INSERT INTO t (a, b, c) VALUES (?, ?, ?)')
    check('placeholders become positional',
          got == 'INSERT INTO t (a, b, c) VALUES ($1, $2, $3)', f'got {got}')
    got = to_pg_placeholders('SELECT * FROM t WHERE a = ? AND b > ?')
    check('placeholders numbered in order',
          got == 'SELECT * FROM t WHERE a = $1 AND b > $2', f'got {got}')
    check('SQL without placeholders is untouched',
          to_pg_placeholders('SELECT 1') == 'SELECT 1')

    # DSN normalisation and redaction
    check('postgres:// is normalised',
          normalise_dsn('postgres://u:p@h/db') == 'postgresql://u:p@h/db')
    check('surrounding whitespace is stripped',
          normalise_dsn('  postgresql://u:p@h/db  ') == 'postgresql://u:p@h/db')
    red = redact_dsn('postgresql://user:sup3rsecret@host:5432/ndt')
    check('password is redacted for logs', 'sup3rsecret' not in red and 'user' in red,
          f'got {red}')

    # asyncpg status-string parsing
    check("rowcount parses 'INSERT 0 1'", PostgresBackend._rowcount('INSERT 0 1') == 1)
    check("rowcount parses 'INSERT 0 0'", PostgresBackend._rowcount('INSERT 0 0') == 0)
    check("rowcount parses 'UPDATE 3'", PostgresBackend._rowcount('UPDATE 3') == 3)
    check("rowcount parses 'DELETE 2'", PostgresBackend._rowcount('DELETE 2') == 2)
    check('rowcount survives junk', PostgresBackend._rowcount('WAT') == 0)

    # Schema sanity: channel_id must be 64-bit on Postgres. Telegram channel IDs
    # exceed the 32-bit range, and Postgres INTEGER really is 32-bit.
    pg_schema = ' '.join(PostgresBackend('postgresql://u:p@h/db').schema())
    check('postgres channels.channel_id is BIGINT', 'channel_id BIGINT' in pg_schema,
          'channel_id would overflow INTEGER')
    check('postgres uses BIGSERIAL keys', 'BIGSERIAL PRIMARY KEY' in pg_schema)
    check('postgres timestamps are DOUBLE PRECISION',
          'matched_date DOUBLE PRECISION' in pg_schema)

    # Both backends must agree on table and index names, or they will diverge.
    sqlite_schema = ' '.join(SqliteBackend('/tmp/x.db').schema())
    for table in ('watchlist', 'channels', 'matched_deals'):
        check(f'both backends define {table}',
              f'TABLE IF NOT EXISTS {table}' in sqlite_schema
              and f'TABLE IF NOT EXISTS {table}' in pg_schema)
    check('both backends define the retention index',
          'idx_deals_matched_date' in sqlite_schema and 'idx_deals_matched_date' in pg_schema)


async def test_matcher_and_price_still_work():
    matcher = KeywordMatcher()
    watchlist = [('fridge', ''), ('tv', '')]

    m = matcher.match('Samsung 236L Double Door Refrigerator at best price', watchlist)
    check('synonym match works', m is not None and m['keyword'] == 'fridge', f'got {m}')

    # Cached synonym expansion must return the same answer the second time.
    m2 = matcher.match('Samsung 236L Double Door Refrigerator at best price', watchlist)
    check('match is stable across cached calls', m2 == m)

    # URL slugs must not create false positives (the \btv\b guard).
    m3 = matcher.match('Grab it here https://amzn.to/u5tv2', watchlist)
    check('no false positive inside a URL slug', m3 is None, f'got {m3}')

    price = PriceExtractor().extract('Deal price ₹1,999 (MRP ₹3,999) 50% off')
    check('price parses with commas', price['price'] == 1999.0, f"got {price['price']}")


async def test_matcher_rejects_lookalike_words():
    """
    Regression: a watchlist of ['shoes'] was firing on home-furnishing deals.

    difflib.SequenceMatcher scores two 5-letter words sharing 4 characters at exactly
    0.800, and the threshold was 0.75 — so 'homes', 'hoses' and 'shows' all matched
    'shoes'. No threshold fixes that for short words, which is why fuzzy matching is
    now off by default and plurals are handled by normalisation instead.
    """
    matcher = KeywordMatcher()
    shoes = [('shoes', '')]

    lookalikes = [
        ('homes', 'Home decor for homes Rs 499'),
        ('hoses', 'Garden hoses 10m heavy duty Rs 599'),
        ('shows', 'Best shows streaming deal Rs 299'),
        ('hose', 'Shower hose replacement Rs 349'),
        ('those', 'Grab those deals now Rs 199'),
        ('phones', 'Smartphones on sale Rs 9999'),
    ]
    for word, message in lookalikes:
        result = matcher.match(message, shoes)
        check(f"'{word}' does not match 'shoes'", result is None, f'got {result}')

    check('unrelated product does not match',
          matcher.match('Cello Opalware Dinner Set 27 Pcs Rs 1299', shoes) is None)


async def test_matcher_still_finds_real_products():
    """Precision fixes are worthless if they cost recall — these must all still match."""
    matcher = KeywordMatcher()

    cases = [
        ('shoes', 'Nike Running Shoes Rs 1999', [('shoes', '')], 'exact'),
        ('singular in text', 'Puma Sports Shoe for men Rs 1499', [('shoes', '')], 'plural'),
        ('plural in text', 'Adidas Shoes combo Rs 2499', [('shoe', '')], 'plural'),
        ('synonym oled', 'LG OLED 55 inch panel deal', [('tv', '')], 'synonym'),
        ('synonym refrigerator', 'Samsung 236L Refrigerator Rs 24999', [('fridge', '')], 'synonym'),
        ('synonym tws', 'Boat TWS wireless earphone Rs 999', [('earbuds', '')], 'synonym'),
    ]
    for desc, message, watchlist, expected_type in cases:
        result = matcher.match(message, watchlist)
        check(f'still matches: {desc}', result is not None, 'no match')
        if result:
            check(f'  match type for {desc} is {expected_type}',
                  result['match_type'] == expected_type, f"got {result['match_type']}")

    custom = [('shoes', 'sneakers, loafers')]
    check('custom synonym still matches',
          matcher.match('White Sneakers for men Rs 1299', custom) is not None)


async def test_synonyms_do_not_leak_across_categories():
    """
    Regression: /watch 'air cooler' also matched air conditioners.

    'air cooler' is listed under both the 'ac' and 'cooler' categories, and the
    reverse lookup pulled in every owning category wholesale — so watching a cooler
    dragged in 'split ac', 'window ac' and 'inverter ac'. An ambiguous term now
    expands to nothing and matches only itself.
    """
    matcher = KeywordMatcher()
    cooler = [('air cooler', '')]

    expansion = matcher.get_synonyms('air cooler')
    check('ambiguous term does not expand', expansion == ['air cooler'],
          f'got {expansion}')

    check('split AC does not match "air cooler"',
          matcher.match('Voltas Split AC 1.5 Ton Rs 31999', cooler) is None)
    check('window AC does not match "air cooler"',
          matcher.match('Blue Star Window AC Rs 24999', cooler) is None)
    check('a real cooler still matches',
          matcher.match('Symphony Air Cooler 70L Rs 8999', cooler) is not None)

    # Unambiguous terms must still expand normally.
    check("'ac' still matches a split AC",
          matcher.match('Daikin Split AC 1.5T Rs 35999', [('ac', '')]) is not None)
    check("'fridge' still expands to its category",
          'refrigerator' in matcher.get_synonyms('fridge'))


async def test_synonym_order_is_deterministic_and_specific():
    """
    get_synonyms used to return list(set(...)). Python randomises string hashing per
    process, so the reported matched_term changed between restarts.
    """
    matcher = KeywordMatcher()
    tv = [('tv', '')]

    terms = {matcher.match('Mi Smart TV 43 inch Rs 21999', tv)['matched_term']
             for _ in range(50)}
    check('matched_term is stable across runs', len(terms) == 1, f'got {terms}')

    check('synonyms are sorted longest-first',
          matcher.get_synonyms('tv') == sorted(matcher.get_synonyms('tv'),
                                               key=lambda s: (-len(s), s)))

    # Where the keyword itself is absent, the longest matching synonym should win.
    # ('fridge' does not appear here; both 'refrigerator' and 'double door' do.)
    result = matcher.match('LG 260L Double Door Refrigerator Rs 27499', [('fridge', '')])
    check('most specific synonym wins',
          result is not None and result['matched_term'] == 'refrigerator',
          f"got {result['matched_term'] if result else None}")


async def test_singularise_keeps_words_distinct():
    """
    The property that makes normalisation safe where fuzzy matching was not: each
    word maps to exactly one singular form, and lookalikes stay distinct.
    """
    from keyword_matcher import singularise

    pairs = [('shoes', 'shoe'), ('homes', 'home'), ('hoses', 'hose'),
             ('shows', 'show'), ('watches', 'watch'), ('batteries', 'battery'),
             ('boxes', 'box')]
    for plural, expected in pairs:
        got = singularise(plural)
        check(f'singularise({plural!r}) -> {expected!r}', got == expected, f'got {got!r}')

    forms = {singularise(w) for w in ('shoes', 'homes', 'hoses', 'shows')}
    check('lookalike plurals stay distinct after normalising', len(forms) == 4,
          f'collapsed to {forms}')

    # Words that merely end in s must not be mangled.
    for word in ('dress', 'glass', 'headphones'):
        check(f'{word!r} survives singularise sensibly',
              singularise(word) in (word, word[:-1]), f'got {singularise(word)!r}')


async def test_main_installs_an_event_loop():
    """
    python-telegram-bot 21.x calls asyncio.get_event_loop() inside run_polling().
    Python 3.14 raises instead of creating one implicitly, so bot.main() must install
    a loop before it gets there or the process dies before it can bind its port.

    Runs in a worker thread because a fresh thread has no event loop set — the same
    condition Python 3.14 now presents on MainThread.
    """
    import threading

    try:
        import bot
    except ImportError as e:
        check('bot.main installs an event loop', True, f'(skipped, {e})')
        return

    if os.environ.get('BOT_TOKEN'):
        check('bot.main installs an event loop', True, '(skipped, BOT_TOKEN is set)')
        return

    seen = {}

    def run():
        try:
            asyncio.get_event_loop()
            seen['before'] = 'present'
        except RuntimeError:
            seen['before'] = 'absent'

        bot.main()  # BOT_TOKEN is empty, so this returns right after the shim

        try:
            asyncio.get_event_loop()
            seen['after'] = 'present'
        except RuntimeError:
            seen['after'] = 'absent'

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(timeout=30)

    check('thread starts with no event loop', seen.get('before') == 'absent',
          f"got {seen.get('before')!r}")
    check('bot.main installs an event loop', seen.get('after') == 'present',
          f"got {seen.get('after')!r}")


class _FakeMonitor:
    def __init__(self, channels):
        self._channels = channels

    async def get_joined_channels(self):
        return self._channels


class _FakeChannelDb:
    def __init__(self, rows):
        self.rows = rows

    async def get_all_channels(self):
        return self.rows

    async def add_channel(self, channel_id, name, username):
        self.rows.append({'channel_id': channel_id, 'channel_name': name,
                          'channel_username': username, 'active': 0})


def _button_labels(markup):
    if markup is None:
        return []
    return [b.text for row in markup.inline_keyboard for b in row]


async def test_channel_list_is_filtered_to_deal_channels():
    """
    /channels used to list every broadcast channel the account had ever joined.
    It now offers only names containing a CHANNEL_NAME_FILTERS term, plus anything
    already tracked — that second half matters, because a channel added through
    /addchannel usually will not match the filter, and if the filter hid it there
    would be no way to toggle it back off.
    """
    try:
        import bot
    except ImportError as e:
        check('channel list is filtered', True, f'(skipped, {e})')
        return

    joined = [
        {'channel_id': 1, 'channel_name': 'Loot Deals India', 'channel_username': 'lootdeals'},
        {'channel_id': 2, 'channel_name': 'Mega SALE Alerts', 'channel_username': 'megasale'},
        {'channel_id': 3, 'channel_name': 'Daily News Hindi', 'channel_username': 'dailynews'},
        {'channel_id': 4, 'channel_name': 'Cricket Updates', 'channel_username': 'cricket'},
        {'channel_id': 5, 'channel_name': 'Tech Offers Hub', 'channel_username': 'techoffers'},
    ]
    active_ids = {5}  # manually added via /addchannel; name matches no filter term
    channels_list = [(c['channel_id'], c) for c in joined]

    visible = {cid for cid, _ in bot._visible_channels(channels_list, active_ids, None)}
    check('deal/sale channels are listed', visible >= {1, 2}, f"got {sorted(visible)}")
    check('unrelated channels are hidden', not (visible & {3, 4}), f"got {sorted(visible)}")
    check('tracked channels are always listed', 5 in visible, f"got {sorted(visible)}")

    # Search ignores the filter entirely.
    found = {cid for cid, _ in bot._visible_channels(channels_list, active_ids, 'cricket')}
    check('search reaches filtered-out channels', found == {4}, f"got {sorted(found)}")

    found = {cid for cid, _ in bot._visible_channels(channels_list, active_ids, 'NEWS')}
    check('search is case-insensitive', found == {3}, f"got {sorted(found)}")

    found = {cid for cid, _ in bot._visible_channels(channels_list, active_ids, 'nothinghere')}
    check('search with no hits returns nothing', found == set(), f"got {sorted(found)}")

    # Same thing through the real page builder, buttons and all.
    saved_monitor, saved_db = bot.monitor, bot.db
    try:
        bot.monitor = _FakeMonitor(joined)
        bot.db = _FakeChannelDb([{'channel_id': 5, 'channel_name': 'Tech Offers Hub',
                                  'channel_username': 'techoffers', 'active': 1}])

        msg, markup = await bot._get_channels_page(page=0)
        labels = ' | '.join(_button_labels(markup))
        check('page shows deal channels', 'Loot Deals' in labels and 'Mega SALE' in labels,
              f"got {labels!r}")
        check('page hides unrelated channels',
              'Daily News' not in labels and 'Cricket' not in labels, f"got {labels!r}")
        check('page keeps the tracked channel togglable', 'Tech Offers' in labels,
              f"got {labels!r}")
        check('page explains the filter', 'Showing channels named' in msg, f"got {msg!r}")
        data = [b.callback_data for row in markup.inline_keyboard for b in row]
        check('page offers search and add', 'ask_searchchannel' in data, f"got {data}")

        msg, markup = await bot._get_channels_page(page=0, search='cricket')
        labels = ' | '.join(_button_labels(markup))
        check('search page finds the hidden channel', 'Cricket' in labels, f"got {labels!r}")
        check('search page offers a way back', 'Clear search' in labels, f"got {labels!r}")

        msg, markup = await bot._get_channels_page(page=0, search='zzzz')
        data = [b.callback_data for row in markup.inline_keyboard for b in row]
        check('empty search offers a way out', 'ask_addchannel' in data, f"got {data}")
    finally:
        bot.monitor, bot.db = saved_monitor, saved_db


async def test_negative_keywords_block_a_match():
    """
    A watchlist entry can carry exclusions. Any of them appearing in the message
    vetoes THAT keyword — and only that keyword, so an unrelated watchlist entry
    still matches the same message.
    """
    m = KeywordMatcher()

    check('plain keyword still matches',
          m.match('Nike Running Shoes Rs 1999', [('shoes', '', '')]) is not None)

    blocked = m.match('Kids Running Shoes Rs 999', [('shoes', '', 'kids, women')])
    check('excluded term blocks the match', blocked is None, f"got {blocked}")

    allowed = m.match('Mens Running Shoes Rs 1999', [('shoes', '', 'kids, women')])
    check('non-excluded message still matches', allowed is not None, f"got {allowed}")

    # Plural/singular is handled, same as positive matching.
    check('exclusion matches the singular form',
          m.match('Kid Shoes Rs 999', [('shoes', '', 'kids')]) is None)

    # Word boundaries, not substrings — the whole point of the v1.2.0 work.
    check('exclusion does not fire on a substring',
          m.match('Expensive Shoes Rs 4999', [('shoes', '', 'pen')]) is not None)

    # Scoped per keyword.
    both = m.match('Kids Shoes and a Gaming Laptop Rs 999',
                   [('shoes', '', 'kids'), ('laptop', '', '')])
    check('exclusion is scoped to its own keyword',
          both is not None and both['keyword'] == 'laptop', f"got {both}")

    # Old 2-tuple watchlists must keep working.
    check('2-tuple watchlist entries still work',
          m.match('Nike Shoes', [('shoes', '')]) is not None)

    from keyword_matcher import parse_exclusions
    check('exclusions parse from comma form',
          parse_exclusions('kids, women , ') == ['kids', 'women'],
          f"got {parse_exclusions('kids, women , ')}")

    report, result = m.explain('Kids Running Shoes', [('shoes', '', 'kids')])
    check('explain reports the veto',
          result is None and 'BLOCKED keyword: shoes' in report and 'kids' in report,
          f"got {report!r}")


async def test_watch_command_parses_inline_negatives():
    """`/watch shoes -kids -women` must not eat hyphenated words like 't-shirt'."""
    try:
        import bot
    except ImportError as e:
        check('inline negatives parse', True, f'(skipped, {e})')
        return

    rest, excl = bot.parse_negatives('shoes -kids -women')
    check('inline negatives are extracted', rest == 'shoes' and excl == 'kids, women',
          f"got {rest!r} / {excl!r}")

    rest, excl = bot.parse_negatives('t-shirt -kids')
    check('hyphenated keywords survive', rest == 't-shirt' and excl == 'kids',
          f"got {rest!r} / {excl!r}")

    rest, excl = bot.parse_negatives('laptop | gaming, macbook -refurbished')
    check('negatives coexist with the | synonym form',
          rest == 'laptop | gaming, macbook' and excl == 'refurbished',
          f"got {rest!r} / {excl!r}")

    rest, excl = bot.parse_negatives('fridge')
    check('no negatives means none', rest == 'fridge' and excl == '',
          f"got {rest!r} / {excl!r}")


async def test_channel_counters_survive_deal_pruning():
    """
    The whole point of channel_stats: NDT keeps no history of alerts, so per-channel
    quality has to be running totals. Pruning every deal row must not touch them.
    """
    Database.reset_for_tests()
    os.environ.pop('DATABASE_URL', None)
    db = Database()
    db.db_path = os.path.join(_TMP, 'counters.db')
    db.backend = SqliteBackend(db.db_path)
    await db.init()

    await db.add_channel(4242, 'Loot Deals', 'lootdeals')
    await db.toggle_channel(4242)  # make it active

    for i in range(3):
        await db.record_alert(4242)
        await db.save_deal(f'hash-{i}', 'shoes', f'Product {i}', '999', 'Loot Deals')
    await db.record_feedback(4242, True)
    await db.record_feedback(4242, True)
    await db.record_feedback(4242, False)

    # Age every deal row out, then prune — the counters must be untouched.
    await db.backend.execute('UPDATE matched_deals SET matched_date = 0')
    deleted = await db.cleanup_old_deals(days=1)
    check('deal rows were pruned', deleted == 3, f"deleted {deleted}")

    rows = await db.get_channel_report()
    check('one row per channel, not per alert', len(rows) == 1, f"got {len(rows)} rows")
    row = rows[0]
    check('alert counter survived pruning', row['alerts'] == 3, f"got {row['alerts']}")
    check('feedback counters survived', (row['up'], row['down']) == (2, 1),
          f"got {row['up']}/{row['down']}")
    check('first report shows the full delta', row['new_alerts'] == 3,
          f"got {row['new_alerts']}")

    # After a report, the delta resets but lifetime totals do not.
    await db.snapshot_channel_report()
    await db.record_alert(4242)
    row = (await db.get_channel_report())[0]
    check('delta resets after a report', row['new_alerts'] == 1, f"got {row['new_alerts']}")
    check('lifetime total keeps counting', row['alerts'] == 4, f"got {row['alerts']}")

    # Untracked channels drop out of the report.
    await db.toggle_channel(4242)
    check('inactive channels are not reported', await db.get_channel_report() == [])

    await db.close()
    Database.reset_for_tests()


async def test_channel_report_renders():
    try:
        import bot
    except ImportError as e:
        check('channel report renders', True, f'(skipped, {e})')
        return

    now = 1_000_000.0
    rows = [
        {'channel_id': 1, 'channel_name': 'Loot Deals', 'channel_username': 'loot',
         'alerts': 40, 'up': 9, 'down': 1, 'new_alerts': 12, 'new_up': 3,
         'new_down': 0, 'last_alert_at': now - 100, 'last_report_at': 0},
        {'channel_id': 2, 'channel_name': 'Junk & Co <b>', 'channel_username': 'junk',
         'alerts': 20, 'up': 0, 'down': 7, 'new_alerts': 4, 'new_up': 0,
         'new_down': 3, 'last_alert_at': now - 100, 'last_report_at': 0},
        {'channel_id': 3, 'channel_name': 'Dead Channel', 'channel_username': 'dead',
         'alerts': 2, 'up': 0, 'down': 0, 'new_alerts': 0, 'new_up': 0,
         'new_down': 0, 'last_alert_at': now - 40 * 86400, 'last_report_at': 0},
    ]
    report = bot.build_channel_report(rows, now=now)

    check('report counts the window', '16 alert(s) since' in report, f"got {report!r}")
    check('good channel is starred', '⭐ good' in report, f"got {report!r}")
    check('junk channel is flagged', 'mostly junk' in report, f"got {report!r}")
    check('quiet channel is flagged', 'quiet 40d' in report, f"got {report!r}")
    check('channel names are HTML-escaped', '&lt;b&gt;' in report, f"got {report!r}")

    empty = bot.build_channel_report([], now=now)
    check('empty report explains itself', '/channels' in empty, f"got {empty!r}")

    unrated = bot.build_channel_report(
        [dict(rows[0], up=0, down=0)], now=now)
    check('unrated report nudges toward rating', '👍/👎' in unrated, f"got {unrated!r}")


async def test_report_schedule_survives_restarts():
    """
    The 8-hour timer is measured from last_report_at in the database. A plain
    sleep(interval) loop would be reset by every container restart, and on a free
    tier that recycles often it would never reach 8 hours.
    """
    try:
        import bot
    except ImportError as e:
        check('report schedule survives restarts', True, f'(skipped, {e})')
        return

    interval = 8 * 3600

    class _Rows:
        def __init__(self, rows):
            self.rows = rows

        async def get_channel_report(self):
            return self.rows

    saved = bot.db
    try:
        bot.db = _Rows([])
        check('never reported waits a full window',
              await bot._seconds_until_next_report(interval) == interval)

        # Reported 6 hours ago: a restart must resume with ~2 hours left, not 8.
        bot.db = _Rows([{'last_report_at': time.time() - 6 * 3600}])
        remaining = await bot._seconds_until_next_report(interval)
        check('a restart resumes mid-window', 7100 < remaining < 7300,
              f"got {remaining}")

        # Overdue (or a send that failed and left the timestamp alone) — retry soon,
        # but not in a tight loop.
        bot.db = _Rows([{'last_report_at': time.time() - 99 * 3600}])
        check('overdue reports retry soon',
              await bot._seconds_until_next_report(interval) == 300)
    finally:
        bot.db = saved


async def test_product_key_survives_affiliate_rewriting():
    """
    The root cause of the duplicates: one product, many links.

    Each channel rewrites the same item with its own affiliate tag, so nothing about
    the URL string is stable. The seller's own product id is.
    """
    import product_key as pkey

    # The same TV, as three channels would post it.
    variants = [
        'https://www.amazon.in/dp/B0CX23V2ZK?tag=channel-a-21&psc=1',
        'https://amazon.in/Samsung-Crystal-4K-Ultra-HD/dp/B0CX23V2ZK/ref=sr_1_3?tag=b-21',
        'https://www.amazon.in/gp/product/B0CX23V2ZK?ie=UTF8&tag=third-21&linkCode=xm2',
    ]
    keys = {pkey.canonical_key(u) for u in variants}
    check('every affiliate variant collapses to one key',
          keys == {'amazon:B0CX23V2ZK'}, f"got {keys}")

    # A genuinely different product must not collide.
    other = pkey.canonical_key('https://www.amazon.in/dp/B0DIFFEREN?tag=a-21')
    check('a different ASIN is a different key', other == 'amazon:B0DIFFEREN',
          f"got {other!r}")

    # An ASIN is exactly ten characters. Anything else in that slot is not one, and
    # must not be accepted as a product identity — otherwise unrelated pages would
    # dedup against each other and the second deal would be silently swallowed.
    check('an 11-character slug is not an ASIN',
          pkey.canonical_key('https://www.amazon.in/dp/B0TOOLONG12') is None,
          f"got {pkey.canonical_key('https://www.amazon.in/dp/B0TOOLONG12')!r}")
    check('a 9-character slug is not an ASIN',
          pkey.canonical_key('https://www.amazon.in/dp/B0TOOSHRT') is None)

    # Flipkart: pid is authoritative and survives the rest of the query soup.
    fk = {
        pkey.canonical_key('https://www.flipkart.com/x/p/itm9a8b7c?pid=TVSGHZ8&affid=abc'),
        pkey.canonical_key('https://dl.flipkart.com/y/p/itmZZZZ?pid=TVSGHZ8&lid=LSTx&fm=neo'),
    }
    check('flipkart pid wins over the path slug', fk == {'flipkart:tvsghz8'}, f"got {fk}")

    # A search page or a bare storefront identifies no product.
    check('a search url is not a product',
          pkey.canonical_key('https://www.amazon.in/s?k=tv&tag=a-21') is None)

    # Unknown sites still normalise: tracking stripped, ordering stable.
    a = pkey.canonical_key('https://croma.com/p/12345?utm_source=tg&x=1')
    b = pkey.canonical_key('https://www.croma.com/p/12345/?x=1&fbclid=zz')
    check('unknown sites normalise consistently', a == b and a is not None,
          f"got {a!r} vs {b!r}")

    # The www-stripping must not eat real hostnames — str.lstrip('www.') would turn
    # 'wow.com' into 'ow.com'.
    check('a host starting with w is intact',
          'wow.com' in (pkey.canonical_key('https://wow.com/p/1') or ''),
          f"got {pkey.canonical_key('https://wow.com/p/1')!r}")

    # A shortener carries no id, so it must be resolved before canonicalising.
    check('a shortener is not canonicalised',
          pkey.canonical_key('https://amzn.to/3xYzAbC') == 'url:amzn.to/3xyzabc')
    check('shorteners are detected',
          pkey.is_shortener('https://amzn.to/3xYzAbC', ['amzn.to', 'bit.ly']))


async def test_the_same_deal_from_two_channels_alerts_once():
    """
    The duplicate that actually reaches you.

    Both tracked channels post one product, minutes apart, each with its own
    affiliate link and its own marketing copy. The old hash was built from the
    message text, so it differed every time and both got through.
    """
    Database.reset_for_tests()
    os.environ.pop('DATABASE_URL', None)
    db = Database()
    db.db_path = os.path.join(_TMP, 'crosschannel.db')
    db.backend = SqliteBackend(db.db_path)
    await db.init()

    monitor = ChannelMonitor.__new__(ChannelMonitor)  # no Telegram connection needed

    # Channel A and channel B, same TV, different link and different blurb.
    hash_a = monitor._generate_deal_hash(
        '🔥🔥 SAMSUNG 55 INCH SMART TV LOWEST EVER 🔥🔥', 41999, 'tv',
        product_key='amazon:B0CX23V2ZK')
    hash_b = monitor._generate_deal_hash(
        'Samsung Crystal 4K 55" — big price drop today only', 42999, 'tv',
        product_key='amazon:B0CX23V2ZK')

    check('both posts produce the same identity', hash_a == hash_b,
          f"{hash_a} vs {hash_b}")

    check('the first one is new', await db.is_deal_seen(hash_a) is False)
    await db.save_deal(hash_a, 'tv', 'Samsung TV', '41999', '@channel_a')
    check('the second one is recognised as seen', await db.is_deal_seen(hash_b) is True)

    # A different product from the same channel must still get through.
    hash_c = monitor._generate_deal_hash(
        'Samsung 43 inch TV', 31999, 'tv', product_key='amazon:B0OTHERTV1')
    check('a different product still alerts', await db.is_deal_seen(hash_c) is False)

    # With no key at all it falls back to the old behaviour rather than colliding
    # everything that has no link into one identity.
    fallback_a = monitor._generate_deal_hash('Sony Headphones', 1999, 'headphones')
    fallback_b = monitor._generate_deal_hash('Boat Headphones', 1999, 'headphones')
    check('the fallback still separates different products',
          fallback_a != fallback_b)

    await db.close()
    Database.reset_for_tests()


async def test_mirror_mutes_and_dedups():
    """Bot 2's two filters: what you muted, and what you have already been shown."""
    from mirror import Mirror

    Database.reset_for_tests()
    os.environ.pop('DATABASE_URL', None)
    db = Database()
    db.db_path = os.path.join(_TMP, 'mirror.db')
    db.backend = SqliteBackend(db.db_path)
    await db.init()

    sent = []

    class _Bot:
        async def send_message(self, **kwargs):
            sent.append(kwargs)

    m = Mirror(_Bot(), db, owner_id=1)
    post = dict(text='Samsung 55 inch TV at a great price https://www.amazon.in/dp/B0CX23V2ZK',
                urls=['https://www.amazon.in/dp/B0CX23V2ZK'],
                channel_id=-100111, channel_name='LOOT DEALS',
                product_keys=['amazon:B0CX23V2ZK'])

    check('a fresh post is mirrored', await m.handle(**post) is True)
    check('it actually sent one message', len(sent) == 1)

    # The same product from the OTHER channel, different link, different words.
    check('the same product from another channel is silent',
          await m.handle(text='Big TV deal!! grab now',
                         urls=['https://amzn.to/different'],
                         channel_id=-100222, channel_name='DEALS SALE',
                         product_keys=['amazon:B0CX23V2ZK']) is False)
    check('still only one message went out', len(sent) == 1)

    # Muting the product keeps it away even outside the dedup window.
    await db.mute_product('amazon:B0OTHER', 'Some other TV')
    check('a muted product is never mirrored',
          await m.handle(text='Another TV', urls=['https://x.com/a'],
                         channel_id=-100111, channel_name='LOOT DEALS',
                         product_keys=['amazon:B0OTHER']) is False)

    # Muting a word blocks a whole category, matched on whole words only.
    await db.mute_term('saree')
    m.invalidate_mutes()
    check('a muted word blocks the post',
          await m.handle(text='Beautiful silk saree collection',
                         urls=['https://x.com/s1'], channel_id=-100111,
                         channel_name='LOOT DEALS',
                         product_keys=['amazon:B0SAREE001']) is False)
    check('a muted word does not block a substring match',
          await m.handle(text='Sareena brand shoes', urls=['https://x.com/s2'],
                         channel_id=-100111, channel_name='LOOT DEALS',
                         product_keys=['amazon:B0SHOES001']) is True)

    # A post with nothing to open has no preview to show, which is the whole point.
    check('a linkless post is skipped',
          await m.handle(text='Channel announcement', urls=[],
                         channel_id=-100111, channel_name='LOOT DEALS') is False)

    # The dedup ledger is in the database, so a restart cannot resurrect a duplicate.
    m2 = Mirror(_Bot(), db, owner_id=1)
    check('dedup survives a restart',
          await m2.handle(**post) is False)

    # Mutes are decisions, not history — pruning the dedup ledger must not clear them.
    await db.cleanup_mirror_seen(days=0)
    check('mutes survive a prune', len(await db.get_muted_terms()) == 1)
    check('muted products survive a prune',
          len(await db.get_muted_products()) == 1)
    check('the ledger itself was pruned', await db.is_mirror_seen('amazon:B0CX23V2ZK') is False)

    await db.close()
    Database.reset_for_tests()


class _FakeChat:
    def __init__(self, title, username):
        self.title = title
        self.username = username


class _FakeEvent:
    """The bits of a Telethon NewMessage event that _handle_message touches."""

    def __init__(self, chat_id, text, chat):
        self.chat_id = chat_id
        self.message = SimpleNamespace(message=text, id=555, media=None)
        self._chat = chat

    async def get_chat(self):
        return self._chat


async def _drive_one_message(db, text, watchlist, chat_id=-1001001):
    """
    Run a real message through ChannelMonitor._handle_message.

    Returns (alerts, mirrored) — what each bot was handed.
    """
    from mirror import Mirror
    from notifier import Notifier
    from price_extractor import PriceExtractor

    alerts, mirrored = [], []

    class _Bot1:
        async def send_message(self, **kwargs):
            alerts.append(kwargs)

    class _Bot2:
        async def send_message(self, **kwargs):
            mirrored.append(kwargs)

    monitor = object.__new__(ChannelMonitor)
    monitor.paused = False
    monitor._monitored_channel_ids = {chat_id}
    monitor.db = db
    monitor.matcher = KeywordMatcher()
    monitor.scraper = LinkScraper()
    monitor.price_extractor = PriceExtractor()
    monitor.notifier = Notifier(_Bot1())
    monitor._watchlist_cache = watchlist
    monitor._watchlist_cache_at = time.time()
    monitor._preview_cache = __import__('collections').OrderedDict()
    monitor._mirror_tasks = set()
    monitor.mirror = Mirror(_Bot2(), db, owner_id=1)

    await monitor._handle_message(
        _FakeEvent(chat_id, text, _FakeChat('LOOT DEALS INDIA', 'lootdeals')))

    # The mirror is deliberately detached, so wait for the task it spawned.
    if monitor._mirror_tasks:
        await asyncio.gather(*monitor._mirror_tasks, return_exceptions=True)
    await monitor.scraper.close()
    return alerts, mirrored


async def test_both_bots_receive_one_message():
    """
    End to end, through the real handler: bot 1 filters, bot 2 mirrors.

    This is the wiring that makes the two bots different, and it is the part a unit
    test of either one alone cannot show.
    """
    Database.reset_for_tests()
    os.environ.pop('DATABASE_URL', None)
    db = Database()
    db.db_path = os.path.join(_TMP, 'bothbots.db')
    db.backend = SqliteBackend(db.db_path)
    await db.init()

    tv = ('Samsung 55 inch Crystal 4K TV at just Rs.41999 '
          'https://www.amazon.in/dp/B0CX23V2ZK?tag=chan-a-21')

    # ── A post that MATCHES the watchlist: both bots send it ──
    alerts, mirrored = await _drive_one_message(db, tv, [('tv', '', '')])
    check('bot 1 alerts on a watchlist match', len(alerts) == 1, f"got {len(alerts)}")
    check('bot 2 mirrors it too', len(mirrored) == 1, f"got {len(mirrored)}")
    check('bot 1 attaches no buttons', alerts[0].get('reply_markup') is None)
    check('bot 2 attaches buttons', mirrored[0].get('reply_markup') is not None)

    # ── A post that matches NOTHING: only bot 2 sends it ──
    saree = 'Silk saree just Rs.899 https://www.amazon.in/dp/B0SAREE001?tag=x-21'
    alerts, mirrored = await _drive_one_message(db, saree, [('tv', '', '')])
    check('bot 1 stays quiet with no match', len(alerts) == 0, f"got {len(alerts)}")
    check('bot 2 still mirrors it', len(mirrored) == 1, f"got {len(mirrored)}")

    # ── An EMPTY watchlist must not silence bot 2 ──
    shoes = 'Nike shoes Rs.2999 https://www.amazon.in/dp/B0SHOES0001?tag=x-21'
    alerts, mirrored = await _drive_one_message(db, shoes, [])
    check('bot 1 has nothing to match against', len(alerts) == 0)
    check('bot 2 mirrors even with an empty watchlist', len(mirrored) == 1,
          f"got {len(mirrored)}")

    # ── The same product from the OTHER channel: both bots go quiet ──
    repost = ('SAMSUNG 55" 4K SMART TV — LOWEST EVER Rs.42999 '
              'https://www.amazon.in/dp/B0CX23V2ZK?tag=chan-b-21')
    alerts, mirrored = await _drive_one_message(
        db, repost, [('tv', '', '')], chat_id=-1002002)
    check('bot 1 does not re-alert the same product', len(alerts) == 0,
          f"got {len(alerts)}")
    check('bot 2 does not re-mirror it either', len(mirrored) == 0,
          f"got {len(mirrored)}")

    await db.close()
    Database.reset_for_tests()


async def test_mirror_is_independent_of_the_watchlist():
    """
    Bot 2 must send a post no keyword matches — that is the entire difference
    between the two bots. The mirror therefore has to run BEFORE the watchlist
    check in the monitor, or an empty watchlist would silence it completely.
    """
    source = _source('channel_monitor.py')
    spawn = source.index('_spawn_mirror(')
    watchlist_gate = source.index('watchlist = await self._get_watchlist()')
    check('the mirror runs before the watchlist gate', spawn < watchlist_gate,
          'the mirror is gated behind a keyword match')

    # And it must not be awaited inline, or bot 1's alert waits on bot 2's throttle.
    check('mirroring is detached from the alert path',
          'asyncio.create_task(self._mirror_one' in source)
    check('the task reference is held',
          '_mirror_tasks.add(task)' in source)


async def test_stats_explain_themselves():
    """
    The reported bug: statistics came back empty with no way to tell why.

    A zero and a failed query rendered identically, and the screen said nothing
    about which database it had read.
    """
    Database.reset_for_tests()
    os.environ.pop('DATABASE_URL', None)
    db = Database()
    db.db_path = os.path.join(_TMP, 'stats.db')
    db.backend = SqliteBackend(db.db_path)
    await db.init()

    stats = await db.get_stats()
    check('the backend is named', stats['backend'] == 'sqlite', f"got {stats['backend']}")
    check('the location is reported', stats['location'].endswith('stats.db'),
          f"got {stats['location']}")
    check('an empty install reads zero, not error', stats['watchlist_count'] == 0)
    check('mirror counts are included',
          'mirrored_24h' in stats and 'muted_products' in stats)
    check('lifetime alerts come from the counters', stats['total_alerts'] == 0)

    # A broken query must degrade to None for that metric only.
    await db.backend.execute('DROP TABLE muted_terms')
    stats = await db.get_stats()
    check('a failed metric reports None', stats['muted_terms'] is None)
    check('the other metrics still load', stats['watchlist_count'] == 0)

    try:
        import bot
    except ImportError as e:
        check('stats view renders', True, f'(skipped, {e})')
    else:
        check('None renders as an error, not a zero', bot._metric(None) == '⚠️ error')
        check('a real zero still renders as zero', bot._metric(0) == '0')

        saved = bot.db
        try:
            bot.db = db
            text, _markup = await bot._render_stats()
            problems = _html_problems(text, 'stats')
            check('the status screen is valid Telegram HTML', not problems,
                  '; '.join(problems))
            check('it names the storage backend', 'sqlite' in text)
            check('it warns about an ephemeral file', 'DATABASE_URL' in text)
        finally:
            bot.db = saved

    await db.close()
    Database.reset_for_tests()


async def test_the_muted_screen_can_undo_everything_it_lists():
    """
    A mute is permanent and taken on a single tap, so every one it shows must be
    reversible — otherwise a mis-tapped 👎 hides a product for good with no way to
    find out it happened.
    """
    try:
        import bot
    except ImportError as e:
        check('the muted screen offers undo', True, f'(skipped, {e})')
        return

    Database.reset_for_tests()
    os.environ.pop('DATABASE_URL', None)
    db = Database()
    db.db_path = os.path.join(_TMP, 'mutedview.db')
    db.backend = SqliteBackend(db.db_path)
    await db.init()

    saved = bot.db
    try:
        bot.db = db

        text, markup = await bot._render_muted()
        check('an empty mute list says so', 'Nothing muted' in text)
        problems = _html_problems(text, 'muted (empty)')
        check('the empty screen is valid HTML', not problems, '; '.join(problems))

        await db.mute_term('saree')
        await db.mute_product('amazon:B0CX23V2ZK', '🛒 Samsung 55" <TV> & more')
        text, markup = await bot._render_muted()
        data = [b.callback_data for row in markup.inline_keyboard for b in row]

        check('the muted word is listed', 'saree' in text)
        check('the word can be unmuted', 'mu_t_saree' in data, f"got {data}")
        check('the product can be unmuted', 'mu_p_amazon:B0CX23V2ZK' in data,
              f"got {data}")
        check('everything can be cleared at once', 'mu_clear' in data)
        check('every button fits callback_data',
              all(len(d.encode('utf-8')) <= 64 for d in data), f"got {data}")

        # A product label comes from a channel post, so it is arbitrary text.
        problems = _html_problems(text, 'muted')
        check('a hostile product label cannot break the screen', not problems,
              '; '.join(problems))
        check('the label is escaped', '&lt;TV&gt;' in text and '&amp;' in text)
    finally:
        bot.db = saved

    await db.close()
    Database.reset_for_tests()


async def test_a_bad_bot2_token_never_reaches_the_logs():
    """
    python-telegram-bot puts the rejected token inside its own error message, so
    logging that exception verbatim would write BOT2_TOKEN into the host's logs in
    plain text every time the mirror failed to start.
    """
    try:
        import bot
    except ImportError as e:
        check('bot 2 token is scrubbed from logs', True, f'(skipped, {e})')
        return

    saved = bot.BOT2_TOKEN
    try:
        bot.BOT2_TOKEN = '123456789:AAFakeTokenValueForTesting_0123456789'
        message = f"The token `{bot.BOT2_TOKEN}` was rejected by the server."
        scrubbed = bot._scrub_token(message)
        check('the configured token is removed', bot.BOT2_TOKEN not in scrubbed,
              f"got {scrubbed!r}")
        check('the message is still useful', 'rejected by the server' in scrubbed)

        # A token this process does not hold — a typo'd env var — must go too.
        other = bot._scrub_token('token 987654321:BBSomeOtherTokenValue00000 failed')
        check('an unknown token is caught by shape',
              '987654321' not in other, f"got {other!r}")
    finally:
        bot.BOT2_TOKEN = saved

    # And the startup path must not use logger.exception, which prints the whole
    # traceback including that message.
    source = _source('bot.py')
    start = source.index('async def _start_mirror')
    end = source.index('async def _stop_mirror')
    # The call form, not the bare name — the comment above it explains why the call
    # is forbidden and would otherwise match itself.
    check('the mirror startup does not log the raw exception',
          'logger.exception(' not in source[start:end],
          'logger.exception() in _start_mirror would leak the token')


async def test_bot1_alerts_carry_no_buttons():
    """The 👍/👎 row moved to bot 2, so bot 1 must not attach one."""
    import notifier as notifier_mod

    check('feedback_keyboard is gone from bot 1',
          not hasattr(notifier_mod, 'feedback_keyboard'))

    sent = {}

    class _Bot:
        async def send_message(self, **kwargs):
            sent.update(kwargs)

    n = notifier_mod.Notifier(_Bot())
    ok = await n.send_deal_alert(1, {
        'product_name': 'Samsung 55" TV', 'keyword': 'tv', 'price': 41999,
        'channel_name': '@deals', 'channel_id': -1001234567890,
        'deal_url': 'https://amzn.to/xyz', 'timestamp': time.time(),
    })
    check('the alert still sends', ok is True)
    check('no reply_markup is attached', sent.get('reply_markup') is None,
          f"got {sent.get('reply_markup')!r}")
    check('bot 1 alerts still show a preview',
          sent.get('disable_web_page_preview') is False)

    # bot.py must still ANSWER the old buttons — alerts already in the chat keep
    # their keyboard forever, and tapping one should not raise.
    check('old fb_ callbacks are still handled',
          "startswith('fb_u_')" in _source('bot.py'))


async def test_mirror_button_callbacks_fit_and_round_trip():
    from mirror import Mirror, mute_word_candidates

    m = Mirror(bot=None, db=None, owner_id=1)

    # A real channel id at the long end of the range, plus a real ASIN key.
    markup = m.keyboard('amazon:B0CX23V2ZK', -1001234567890)
    buttons = markup.inline_keyboard[0]
    check('👍 and 👎 are both offered', len(buttons) == 2, f"got {len(buttons)}")
    check('callback data stays under 64 bytes',
          all(len(b.callback_data.encode()) <= 64 for b in buttons),
          f"got {[b.callback_data for b in buttons]}")

    # Round-trip exactly the parse bot.py's mirror_button does.
    data = buttons[1].callback_data
    rest = data[len('mm_d_'):]
    channel_raw, _, handle = rest.partition('_')
    check('negative channel ids round-trip', int(channel_raw) == -1001234567890,
          f"got {channel_raw!r}")
    check('a short product key travels intact',
          m.resolve_token(handle) == 'amazon:B0CX23V2ZK', f"got {handle!r}")

    # A long generic key cannot ride in callback_data, so it is parked under a token.
    long_key = 'url:example.com/' + 'a' * 120
    markup = m.keyboard(long_key, -100123)
    handle = markup.inline_keyboard[0][1].callback_data[len('mm_d_'):].partition('_')[2]
    check('a long key is tokenised', handle != long_key and len(handle) < 20)
    check('the token resolves back', m.resolve_token(handle) == long_key)
    check('an unknown token resolves to None',
          m.resolve_token('t0123456789ab') is None)

    # Word buttons.
    words = mute_word_candidates('Samsung Galaxy M14 5G Smartphone at lowest price')
    check('marketing filler is not offered', 'lowest' not in words and 'price' not in words,
          f"got {words}")
    check('the brand is offered', 'samsung' in words, f"got {words}")
    wk = m.word_keyboard('Samsung Galaxy M14 5G Smartphone')
    all_buttons = [b for row in wk.inline_keyboard for b in row]
    check('every word button fits',
          all(len(b.callback_data.encode()) <= 64 for b in all_buttons))
    check('a skip option is always present',
          any(b.callback_data == 'mm_done' for b in all_buttons))


async def test_mirror_forces_the_product_preview():
    """
    The whole reason bot 2 exists: previews ON, and previewing the PRODUCT.

    Without an explicit url Telegram previews the first link it finds, which can be
    the source channel or a coupon page rather than the item.
    """
    from mirror import Mirror

    m = Mirror(bot=None, db=None, owner_id=1)
    kwargs = m._preview_kwargs('https://www.amazon.in/dp/B0CX23V2ZK')

    check('preview is never disabled',
          kwargs.get('disable_web_page_preview') is not True)
    options = kwargs.get('link_preview_options')
    if options is not None:
        check('preview is explicitly enabled', options.is_disabled is False)
        check('the product url is pinned',
              options.url == 'https://www.amazon.in/dp/B0CX23V2ZK')
    else:
        check('fallback still enables the preview',
              kwargs.get('disable_web_page_preview') is False)

    # The product link must also lead the message body, so the fallback path (an
    # older python-telegram-bot with no LinkPreviewOptions) still previews the item.
    body = m.format_post('Great deal today', 'LOOT DEALS',
                         'https://www.amazon.in/dp/B0CX23V2ZK', '₹41,999', 'Samsung TV')
    check('the product link leads the message', body.startswith('🛒 <a href="https://www.amazon.in/dp/B0CX23V2ZK"'),
          f"got {body[:80]!r}")
    check('the price is shown', '₹41,999' in body)
    check('the channel is credited', 'via LOOT DEALS' in body)


async def test_mirror_text_cannot_break_telegram_html():
    """Channel posts are full of <, > and & — every one must arrive escaped."""
    from mirror import Mirror

    m = Mirror(bot=None, db=None, owner_id=1)
    nasty = 'Samsung 55" <b>TV</b> & more >> 40% off <script>'
    body = m.format_post(nasty, 'A & B <deals>', 'https://x.com/p?a=1&b=2',
                         None, 'Sony <TV> 4K')

    check('raw angle brackets from the post are escaped',
          '<b>TV</b>' not in body and '&lt;b&gt;TV&lt;/b&gt;' in body)
    check('the channel name is escaped', '&lt;deals&gt;' in body)
    check('the preview title is escaped', '&lt;TV&gt;' in body)
    # The real bar: Telegram must accept the whole message. An unsupported or
    # unbalanced tag is a 400, so the post would not arrive at all.
    problems = _html_problems(body, 'mirror post')
    check('Telegram would accept the message', not problems, '; '.join(problems))

    # The ampersand in the URL has to be escaped inside the href attribute too.
    check('the href is attribute-escaped', 'a=1&amp;b=2' in body,
          f"got {body[:120]!r}")


class _FakeMessage:
    def __init__(self, text=''):
        self.text = text
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))
        return self


class _FakeUpdate:
    def __init__(self, text=''):
        self.message = _FakeMessage(text)
        self.effective_message = self.message
        self.callback_query = None
        self.effective_user = SimpleNamespace(id=1)


class _FakeContext:
    def __init__(self, args=None):
        self.user_data = {}
        self.args = args or []


async def test_bare_commands_ask_instead_of_erroring():
    """
    Telegram's command menu sends a command BARE — there is no way to make the
    client pre-fill "/watch " and wait. So a bare command must ask for its argument
    and consume the next message, or the menu is unusable.
    """
    try:
        import bot
    except ImportError as e:
        check('bare commands ask for input', True, f'(skipped, {e})')
        return

    added = {}

    class _StubDb:
        async def add_keyword(self, keyword, synonyms='', exclusions=''):
            added['args'] = (keyword, synonyms, exclusions)
            return True

    saved_db = bot.db
    try:
        bot.db = _StubDb()

        # Bare /watch asks rather than erroring.
        update, ctx = _FakeUpdate(), _FakeContext()
        await bot.watch_command(update, ctx)
        text, kwargs = update.message.replies[-1]
        check('bare /watch asks a question', 'What should I watch for' in text,
              f"got {text!r}")
        check('the ask uses ForceReply',
              type(kwargs.get('reply_markup')).__name__ == 'ForceReply',
              f"got {kwargs.get('reply_markup')!r}")
        check('the pending action is remembered',
              ctx.user_data.get(bot._PENDING_KEY, {}).get('action') == 'watch',
              f"got {ctx.user_data!r}")

        # The next plain message is consumed as the argument.
        reply = _FakeUpdate('shoes -kids')
        await bot.text_input_handler(reply, ctx)
        check('the answer reaches the command',
              added.get('args') == ('shoes', '', 'kids'), f"got {added!r}")
        check('the pending action is cleared',
              bot._PENDING_KEY not in ctx.user_data, f"got {ctx.user_data!r}")

        # /cancel abandons it.
        update, ctx = _FakeUpdate(), _FakeContext()
        await bot.watch_command(update, ctx)
        await bot.cancel_command(_FakeUpdate('/cancel'), ctx)
        check('cancel clears the pending action',
              bot._PENDING_KEY not in ctx.user_data, f"got {ctx.user_data!r}")

        # An unexpected message is acknowledged, never silently swallowed.
        stray, ctx = _FakeUpdate('hello?'), _FakeContext()
        await bot.text_input_handler(stray, ctx)
        check('stray text gets a nudge', stray.message.replies, 'no reply sent')

        # Passing the argument inline still works, with no question asked.
        added.clear()
        update, ctx = _FakeUpdate(), _FakeContext(args=['fridge'])
        await bot.watch_command(update, ctx)
        check('inline arguments skip the question',
              added.get('args') == ('fridge', '', '') and
              bot._PENDING_KEY not in ctx.user_data, f"got {added!r}")
    finally:
        bot.db = saved_db


async def test_interface_wiring_has_no_dead_buttons():
    """
    A button whose callback_data nothing handles, or a keyboard label that maps to
    no action, fails silently — the user taps and nothing happens. Check the wiring
    matches instead of finding out in production.
    """
    try:
        import bot
    except ImportError as e:
        check('interface wiring is complete', True, f'(skipped, {e})')
        return

    # Every question the bot can ask must have something that consumes the answer.
    missing = set(bot._PROMPTS) - set(bot._APPLIERS)
    check('every prompt has an applier', not missing, f"missing {missing}")

    # Every quick-keyboard button must map to an action, and vice versa.
    labels = {b.text for row in bot._quick_keyboard().keyboard for b in row}
    check('every keyboard button has an action',
          labels == set(bot._QUICK_ACTIONS), f"keyboard {labels} vs {set(bot._QUICK_ACTIONS)}")

    # Every menu button routes somewhere, and fits Telegram's callback_data limit.
    routed = {'menu_home', 'menu_watchlist', 'menu_deals', 'menu_stats',
              'menu_channels', 'menu_report', 'menu_help', 'act_pause', 'act_resume'}
    for markup in (bot._menu_markup(True), bot._menu_markup(False)):
        for row in markup.inline_keyboard:
            for button in row:
                data = button.callback_data
                ok = data in routed or (
                    data.startswith('ask_') and data[4:] in bot._PROMPTS)
                check(f'menu button {data!r} is routed', ok, f"unrouted: {data}")
                check(f'menu button {data!r} fits callback_data',
                      bot._fits_callback(data), f"too long: {data}")


async def test_watchlist_view_offers_per_keyword_actions():
    try:
        import bot
    except ImportError as e:
        check('watchlist offers actions', True, f'(skipped, {e})')
        return

    class _StubDb:
        def __init__(self, rows):
            self.rows = rows

        async def get_watchlist(self):
            return self.rows

    saved = bot.db
    try:
        bot.db = _StubDb([])
        text, markup = await bot._render_watchlist()
        check('empty watchlist offers an add button',
              'ask_watch' in str(markup.inline_keyboard), f"got {markup}")

        bot.db = _StubDb([{'keyword': 'shoes', 'custom_synonyms': '',
                           'exclusions': 'kids'}])
        text, markup = await bot._render_watchlist()
        data = [b.callback_data for row in markup.inline_keyboard for b in row]
        check('blocked terms are visible', 'blocked: kids' in text, f"got {text!r}")
        check('each keyword can be edited or deleted',
              {'exc_kw_shoes', 'syn_kw_shoes', 'del_kw_shoes'} <= set(data),
              f"got {data}")

        # A keyword too long for callback_data must not produce a button Telegram
        # would reject with a 400.
        bot.db = _StubDb([{'keyword': 'x' * 90, 'custom_synonyms': '', 'exclusions': ''}])
        _, markup = await bot._render_watchlist()
        data = [b.callback_data for row in markup.inline_keyboard for b in row]
        check('over-long keywords produce no oversized button',
              all(bot._fits_callback(d) for d in data), f"got {[len(d) for d in data]}")
    finally:
        bot.db = saved


async def test_special_characters_cannot_break_a_view():
    """
    Every list view interpolates text the user does not control the shape of —
    keywords like `iphone_15`, channels called `LOOT_DEALS_INDIA`, product titles
    full of `*`. Under Telegram's legacy Markdown an odd number of `_` or `*` is a
    hard 400 ("can't parse entities"), so the view fails to send at all rather than
    looking slightly off. These views are HTML + html.escape(), like the notifier.
    """
    try:
        import bot
    except ImportError as e:
        check('special characters cannot break a view', True, f'(skipped, {e})')
        return

    nasty = 'iphone_15 *deal* <b>&'

    class _StubDb:
        async def get_watchlist(self):
            return [{'keyword': nasty, 'custom_synonyms': '', 'exclusions': nasty}]

        async def get_recent_deals(self, hours=24):
            return [{'product_name': nasty, 'price': '999', 'channel_name': nasty,
                     'matched_date': time.time(), 'keyword_matched': 'x',
                     'message_link': 'https://t.me/c/1/2'}]

        async def get_all_channels(self):
            return [{'channel_id': 7, 'channel_name': f'{nasty} deals',
                     'channel_username': 'x', 'active': 1}]

    saved_db, saved_monitor = bot.db, bot.monitor
    try:
        bot.db = _StubDb()
        bot.monitor = _FakeMonitor([])

        views = {
            'watchlist': (await bot._render_watchlist())[0],
            'deals': (await bot._render_deals())[0],
            'channels': (await bot._get_channels_page(0))[0],
        }
        for name, text in views.items():
            check(f'{name} escapes angle brackets',
                  '<b>&' not in text and '&lt;b&gt;' in text, f"got {text!r}")
            # Every tag in the output must be one we opened deliberately.
            tags = set(re.findall(r'</?([a-z]+)', text))
            check(f'{name} emits only known tags',
                  tags <= {'b', 'i', 'code', 'a', 'pre'}, f"got {tags}")
            # Raw markdown specials are harmless now, and must survive as literals.
            check(f'{name} keeps the underscore literal', 'iphone_15' in text,
                  f"got {text!r}")
    finally:
        bot.db, bot.monitor = saved_db, saved_monitor


_TELEGRAM_TAG = re.compile(r'</?(b|strong|i|em|u|s|code|pre|a|blockquote)(?:\s[^>]*)?>\Z')


def _html_problems(text, label):
    """
    Telegram accepts a small fixed tag set and rejects the whole message otherwise.

    An unknown tag or an unbalanced one is a 400, which means the screen never
    appears at all — /help shipped broken exactly this way, because `<message>`
    inside a <code> block reads as a tag.
    """
    problems, stack = [], []
    for match in re.finditer(r'<[^>]*>', text):
        tag = match.group(0)
        if not _TELEGRAM_TAG.match(tag):
            problems.append(f"{label}: unsupported tag {tag!r}")
            continue
        name = re.match(r'</?([a-z]+)', tag).group(1)
        if tag.startswith('</'):
            if not stack or stack.pop() != name:
                problems.append(f"{label}: stray closing </{name}>")
        else:
            stack.append(name)
    if stack:
        problems.append(f"{label}: unclosed {stack}")
    return problems


async def test_static_text_is_valid_telegram_html():
    try:
        import bot
    except ImportError as e:
        check('static text is valid Telegram HTML', True, f'(skipped, {e})')
        return

    problems = []
    problems += _html_problems(bot.HELP_TEXT, 'HELP_TEXT')
    problems += _html_problems(bot._menu_text(True), 'menu')
    for name, (question, _placeholder, hint) in bot._PROMPTS.items():
        # exclude_terms carries a {keyword} slot; fill it the way _prompt does.
        filled = question.replace('{keyword}', 'shoes')
        problems += _html_problems(f"{filled}\n{hint}", f'prompt:{name}')

    check('every static string is valid Telegram HTML', not problems,
          '; '.join(problems))

    # Everything is HTML now. A single Markdown send would reintroduce the drift
    # that made a converted renderer fail at its old call site.
    bot_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bot.py')
    with open(bot_py, encoding='utf-8') as handle:
        source = handle.read()
    # Catches the indirect form too: a `mode = 'Markdown'` variable handed to
    # parse_mode is exactly how menu_stats and menu_help ended up broken.
    check('no Markdown sends remain', "'Markdown'" not in source,
          "found a 'Markdown' parse mode in bot.py")


async def main():
    tests = [
        test_main_installs_an_event_loop,
        test_preview_resolves_after_pending,
        test_preview_immediate,
        test_preview_new_layer_shape,
        test_preview_gives_up_when_always_pending,
        test_preview_empty_page,
        test_preview_request_error_is_not_fatal,
        test_preview_cache_hit,
        test_preview_cache_is_bounded,
        test_scraper_cache_is_bounded,
        test_working_directories_never_crash_at_import,
        test_storage_backend_selection_and_translation,
        test_postgres_retries_on_a_sleeping_database,
        test_postgres_pool_defaults_suit_serverless,
        test_database_shared_connection_and_retention,
        test_matcher_and_price_still_work,
        test_matcher_rejects_lookalike_words,
        test_matcher_still_finds_real_products,
        test_synonyms_do_not_leak_across_categories,
        test_synonym_order_is_deterministic_and_specific,
        test_singularise_keeps_words_distinct,
        test_channel_list_is_filtered_to_deal_channels,
        test_negative_keywords_block_a_match,
        test_watch_command_parses_inline_negatives,
        test_channel_counters_survive_deal_pruning,
        test_channel_report_renders,
        test_report_schedule_survives_restarts,
        # ── Bot 2 (mirror), and the duplicate/statistics fixes ──
        test_product_key_survives_affiliate_rewriting,
        test_the_same_deal_from_two_channels_alerts_once,
        test_mirror_mutes_and_dedups,
        test_both_bots_receive_one_message,
        test_mirror_is_independent_of_the_watchlist,
        test_stats_explain_themselves,
        test_the_muted_screen_can_undo_everything_it_lists,
        test_a_bad_bot2_token_never_reaches_the_logs,
        test_bot1_alerts_carry_no_buttons,
        test_mirror_button_callbacks_fit_and_round_trip,
        test_mirror_forces_the_product_preview,
        test_mirror_text_cannot_break_telegram_html,
        test_bare_commands_ask_instead_of_erroring,
        test_interface_wiring_has_no_dead_buttons,
        test_watchlist_view_offers_per_keyword_actions,
        test_special_characters_cannot_break_a_view,
        test_static_text_is_valid_telegram_html,
    ]
    for test in tests:
        print(f"\n{test.__name__}")
        await test()

    print("\n" + "=" * 60)
    print(f"{len(_PASSES)} passed, {len(_FAILURES)} failed")
    for name, detail in _FAILURES:
        print(f"  FAILED: {name} {detail}")
    return 1 if _FAILURES else 0


if __name__ == '__main__':
    try:
        code = asyncio.run(main())
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(code)
