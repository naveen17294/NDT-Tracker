"""
Bot 2 — the channel mirror.

Bot 1 is a filter: it sends only what matches your watchlist. Bot 2 is the inverse,
a feed that sends everything from the same tracked channels EXCEPT what you have
muted. Between them you get the deals you asked for by name, and the chance to see
the ones you didn't know to ask for.

Why it exists at all: a Telegram channel is a poor place to shop. Posts arrive as
walls of emoji with a bare shortened link, no preview card, and no way to tell a
₹400 phone case from a ₹40,000 phone without opening every one. The mirror re-sends
each post into a private chat with the link preview forced ON and pinned to the
product URL, so the image, title and price render inline. That is the entire reason
for bot 2, which is why the preview is not configurable.

Three things keep the feed from becoming noise:

* Mutes. 👎 on a post mutes that product by canonical key (so it stays muted when a
  channel reposts it under a new affiliate tag), then offers words from its title to
  mute a whole brand or category in one more tap.
* Cross-channel dedup. Both tracked channels post the same product minutes apart.
  Keying on the product identity rather than the link or the text means you see it
  once — see product_key.py for why the link itself cannot do this job.
* Rate limiting. Telegram starts refusing at roughly one message per second to a
  single chat, and a deal channel can burst well past that.
"""

import asyncio
import hashlib
import html
import logging
import re
import time
from collections import OrderedDict

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from config import (
    MIRROR_MAX_TEXT,
    MIRROR_REQUIRE_LINK,
    MIRROR_SEND_INTERVAL,
    WATCHLIST_CACHE_TTL,
)
from utils import truncate

logger = logging.getLogger(__name__)

# Bot API 7.0 / python-telegram-bot 21 let a message say WHICH url to preview. That
# matters here: without it Telegram previews the first link in the message, which is
# not necessarily the product. Older installs fall back to plain preview-on.
try:
    from telegram import LinkPreviewOptions
    _HAS_PREVIEW_OPTIONS = True
except ImportError:  # pragma: no cover - depends on the installed PTB version
    LinkPreviewOptions = None
    _HAS_PREVIEW_OPTIONS = False

# Words never worth offering as a mute candidate — they would silence everything.
_STOPWORDS = {
    'the', 'and', 'for', 'with', 'from', 'this', 'that', 'you', 'your', 'all',
    'new', 'now', 'off', 'out', 'get', 'buy', 'best', 'only', 'just', 'deal',
    'deals', 'loot', 'sale', 'price', 'offer', 'offers', 'free', 'link', 'click',
    'here', 'today', 'limited', 'stock', 'live', 'lowest', 'ever', 'big', 'flat',
    'upto', 'rs', 'inr', 'mrp', 'discount', 'coupon', 'apply', 'use', 'code',
    'shop', 'online', 'india', 'amazon', 'flipkart', 'myntra', 'ajio', 'meesho',
    'https', 'http', 'www', 'com', 'more', 'per', 'pack', 'set', 'combo',
}

_WORD = re.compile(r'[a-z][a-z0-9]{2,}')

# Telegram caps callback_data at 64 bytes. Everything below is built to fit, and
# _fits() is the guard for the cases that cannot be predicted (a long generic URL
# key, an unusually long word).
_CALLBACK_LIMIT = 64


def _fits(data):
    return len(data.encode('utf-8')) <= _CALLBACK_LIMIT


def mute_word_candidates(title, limit=4):
    """
    Words from a product title worth offering as a category mute.

    Deliberately dumb — lowercase alphanumeric words, stopwords and marketing filler
    removed, first occurrence order kept because a product title leads with its
    brand and model. No stemming: muting 'saree' should not silently also mute
    'sarees', because the user picked a specific word and deserves to get it.
    """
    if not title:
        return []
    seen = []
    for word in _WORD.findall(title.lower()):
        if word in _STOPWORDS or len(word) > 24:
            continue
        if word not in seen:
            seen.append(word)
        if len(seen) >= limit:
            break
    return seen


