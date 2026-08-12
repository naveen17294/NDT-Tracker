import difflib
from utils import clean_text, remove_emojis
from config import FUZZY_MATCH_THRESHOLD


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


class KeywordMatcher:
    """Smart keyword matching with synonyms and fuzzy matching."""

    def __init__(self):
        self.synonym_map = SYNONYM_MAP

    def get_synonyms(self, keyword, custom_synonyms=''):
        """Get all synonyms for a keyword (built-in + custom)."""
        keyword = keyword.lower().strip()
        synonyms = set()

        # Add the keyword itself
        synonyms.add(keyword)

        # Add built-in synonyms
        if keyword in self.synonym_map:
            synonyms.update(self.synonym_map[keyword])

        # Check if keyword matches any synonym (reverse lookup)
        for main_keyword, syns in self.synonym_map.items():
            if keyword in syns:
                synonyms.add(main_keyword)
                synonyms.update(syns)

        # Add custom synonyms
        if custom_synonyms:
            for syn in custom_synonyms.split(','):
                syn = syn.strip()
                if syn:
                    synonyms.add(syn)

        return list(synonyms)

    def match(self, text, watchlist):
        """
        Check if text matches any keyword in the watchlist.

        Args:
            text: Message text to check (already cleaned preferred)
            watchlist: List of tuples [(keyword, custom_synonyms), ...]

        Returns:
            dict or None:
                {
                    'keyword': str,          # Original watchlist keyword
                    'matched_term': str,      # The actual term that matched
                    'confidence': str,        # 'high', 'medium', 'low'
                    'match_type': str,        # 'exact', 'synonym', 'fuzzy'
                }
        """
        if not text or not watchlist:
            return None

        cleaned = clean_text(remove_emojis(text))
        if not cleaned:
            return None

        # Split text into words for word-level matching
        words = cleaned.split()
        # Also create bigrams and trigrams for multi-word matches
        bigrams = [' '.join(words[i:i+2]) for i in range(len(words)-1)]
        trigrams = [' '.join(words[i:i+3]) for i in range(len(words)-2)]
        all_ngrams = words + bigrams + trigrams

        best_match = None
        best_confidence = 0

        for keyword, custom_synonyms in watchlist:
            synonyms = self.get_synonyms(keyword, custom_synonyms)

            for synonym in synonyms:
                synonym_lower = synonym.lower()

                # ── Priority 1: Exact word-boundary match in full text ──
                import re
                if re.search(rf'\b{re.escape(synonym_lower)}\b', cleaned):
                    confidence = 1.0
                    match_type = 'exact' if synonym_lower == keyword.lower() else 'synonym'
                    if confidence > best_confidence:
                        best_confidence = confidence
                        best_match = {
                            'keyword': keyword,
                            'matched_term': synonym,
                            'confidence': 'high',
                            'match_type': match_type,
                        }
                    # High confidence exact match — return immediately
                    if match_type == 'exact':
                        return best_match
                    continue

                # ── Priority 2: Fuzzy match against n-grams ──
                for ngram in all_ngrams:
                    ratio = difflib.SequenceMatcher(None, synonym_lower, ngram).ratio()
                    if ratio >= FUZZY_MATCH_THRESHOLD and ratio > best_confidence:
                        best_confidence = ratio
                        best_match = {
                            'keyword': keyword,
                            'matched_term': f"{synonym} (~{ngram})",
                            'confidence': 'medium' if ratio >= 0.85 else 'low',
                            'match_type': 'fuzzy',
                        }

        return best_match

    def get_display_synonyms(self, keyword, custom_synonyms=''):
        """Get a short display list of synonyms for UI."""
        synonyms = self.get_synonyms(keyword, custom_synonyms)
        # Remove the keyword itself from display
        display = [s for s in synonyms if s != keyword.lower()]
        return display[:5]  # Show max 5 for readability
