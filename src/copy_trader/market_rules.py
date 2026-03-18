"""
Market-type-specific rules.
Different market types need different entry/exit behavior.
"""
import re
import time


def classify_market(question: str, market_info: dict = None) -> str:
    """
    Classify a market into a type.
    Returns: 'crypto_5min', 'crypto_15min', 'crypto_hourly', 'sports', 'politics', 'other'
    """
    q = (question or '').lower()

    # Crypto up/down markets
    crypto_tokens = ['bitcoin', 'ethereum', 'solana', 'xrp', 'doge']
    if any(tok in q for tok in crypto_tokens) and 'up or down' in q:
        # Parse duration from market name
        # "Bitcoin Up or Down - March 17, 10:00AM-10:05AM ET" → 5min
        # "Bitcoin Up or Down - March 17, 10AM ET" → hourly
        # "Bitcoin Up or Down - March 17, 10:00AM-10:15AM ET" → 15min
        time_match = re.search(r'(\d{1,2}):(\d{2})(AM|PM)\s*-\s*(\d{1,2}):(\d{2})(AM|PM)', q)
        if time_match:
            h1, m1 = int(time_match.group(1)), int(time_match.group(2))
            h2, m2 = int(time_match.group(4)), int(time_match.group(5))
            ampm1, ampm2 = time_match.group(3), time_match.group(6)

            if ampm1.upper() == 'PM' and h1 != 12: h1 += 12
            if ampm2.upper() == 'PM' and h2 != 12: h2 += 12

            duration_min = (h2 * 60 + m2) - (h1 * 60 + m1)
            if duration_min <= 0: duration_min += 24 * 60

            if duration_min <= 5:
                return 'crypto_5min'
            elif duration_min <= 15:
                return 'crypto_15min'
            elif duration_min <= 60:
                return 'crypto_hourly'
            else:
                return 'crypto_other'

        # Check for hourly pattern like "10AM ET" (no range)
        if re.search(r'\d{1,2}(AM|PM)\s+ET\b', q) and '-' not in q.split(',')[-1]:
            return 'crypto_hourly'

        return 'crypto_other'

    # Sports
    sports_keywords = ['spread:', 'o/u', 'over', 'under', 'vs.', 'win on', 'end in a draw',
                       'exact score:', 'leading at halftime', 'nba', 'nfl', 'nhl',
                       'fc ', ' fc', 'goals']
    if any(kw in q for kw in sports_keywords):
        return 'sports'

    # Politics
    politics_keywords = ['president', 'election', 'congress', 'senate', 'governor',
                         'trump', 'biden', 'democrat', 'republican']
    if any(kw in q for kw in politics_keywords):
        return 'politics'

    return 'other'


def parse_market_window(question: str):
    """
    Parse the start/end time from a crypto market question.
    Returns (start_utc_ts, end_utc_ts) or (None, None) if can't parse.
    """
    q = question or ''

    # Extract date: "March 17"
    date_match = re.search(r'(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})', q)
    if not date_match:
        return None, None

    month_name = date_match.group(1)
    day = int(date_match.group(2))
    months = {'January': 1, 'February': 2, 'March': 3, 'April': 4, 'May': 5, 'June': 6,
              'July': 7, 'August': 8, 'September': 9, 'October': 10, 'November': 11, 'December': 12}
    month = months.get(month_name, 3)
    year = 2026  # Assume current year

    # Extract time range: "10:00AM-10:05AM ET"
    time_match = re.search(r'(\d{1,2}):(\d{2})(AM|PM)\s*-\s*(\d{1,2}):(\d{2})(AM|PM)', q)
    if time_match:
        h1, m1 = int(time_match.group(1)), int(time_match.group(2))
        h2, m2 = int(time_match.group(4)), int(time_match.group(5))
        ampm1, ampm2 = time_match.group(3).upper(), time_match.group(6).upper()
    else:
        return None, None

    if ampm1 == 'PM' and h1 != 12: h1 += 12
    if ampm1 == 'AM' and h1 == 12: h1 = 0
    if ampm2 == 'PM' and h2 != 12: h2 += 12
    if ampm2 == 'AM' and h2 == 12: h2 = 0

    from datetime import datetime, timezone, timedelta
    # ET = UTC-4 (EDT in March)
    et_offset = timedelta(hours=-4)
    start_et = datetime(year, month, day, h1, m1, tzinfo=timezone(et_offset))
    end_et = datetime(year, month, day, h2, m2, tzinfo=timezone(et_offset))

    return start_et.timestamp(), end_et.timestamp()


def get_entry_rules(market_type: str) -> dict:
    """Get entry rules for a market type."""
    if market_type.startswith('crypto_'):
        return {
            'max_entry_sec': 120,       # Only enter within first 2 minutes
            'hold_to_resolution': True,  # Hold to expiration
            'copy_leader_exit': True,    # BUT if leader sells, we sell too
        }
    elif market_type == 'sports':
        return {
            'max_entry_sec': None,       # No time restriction for sports
            'hold_to_resolution': True,  # Hold to resolution
            'copy_leader_exit': True,    # Follow leader exits
        }
    else:
        return {
            'max_entry_sec': None,
            'hold_to_resolution': True,
            'copy_leader_exit': True,
        }
