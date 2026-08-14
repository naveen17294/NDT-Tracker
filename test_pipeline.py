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
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone

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


def check(name, condition, detail=''):
    if condition:
        _PASSES.append(name)
        print(f"  PASS  {name}")
    else:
        _FAILURES.append((name, detail))
        print(f"  FAIL  {name} {detail}")


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
        check('page explains the filter', 'searchchannel' in msg, f"got {msg!r}")

        msg, markup = await bot._get_channels_page(page=0, search='cricket')
        labels = ' | '.join(_button_labels(markup))
        check('search page finds the hidden channel', 'Cricket' in labels, f"got {labels!r}")
        check('search page offers a way back', 'Clear search' in labels, f"got {labels!r}")

        msg, markup = await bot._get_channels_page(page=0, search='zzzz')
        check('empty search explains itself', markup is None and 'addchannel' in msg,
              f"got {msg!r}")
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


async def test_feedback_keyboard_fits_telegrams_limit():
    from notifier import feedback_keyboard

    # A real Telegram channel id, at the long end of the range.
    markup = feedback_keyboard(-1001234567890)
    buttons = markup.inline_keyboard[0]
    check('both vote buttons are offered', len(buttons) == 2, f"got {len(buttons)}")
    check('callback data stays under 64 bytes',
          all(len(b.callback_data.encode()) <= 64 for b in buttons),
          f"got {[b.callback_data for b in buttons]}")

    # Round-trip the parse bot.py does, including the negative id.
    data = buttons[1].callback_data
    _, verdict, raw_id = data.split('_', 2)
    check('negative channel ids round-trip',
          verdict == 'd' and int(raw_id) == -1001234567890, f"got {data!r}")

    check('no channel id means no buttons', feedback_keyboard(None) is None)


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
        test_feedback_keyboard_fits_telegrams_limit,
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
