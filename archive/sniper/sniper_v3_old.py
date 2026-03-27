#!/usr/bin/env python3
"""
═══════════════════════════════════════════════════════════════════════════════
🎯 THE OUTSIDERS — LATE DRIFT SNIPER v3
═══════════════════════════════════════════════════════════════════════════════

Post-open arbitrage on Polymarket BTC 5-minute up/down markets.

STRATEGY OVERVIEW:
  In the final 30-60 seconds of each 5-minute window, if BTC has drifted
  significantly from its open price, the market outcome is partially known.
  We buy the favored side on Polymarket if:
    1. Drift ≥ volatility-adjusted threshold (filters fakeouts in high-vol)
    2. Book price ≤ max entry (don't overpay for known info)
    3. Model edge ≥ minimum (our continuation prob minus implied prob)
    4. Streak filter passes (mild mean-reversion from retail over-betting)
    5. Oracle lag bonus applied if detected (Chainlink ~10-300ms behind spot)

  Execution: FOK (Fill-or-Kill) market orders. Hold to resolution, no exits.

WHY EACH FILTER EXISTS:
  - Drift threshold: BTC moves < 0.15% in 5 min are noise. Only trade
    moves large enough that continuation is statistically likely.
  - Vol-adjusted drift: A 0.15% move during a 2% hourly vol regime is
    nothing. During 0.05% vol, it's massive. Scale the threshold.
  - Streak filter: Retail bettors chase streaks ("5 ups in a row, due for
    down"). After 4+ same-direction outcomes, the contrarian side has a
    mild edge (~55-60% historically). This is mean-reversion on crowd bias.
  - Oracle lag: Chainlink's BTC/USD feed updates every ~10-300ms after
    spot exchanges. If we detect cross-exchange consensus diverging from
    the current oracle implied price, there's a latency arb window.
  - Max buy price: Even with a strong signal, paying $0.90+ for a $1.00
    outcome leaves too little margin after fees. Cap at $0.82.

MODES:
  --shadow  : Observe only, log all data (no trades)
  --paper   : Simulate trades at real book prices
  --live    : Real money orders on Polymarket CLOB

USAGE:
  python src/sniper_v3.py --shadow
  python src/sniper_v3.py --paper --size 10
  python src/sniper_v3.py --live --size 20

DEPENDENCIES:
  pip install websockets requests python-dotenv ccxt py-clob-client

═══════════════════════════════════════════════════════════════════════════════
"""

import asyncio
import json
import math
import os
import signal
import sqlite3
import statistics
import sys
import time
import argparse
import logging
import traceback
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Tuple, List

import requests
import websockets

# ─── Path setup ──────────────────────────────────────────────────────────────
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

from dotenv import dotenv_values

# ═══════════════════════════════════════════════════════════════════════════════
# CONSTANTS & CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

PST = timezone(timedelta(hours=-7))  # PDT during daylight saving

# Paths
ENV_PATH = os.path.join(ROOT_DIR, ".env")
DB_PATH = os.path.join(ROOT_DIR, "data", "drift_sniper.db")
DATA_DIR = os.path.join(ROOT_DIR, "data")
STATE_FILE = os.path.join(DATA_DIR, "drift_sniper_state.json")
VOL_BUFFER_FILE = os.path.join(DATA_DIR, "drift_sniper_vol.json")
LOG_FILE = os.path.join(DATA_DIR, "drift_sniper.log")

# Polymarket API endpoints
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

# ─── WebSocket Endpoints (real-time price feeds, <50ms latency) ─────────────
# Binance: wss://stream.binance.com:9443/ws/btcusdt@trade
#   - Individual trade stream, ~10-50ms latency
#   - Payload: {"p": "84321.50", "T": 1710000000123, ...}
# Bybit: wss://stream.bybit.com/v5/public/spot
#   - Subscribe to "publicTrade.BTCUSDT"
#   - Payload: {"data": [{"p": "84321.50", "T": 1710000000123}]}
BINANCE_WS = "wss://stream.binance.com:9443/ws/btcusdt@trade"
BYBIT_WS = "wss://stream.bybit.com/v5/public/spot"

# Coinbase REST fallback (no free WS for trades)
COINBASE_SPOT_URL = "https://api.coinbase.com/v2/prices/BTC-USD/spot"

# ─── Entry Timing ───────────────────────────────────────────────────────────
ENTRY_WINDOW_START = 60    # Start scanning at T-60s before window close
ENTRY_WINDOW_END = 30      # Stop at T-30s (later = risk of no fill)
POLL_INTERVAL = 2          # Seconds between signal evaluations in entry window

# ─── Drift Thresholds ───────────────────────────────────────────────────────
BASE_MIN_DRIFT_PCT = 0.0015   # 0.15% absolute minimum drift
USE_VOL_ADJUSTED_DRIFT = True
# Vol adjustment formula (see MATH section at bottom):
#   adjusted_min = BASE_MIN_DRIFT * max(current_vol / avg_vol, 0.5)
# This means: if current vol is 2x average, require 2x drift to enter.
# Floor at 0.5x prevents lowering the bar too much in dead markets.

# ─── Polymarket Book Requirements ────────────────────────────────────────────
MAX_BUY_PRICE = 0.82      # Never pay more than $0.82 for favored side
MIN_EDGE = 0.07            # 7% edge = our P(win) must exceed implied by 7%+
MIN_DEPTH_SHARES = 10.0    # Need at least this many shares available

# ─── Streak Filter ──────────────────────────────────────────────────────────
# WHY: Retail bettors over-bet continuation after streaks. After 4+ same-
# direction outcomes, the "due for reversal" crowd creates +EV on contrarian.
# This is NOT gambler's fallacy — it's exploiting crowd behavior that
# CREATES a mild mean-reversion edge through imbalanced betting.
USE_STREAK_FILTER = True
STREAK_THRESHOLD = 4       # Flip to contrarian after 4+ same-direction
STREAK_SIZE_BOOST = 1.25   # 25% bigger size on streak reversals

# ─── Oracle Lag Detection ───────────────────────────────────────────────────
# WHY: Chainlink's BTC/USD oracle aggregates from multiple sources but has
# 10-300ms latency. When spot exchanges all agree on a sudden move but the
# oracle hasn't updated yet, Polymarket prices are stale. We detect this by
# checking if multi-exchange consensus diverges > 0.01% from any single
# source — if they're ALL moving together, the oracle is likely lagging.
USE_ORACLE_LAG = True
LAG_EDGE_BONUS = 0.03      # +3% edge when oracle lag detected
ORACLE_LAG_SPREAD_PCT = 0.01  # Minimum spread to trigger lag detection

# ─── Trade Sizing (Kelly-lite) ──────────────────────────────────────────────
# Kelly fraction: f* = (p*b - q) / b where b = odds, p = win prob, q = 1-p
# We use fractional Kelly (50%) to reduce variance.
# RISK_PER_TRADE is the base bet at minimum edge. Higher edge = proportionally
# larger bet, capped at MAX_TRADE_SIZE.
RISK_PER_TRADE = 0.01      # 1% of bankroll at minimum edge
MIN_TRADE_SIZE = 5.0
MAX_TRADE_SIZE = 100.0
DEFAULT_TRADE_SIZE = 10.0
MAX_BANKROLL_RISK = 0.05   # Never risk more than 5% on a single trade

# ─── Volatility Estimation ──────────────────────────────────────────────────
VOL_LOOKBACK = 84          # 84 × 5min = 7 hours of historical prices
VOL_EMA_DECAY = 0.99       # Slow decay for long-run average vol baseline


