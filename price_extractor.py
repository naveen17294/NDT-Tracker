import re
from config import PRICE_PATTERNS, DISCOUNT_PATTERN


class PriceExtractor:
    """Extract prices from message text (Indian e-commerce formats)."""

    def __init__(self):
        self.price_patterns = [re.compile(p, re.IGNORECASE) for p in PRICE_PATTERNS]
        self.discount_pattern = re.compile(DISCOUNT_PATTERN, re.IGNORECASE)
        self.mrp_pattern = re.compile(
            r'(?:MRP|M\.R\.P|original|actual|was)\s*[₹:.]?\s*([\d,]+(?:\.\d{1,2})?)',
            re.IGNORECASE
        )
        self.deal_price_pattern = re.compile(
            r'(?:at|for|just|only|now|price|deal|offer)\s*[₹:.]?\s*([\d,]+(?:\.\d{1,2})?)',
            re.IGNORECASE
        )

    def _parse_price(self, price_str):
        """Convert price string to float (remove commas)."""
        try:
            return float(price_str.replace(',', ''))
        except (ValueError, AttributeError):
            return None

    def extract(self, text):
        """
        Extract price info from message text.

        Returns dict:
            {
                'price': float or None,         # Deal/offer price
                'original_price': float or None, # MRP/original price
                'discount': str or None,         # "24% off"
                'raw_prices': [float],           # All prices found
            }
        """
        if not text:
            return {'price': None, 'original_price': None, 'discount': None, 'raw_prices': []}

        # Find all prices in the text
        all_prices = []
        for pattern in self.price_patterns:
            matches = pattern.findall(text)
            for match in matches:
                price = self._parse_price(match)
                if price and price > 0:
                    all_prices.append(price)

        # Remove duplicates, keep order
        seen = set()
        unique_prices = []
        for p in all_prices:
            if p not in seen:
                seen.add(p)
                unique_prices.append(p)

        # Try to identify deal price vs MRP
        deal_price = None
        original_price = None

        # Check for explicit MRP
        mrp_match = self.mrp_pattern.search(text)
        if mrp_match:
            original_price = self._parse_price(mrp_match.group(1))

        # Check for explicit deal price
        deal_match = self.deal_price_pattern.search(text)
        if deal_match:
            deal_price = self._parse_price(deal_match.group(1))

        # If we have both, validate (deal should be less than MRP)
        if deal_price and original_price:
            if deal_price > original_price:
                deal_price, original_price = original_price, deal_price
        elif len(unique_prices) >= 2:
            # Two prices found — smaller is deal, larger is MRP
            sorted_prices = sorted(unique_prices)
            deal_price = sorted_prices[0]
            original_price = sorted_prices[-1]
            if deal_price == original_price:
                original_price = None
        elif len(unique_prices) == 1:
            deal_price = unique_prices[0]

        # Extract discount percentage
        discount = None
        discount_match = self.discount_pattern.search(text)
        if discount_match:
            discount = f"{discount_match.group(1)}% off"
        elif deal_price and original_price and original_price > deal_price:
            # Calculate discount
            pct = ((original_price - deal_price) / original_price) * 100
            discount = f"{int(pct)}% off"

        return {
            'price': deal_price,
            'original_price': original_price,
            'discount': discount,
            'raw_prices': unique_prices,
        }
