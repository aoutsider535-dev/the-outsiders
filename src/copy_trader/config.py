"""
Copy Trader Configuration
All tunable parameters in one place.
"""

# ═══════════════════════════════════════════════════════════
# LEADERS
# ═══════════════════════════════════════════════════════════
LEADERS = {
    "0xdc876e6873772d38716fda7f2452a78d426d7ab6": {
        "name": "Leader_1",
        "enabled": True,
        "weight": 1.0,  # Sizing multiplier (leaderWeighting)
    },
    "0xd0d6053c3c37e727402d84c14069780d360993aa": {
        "name": "Leader_2",
        "enabled": True,
        "weight": 1.0,
    },
    "0x15ceffed7bf820cd2d90f90ea24ae9909f5cd5fa": {
        "name": "Leader_3",
        "enabled": True,
        "weight": 1.0,
    },
    "0xdb27bf2ac5d428a9c63dbc914611036855a6c56e": {
        "name": "Leader_4",
        "enabled": True,
        "weight": 1.0,
    },
    "0x204f72f35326db932158cba6adff0b9a1da95e14": {
        "name": "Leader_5",
        "enabled": True,
        "weight": 1.0,
    },
    "0x02227b8f5a9636e895607edd3185ed6ee5598ff7": {
        "name": "Leader_6",
        "enabled": True,
        "weight": 1.0,
    },
    "0x37c1874a60d348903594a96703e0507c518fc53a": {
        "name": "Leader_7",
        "enabled": True,
        "weight": 1.0,
    },
    "0x2d8b401d2f0e6937afebf18e19e11ca568a5260a": {
        "name": "Leader_8",
        "enabled": True,
        "weight": 1.0,
    },
    "0x0b9cae2b0dfe7a71c413e0604eaac1c352f87e44": {
        "name": "Leader_9",
        "enabled": True,
        "weight": 1.0,
    },
}

# ═══════════════════════════════════════════════════════════
# ENTRY FILTERS — Should we copy this trade?
# ═══════════════════════════════════════════════════════════

# Timing
REVERT_TRADE = False                # False = only copy BUYs, skip SELLs
ENTRY_TRADE_SEC = 300               # Skip if leader's trade is older than 5 minutes
TRADE_SEC_FROM_RESOLVE = 300        # Skip if market ends within N seconds (5 min)
MIN_MARKET_AGE_SEC = 600            # Skip if market opened < N seconds ago (10 min)

# Price
MAX_ENTRY_PRICE = 0.95              # Don't buy tokens above this (too little upside)
MIN_ENTRY_PRICE = 0.05              # Don't buy tokens below this (lottery tickets)
SLIPPAGE_TOLERANCE = 0.03           # Max 3% slippage vs leader's fill price

# Leader trade size
MIN_LEADER_TRADE_SIZE = 10.0        # Ignore leader trades < $10 (noise/test trades)

# Market quality
MIN_MARKET_LIQUIDITY = 0             # Disabled — CLOB API doesn't expose liquidity
MIN_MARKET_VOLUME = 0                # Disabled — CLOB API doesn't expose volume

# Leader conviction
LEADER_CONVICTION_PCT = 0.0         # Only copy if leader puts > X% of portfolio in trade (0 = disabled)

# Duplicates
DUPLICATE_FILTER = True             # Skip if we already have a position in this exact market

# Leader quality
MIN_LEADER_WIN_RATE = 0.0           # Only copy leaders above X% WR (0 = disabled, needs tracking data)
MIN_LEADER_TRADES = 0               # Observe N trades before copying live (0 = copy immediately)

# Anti-gaming
RECENT_SELL_CHECK = True            # Skip if leader sold same token in last N minutes
RECENT_SELL_WINDOW_SEC = 300        # How far back to check for recent sells (5 min)

# Category filters
ALLOWED_CATEGORIES = ["all"]        # ["all"] or ["politics", "sports", "crypto", "economics", ...]
BLOCKED_MARKETS = []                # Specific condition IDs to never trade

# Conflict
CONFLICT_RESOLUTION = "skip"        # "skip" | "follow_best" | "follow_first" | "both"

# ═══════════════════════════════════════════════════════════
# POSITION SIZING — How much do we copy?
# ═══════════════════════════════════════════════════════════

SIZING_MODE = "fixed"               # "fixed" | "proportional" | "kelly"
FIXED_TRADE_SIZE = 5.0              # Flat $ per copy trade (when sizing_mode = "fixed")
COPY_FRACTION = 0.10                # Copy X of leader's trade size (when sizing_mode = "proportional")
MIN_TRADE_SIZE = 2.0                # Floor
MAX_TRADE_SIZE = 25.0               # Cap

# ═══════════════════════════════════════════════════════════
# RISK LIMITS
# ═══════════════════════════════════════════════════════════

MAX_POSITIONS_TOTAL = 20            # Max open positions at once
MAX_POSITIONS_PER_MARKET = 3        # Max positions in a single market
MAX_EXPOSURE_TOTAL = 100.0          # Max total $ deployed
MAX_EXPOSURE_PER_LEADER = 50.0      # Max $ following one leader
MAX_EXPOSURE_PER_MARKET = 30.0      # Max $ in one market
MAX_EXPOSURE_PER_CATEGORY = 0.30    # Max fraction of portfolio per category (30%)
MAX_PORTFOLIO_RISK_PCT = 0.50       # Total portfolio at risk can't exceed 50%
DAILY_LOSS_LIMIT = 25.0             # Stop ALL copying if daily P&L hits -$X
MAX_DRAWDOWN = 50.0                 # Kill switch — stop if total drawdown exceeds $X

# ═══════════════════════════════════════════════════════════
# EXIT FILTERS — When do we get out?
# ═══════════════════════════════════════════════════════════

COPY_LEADER_EXIT = True             # If leader sells, we exit too
COPY_LEADER_EXIT_DELAY_SEC = 10     # Delay before copying leader's exit (avoid front-running)
STOP_LOSS_PCT = 0.0                 # Our own SL (0 = disabled, hold to resolution)
TAKE_PROFIT_PCT = 0.0               # Our own TP (0 = disabled, hold to resolution)
MAX_HOLD_TIME_DAYS = 30             # Force exit after 30 days (safety net for long-term markets)
TRAILING_STOP_PCT = 0.0             # Trailing SL (0 = disabled)

# ═══════════════════════════════════════════════════════════
# LEADER MANAGEMENT
# ═══════════════════════════════════════════════════════════

LEADER_COOLDOWN_LOSSES = 3          # Pause leader after N consecutive losses
LEADER_COOLDOWN_HOURS = 24          # How long to pause
LEADER_CORRELATION_CHECK = True     # Detect if two leaders copy each other

# ═══════════════════════════════════════════════════════════
# OPERATIONAL
# ═══════════════════════════════════════════════════════════

POLL_INTERVAL_SEC = 3               # How often to check leader activity
TX_CONFIRMATION = True              # Wait for on-chain confirmation
RETRY_COUNT = 3                     # Retry failed orders N times
RETRY_DELAY_SEC = 2                 # Delay between retries
PAPER_MODE = True                   # True = paper trading, False = live

# Alerting
ALERT_ON_COPY = True                # Notify when trade is copied
ALERT_ON_FILTER = False             # Notify when trade is filtered out
ALERT_ON_LEADER_FLAG = True         # Notify when leader is flagged/paused

# Database
DB_PATH = "data/copy_trader.db"
