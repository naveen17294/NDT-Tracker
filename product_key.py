"""
Canonical product identity — what makes two posts "the same deal".

The raw URL cannot answer that. Every deal channel rewrites a product link with its
own affiliate tag, so one Amazon deal posted in two channels arrives as two
different links wrapped in two different marketing blasts. Dedup that keys on the
link, or on the message text, sees two unrelated deals and alerts twice. That is
the duplicate you actually notice.

Reducing a link to the seller's own product id fixes it at the root:

    https://www.amazon.in/dp/B0CX23V2ZK?tag=chan-a-21&psc=1   -> amazon:B0CX23V2ZK
    https://amazon.in/Some-Product-Name/dp/B0CX23V2ZK/?tag=b  -> amazon:B0CX23V2ZK

Same key, so the second one is recognised as already seen no matter which channel
sent it or what it called the product. That key is also what a 👎 mutes, which is
why it has to survive reposts — muting a URL would last exactly until the channel
posted the item again with a fresh tag.

This module is deliberately pure and synchronous. Short links (amzn.to, fkrt.it)
cannot be canonicalised without following the redirect, and that network hop lives
in LinkScraper.resolve_final_url() so everything here stays trivially testable.
"""

import re
from urllib.parse import parse_qsl, urlparse

from utils import extract_urls

# ── Affiliate, session and tracking parameters ──
# Stripped before a URL is used as an identity. Everything here varies per channel
# or per click, so leaving any of it in would defeat the whole point.
_TRACKING_PARAMS = {
    # Amazon
    'tag', 'ref', 'ref_', 'linkcode', 'creative', 'creativeasin', 'ascsubtag',
    'psc', 'th', 'smid', 'linkid', 'pd_rd_i', 'pd_rd_r', 'pd_rd_w', 'pd_rd_wg',
    'pf_rd_p', 'pf_rd_r', 'content-id', 'qid', 'sr', 'crid', 'sprefix', 'keywords',
    # Flipkart
    'affid', 'affexdesc', 'affextparam1', 'affextparam2', 'lid', 'marketplace',
    'srno', 'otracker', 'otracker1', 'fm', 'iid', 'ppt', 'ppn', 'ssid', 'spid',
    'qh', 'cmpid', 'store', '_refid', 'aff_id', 'aff_sub',
    # Generic / analytics
    'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content',
    'utm_id', 'gclid', 'fbclid', 'msclkid', 'igshid', 'source', 'src',
    'clickid', 'click_id', 'subid', 'sub_id', 'partner', 'pid_aff',
}

# A 10-character Amazon ASIN. Anchored on the path segments Amazon actually uses so
# a random 10-char slug elsewhere in the URL cannot be mistaken for one.
_ASIN_PATH = re.compile(
    r'/(?:dp|gp/product|gp/aw/d|product|exec/obidos/asin)/([A-Z0-9]{10})(?:[/?]|$)',
    re.IGNORECASE,
)
_ASIN_BARE = re.compile(r'^[A-Z0-9]{10}$', re.IGNORECASE)

# Flipkart's product id appears either as the pid query parameter (authoritative,
# survives every URL rewrite) or as an itm* slug in the path.
_FLIPKART_ITM = re.compile(r'/p/(itm[a-z0-9]+)', re.IGNORECASE)

# Myntra and Ajio identify a product by a bare numeric id in the path.
_MYNTRA_ID = re.compile(r'/(\d{5,12})/buy', re.IGNORECASE)
_AJIO_ID = re.compile(r'/p/(\d{6,14})', re.IGNORECASE)
_MEESHO_ID = re.compile(r'/p/([a-z0-9]+)', re.IGNORECASE)

_NON_WORD = re.compile(r'[^a-z0-9]+')


def _host(url):
    """
    Hostname, lowercased, with a leading 'www.' removed.

    Not str.lstrip('www.') — that strips a character SET, so 'wow.com' would come
    back as 'ow.com' and every key for that host would be wrong.
    """
    try:
        host = (urlparse(url).netloc or '').lower()
    except Exception:
        return ''
    if host.startswith('www.'):
        host = host[4:]
    return host


