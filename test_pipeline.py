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


async def test_database_shared_connection_and_retention():
    import time as _time

    db = Database()
    check('Database is a singleton', db is Database())
    await db.init()

    conn_first = await db._ensure_conn()
    await db.add_keyword('fridge', 'samsung')
    conn_second = await db._ensure_conn()
    check('DB reuses one connection', conn_first is conn_second)

    kws = await db.get_all_keywords()
    check('keyword round-trips', ('fridge', 'samsung') in kws, f'got {kws}')

    await db.save_deal('hash-fresh', 'fridge', 'Samsung Fridge', '24999', '@deals', '')
    check('fresh deal is seen', await db.is_deal_seen('hash-fresh') is True)

    # Backdate a row well past the retention window and prove the sweep removes it.
    async with db._session() as conn:
        await conn.execute(
            'INSERT INTO matched_deals (deal_hash, keyword_matched, matched_date) VALUES (?, ?, ?)',
            ('hash-ancient', 'fridge', _time.time() - (60 * 86400))
        )
        await conn.commit()

    deleted = await db.cleanup_old_deals(days=7)
    check('retention sweep deletes old rows', deleted == 1, f'deleted={deleted}')
    check('retention sweep keeps fresh rows', await db.is_deal_seen('hash-fresh') is True)

    await db.close()


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
        test_database_shared_connection_and_retention,
        test_matcher_and_price_still_work,
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
