import difflib
import re

from config import (
    FUZZY_MATCH_ENABLED,
    FUZZY_MATCH_THRESHOLD,
    FUZZY_MAX_LENGTH_DIFF,
    FUZZY_MIN_LENGTH,
)
from utils import clean_text, remove_emojis


# ═══════════════════════════════════════
#  Built-in Synonym Dictionary
#  Covers common Indian e-commerce categories
# ═══════════════════════════════════════

SYNONYM_MAP = {
    # Kitchen & Appliances
    'fridge': ['refrigerator', 'fridge', 'double door', 'single door', 'mini fridge', 'side by side', 'french door fridge'],
    'washing machine': ['washing machine', 'washer', 'front load', 'top load', 'semi automatic', 'fully automatic'],
    'microwave': ['microwave', 'microwave oven', 'otg', 'oven toaster', 'convection oven'],
    'mixer': ['mixer', 'mixer grinder', 'blender', 'juicer', 'hand blender', 'food processor'],
    'water purifier': ['water purifier', 'ro purifier', 'water filter', 'ro water'],
    'dishwasher': ['dishwasher', 'dish washer'],
    'induction': ['induction', 'induction cooktop', 'cooktop', 'induction stove'],
    'air fryer': ['air fryer', 'airfryer', 'air frier'],
    'chimney': ['chimney', 'kitchen chimney', 'auto clean chimney', 'range hood'],
    'iron': ['iron', 'steam iron', 'dry iron', 'iron press', 'garment steamer'],
    'vacuum cleaner': ['vacuum cleaner', 'vacuum', 'robot vacuum', 'robotic vacuum', 'handheld vacuum'],
    'geyser': ['geyser', 'water heater', 'instant water heater', 'storage water heater'],

    # Electronics
    'tv': ['television', 'smart tv', 'led tv', 'oled', 'qled', '4k tv', 'android tv', 'google tv', 'fire tv'],
    'laptop': ['notebook', 'laptop', 'macbook', 'chromebook', 'gaming laptop', 'ultrabook', 'thinkpad'],
    'phone': ['mobile', 'smartphone', 'phone', 'iphone', 'android phone', 'cell phone', '5g phone'],
    'tablet': ['tablet', 'ipad', 'tab', 'drawing tablet', 'galaxy tab'],
    'monitor': ['monitor', 'gaming monitor', 'curved monitor', 'ultrawide', 'computer monitor'],
    'keyboard': ['keyboard', 'mechanical keyboard', 'wireless keyboard', 'gaming keyboard'],
    'mouse': ['mouse', 'wireless mouse', 'gaming mouse', 'ergonomic mouse', 'trackpad'],

    # Audio
    'earbuds': ['earbuds', 'tws', 'earphones', 'wireless earbuds', 'true wireless', 'in-ear'],
    'headphones': ['headphones', 'headset', 'over ear', 'on ear', 'anc headphones', 'noise cancelling'],
    'speaker': ['speaker', 'bluetooth speaker', 'soundbar', 'home theatre', 'party speaker', 'portable speaker'],
    'neckband': ['neckband', 'neckband earphones', 'wireless neckband', 'neck band'],

    # Wearables
    'watch': ['smartwatch', 'smart watch', 'fitness band', 'fitness tracker', 'apple watch', 'galaxy watch'],

    # Cooling
    'ac': ['air conditioner', 'split ac', 'window ac', 'inverter ac', 'portable ac', 'air cooler'],
    'fan': ['fan', 'ceiling fan', 'table fan', 'pedestal fan', 'tower fan', 'exhaust fan'],
    'cooler': ['air cooler', 'desert cooler', 'personal cooler', 'tower cooler'],

    # Camera
    'camera': ['camera', 'dslr', 'mirrorless', 'action camera', 'gopro', 'webcam', 'security camera', 'cctv'],

    # Grooming
    'trimmer': ['trimmer', 'grooming kit', 'shaver', 'beard trimmer', 'hair trimmer', 'body groomer'],
    'hair dryer': ['hair dryer', 'hair straightener', 'hair curler', 'blow dryer'],

    # Computer Peripherals
    'printer': ['printer', 'all-in-one printer', 'laser printer', 'inkjet', 'ink tank'],
    'hard disk': ['hard disk', 'hdd', 'external hard drive', 'portable hard drive'],
    'ssd': ['ssd', 'solid state drive', 'nvme', 'm.2 ssd', 'portable ssd'],
    'pen drive': ['pen drive', 'pendrive', 'usb drive', 'flash drive', 'thumb drive'],
    'memory card': ['memory card', 'sd card', 'micro sd', 'microsd'],
    'router': ['router', 'wifi router', 'mesh router', 'range extender', 'wifi extender'],
    'ups': ['ups', 'uninterruptible power supply', 'battery backup'],
    'power bank': ['power bank', 'powerbank', 'portable charger'],
    'charger': ['charger', 'fast charger', 'wireless charger', 'charging cable', 'type c charger'],

    # Gaming
    'gaming': ['gaming console', 'ps5', 'playstation', 'xbox', 'nintendo switch', 'gaming controller', 'joystick'],

    # Furniture / Home
    'mattress': ['mattress', 'bed mattress', 'foam mattress', 'spring mattress', 'orthopaedic mattress'],
    'chair': ['chair', 'office chair', 'gaming chair', 'ergonomic chair', 'study chair'],
    'desk': ['desk', 'study table', 'computer desk', 'standing desk', 'office table'],

    # Fitness
    'treadmill': ['treadmill', 'walking pad', 'running machine'],
    'cycle': ['cycle', 'bicycle', 'exercise bike', 'gym cycle', 'spin bike'],
}