def _query(url):
    """Query parameters as a lowercase-keyed dict, tracking junk removed."""
    try:
        pairs = parse_qsl(urlparse(url).query, keep_blank_values=False)
    except Exception:
        return {}
    return {
        key.lower(): value
        for key, value in pairs
        if key.lower() not in _TRACKING_PARAMS
    }


def _raw_query(url):
    """Query parameters with nothing stripped — for reading pid before it is removed."""
    try:
        pairs = parse_qsl(urlparse(url).query, keep_blank_values=False)
    except Exception:
        return {}
    return {key.lower(): value for key, value in pairs}


def is_shortener(url, shorteners):
    """True when the URL points at a known link shortener."""
    host = _host(url)
    if not host:
        return False
    return any(short.lower() in host for short in shorteners)


def canonical_key(url):
    """
    Reduce a product URL to a stable identity, or None when it isn't one.

    Returns a short string safe to store and to put in callback data, e.g.
    'amazon:B0CX23V2ZK', 'flipkart:itm9a8b7c6d', or 'url:croma.com/p/12345' when the
    site is unknown. None for a shortener (the redirect has to be followed first) and
    for anything that clearly isn't a product page.
    """
    if not url:
        return None

    host = _host(url)
    if not host:
        return None

    try:
        path = urlparse(url).path or ''
    except Exception:
        return None

    # ── Amazon ──
    if 'amazon.' in host or host == 'amzn.in':
        match = _ASIN_PATH.search(path)
        if match:
            return f'amazon:{match.group(1).upper()}'
        asin = _raw_query(url).get('asin', '')
        if asin and _ASIN_BARE.match(asin):
            return f'amazon:{asin.upper()}'
        # A search or storefront link identifies no single product.
        return None

    # ── Flipkart ──
    if 'flipkart.' in host:
        pid = _raw_query(url).get('pid', '')
        if pid:
            return f'flipkart:{pid.lower()}'
        match = _FLIPKART_ITM.search(path)
        if match:
            return f'flipkart:{match.group(1).lower()}'
        return None

    # ── Myntra / Ajio / Meesho ──
    if 'myntra.' in host:
        match = _MYNTRA_ID.search(path)
        if match:
            return f'myntra:{match.group(1)}'
        return None

    if 'ajio.' in host:
        match = _AJIO_ID.search(path)
        if match:
            return f'ajio:{match.group(1)}'
        return None

    if 'meesho.' in host:
        match = _MEESHO_ID.search(path)
        if match:
            return f'meesho:{match.group(1).lower()}'
        return None

    # ── Anything else ──
    # Host plus path, tracking parameters dropped and any remaining ones sorted so
    # two orderings of the same link agree. Not as strong as a real product id, but
    # far better than the raw URL.
    clean_path = path.rstrip('/').lower()
    if not clean_path or clean_path == '':
        return None
    params = _query(url)
    if params:
        suffix = '?' + '&'.join(f'{k}={v}' for k, v in sorted(params.items()))
    else:
        suffix = ''
    return f'url:{host}{clean_path}{suffix}'


def title_key(title):
    """
    Last-resort identity for a post with no usable link.

    Words only, sorted, so the same product survives a reworded blast and a
    different emoji header. Weak on purpose — it is only reached when nothing
    better exists.
    """
    if not title:
        return None
    words = [w for w in _NON_WORD.split(title.lower()) if len(w) > 2]
    if not words:
        return None
    return 'title:' + '-'.join(sorted(set(words))[:8])


def keys_from_urls(urls):
    """Canonical keys for a list of URLs, in order, duplicates removed."""
    keys = []
    for url in urls or []:
        key = canonical_key(url)
        if key and key not in keys:
            keys.append(key)
    return keys


def keys_from_text(text):
    """Canonical keys for every product URL in a block of text."""
    return keys_from_urls(extract_urls(text))


def primary_key(urls=None, title=None):
    """
    The single best identity available for a post.

    Link-derived keys win over the title, because a title is whatever marketing
    copy the channel chose to write.
    """
    for key in keys_from_urls(urls or []):
        return key
    return title_key(title)
