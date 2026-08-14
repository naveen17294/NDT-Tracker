import re
import unicodedata


# URL extraction regex — handles all common URL patterns
URL_REGEX = re.compile(
    r'https?://[^\s<>"\')\]}>，。！？、；：]+',
    re.IGNORECASE
)


def extract_urls(text):
    """Extract all URLs from message text."""
    if not text:
        return []
    urls = URL_REGEX.findall(text)
    # Clean trailing punctuation that might be captured
    cleaned = []
    for url in urls:
        url = url.rstrip('.,;:!?)')
        if url:
            cleaned.append(url)
    return cleaned


def clean_text(text):
    """Normalize text for matching — lowercase, remove URLs, remove excess whitespace."""
    if not text:
        return ''
    # Remove URLs so we don't accidentally match keywords inside URL slugs (e.g. u5tv2 -> tv)
    text = URL_REGEX.sub(' ', text)
    # Normalize unicode characters
    text = unicodedata.normalize('NFKD', text)
    # Lowercase
    text = text.lower()
    # Remove extra whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def remove_emojis(text):
    """Remove emoji characters from text."""
    if not text:
        return ''
    emoji_pattern = re.compile(
        "["
        "\U0001F600-\U0001F64F"  # Emoticons
        "\U0001F300-\U0001F5FF"  # Symbols & pictographs
        "\U0001F680-\U0001F6FF"  # Transport & map
        "\U0001F1E0-\U0001F1FF"  # Flags
        "\U00002702-\U000027B0"
        "\U000024C2-\U0001F251"
        "\U0001f926-\U0001f937"
        "\U00010000-\U0010ffff"
        "\u2640-\u2642"
        "\u2600-\u2B55"
        "\u200d"
        "\u23cf"
        "\u23e9"
        "\u231a"
        "\ufe0f"
        "\u3030"
        "]+",
        flags=re.UNICODE
    )
    return emoji_pattern.sub('', text)


def channel_display_name(channel):
    """Best available human name for a channel row, never empty."""
    return (
        channel.get('channel_name')
        or channel.get('channel_username')
        or f"Channel {channel.get('channel_id')}"
    )


def channel_matches(channel, terms):
    """
    True if any term appears in the channel's title or @username.

    Plain case-insensitive substring, which is what makes 'deal' cover 'deals' and
    'sale' cover 'sales' without listing every inflection. An empty term list means
    "no filter" and matches everything.
    """
    if not terms:
        return True
    haystack = "{} {}".format(
        channel.get('channel_name') or '',
        channel.get('channel_username') or '',
    ).lower()
    return any(term in haystack for term in terms if term)


def format_time_ago(seconds):
    """Format seconds into human-readable 'X ago' string."""
    if seconds < 60:
        return f"{int(seconds)}s ago"
    elif seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    elif seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    else:
        return f"{int(seconds // 86400)}d ago"


def format_price(price):
    """Format number as Indian currency string."""
    if price is None:
        return "N/A"
    # Indian number formatting (1,23,456)
    price = int(price)
    s = str(price)
    if len(s) <= 3:
        return f"₹{s}"
    # Last 3 digits
    result = s[-3:]
    s = s[:-3]
    # Group remaining in pairs
    while s:
        result = s[-2:] + ',' + result
        s = s[:-2]
    return f"₹{result}"


def truncate(text, max_length=50):
    """Truncate text with ellipsis."""
    if not text:
        return ''
    if len(text) <= max_length:
        return text
    return text[:max_length - 3] + '...'