# Match tiers, best first. The tier decides which match wins; within a tier the
# longer (more specific) term wins, so "smart tv" beats "tv" on the same message.
TIER_EXACT = 0
TIER_SYNONYM = 1
TIER_PLURAL = 2
TIER_FUZZY = 3

_TIER_META = {
    TIER_EXACT: ('exact', 'high'),
    TIER_SYNONYM: ('synonym', 'high'),
    TIER_PLURAL: ('plural', 'high'),
    TIER_FUZZY: ('fuzzy', 'low'),
}


def singularise(word):
    """
    Reduce an English plural to its singular form.

    Deliberately conservative — it only strips suffixes that are unambiguous. This
    replaces what fuzzy matching was really being used for ('shoe' vs 'shoes') without
    fuzzy's failure mode, because normalisation maps each word to exactly one form:
    shoes->shoe, homes->home, hoses->hose, shows->show all stay distinct, whereas
    difflib scored every one of those pairs at 0.800 and matched them.
    """
    if len(word) <= 3:
        return word
    if word.endswith('ies') and len(word) > 4:
        return word[:-3] + 'y'
    for suffix in ('ches', 'shes', 'sses', 'xes', 'zes'):
        if word.endswith(suffix):
            return word[:-2]
    if word.endswith('s') and not word.endswith(('ss', 'us', 'is')):
        return word[:-1]
    return word


def singularise_phrase(text):
    """Singularise every word in a string, preserving word order and spacing."""
    return ' '.join(singularise(word) for word in text.split())


def parse_exclusions(exclusions):
    """
    Split a stored exclusions string into terms.

    Accepts commas or whitespace as separators, so both `/exclude shoes | kids, women`
    and the inline `/watch shoes -kids -women` form land in the same shape.
    """
    if not exclusions:
        return []
    terms = []
    for chunk in exclusions.split(','):
        term = chunk.strip().lower()
        if term:
            terms.append(term)
    return terms


