"""
V4 Configuration — The Outsiders Information Edge Framework
"""

import os
from datetime import timezone, timedelta

PST = timezone(timedelta(hours=-7))
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "v4.db")
LOG_PATH = os.path.join(DATA_DIR, "v4.log")
ENV_PATH = os.path.join(BASE_DIR, ".env")

# ── Market Selection ──
MIN_BOOK_DEPTH_USD = 50        # Minimum USD liquidity on each side
MAX_SPREAD_PCT = 0.05          # Maximum bid-ask spread as % of mid price
MIN_VOLUME_24H = 500           # Minimum 24h volume in USD
RESOLUTION_HORIZON_MIN_HOURS = 1    # Skip markets resolving in < 1 hour
RESOLUTION_HORIZON_MAX_DAYS = 30    # Skip markets resolving in > 30 days

# ── Edge Detection ──
MIN_EDGE_PCT = 0.04            # Minimum 4% edge (our probability vs market) to trade
CONFIDENCE_FLOOR = 0.60        # Don't trade if our confidence in our estimate is < 60%

# ── Position Sizing (Kelly Criterion) ──
KELLY_FRACTION = 0.25          # Quarter-Kelly (conservative)
MAX_POSITION_PCT = 0.05        # Never risk > 5% of bankroll on one market
MIN_POSITION_USD = 5.0         # Minimum position size
MAX_POSITION_USD = 50.0        # Maximum position size
INITIAL_BANKROLL = 50.0        # Starting capital

# ── Execution ──
USE_LIMIT_ORDERS = True        # Always maker, never taker
ORDER_STALE_SECONDS = 300      # Cancel/replace if market moves after 5 min
MAX_OPEN_POSITIONS = 10        # Portfolio-level limit
MAX_SAME_CATEGORY = 3          # Max positions in same category (politics, crypto, etc.)

# ── Risk Management ──
MAX_DRAWDOWN_PCT = 0.20        # Stop trading if portfolio drops 20% from peak
DAILY_LOSS_LIMIT_PCT = 0.05    # Stop for the day if down 5%

# ── Categories we target ──
TARGET_CATEGORIES = [
    "politics",         # Elections, legislation, appointments
    "economics",        # Fed decisions, jobs data, GDP
    "crypto",           # Long-horizon crypto (NOT 5-min binary)
    "sports",           # With statistical models
    "culture",          # Award shows, viral events
    "science",          # Scheduled launches, approvals
]

# Categories we AVOID
AVOID_PATTERNS = [
    "updown-5m",        # 5-minute binary crypto — proven coin flip
    "updown-1m",        # 1-minute binary crypto
]

# ── API ──
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
POLYMARKET_URL = "https://polymarket.com"

# ── Trade Frequency Target ──
TARGET_TRADES_PER_WEEK = (5, 15)  # 5-15 high-conviction trades