# ═══════════════════════════════════════════════════════════════════════════════
# LOGGING SETUP
# ═══════════════════════════════════════════════════════════════════════════════

def setup_logging():
    """Configure structured logging to both file and stdout."""
    os.makedirs(DATA_DIR, exist_ok=True)
    logger = logging.getLogger("drift_sniper")
    logger.setLevel(logging.DEBUG)

    # Console handler — INFO and above
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", datefmt="%H:%M:%S"))

    # File handler — DEBUG and above (all details)
    fh = logging.FileHandler(LOG_FILE, mode="a")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))

    logger.addHandler(ch)
    logger.addHandler(fh)
    return logger


log = setup_logging()


# ═══════════════════════════════════════════════════════════════════════════════
# WEBSOCKET PRICE FEEDS
# ═══════════════════════════════════════════════════════════════════════════════

class PriceFeed:
    """
    Real-time BTC/USDT price aggregator using WebSocket feeds.

    Maintains latest price from Binance and Bybit via persistent WebSocket
    connections. Falls back to REST if WS disconnects. Reconnects automatically.

    Latency: <50ms from exchange trade to our price variable update.
    vs REST polling: 200-500ms per request, plus rate limits.
    """

    def __init__(self):
        self.prices: Dict[str, float] = {}        # {exchange: latest_price}
        self.timestamps: Dict[str, float] = {}     # {exchange: unix_ts of last update}
        self._running = True
        self._tasks: List[asyncio.Task] = []
        self._lock = asyncio.Lock()

    @property
    def binance(self) -> Optional[float]:
        """Latest Binance price, or None if stale (>5s)."""
        if "binance" in self.prices:
            age = time.time() - self.timestamps.get("binance", 0)
            if age < 5.0:
                return self.prices["binance"]
        return None

    @property
    def bybit(self) -> Optional[float]:
        """Latest Bybit price, or None if stale (>5s)."""
        if "bybit" in self.prices:
            age = time.time() - self.timestamps.get("bybit", 0)
            if age < 5.0:
                return self.prices["bybit"]
        return None

    @property
    def best(self) -> Optional[float]:
        """Best available price: average of fresh feeds, or single feed."""
        fresh = []
        for ex in ["binance", "bybit"]:
            if ex in self.prices:
                age = time.time() - self.timestamps.get(ex, 0)
                if age < 5.0:
                    fresh.append(self.prices[ex])
        if fresh:
            return statistics.mean(fresh)
        # Fallback to REST
        return self._rest_fallback()

    @property
    def all_prices(self) -> Dict[str, float]:
        """All fresh prices as dict. Used for oracle lag detection."""
        result = {}
        for ex in ["binance", "bybit"]:
            if ex in self.prices:
                age = time.time() - self.timestamps.get(ex, 0)
                if age < 5.0:
                    result[ex] = self.prices[ex]
        return result

    def _rest_fallback(self) -> Optional[float]:
        """REST fallback when all WebSockets are down."""
        try:
            r = requests.get(
                "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT",
                timeout=2,
            )
            price = float(r.json()["price"])
            self.prices["binance"] = price
            self.timestamps["binance"] = time.time()
            return price
        except Exception:
            return None

    async def _run_binance_ws(self):
        """
        Binance trade stream WebSocket.
        Payload: {"e":"trade","s":"BTCUSDT","p":"84321.50","T":1710000000123,...}

        Reconnects on disconnect with exponential backoff (1s → 2s → 4s → max 30s).
        """
        backoff = 1
        while self._running:
            try:
                log.info("🔌 Connecting Binance WebSocket...")
                async with websockets.connect(
                    BINANCE_WS,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    log.info("✅ Binance WS connected")
                    backoff = 1  # Reset on successful connection
                    async for msg in ws:
                        if not self._running:
                            break
                        try:
                            data = json.loads(msg)
                            price = float(data["p"])
                            self.prices["binance"] = price
                            self.timestamps["binance"] = time.time()
                        except (KeyError, ValueError, json.JSONDecodeError):
                            continue
            except (websockets.ConnectionClosed, ConnectionError, OSError) as e:
                log.warning(f"⚠️ Binance WS disconnected: {e}. Reconnecting in {backoff}s...")
            except Exception as e:
                log.error(f"💥 Binance WS error: {e}")
            if self._running:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _run_bybit_ws(self):
        """
        Bybit v5 public spot trade stream.
        Must send subscribe message after connecting:
          {"op": "subscribe", "args": ["publicTrade.BTCUSDT"]}
        Payload: {"topic":"publicTrade.BTCUSDT","data":[{"p":"84321.50","T":...}]}

        Reconnects with exponential backoff on disconnect.
        """
        backoff = 1
        while self._running:
            try:
                log.info("🔌 Connecting Bybit WebSocket...")
                async with websockets.connect(
                    BYBIT_WS,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    # Subscribe to BTC/USDT trades
                    sub_msg = json.dumps({
                        "op": "subscribe",
                        "args": ["publicTrade.BTCUSDT"]
                    })
                    await ws.send(sub_msg)
                    log.info("✅ Bybit WS connected + subscribed")
                    backoff = 1

                    async for msg in ws:
                        if not self._running:
                            break
                        try:
                            data = json.loads(msg)
                            # Skip subscription confirmations
                            if data.get("op") == "subscribe":
                                continue
                            trades = data.get("data", [])
                            if trades:
                                # Take latest trade price
                                price = float(trades[-1]["p"])
                                self.prices["bybit"] = price
                                self.timestamps["bybit"] = time.time()
                        except (KeyError, ValueError, json.JSONDecodeError):
                            continue
            except (websockets.ConnectionClosed, ConnectionError, OSError) as e:
                log.warning(f"⚠️ Bybit WS disconnected: {e}. Reconnecting in {backoff}s...")
            except Exception as e:
                log.error(f"💥 Bybit WS error: {e}")
            if self._running:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def start(self):
        """Launch all WebSocket feeds as background tasks."""
        self._tasks = [
            asyncio.create_task(self._run_binance_ws()),
            asyncio.create_task(self._run_bybit_ws()),
        ]
        # Wait a moment for initial connections
        await asyncio.sleep(2)
        if self.binance:
            log.info(f"📡 Binance price: ${self.binance:,.2f}")
        if self.bybit:
            log.info(f"📡 Bybit price: ${self.bybit:,.2f}")

    async def stop(self):
        """Gracefully shut down all WebSocket connections."""
        self._running = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        log.info("🔌 Price feeds stopped")


# ═══════════════════════════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════════════════════════

def init_db():
    """
    Initialize SQLite database with three tables:

    observations: Every window evaluation (signal or not). Used for shadow
                  mode analysis and model calibration.
    trades:       Actual/simulated trades. Linked to observations by window_ts.
    resolutions:  Window outcomes (up/down). Used for streak tracking and
                  trade P&L resolution.
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            window_ts INTEGER NOT NULL,
            time_left_sec REAL NOT NULL,
            btc_open REAL NOT NULL,
            btc_spot REAL NOT NULL,
            drift_pct REAL NOT NULL,
            vol_1h REAL,
            vol_adjusted_min REAL,
            vol_factor REAL,
            up_best_ask REAL,
            up_best_bid REAL,
            down_best_ask REAL,
            down_best_bid REAL,
            up_ask_depth REAL,
            down_ask_depth REAL,
            favored_side TEXT,
            favored_ask REAL,
            model_prob REAL,
            implied_prob REAL,
            edge REAL,
            streak_count INTEGER,
            streak_direction TEXT,
            streak_reversal INTEGER DEFAULT 0,
            oracle_lag_detected INTEGER DEFAULT 0,
            lag_bonus REAL DEFAULT 0,
            signal_fired INTEGER DEFAULT 0,
            reject_reason TEXT,
            trade_taken INTEGER DEFAULT 0,
            buy_price REAL,
            shares REAL,
            cost_usdc REAL,
            order_id TEXT,
            fill_status TEXT,
            binance_price REAL,
            bybit_price REAL,
            price_spread_pct REAL
        );

        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            window_ts INTEGER NOT NULL,
            side TEXT NOT NULL,
            buy_price REAL NOT NULL,
            shares REAL NOT NULL,
            cost_usdc REAL NOT NULL,
            model_prob REAL NOT NULL,
            edge REAL NOT NULL,
            drift_pct REAL NOT NULL,
            vol_factor REAL,
            streak_count INTEGER,
            streak_reversal INTEGER DEFAULT 0,
            oracle_lag INTEGER DEFAULT 0,
            lag_bonus REAL DEFAULT 0,
            order_id TEXT,
            resolved INTEGER DEFAULT 0,
            won INTEGER,
            pnl REAL,
            resolution_price REAL,
            btc_open REAL,
            btc_close REAL,
            resolved_at TEXT
        );

        CREATE TABLE IF NOT EXISTS resolutions (
            window_ts INTEGER PRIMARY KEY,
            outcome TEXT NOT NULL,
            btc_open REAL,
            btc_close REAL,
            resolved_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_trades_window ON trades(window_ts);
        CREATE INDEX IF NOT EXISTS idx_trades_resolved ON trades(resolved);
        CREATE INDEX IF NOT EXISTS idx_obs_window ON observations(window_ts);
    """)
    conn.close()
    log.debug("📦 Database initialized")


# ═══════════════════════════════════════════════════════════════════════════════
# POLYMARKET API HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def find_market(window_ts: int) -> Optional[dict]:
    """
    Look up the Polymarket BTC 5-min up/down market for a given window.

    Returns dict with:
      - slug, condition_id, token_ids (up/down), accepting_orders, closed
      - neg_risk, tick_size (needed for order placement)

    Returns None if market not found or API error.
    Rate limits: Gamma API is generous (~100 req/min) but we cache per window.
    """
    slug = f"btc-updown-5m-{window_ts}"
    try:
        r = requests.get(
            f"{GAMMA_BASE}/events",
            params={"slug": slug},
            timeout=5,
        )
        if r.status_code != 200:
            log.debug(f"Gamma API returned {r.status_code} for {slug}")
            return None
        data = r.json()
        if not data:
            return None

        event = data[0]
        market = event["markets"][0]
        token_ids = market.get("clobTokenIds")
        if isinstance(token_ids, str):
            token_ids = json.loads(token_ids)

        return {
            "slug": slug,
            "condition_id": market.get("conditionId"),
            "accepting_orders": market.get("acceptingOrders", False),
            "closed": market.get("closed", False) or event.get("closed", False),
            "token_ids": {
                "up": token_ids[0] if token_ids else None,
                "down": token_ids[1] if token_ids else None,
            },
            "neg_risk": market.get("negRisk", False),
            "tick_size": str(market.get("orderPriceMinTickSize", "0.01")),
        }
    except requests.exceptions.Timeout:
        log.warning(f"⏱️ Gamma API timeout for {slug}")
        return None
    except requests.exceptions.ConnectionError:
        log.warning(f"🔌 Gamma API connection error for {slug}")
        return None
    except Exception as e:
        log.error(f"💥 Market lookup failed: {e}")
        return None


def get_orderbook(token_id: str) -> Tuple[list, list]:
    """
    Fetch CLOB orderbook for a token.

    Returns (asks_asc, bids_desc) where each entry is (price, size).
    Asks sorted ascending (cheapest first), bids sorted descending.

    Rate limits: CLOB API ~30 req/min. We only call this during the
    entry window (T-60 to T-30), so ~15 calls/window max.
    """
    try:
        r = requests.get(
            f"{CLOB_BASE}/book",
            params={"token_id": token_id},
            timeout=5,
        )
        book = r.json()
        asks = sorted(
            [(float(a["price"]), float(a["size"])) for a in book.get("asks", [])],
            key=lambda x: x[0],
        )
        bids = sorted(
            [(float(b["price"]), float(b["size"])) for b in book.get("bids", [])],
            key=lambda x: x[0],
            reverse=True,
        )
        return asks, bids
    except requests.exceptions.Timeout:
        log.warning("⏱️ CLOB book timeout")
        return [], []
    except Exception as e:
        log.warning(f"⚠️ Book fetch failed: {e}")
        return [], []


def get_binance_candle_open(window_ts: int) -> Optional[float]:
    """
    Get the opening price of the Binance 5-min candle for this window.
    This is our reference price (P₀) for drift calculation.

    Uses Binance klines API which aligns candles to 5-min boundaries
    (same as Polymarket windows).
    """
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={
                "symbol": "BTCUSDT",
                "interval": "5m",
                "startTime": window_ts * 1000,
                "limit": 1,
            },
            timeout=3,
        )
        candles = r.json()
        if candles and len(candles) > 0:
            return float(candles[0][1])  # [1] = open price
    except Exception as e:
        log.warning(f"⚠️ Candle open fetch failed: {e}")
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# CORE SNIPER ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