class Mirror:
    """
    Formats and sends the mirror feed through bot 2.

    Holds no reference to bot 1. The only things shared with the rest of the process
    are the database and the Telethon monitor's LinkScraper (for resolving short
    links), so a failure in here can slow the mirror down but cannot stop alerts.
    """

    def __init__(self, bot, db, owner_id, scraper=None):
        self.bot = bot                  # telegram.Bot built on BOT2_TOKEN
        self.db = db
        self.owner_id = owner_id
        self.scraper = scraper
        self.paused = False

        # Muted words are consulted on every mirrored post, so they are cached the
        # same way the watchlist is.
        self._terms = None
        self._terms_at = 0.0
        self._term_patterns = {}

        # Serialises sends and spaces them out; Telegram 429s a burst to one chat.
        self._send_lock = asyncio.Lock()
        self._last_send = 0.0

        # Canonical keys too long for callback_data, parked under a short token.
        # Bounded and in-memory on purpose: this is a convenience for buttons that
        # are about to be tapped, not a record of what was sent. A token lost to a
        # restart degrades to "mute a word instead", which the message still offers.
        self._tokens = OrderedDict()

        self.sent = 0
        self.skipped_muted = 0
        self.skipped_duplicate = 0

    # ══════════════════════════════════════
    #  Mute list
    # ══════════════════════════════════════

    def invalidate_mutes(self):
        """Drop the cached mute list so the next post re-reads it."""
        self._terms = None
        self._terms_at = 0.0

    async def muted_terms(self):
        now = time.time()
        if self._terms is not None and (now - self._terms_at) < WATCHLIST_CACHE_TTL:
            return self._terms
        try:
            self._terms = await self.db.get_muted_terms()
        except Exception as e:
            # A mute list we cannot read must not stop the feed; it fails open.
            logger.error(f"Could not read muted terms: {e}")
            self._terms = self._terms or []
        self._terms_at = now
        return self._terms

    def _pattern(self, term):
        pattern = self._term_patterns.get(term)
        if pattern is None:
            # Whole words only, so muting 'tv' cannot silence 'tvs' or 'advert'.
            pattern = re.compile(r'(?<!\w)' + re.escape(term) + r'(?!\w)',
                                 re.IGNORECASE)
            self._term_patterns[term] = pattern
        return pattern

    async def muted_by_term(self, text):
        """The first muted word found in the text, or None."""
        if not text:
            return None
        for term in await self.muted_terms():
            if self._pattern(term).search(text):
                return term
        return None

    # ══════════════════════════════════════
    #  Buttons
    # ══════════════════════════════════════

    def _token_for(self, product_key, channel_id=0):
        """
        Short handle for a product key that will not fit in callback_data.

        Returns the key itself when it fits, which covers every real product id
        (`amazon:B0CX23V2ZK` is 18 bytes) and means those buttons keep working after
        a restart. Only long generic URL keys need the token map.

        The budget check includes the channel id, because the 👎 button carries both
        — it mutes the product AND counts against the channel it came from.
        """
        if _fits(f'mm_d_{channel_id}_{product_key}'):
            return product_key
        token = 't' + hashlib.md5(product_key.encode()).hexdigest()[:12]
        self._tokens[token] = product_key
        self._tokens.move_to_end(token)
        while len(self._tokens) > 2000:
            self._tokens.popitem(last=False)
        return token

    def resolve_token(self, token):
        """Product key for a token from callback data. None if it has been lost."""
        if not token:
            return None
        if token in self._tokens:
            self._tokens.move_to_end(token)
            return self._tokens[token]
        # Not a token at all — the key was short enough to travel intact.
        if not token.startswith('t') or ':' in token:
            return token
        return None

    def keyboard(self, product_key, channel_id):
        """
        👍 / 👎 for a fresh mirror post.

        Both buttons carry the channel id so a vote can be credited to the channel
        without storing anything about the post; 👎 carries the product identity as
        well, because it has to mute the item too. bot.py's mirror_button parses
        'mm_d_<channel_id>_<handle>' — keep the two in step.
        """
        row = []
        # 0 stands in for "channel unknown", and the handler skips the channel vote
        # rather than inventing a counter row for a channel that does not exist.
        channel = 0 if channel_id is None else channel_id

        if channel_id is not None:
            row.append(InlineKeyboardButton('👍 Good', callback_data=f'mm_u_{channel}'))
        if product_key:
            handle = self._token_for(product_key, channel)
            data = f'mm_d_{channel}_{handle}'
            if _fits(data):
                row.append(InlineKeyboardButton('👎 Not interested', callback_data=data))
        if not row:
            return None
        return InlineKeyboardMarkup([row])

    def word_keyboard(self, title):
        """
        Offered after a 👎 — one tap per word to mute a whole brand or category.

        This is the second half of "mute the product, then optionally mute a word":
        the product is already muted by the time these appear, so every button here
        is additive and skipping them is a valid answer.
        """
        buttons = []
        for word in mute_word_candidates(title):
            data = f'mw_{word}'
            if _fits(data):
                buttons.append(InlineKeyboardButton(f'🔇 {word}', callback_data=data))
        rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
        rows.append([InlineKeyboardButton('✓ Just this product', callback_data='mm_done')])
        return InlineKeyboardMarkup(rows)

    # ══════════════════════════════════════
    #  Formatting
    # ══════════════════════════════════════

    def format_post(self, text, channel_name, product_url, price_str=None,
                    preview_title=None):
        """
        The mirrored post.

        Kept close to the original — this is a mirror, not a rewrite. The product
        link leads so it is the first thing tappable, the channel's own words follow
        so nothing is lost in translation, and the source channel is a footer rather
        than a headline.
        """
        lines = []

        if product_url:
            label = html.escape(truncate(preview_title, 60)) if preview_title else 'Open product'
            lines.append(f'🛒 <a href="{html.escape(product_url, quote=True)}">{label}</a>')

        if price_str:
            lines.append(f'💰 <b>{html.escape(price_str)}</b>')

        body = (text or '').strip()
        if body:
            clipped = body[:MIRROR_MAX_TEXT]
            if len(body) > MIRROR_MAX_TEXT:
                clipped += '…'
            lines.append(f'\n{html.escape(clipped)}')

        if channel_name:
            lines.append(f'\n<i>via {html.escape(channel_name)}</i>')

        return '\n'.join(lines) if lines else ''

    # ══════════════════════════════════════
    #  Sending
    # ══════════════════════════════════════

    async def _throttled_send(self, **kwargs):
        """One send at a time, spaced by MIRROR_SEND_INTERVAL, with one 429 retry."""
        async with self._send_lock:
            gap = time.time() - self._last_send
            if gap < MIRROR_SEND_INTERVAL:
                await asyncio.sleep(MIRROR_SEND_INTERVAL - gap)
            try:
                result = await self.bot.send_message(**kwargs)
            except Exception as e:
                retry_after = getattr(e, 'retry_after', None)
                if retry_after is None:
                    raise
                # Telegram told us exactly how long to wait; obeying it once is
                # cheaper than dropping the post.
                logger.warning(f"Mirror rate-limited, waiting {retry_after}s")
                await asyncio.sleep(float(retry_after) + 0.5)
                result = await self.bot.send_message(**kwargs)
            self._last_send = time.time()
            return result

    def _preview_kwargs(self, product_url):
        """
        Force the preview, and force it to be the PRODUCT's preview.

        Without an explicit url Telegram previews the first link in the message,
        which can be the source-channel link or a coupon page. With no
        LinkPreviewOptions available at all, fall back to plain preview-on and rely
        on the product link leading the message.
        """
        if _HAS_PREVIEW_OPTIONS and product_url:
            return {'link_preview_options': LinkPreviewOptions(
                is_disabled=False, url=product_url, prefer_large_media=True,
                show_above_text=True)}
        if _HAS_PREVIEW_OPTIONS:
            return {'link_preview_options': LinkPreviewOptions(is_disabled=False)}
        return {'disable_web_page_preview': False}

    async def handle(self, text, urls, channel_id, channel_name,
                     price_str=None, preview_title=None, product_keys=None):
        """
        Mirror one channel post. Returns True only when a message actually went out.

        Order matters: the cheap local checks run before anything touches the
        database, and the dedup row is only written after a successful send, so a
        failed send does not silently suppress the next copy of the post.
        """
        if self.paused:
            return False

        urls = list(urls or [])
        if MIRROR_REQUIRE_LINK and not urls:
            return False

        body_for_matching = '\n'.join(filter(None, [text, preview_title]))

        muted_term = await self.muted_by_term(body_for_matching)
        if muted_term:
            self.skipped_muted += 1
            logger.info(f"🔇 Mirror skipped (muted word '{muted_term}')")
            return False

        keys = list(product_keys or [])
        if keys:
            try:
                if await self.db.muted_product_keys(keys):
                    self.skipped_muted += 1
                    logger.info("🔇 Mirror skipped (muted product)")
                    return False
            except Exception as e:
                logger.error(f"Could not check muted products: {e}")

        # Identity for dedup: the product if we could name it, otherwise the post's
        # own text. Falling back to a text hash keeps two channels posting the same
        # blast from both getting through, which is the common case for a post whose
        # link we could not canonicalise.
        dedup_key = keys[0] if keys else 'txt:' + hashlib.md5(
            (body_for_matching or '').strip().lower().encode()).hexdigest()

        try:
            if await self.db.is_mirror_seen(dedup_key):
                self.skipped_duplicate += 1
                logger.info(f"🚫 Mirror skipped duplicate: {dedup_key[:40]}")
                return False
        except Exception as e:
            logger.error(f"Could not check mirror dedup: {e}")

        product_url = urls[0] if urls else None
        message = self.format_post(text, channel_name, product_url, price_str,
                                   preview_title)
        if not message:
            return False

        try:
            await self._throttled_send(
                chat_id=self.owner_id,
                text=message,
                parse_mode='HTML',
                reply_markup=self.keyboard(keys[0] if keys else None, channel_id),
                **self._preview_kwargs(product_url),
            )
        except Exception as e:
            logger.error(f"Mirror send failed: {e}")
            return False

        self.sent += 1
        try:
            await self.db.mark_mirror_seen(dedup_key)
        except Exception as e:
            logger.warning(f"Could not record mirror dedup key: {e}")
        return True