class KeywordMatcher:
    """Keyword matching with synonyms, plural handling and optional fuzzy matching."""

    def __init__(self):
        self.synonym_map = SYNONYM_MAP
        # get_synonyms() walks the whole SYNONYM_MAP doing a reverse lookup, and
        # match() called it once per watchlist keyword per incoming message. Memoise
        # it — the inputs are a handful of stable strings.
        self._synonym_cache = {}
        # Compiled word-boundary patterns. re's internal cache is only 512 entries and
        # gets evicted by every other regex in the process.
        self._pattern_cache = {}

    def _pattern_for(self, term):
        pattern = self._pattern_cache.get(term)
        if pattern is None:
            pattern = re.compile(rf'\b{re.escape(term)}\b')
            self._pattern_cache[term] = pattern
        return pattern

    def get_synonyms(self, keyword, custom_synonyms=''):
        """
        Get all search terms for a keyword (built-in synonyms + custom).

        Returned sorted longest-first, then alphabetically. Sorting matters for two
        reasons: the most specific term should win a match, and the previous version
        returned list(set(...)), whose order varies between processes because Python
        randomises string hashing — so `matched_term` changed across restarts.
        """
        cache_key = (keyword.lower().strip(), (custom_synonyms or '').lower().strip())
        cached = self._synonym_cache.get(cache_key)
        if cached is not None:
            return cached

        keyword = cache_key[0]
        synonyms = {keyword}

        # Forward lookup: the keyword names a category outright.
        if keyword in self.synonym_map:
            synonyms.update(self.synonym_map[keyword])

        # Reverse lookup: the keyword is a synonym inside some category.
        #
        # Only expand when exactly one category claims it. A term can sit in two
        # categories — 'air cooler' is listed under both 'ac' and 'cooler' — and
        # pulling in every owner meant /watch "air cooler" also matched 'split ac',
        # 'window ac' and 'inverter ac'. Watching a cooler should not alert you about
        # air conditioners, so an ambiguous term expands to nothing and matches only
        # itself.
        owners = [key for key, syns in self.synonym_map.items() if keyword in syns]
        if len(owners) == 1:
            synonyms.add(owners[0])
            synonyms.update(self.synonym_map[owners[0]])

        # Custom synonyms are always honoured — the user asked for them explicitly.
        if custom_synonyms:
            for syn in custom_synonyms.split(','):
                syn = syn.strip()
                if syn:
                    synonyms.add(syn)

        result = sorted(synonyms, key=lambda s: (-len(s), s))
        self._synonym_cache[cache_key] = result
        return result

    def match(self, text, watchlist):
        """
        Check if text matches any keyword in the watchlist.

        Args:
            text: Message text to check
            watchlist: List of tuples [(keyword, custom_synonyms, exclusions), ...].
                       The third element is optional — a plain (keyword, synonyms)
                       pair still works.

        Returns:
            dict or None:
                {
                    'keyword': str,        # Original watchlist keyword
                    'matched_term': str,   # The actual term that matched
                    'confidence': str,     # 'high' or 'low'
                    'match_type': str,     # 'exact', 'synonym', 'plural', 'fuzzy'
                }
        """
        if not text or not watchlist:
            return None

        cleaned = clean_text(remove_emojis(text))
        if not cleaned:
            return None

        # Singularised copy of the message, so 'shoes' in the watchlist can match
        # 'shoe' in the text and vice versa without any similarity guessing.
        cleaned_singular = singularise_phrase(cleaned)

        best = None
        best_rank = None  # (tier, -len(term)) — lower sorts better

        for entry in watchlist:
            keyword, custom_synonyms = entry[0], entry[1]
            exclusions = entry[2] if len(entry) > 2 else ''
            keyword_lower = keyword.lower().strip()

            # Negative keywords veto this keyword for this message, before any term is
            # tried. Scoped to the one keyword on purpose: `/watch shoes -kids` must
            # not stop a `laptop` on the watchlist from matching the same message.
            if self.is_excluded(cleaned, cleaned_singular, exclusions):
                continue

            for synonym in self.get_synonyms(keyword, custom_synonyms):
                synonym_lower = synonym.lower()
                is_keyword_itself = synonym_lower == keyword_lower

                # ── Tier 0/1: exact word-boundary match ──
                if self._pattern_for(synonym_lower).search(cleaned):
                    tier = TIER_EXACT if is_keyword_itself else TIER_SYNONYM
                    rank = (tier, -len(synonym_lower))
                    if best_rank is None or rank < best_rank:
                        best_rank = rank
                        best = self._result(keyword, synonym, tier)
                    # An exact hit on the keyword itself is the best possible outcome.
                    if tier == TIER_EXACT:
                        return best
                    continue

                # ── Tier 2: plural/singular variant ──
                synonym_singular = singularise_phrase(synonym_lower)
                if (synonym_singular != synonym_lower or cleaned_singular != cleaned) \
                        and self._pattern_for(synonym_singular).search(cleaned_singular):
                    rank = (TIER_PLURAL, -len(synonym_lower))
                    if best_rank is None or rank < best_rank:
                        best_rank = rank
                        best = self._result(keyword, synonym, TIER_PLURAL)
                    continue

                # ── Tier 3: fuzzy, off by default ──
                if not FUZZY_MATCH_ENABLED:
                    continue
                fuzzy = self._fuzzy_match(synonym_lower, cleaned)
                if fuzzy is not None:
                    ratio, ngram = fuzzy
                    rank = (TIER_FUZZY, -ratio)
                    if best_rank is None or rank < best_rank:
                        best_rank = rank
                        best = self._result(
                            keyword, f"{synonym} (~{ngram})", TIER_FUZZY
                        )

        return best

    def is_excluded(self, cleaned, cleaned_singular, exclusions):
        """
        True if any negative keyword appears in the message.

        Uses the same word-boundary + singularisation machinery as a positive match,
        so `-kids` blocks "kid" too and cannot fire on a substring — `-pen` must not
        veto "expensive". Exclusions are always word-for-word: no synonym expansion,
        because the user naming a term to block means that term, not a category.
        """
        for term in parse_exclusions(exclusions):
            if self._pattern_for(term).search(cleaned):
                return True
            singular = singularise_phrase(term)
            if self._pattern_for(singular).search(cleaned_singular):
                return True
        return False

    def excluded_terms(self, text, exclusions):
        """Which negative keywords a message trips. Backs /testmatch's explanation."""
        cleaned = clean_text(remove_emojis(text))
        cleaned_singular = singularise_phrase(cleaned)
        return [
            term for term in parse_exclusions(exclusions)
            if self.is_excluded(cleaned, cleaned_singular, term)
        ]

    def _fuzzy_match(self, synonym_lower, cleaned):
        """
        Best fuzzy candidate for a synonym, or None.

        Heavily gated. difflib's ratio is only meaningful for reasonably long strings
        of similar length: two 5-letter words differing by one character score 0.800,
        which is why the old 0.75 threshold matched 'shoes' to 'homes'.
        """
        if len(synonym_lower) < FUZZY_MIN_LENGTH:
            return None

        words = cleaned.split()
        # Compare against n-grams of the same word count as the synonym, so a
        # single word is never compared against a three-word phrase.
        span = len(synonym_lower.split())
        ngrams = [' '.join(words[i:i + span]) for i in range(len(words) - span + 1)]

        best = None
        for ngram in ngrams:
            if len(ngram) < FUZZY_MIN_LENGTH:
                continue
            if abs(len(ngram) - len(synonym_lower)) > FUZZY_MAX_LENGTH_DIFF:
                continue
            ratio = difflib.SequenceMatcher(None, synonym_lower, ngram).ratio()
            if ratio >= FUZZY_MATCH_THRESHOLD and (best is None or ratio > best[0]):
                best = (ratio, ngram)
        return best

    @staticmethod
    def _result(keyword, matched_term, tier):
        match_type, confidence = _TIER_META[tier]
        return {
            'keyword': keyword,
            'matched_term': matched_term,
            'confidence': confidence,
            'match_type': match_type,
        }

    def explain(self, text, watchlist):
        """
        Human-readable account of why a message did or didn't match.

        Backs the /testmatch bot command. This class of bug was invisible for so long
        because a wrong match looked exactly like a right one from the outside.
        """
        cleaned = clean_text(remove_emojis(text))
        lines = [f"Cleaned text: {cleaned[:200] or '(empty)'}"]
        lines.append(f"Fuzzy matching: {'ON' if FUZZY_MATCH_ENABLED else 'OFF'}")

        if not watchlist:
            lines.append("Watchlist is empty — nothing can match.")
            return '\n'.join(lines), None

        # Keywords that WOULD have matched but were vetoed by a negative keyword.
        # Reporting these is the whole point: a suppressed alert is invisible
        # otherwise, and looks identical to a keyword that simply didn't match.
        vetoed = []
        for entry in watchlist:
            exclusions = entry[2] if len(entry) > 2 else ''
            if not exclusions:
                continue
            tripped = self.excluded_terms(text, exclusions)
            if tripped and self.match(text, [(entry[0], entry[1])]):
                vetoed.append((entry[0], tripped))

        result = self.match(text, watchlist)
        if result:
            lines.append("")
            lines.append(f"MATCHED keyword: {result['keyword']}")
            lines.append(f"  via term  : {result['matched_term']}")
            lines.append(f"  match type: {result['match_type']} ({result['confidence']})")
        else:
            lines.append("")
            lines.append("No match.")
            lines.append(f"Checked {len(watchlist)} keyword(s): "
                         + ', '.join(entry[0] for entry in watchlist[:10]))

        for keyword, tripped in vetoed:
            lines.append("")
            lines.append(f"BLOCKED keyword: {keyword}")
            lines.append(f"  would have matched, but you excluded: {', '.join(tripped)}")

        return '\n'.join(lines), result

    def get_display_synonyms(self, keyword, custom_synonyms=''):
        """Get a short display list of synonyms for UI."""
        synonyms = self.get_synonyms(keyword, custom_synonyms)
        display = [s for s in synonyms if s != keyword.lower().strip()]
        return display[:5]  # Show max 5 for readability