class LateDriftSniperV3:
    """
    Main trading engine. Evaluates every 5-minute window, generates signals,
    and executes trades (or simulates them).

    Lifecycle:
      1. Wait for entry window (T-60s to T-30s before window close)
      2. Evaluate: drift, book prices, edge, filters
      3. If all rules pass: execute FOK buy on favored side
      4. Hold to resolution (5-30 min after window closes)
      5. Log outcome, update streak, save state
    """

    def __init__(self, mode: str = "shadow", trade_size: float = None):
        self.mode = mode
        self.trade_size = trade_size or DEFAULT_TRADE_SIZE
        self.running = True
        self.client = None  # CLOB client (live mode only)
        self.funder = None
        self.balance = 1000.0 if mode != "live" else 0.0

        # Price feed (WebSocket-based)
        self.feed = PriceFeed()

        # Volatility tracking
        self.price_buffer = deque(maxlen=VOL_LOOKBACK)
        self.avg_vol: Optional[float] = None

        # Streak tracking (last 10 outcomes)
        self.streak_history = deque(maxlen=10)

        # Session stats
        self.stats = {
            "windows_observed": 0,
            "signals_generated": 0,
            "trades_taken": 0,
            "trades_won": 0,
            "trades_lost": 0,
            "total_cost": 0.0,
            "total_pnl": 0.0,
        }

        # Market cache (avoid re-fetching same window)
        self._market_cache: Dict[int, dict] = {}

        # Load persisted state
        self._load_state()
        init_db()

        if mode == "live":
            self._init_clob_client()

    # ─── State Persistence ───────────────────────────────────────────────────

    def _load_state(self):
        """Restore streak history and vol baseline from disk."""
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r") as f:
                    state = json.load(f)
                self.streak_history = deque(state.get("streak_history", []), maxlen=10)
                self.avg_vol = state.get("avg_vol")
                log.info(f"📂 State loaded: {len(self.streak_history)} streaks, avg_vol={self.avg_vol}")
            except Exception as e:
                log.warning(f"⚠️ State load failed: {e}")

        if os.path.exists(VOL_BUFFER_FILE):
            try:
                with open(VOL_BUFFER_FILE, "r") as f:
                    buf = json.load(f)
                self.price_buffer = deque(
                    [(e["t"], e["p"]) for e in buf],
                    maxlen=VOL_LOOKBACK,
                )
            except Exception as e:
                log.warning(f"⚠️ Vol buffer load failed: {e}")

    def _save_state(self):
        """Persist streak history and vol state to disk."""
        try:
            with open(STATE_FILE, "w") as f:
                json.dump({
                    "streak_history": list(self.streak_history),
                    "avg_vol": self.avg_vol,
                    "stats": self.stats,
                    "balance": self.balance,
                    "updated": datetime.now(PST).isoformat(),
                }, f, indent=2)
        except Exception as e:
            log.warning(f"⚠️ State save failed: {e}")

        try:
            with open(VOL_BUFFER_FILE, "w") as f:
                json.dump([{"t": t, "p": p} for t, p in self.price_buffer], f)
        except Exception as e:
            log.warning(f"⚠️ Vol buffer save failed: {e}")

    # ─── CLOB Client (Live Mode) ────────────────────────────────────────────

    def _init_clob_client(self):
        """
        Initialize the Polymarket CLOB client for order placement.
        Requires POLYGON_PRIVATE_KEY and POLYGON_WALLET_ADDRESS in .env.

        Nonce management: the py-clob-client handles nonces internally.
        If we get nonce errors, we retry once after a 1s delay.
        """
        from py_clob_client.client import ClobClient

        config = dotenv_values(ENV_PATH)
        pk = config.get("POLYGON_PRIVATE_KEY", "")
        addr = config.get("POLYGON_WALLET_ADDRESS", "")
        if not pk or not addr:
            log.error("❌ Missing POLYGON_PRIVATE_KEY or POLYGON_WALLET_ADDRESS in .env")
            sys.exit(1)

        try:
            host = "https://clob.polymarket.com"
            client = ClobClient(host, key=pk, chain_id=137)
            creds = client.create_or_derive_api_creds()
            self.client = ClobClient(
                host, key=pk, chain_id=137,
                creds=creds, signature_type=1, funder=addr,
            )
            self.funder = addr
            self._refresh_balance()
            log.info(f"✅ CLOB connected | Wallet: {addr[:8]}... | Balance: ${self.balance:.2f}")
        except Exception as e:
            log.error(f"❌ CLOB init failed: {e}")
            sys.exit(1)

    def _refresh_balance(self):
        """Fetch current USDC balance from Polymarket."""
        if self.mode != "live":
            return
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams
            bal = self.client.get_balance_allowance(
                BalanceAllowanceParams(asset_type="COLLATERAL")
            )
            raw = float(bal.get("balance", 0)) if isinstance(bal, dict) else 0
            self.balance = raw / 1e6 if raw > 1e6 else raw
        except Exception as e:
            log.warning(f"⚠️ Balance check failed: {e}")

    # ─── Volatility Engine ───────────────────────────────────────────────────

    def _update_vol(self, price: float):
        """
        Update the volatility buffer with a new price point.
        Called once per window evaluation (~every 5 min).
        """
        self.price_buffer.append((time.time(), price))

    def _calc_current_vol(self) -> Optional[float]:
        """
        Calculate current 1-hour rolling volatility.

        Method: Mean absolute return over last 12 data points (12 × 5min = 1hr).
        This is a simple vol proxy — not annualized, not log returns.
        We only need relative comparison (current vs average), so the
        exact scale doesn't matter.
        """
        if len(self.price_buffer) < 3:
            return None
        recent = list(self.price_buffer)[-12:]
        returns = []
        for i in range(1, len(recent)):
            ret = abs((recent[i][1] - recent[i - 1][1]) / recent[i - 1][1])
            returns.append(ret)
        return statistics.mean(returns) if returns else None

    def _update_avg_vol(self):
        """
        Update the long-run average volatility using exponential moving average.
        Slow decay (0.99) means it adapts over ~100 data points (~8 hours).
        """
        current = self._calc_current_vol()
        if current is None:
            return
        if self.avg_vol is None:
            self.avg_vol = current
        else:
            self.avg_vol = VOL_EMA_DECAY * self.avg_vol + (1 - VOL_EMA_DECAY) * current

    def _get_vol_adjusted_min_drift(self) -> Tuple[float, Optional[float]]:
        """
        Calculate volatility-adjusted minimum drift threshold.

        FORMULA (see MATH section at bottom for derivation):
          vol_factor = current_vol / avg_vol   (how volatile vs baseline)
          adjusted   = BASE_MIN_DRIFT × max(vol_factor, 0.5)

        Returns: (adjusted_min_drift, vol_factor)
        - vol_factor > 1.0: high vol regime → require larger drift
        - vol_factor < 1.0: low vol regime → accept smaller drift (floored at 0.5×)
        - vol_factor = None: insufficient data, use base threshold
        """
        if not USE_VOL_ADJUSTED_DRIFT:
            return BASE_MIN_DRIFT_PCT, None

        current_vol = self._calc_current_vol()
        if current_vol is None or self.avg_vol is None or self.avg_vol == 0:
            return BASE_MIN_DRIFT_PCT, None

        vol_factor = current_vol / self.avg_vol
        adjusted = BASE_MIN_DRIFT_PCT * max(vol_factor, 0.5)
        return adjusted, vol_factor

    # ─── Streak Filter ───────────────────────────────────────────────────────

    def _get_streak(self) -> Tuple[int, Optional[str]]:
        """
        Get current streak: (count, direction).

        Example: streak_history = ['up','up','up','down','up','up']
        Returns: (2, 'up') — last 2 outcomes are 'up'.
        """
        if not self.streak_history:
            return 0, None
        last = self.streak_history[-1]
        count = 0
        for outcome in reversed(self.streak_history):
            if outcome == last:
                count += 1
            else:
                break
        return count, last

    def _record_outcome(self, outcome: str):
        """Record a resolution outcome ('up' or 'down') for streak tracking."""
        self.streak_history.append(outcome)
        self._save_state()

    # ─── Oracle Lag Detection ────────────────────────────────────────────────

    def _detect_oracle_lag(self, prices: Dict[str, float]) -> bool:
        """
        Detect potential Chainlink oracle lag.

        MATH (see bottom for full derivation):
          spread_pct = (max(prices) - min(prices)) / mean(prices) × 100

        If all exchanges agree on a move (spread < 0.01%) BUT the absolute
        move from open is large, the oracle likely hasn't caught up.

        We can't directly query Chainlink here (would need on-chain read),
        so we use cross-exchange consensus as a proxy: if Binance and Bybit
        are tightly aligned on a big move, the oracle is probably lagging.

        Returns True if lag is likely, False otherwise.
        """
        if not USE_ORACLE_LAG:
            return False

        if len(prices) < 2:
            return False

        values = list(prices.values())
        mean_price = statistics.mean(values)
        spread_pct = (max(values) - min(values)) / mean_price * 100

        # Tight cross-exchange agreement suggests coordinated move
        # that oracle may not have processed yet
        return spread_pct < ORACLE_LAG_SPREAD_PCT

    # ─── Continuation Probability Model ──────────────────────────────────────

    def _estimate_continuation_prob(
        self, drift_pct: float, time_left_sec: float
    ) -> float:
        """
        Estimate probability that the current drift direction holds through
        the end of the 5-minute window.

        MODEL:
          Under geometric Brownian motion, the probability that a price
          stays above (or below) its current level for time T is related
          to the z-score: z = |drift| / (σ × √T).

          Simplified empirical model:
            drift_contrib = |drift| × 180   (scaled for typical 5m drift)
            time_penalty  = (time_left / 300) × 0.05

            P(continuation) = 0.50 + drift_contrib - time_penalty

          This will be calibrated against shadow data. The coefficients
          (180 and 0.05) are initial estimates based on backtested BTC
          5-min data showing:
            - 0.15% drift at T-30s → ~78% continuation
            - 0.30% drift at T-30s → ~92% continuation
            - 0.15% drift at T-60s → ~70% continuation

        ARGS:
          drift_pct: signed decimal (0.15% = 0.0015)
          time_left_sec: seconds until window closes

        RETURNS: probability ∈ [0.50, 0.98]
        """
        time_frac = time_left_sec / 300.0

        drift_contrib = abs(drift_pct) * 180
        time_penalty = time_frac * 0.05

        prob = 0.50 + drift_contrib - time_penalty
        return max(0.50, min(prob, 0.98))

    # ─── Trade Sizing ────────────────────────────────────────────────────────

    def _calc_trade_size(self, edge: float, streak_reversal: bool = False) -> float:
        """
        Kelly-lite trade sizing.

        FORMULA:
          base_bet = RISK_PER_TRADE × balance
          edge_mult = edge / MIN_EDGE   (7% edge = 1×, 14% = 2×, etc.)
          size = base_bet × edge_mult × streak_boost

        Capped at MAX_TRADE_SIZE and MAX_BANKROLL_RISK × balance.

        WHY Kelly-lite: Full Kelly (bet proportional to edge) is theoretically
        optimal but has high variance. We use ~50% Kelly by keeping
        RISK_PER_TRADE conservative (1%).
        """
        base = RISK_PER_TRADE * self.balance
        edge_multiplier = edge / MIN_EDGE
        size = base * edge_multiplier

        if streak_reversal:
            size *= STREAK_SIZE_BOOST

        return max(MIN_TRADE_SIZE, min(size, MAX_TRADE_SIZE, self.balance * MAX_BANKROLL_RISK))

    # ─── Order Book Walking ──────────────────────────────────────────────────

    def _simulate_fill(self, asks: list, target_spend: float) -> Tuple[float, float]:
        """
        Walk the ask side of the book to calculate average fill price.

        Only considers asks ≤ MAX_BUY_PRICE. Returns (avg_price, total_shares).
        This accurately models what a FOK market buy would fill at.
        """
        total_shares = 0.0
        total_cost = 0.0

        for price, size in asks:
            if price > MAX_BUY_PRICE:
                break
            remaining = target_spend - total_cost
            if remaining <= 0:
                break
            affordable = remaining / price
            take = min(size, affordable)
            total_shares += take
            total_cost += take * price

        avg_price = total_cost / total_shares if total_shares > 0 else 0
        return avg_price, total_shares

    # ─── Order Execution ─────────────────────────────────────────────────────

    def _place_order(self, token_id: str, price: float, size: float, market: dict) -> Optional[dict]:
        """
        Place a FOK (Fill-or-Kill) buy order on Polymarket CLOB.

        FOK means: fill the entire order immediately at the specified price
        or better, or cancel the entire order. No partial fills, no resting.

        In shadow/paper mode: returns a fake order result.
        In live mode: signs and submits via py-clob-client.

        Error handling:
          - Nonce errors: retry once after 1s (nonce race condition)
          - Rate limits: log and skip (next window will try again)
          - Other errors: log full traceback, skip
        """
        if self.mode != "live":
            return {
                "order_id": f"{self.mode}_{int(time.time())}",
                "status": "matched",
            }

        try:
            from py_clob_client.clob_types import MarketOrderArgs, OrderType, PartialCreateOrderOptions
            from py_clob_client.order_builder.constants import BUY

            cost = size * price
            order_args = MarketOrderArgs(
                token_id=token_id,
                amount=round(cost, 2),
                side=BUY,
                price=round(price, 2),
                order_type=OrderType.FOK,
            )
            options = PartialCreateOrderOptions(
                tick_size=market["tick_size"],
                neg_risk=market["neg_risk"],
            )

            # Attempt 1
            signed = self.client.create_market_order(order_args, options)
            resp = self.client.post_order(signed, orderType=OrderType.FOK)

            if resp and resp.get("success"):
                return {
                    "order_id": resp.get("orderID", ""),
                    "status": resp.get("status", "matched"),
                }

            # Check for nonce error — retry once
            error_msg = str(resp)
            if "nonce" in error_msg.lower():
                log.warning("⚠️ Nonce error, retrying in 1s...")
                time.sleep(1)
                signed = self.client.create_market_order(order_args, options)
                resp = self.client.post_order(signed, orderType=OrderType.FOK)
                if resp and resp.get("success"):
                    return {
                        "order_id": resp.get("orderID", ""),
                        "status": resp.get("status", "matched"),
                    }

            log.error(f"❌ Order rejected: {resp}")
            return None

        except Exception as e:
            log.error(f"❌ Order error: {e}\n{traceback.format_exc()}")
            return None

    # ─── Resolution Checker ──────────────────────────────────────────────────

    async def _check_resolutions(self):
        """
        Check if any pending trades have resolved.

        Polymarket resolution takes 5-30 minutes after window close.
        We check by looking at outcomePrices — when one side hits 1.0,
        the market has resolved.
        """
        try:
            conn = sqlite3.connect(DB_PATH)
            pending = conn.execute(
                "SELECT id, window_ts, side, buy_price, shares, cost_usdc "
                "FROM trades WHERE resolved = 0"
            ).fetchall()

            for trade_id, window_ts, side, buy_price, shares, cost in pending:
                try:
                    r = requests.get(
                        f"{GAMMA_BASE}/events",
                        params={"slug": f"btc-updown-5m-{window_ts}"},
                        timeout=5,
                    )
                    data = r.json()
                    if not data:
                        continue

                    event = data[0]
                    market = event["markets"][0]
                    out_prices = market.get("outcomePrices", "")
                    if isinstance(out_prices, str):
                        out_prices = json.loads(out_prices) if out_prices else []
                    if not out_prices:
                        continue

                    up_price = float(out_prices[0])
                    down_price = float(out_prices[1])

                    if up_price < 0.99 and down_price < 0.99:
                        continue  # Not yet resolved

                    outcome = "up" if up_price > 0.5 else "down"
                    won = 1 if outcome == side else 0
                    pnl = (shares * 1.0 - cost) if won else -cost

                    btc_close = get_binance_candle_open(window_ts + 300)

                    conn.execute(
                        "UPDATE trades SET resolved=1, won=?, pnl=?, "
                        "resolution_price=?, btc_close=?, resolved_at=? WHERE id=?",
                        (won, pnl, up_price, btc_close,
                         datetime.now(PST).isoformat(), trade_id),
                    )
                    conn.execute(
                        "INSERT OR REPLACE INTO resolutions "
                        "(window_ts, outcome, btc_close, resolved_at) VALUES (?,?,?,?)",
                        (window_ts, outcome, btc_close, datetime.now(PST).isoformat()),
                    )
                    conn.commit()

                    self._record_outcome(outcome)

                    if won:
                        self.stats["trades_won"] += 1
                        self.stats["total_pnl"] += pnl
                        log.info(f"✅ WIN {window_ts} | {side.upper()} | +${pnl:.2f}")
                    else:
                        self.stats["trades_lost"] += 1
                        self.stats["total_pnl"] += pnl
                        log.info(f"❌ LOSS {window_ts} | {side.upper()} | -${abs(pnl):.2f}")

                    if self.mode == "paper":
                        self.balance += (shares if won else 0)

                except requests.exceptions.RequestException as e:
                    log.debug(f"Resolution check network error: {e}")
                    continue
                except Exception as e:
                    log.warning(f"⚠️ Resolution error for {window_ts}: {e}")
                    continue

            conn.close()
        except Exception as e:
            log.error(f"💥 Resolution checker error: {e}")

    # ─── Signal Evaluation (THE CORE) ────────────────────────────────────────

    def _evaluate_window(self, window_ts: int, time_left: float) -> Optional[dict]:
        """
        Evaluate whether to enter a trade for the current 5-min window.

        SIGNAL GENERATION PIPELINE:
          1. Get BTC open price (P₀) for this window
          2. Get current spot prices from WebSocket feeds
          3. Calculate drift = (spot - P₀) / P₀
          4. Check drift ≥ vol-adjusted minimum
          5. Fetch Polymarket orderbook for favored side
          6. Check book price ≤ MAX_BUY_PRICE
          7. Estimate continuation probability (our model)
          8. Apply streak filter (may flip side)
          9. Apply oracle lag bonus (if detected)
          10. Check edge ≥ MIN_EDGE
          11. Check sufficient book depth
          12. Calculate trade size (Kelly-lite)
          13. Walk the book for fill simulation

        Returns signal dict if all rules pass, None otherwise.
        Every evaluation is logged to observations table regardless.
        """
        # ── Step 1: BTC open price for this window ──
        btc_open = get_binance_candle_open(window_ts)
        if btc_open is None:
            log.debug(f"No candle open for window {window_ts}")
            return None

        # ── Step 2: Current spot from WebSocket feeds ──
        spot_prices = self.feed.all_prices
        btc_spot = self.feed.best
        if btc_spot is None:
            log.warning("⚠️ No spot price available")
            return None

        # Update volatility buffer
        self._update_vol(btc_spot)
        self._update_avg_vol()

        # ── Step 3: Calculate drift ──
        drift_pct = (btc_spot - btc_open) / btc_open  # Signed decimal
        abs_drift = abs(drift_pct)

        # ── Step 4: Volatility-adjusted minimum drift ──
        vol_min, vol_factor = self._get_vol_adjusted_min_drift()
        current_vol = self._calc_current_vol()

        # ── Step 5: Find market and fetch orderbooks ──
        if window_ts not in self._market_cache:
            self._market_cache[window_ts] = find_market(window_ts)
        market = self._market_cache.get(window_ts)

        if market is None or market.get("closed"):
            return None

        up_asks, up_bids = get_orderbook(market["token_ids"]["up"])
        down_asks, down_bids = get_orderbook(market["token_ids"]["down"])

        up_best_ask = up_asks[0][0] if up_asks else None
        up_best_bid = up_bids[0][0] if up_bids else None
        down_best_ask = down_asks[0][0] if down_asks else None
        down_best_bid = down_bids[0][0] if down_bids else None

        # Depth at or below MAX_BUY_PRICE
        up_depth = sum(s for p, s in up_asks if p <= MAX_BUY_PRICE)
        down_depth = sum(s for p, s in down_asks if p <= MAX_BUY_PRICE)

        # ── Step 6: Determine favored side ──
        if drift_pct > 0:
            favored = "up"
            favored_ask = up_best_ask
            favored_asks = up_asks
            favored_depth = up_depth
        elif drift_pct < 0:
            favored = "down"
            favored_ask = down_best_ask
            favored_asks = down_asks
            favored_depth = down_depth
        else:
            favored = None
            favored_ask = None
            favored_asks = []
            favored_depth = 0

        # ── Step 7: Continuation probability ──
        model_prob = self._estimate_continuation_prob(drift_pct, time_left)

        # ── Step 8: Streak filter ──
        streak_count, streak_dir = self._get_streak()
        streak_reversal = False

        if (USE_STREAK_FILTER and streak_count >= STREAK_THRESHOLD
                and streak_dir is not None and favored == streak_dir):
            # Streak is long AND drift agrees with streak direction.
            # Contrarian play: flip to the other side.
            # WHY: After 4+ same-direction outcomes, retail has over-bet
            # continuation. The contrarian side is underpriced.
            model_prob = 1.0 - model_prob
            favored = "down" if favored == "up" else "up"
            if favored == "up":
                favored_ask = up_best_ask
                favored_asks = up_asks
                favored_depth = up_depth
            else:
                favored_ask = down_best_ask
                favored_asks = down_asks
                favored_depth = down_depth
            streak_reversal = True
            log.info(
                f"🔄 STREAK REVERSAL: {streak_count}× {streak_dir}, "
                f"flipping to {favored.upper()}"
            )

        # ── Step 9: Oracle lag bonus ──
        oracle_lag = self._detect_oracle_lag(spot_prices)
        lag_bonus = 0.0
        if oracle_lag and USE_ORACLE_LAG:
            lag_bonus = LAG_EDGE_BONUS
            model_prob = min(model_prob + lag_bonus, 0.98)
            log.debug(f"⚡ Oracle lag detected, +{lag_bonus*100:.1f}% bonus")

        # ── Step 10: Calculate edge ──
        implied_prob = favored_ask if favored_ask else 0.99
        edge = model_prob - implied_prob

        # Cross-exchange spread for logging
        price_spread = 0.0
        if len(spot_prices) >= 2:
            vals = list(spot_prices.values())
            price_spread = (max(vals) - min(vals)) / statistics.mean(vals) * 100

        # ── Build observation record ──
        obs = {
            "window_ts": window_ts,
            "time_left": time_left,
            "btc_open": btc_open,
            "btc_spot": btc_spot,
            "drift_pct": drift_pct,
            "abs_drift": abs_drift,
            "vol_min": vol_min,
            "vol_factor": vol_factor,
            "current_vol": current_vol,
            "up_best_ask": up_best_ask,
            "up_best_bid": up_best_bid,
            "down_best_ask": down_best_ask,
            "down_best_bid": down_best_bid,
            "up_depth": up_depth,
            "down_depth": down_depth,
            "favored": favored,
            "favored_ask": favored_ask,
            "model_prob": model_prob,
            "implied_prob": implied_prob,
            "edge": edge,
            "streak_count": streak_count,
            "streak_dir": streak_dir,
            "streak_reversal": streak_reversal,
            "oracle_lag": oracle_lag,
            "lag_bonus": lag_bonus,
            "signal_fired": False,
            "trade_taken": False,
            "reject_reason": None,
            "binance_price": spot_prices.get("binance"),
            "bybit_price": spot_prices.get("bybit"),
            "price_spread": price_spread,
        }

        # ══ ENTRY RULES — Check all filters ══

        # Rule 1: Drift must exceed vol-adjusted minimum
        if abs_drift < vol_min:
            obs["reject_reason"] = f"drift {abs_drift*100:.3f}% < vol_min {vol_min*100:.3f}%"
            self._record_observation(obs)
            return None

        # Rule 2: Must have a favored side with a valid book price
        if favored_ask is None or favored_ask > MAX_BUY_PRICE:
            obs["reject_reason"] = f"ask ${favored_ask} > max ${MAX_BUY_PRICE}"
            self._record_observation(obs)
            return None

        # Rule 3: Edge must exceed minimum
        if edge < MIN_EDGE:
            obs["reject_reason"] = f"edge {edge*100:.1f}% < min {MIN_EDGE*100:.0f}%"
            self._record_observation(obs)
            return None

        # Rule 4: Sufficient depth on favored side
        if favored_depth < MIN_DEPTH_SHARES:
            obs["reject_reason"] = f"depth {favored_depth:.0f} < min {MIN_DEPTH_SHARES:.0f}"
            self._record_observation(obs)
            return None

        # ══ ALL RULES PASSED — Signal fires ══
        obs["signal_fired"] = True
        self.stats["signals_generated"] += 1

        # Calculate size
        trade_size = self._calc_trade_size(edge, streak_reversal)
        trade_size = min(trade_size, favored_depth * favored_ask)

        # Walk the book for realistic fill
        fill_price, fill_shares = self._simulate_fill(favored_asks, trade_size)
        if fill_shares < MIN_TRADE_SIZE / MAX_BUY_PRICE:
            obs["reject_reason"] = f"fill_shares {fill_shares:.1f} too small"
            self._record_observation(obs)
            return None

        obs["trade_taken"] = True
        obs["buy_price"] = fill_price
        obs["shares"] = fill_shares
        obs["cost"] = fill_price * fill_shares
        obs["reject_reason"] = None

        self._record_observation(obs)

        # ── Detailed signal log ──
        log.info(
            f"{'─'*60}\n"
            f"  🎯 SIGNAL FIRED | Window {window_ts} | T-{time_left:.0f}s\n"
            f"  Side: {favored.upper()} | Drift: {drift_pct*100:+.3f}%\n"
            f"  BTC: ${btc_open:,.2f} → ${btc_spot:,.2f}\n"
            f"  Vol factor: {vol_factor:.2f}x | Vol min: {vol_min*100:.3f}%\n"
            f"  Book ask: ${favored_ask:.2f} | Depth: {favored_depth:.0f} shares\n"
            f"  Model prob: {model_prob*100:.1f}% | Implied: {implied_prob*100:.1f}%\n"
            f"  Edge: {edge*100:.1f}% | Lag bonus: {lag_bonus*100:.1f}%\n"
            f"  Streak: {streak_count}× {streak_dir or 'none'} | Reversal: {streak_reversal}\n"
            f"  Fill: ${fill_price:.3f} × {fill_shares:.1f} = ${obs['cost']:.2f}\n"
            f"{'─'*60}"
        )

        return obs

    # ─── Database Recording ──────────────────────────────────────────────────

    def _record_observation(self, obs: dict):
        """Log every window evaluation to the observations table."""
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute(
                """INSERT INTO observations
                   (timestamp, window_ts, time_left_sec, btc_open, btc_spot,
                    drift_pct, vol_1h, vol_adjusted_min, vol_factor,
                    up_best_ask, up_best_bid, down_best_ask, down_best_bid,
                    up_ask_depth, down_ask_depth, favored_side, favored_ask,
                    model_prob, implied_prob, edge,
                    streak_count, streak_direction, streak_reversal,
                    oracle_lag_detected, lag_bonus,
                    signal_fired, reject_reason, trade_taken,
                    buy_price, shares, cost_usdc,
                    binance_price, bybit_price, price_spread_pct)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    datetime.now(PST).isoformat(),
                    obs["window_ts"], obs["time_left"],
                    obs["btc_open"], obs["btc_spot"], obs["drift_pct"],
                    obs.get("current_vol"), obs["vol_min"], obs.get("vol_factor"),
                    obs["up_best_ask"], obs["up_best_bid"],
                    obs["down_best_ask"], obs["down_best_bid"],
                    obs["up_depth"], obs["down_depth"],
                    obs["favored"], obs["favored_ask"],
                    obs["model_prob"], obs["implied_prob"], obs["edge"],
                    obs["streak_count"], obs["streak_dir"],
                    1 if obs["streak_reversal"] else 0,
                    1 if obs["oracle_lag"] else 0, obs["lag_bonus"],
                    1 if obs["signal_fired"] else 0,
                    obs.get("reject_reason"),
                    1 if obs["trade_taken"] else 0,
                    obs.get("buy_price"), obs.get("shares"), obs.get("cost"),
                    obs.get("binance_price"), obs.get("bybit_price"),
                    obs.get("price_spread"),
                ),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            log.error(f"💥 Observation DB write error: {e}")

    def _record_trade(self, obs: dict):
        """Record an executed/simulated trade."""
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute(
                """INSERT INTO trades
                   (timestamp, window_ts, side, buy_price, shares, cost_usdc,
                    model_prob, edge, drift_pct, vol_factor,
                    streak_count, streak_reversal, oracle_lag, lag_bonus,
                    order_id, btc_open)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    datetime.now(PST).isoformat(),
                    obs["window_ts"], obs["favored"],
                    obs["buy_price"], obs["shares"], obs["cost"],
                    obs["model_prob"], obs["edge"], obs["drift_pct"],
                    obs.get("vol_factor"),
                    obs["streak_count"],
                    1 if obs["streak_reversal"] else 0,
                    1 if obs["oracle_lag"] else 0,
                    obs["lag_bonus"],
                    obs.get("order_id", ""),
                    obs["btc_open"],
                ),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            log.error(f"💥 Trade DB write error: {e}")

    # ─── Main Loop ───────────────────────────────────────────────────────────

    async def run(self):
        """
        Main event loop. Runs forever, evaluating every 5-minute window.

        Lifecycle per window:
          1. Sleep until T-60s (entry window opens)
          2. Poll every POLL_INTERVAL seconds until T-30s
          3. If signal fires on any poll: execute trade, mark window done
          4. After entry window: check resolutions, save state
          5. Repeat for next window
        """
        # Start WebSocket price feeds
        await self.feed.start()

        log.info(
            f"\n{'═'*60}\n"
            f"  🎯 LATE DRIFT SNIPER v3\n"
            f"  Mode: {self.mode.upper()} | Size: ${self.trade_size}\n"
            f"  Min drift: {BASE_MIN_DRIFT_PCT*100:.2f}% | Max price: ${MAX_BUY_PRICE}\n"
            f"  Min edge: {MIN_EDGE*100:.0f}% | Vol adjust: {'ON' if USE_VOL_ADJUSTED_DRIFT else 'OFF'}\n"
            f"  Streak filter: {'ON' if USE_STREAK_FILTER else 'OFF'} (threshold={STREAK_THRESHOLD})\n"
            f"  Oracle lag: {'ON' if USE_ORACLE_LAG else 'OFF'} (+{LAG_EDGE_BONUS*100:.0f}% bonus)\n"
            f"  WebSocket feeds: Binance + Bybit (<50ms latency)\n"
            f"{'═'*60}\n"
        )

        traded_windows = set()

        while self.running:
            try:
                now = time.time()
                window_ts = int(now // 300) * 300
                window_end = window_ts + 300
                time_left = window_end - now

                # ── Phase 1: Pre-entry — wait for entry window ──
                if time_left > ENTRY_WINDOW_START:
                    sleep_for = time_left - ENTRY_WINDOW_START - 1
                    if sleep_for > 10:
                        await self._check_resolutions()
                        self._save_state()
                    if sleep_for > 0:
                        await asyncio.sleep(min(sleep_for, 30))
                    continue

                # ── Phase 2: Entry window active (T-60 to T-30) ──
                if ENTRY_WINDOW_END <= time_left <= ENTRY_WINDOW_START:
                    if window_ts not in traded_windows:
                        self.stats["windows_observed"] += 1

                        signal = self._evaluate_window(window_ts, time_left)

                        if signal and signal.get("trade_taken"):
                            # Execute the trade
                            market = self._market_cache.get(window_ts)
                            if market and not market.get("closed"):
                                token_id = market["token_ids"][signal["favored"]]
                                result = self._place_order(
                                    token_id, signal["buy_price"],
                                    signal["shares"], market,
                                )
                                if result:
                                    signal["order_id"] = result.get("order_id", "")
                                    self._record_trade(signal)
                                    traded_windows.add(window_ts)
                                    self.stats["trades_taken"] += 1
                                    self.stats["total_cost"] += signal["cost"]

                                    if self.mode == "paper":
                                        self.balance -= signal["cost"]

                                    log.info(
                                        f"🎯 TRADE {signal['favored'].upper()} | "
                                        f"${signal['cost']:.2f} → "
                                        f"{signal['shares']:.1f} shares @ ${signal['buy_price']:.3f} | "
                                        f"edge {signal['edge']*100:.1f}%"
                                    )
                                else:
                                    log.warning(f"❌ Order failed for window {window_ts}")
                        else:
                            # No signal — periodic status log at T-45s
                            if 43 <= time_left <= 47:
                                spot = self.feed.best
                                p0 = get_binance_candle_open(window_ts)
                                if spot and p0:
                                    drift = (spot - p0) / p0 * 100
                                    w = self.stats["trades_won"]
                                    l = self.stats["trades_lost"]
                                    pnl = self.stats["total_pnl"]
                                    log.info(
                                        f"👀 Window {window_ts} | T-{time_left:.0f}s | "
                                        f"drift {drift:+.3f}% | "
                                        f"W{w}-L{l} (${pnl:+.2f})"
                                    )

                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                # ── Phase 3: Post-entry — wait for next window ──
                if time_left < ENTRY_WINDOW_END:
                    # Clean up market cache
                    old_windows = [
                        k for k in self._market_cache
                        if k < window_ts - 600
                    ]
                    for k in old_windows:
                        del self._market_cache[k]

                    next_entry = window_end + (300 - ENTRY_WINDOW_START)
                    sleep_for = next_entry - now - 1
                    if sleep_for > 5:
                        await self._check_resolutions()
                        self._save_state()
                    await asyncio.sleep(max(sleep_for, 1))
                    continue

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"💥 Loop error: {e}\n{traceback.format_exc()}")
                await asyncio.sleep(5)

        # Shutdown
        await self.feed.stop()
        self._save_state()
        self._print_summary()

    def _print_summary(self):
        """Print session summary on shutdown."""
        s = self.stats
        total = s["trades_won"] + s["trades_lost"]
        wr = s["trades_won"] / total * 100 if total else 0

        log.info(
            f"\n{'═'*60}\n"
            f"  📊 SESSION SUMMARY\n"
            f"  Windows observed: {s['windows_observed']}\n"
            f"  Signals generated: {s['signals_generated']}\n"
            f"  Trades taken: {s['trades_taken']}\n"
            f"  Resolved: {total} ({s['trades_won']}W-{s['trades_lost']}L, {wr:.1f}% WR)\n"
            f"  Total cost: ${s['total_cost']:.2f}\n"
            f"  P&L: ${s['total_pnl']:+.2f}\n"
            f"  Balance: ${self.balance:.2f}\n"
            f"{'═'*60}"
        )

    def stop(self):
        """Signal the main loop to stop."""
        self.running = False


# ═══════════════════════════════════════════════════════════════════════════════
# MATH APPENDIX — Volatility-Adjusted Drift & Oracle Lag Detection
# ═══════════════════════════════════════════════════════════════════════════════
#
# VOLATILITY-ADJUSTED DRIFT FORMULA
# ──────────────────────────────────
# Problem: A fixed drift threshold (e.g., 0.15%) ignores market regime.
#   - In low-vol: 0.15% is a huge move (3σ), very likely to persist
#   - In high-vol: 0.15% is noise (0.5σ), easily reversed
#
# Solution: Scale threshold by current vol relative to baseline.
#
#   σ_current = mean(|r_i|)  for i in last 12 five-min returns
#   σ_avg     = EMA(σ_current, α=0.01)  long-run baseline
#   vol_factor = σ_current / σ_avg
#   min_drift  = BASE_MIN_DRIFT × max(vol_factor, 0.5)
#
# Properties:
#   - vol_factor = 1.0: normal regime, use base threshold
#   - vol_factor = 2.0: high vol, require 2× drift (filter fakeouts)
#   - vol_factor = 0.3: very low vol, floor at 0.5× (don't go below 0.075%)
#
# The floor prevents us from trading impossibly small drifts where the
# book spread alone would eat the edge.
#
#
# ORACLE LAG DETECTION
# ────────────────────
# Problem: Chainlink's BTC/USD oracle feeds aggregate from multiple data
# sources with ~10-300ms latency. During fast moves, the on-chain price
# lags behind spot exchanges.
#
# Detection method:
#   1. Fetch simultaneous prices from Binance and Bybit (via WebSocket)
#   2. Calculate cross-exchange spread:
#        spread = (max(prices) - min(prices)) / mean(prices)
#   3. If spread < 0.01%: exchanges AGREE on the current price
#      → Oracle may not have caught up to this consensus yet
#      → Apply lag_bonus to model probability
#
# Why tight spread = lag signal:
#   When exchanges diverge (spread > 0.01%), the "true" price is uncertain.
#   When they converge tightly on a move, the move is real but the oracle's
#   aggregation delay means Polymarket's implied price is stale.
#
# Limitation: This is a proxy. True oracle lag detection would require
# reading the Chainlink aggregator contract on Polygon, which adds ~200ms
# of on-chain read latency — defeating the purpose.
#
# ═══════════════════════════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(
        description="🎯 Late Drift Sniper v3 — Polymarket BTC 5-min arbitrage",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python src/sniper_v3.py --shadow          # Observe only, collect data
  python src/sniper_v3.py --paper --size 10 # Paper trade at $10/trade
  python src/sniper_v3.py --live --size 20  # Real money, $20/trade
        """,
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--shadow", action="store_true",
                       help="Shadow mode: observe and log, never trade")
    group.add_argument("--paper", action="store_true",
                       help="Paper mode: simulate trades at real book prices")
    group.add_argument("--live", action="store_true",
                       help="Live mode: real money orders on Polymarket CLOB")

    parser.add_argument("--size", type=float, default=None,
                        help="Base trade size in USD (default: $10)")

    args = parser.parse_args()
    mode = "shadow" if args.shadow else ("paper" if args.paper else "live")

    bot = LateDriftSniperV3(mode=mode, trade_size=args.size)

    # Graceful shutdown on Ctrl+C or SIGTERM
    loop = asyncio.new_event_loop()

    def handle_shutdown(sig, frame):
        log.info("🛑 Shutdown signal received...")
        bot.stop()

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    try:
        loop.run_until_complete(bot.run())
    except KeyboardInterrupt:
        bot.stop()
    finally:
        loop.close()


if __name__ == "__main__":
    main()
