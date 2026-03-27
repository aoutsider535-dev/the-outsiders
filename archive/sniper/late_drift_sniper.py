"""
🎯 The Outsiders — LATE DRIFT SNIPER v1

Enter Polymarket BTC 5-min up/down markets in the final 30-60 seconds
when there's a clear BTC price drift + Polymarket mispricing.

Strategy:
  1. Monitor BTC spot price vs window open (P0)
  2. In last 30-60 seconds, check if drift ≥ vol-adjusted minimum
  3. Check if favored-side Polymarket ask ≤ MAX_BUY_PRICE
  4. Check if model edge ≥ MIN_EDGE over implied prob
  5. Apply optional streak/vol/oracle-lag filters
  6. Buy favored side with FOK market order. No early exit — hold to resolution.

Modes:
  --shadow   : Record all signals + book data, never place orders (data collection)
  --paper    : Simulate fills at real book prices, track P&L
  --live     : Real money orders via CLOB

Usage:
  python -m src.late_drift_sniper --shadow
  python -m src.late_drift_sniper --paper
  python -m src.late_drift_sniper --live --size 10
"""
import time
import json
import os
import sys
import sqlite3
import signal
import math
import statistics
import argparse
import concurrent.futures
from datetime import datetime, timezone, timedelta
from collections import deque

import requests
import ccxt

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import dotenv_values

PST = timezone(timedelta(hours=-8))
ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "drift_sniper.db")
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
STATE_FILE = os.path.join(DATA_DIR, "drift_sniper_state.json")
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

# ═══════════════════════════════════════════════════════════════════
# CONFIGURATION — All tunables in one place
# ═══════════════════════════════════════════════════════════════════

# Entry timing
ENTRY_WINDOW_START = 80    # Start looking at T-80s
ENTRY_WINDOW_END = 30      # Stop looking at T-30s (too late = no fill)
POLL_INTERVAL = 3          # Seconds between checks in entry window

# Drift requirements
BASE_MIN_DRIFT_PCT = 0.0015   # 0.15% minimum absolute drift
USE_VOL_ADJUSTED_DRIFT = True  # Scale drift threshold by current volatility

# Polymarket requirements
MAX_BUY_PRICE = 0.86      # Max price to pay for favored-side token
MIN_EDGE = 0.07            # 7% minimum edge over implied prob

# Streak filter
USE_STREAK_FILTER = True
STREAK_THRESHOLD = 4       # Flip to contrarian after 4+ same-direction outcomes
STREAK_SIZE_BOOST = 1.25   # Boost size by 25% on streak reversal

# Oracle lag detection
USE_ORACLE_LAG = True
LAG_EDGE_BONUS = 0.03      # +3% edge bonus if oracle lag detected
# (We approximate lag by comparing multi-exchange avg vs Binance-only)

# Sizing
RISK_PER_TRADE = 0.01      # 1% of bankroll base
MIN_TRADE_SIZE = 5.0       # Polymarket minimum
MAX_TRADE_SIZE = 100.0     # Cap per trade
DEFAULT_TRADE_SIZE = 10.0  # Used in paper/shadow mode

# Volatility buffer
VOL_LOOKBACK = 84          # 84 x 5-min = 7 hours of data for vol estimation
VOL_BUFFER_FILE = os.path.join(DATA_DIR, "drift_sniper_vol.json")

# Multi-exchange spot sources
EXCHANGES = ["binance", "bybit", "coinbase"]


class LateDriftSniper:
    def __init__(self, mode="shadow", trade_size=None):
        """
        mode: 'shadow' (observe only), 'paper' (simulate), 'live' (real orders)
        """
        self.mode = mode
        self.trade_size = trade_size or DEFAULT_TRADE_SIZE
        self.running = True
        self.client = None  # CLOB client, only in live mode
        self.balance = 1000.0 if mode != "live" else 0.0
        self.funder = None

        # Price tracking
        self.price_buffer = deque(maxlen=VOL_LOOKBACK)  # (timestamp, price) for vol calc
        self.avg_vol = None  # Average volatility baseline

        # Streak tracking
        self.streak_history = deque(maxlen=10)  # Last 10 outcomes: 'up' or 'down'

        # Stats
        self.stats = {
            "windows_observed": 0,
            "signals_generated": 0,
            "trades_taken": 0,
            "trades_won": 0,
            "trades_lost": 0,
            "total_cost": 0.0,
            "total_pnl": 0.0,
        }

        # Exchange clients for multi-source pricing
        self.exchanges = {}
        self._init_exchanges()

        # Load persisted state
        self._load_state()
        self._init_db()

        if mode == "live":
            self._init_clob_client()

    def log(self, msg):
        ts = datetime.now(PST).strftime("%H:%M:%S")
        print(f"[{ts}] {msg}", flush=True)
        log_path = os.path.join(DATA_DIR, "drift_sniper.log")
        try:
            with open(log_path, "a") as f:
                f.write(f"[{datetime.now(PST).strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
        except:
            pass

    # ═══════════════════════════════════════════════════════════════
    # INITIALIZATION
    # ═══════════════════════════════════════════════════════════════

    def _init_exchanges(self):
        """Initialize ccxt exchange clients for multi-source spot pricing."""
        for name in EXCHANGES:
            try:
                ex_class = getattr(ccxt, name)
                self.exchanges[name] = ex_class({"timeout": 3000})
            except Exception as e:
                self.log(f"⚠️ Failed to init {name}: {e}")

    def _init_clob_client(self):
        """Initialize Polymarket CLOB client for live trading."""
        from py_clob_client.client import ClobClient

        config = dotenv_values(ENV_PATH)
        pk = config.get("POLYGON_PRIVATE_KEY", "")
        addr = config.get("POLYGON_WALLET_ADDRESS", "")
        if not pk or not addr:
            self.log("❌ Missing POLYGON_PRIVATE_KEY or POLYGON_WALLET_ADDRESS in .env")
            sys.exit(1)

        host = "https://clob.polymarket.com"
        client = ClobClient(host, key=pk, chain_id=137)
        creds = client.create_or_derive_api_creds()
        self.client = ClobClient(
            host, key=pk, chain_id=137,
            creds=creds, signature_type=1, funder=addr,
        )
        self.funder = addr
        self._refresh_balance()
        self.log(f"✅ CLOB connected | Balance: ${self.balance:.2f}")

    def _refresh_balance(self):
        """Refresh USDC balance from CLOB."""
        if self.mode != "live":
            return
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams
            bal = self.client.get_balance_allowance(BalanceAllowanceParams(asset_type="COLLATERAL"))
            raw = float(bal.get("balance", 0)) if isinstance(bal, dict) else 0
            self.balance = raw / 1e6 if raw > 1e6 else raw
        except Exception as e:
            self.log(f"⚠️ Balance check failed: {e}")

    def _init_db(self):
        """Create SQLite tables for trade logging and shadow observations."""
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
                up_best_ask REAL,
                up_best_bid REAL,
                down_best_ask REAL,
                down_best_bid REAL,
                up_ask_depth_50 REAL,
                down_ask_depth_50 REAL,
                favored_side TEXT,
                favored_ask REAL,
                model_prob REAL,
                implied_prob REAL,
                edge REAL,
                streak_count INTEGER,
                streak_direction TEXT,
                oracle_lag_detected INTEGER DEFAULT 0,
                signal_fired INTEGER DEFAULT 0,
                trade_taken INTEGER DEFAULT 0,
                buy_price REAL,
                shares REAL,
                cost_usdc REAL,
                order_id TEXT,
                fill_status TEXT
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
                order_id TEXT,
                resolved INTEGER DEFAULT 0,
                won INTEGER,
                pnl REAL,
                resolution_price REAL,
                btc_open REAL,
                btc_close REAL
            );

            CREATE TABLE IF NOT EXISTS resolutions (
                window_ts INTEGER PRIMARY KEY,
                outcome TEXT NOT NULL,
                btc_open REAL,
                btc_close REAL,
                resolved_at TEXT
            );
        """)
        conn.close()

    def _load_state(self):
        """Load persisted streak history and vol buffer."""
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r") as f:
                    state = json.load(f)
                self.streak_history = deque(state.get("streak_history", []), maxlen=10)
                self.avg_vol = state.get("avg_vol")
                self.log(f"📂 Loaded state: {len(self.streak_history)} streak entries, avg_vol={self.avg_vol}")
            except:
                pass

        if os.path.exists(VOL_BUFFER_FILE):
            try:
                with open(VOL_BUFFER_FILE, "r") as f:
                    buf = json.load(f)
                self.price_buffer = deque([(e["t"], e["p"]) for e in buf], maxlen=VOL_LOOKBACK)
            except:
                pass

    def _save_state(self):
        """Persist streak and vol state to disk."""
        try:
            with open(STATE_FILE, "w") as f:
                json.dump({
                    "streak_history": list(self.streak_history),
                    "avg_vol": self.avg_vol,
                    "updated": datetime.now(PST).isoformat(),
                }, f)
        except:
            pass
        try:
            with open(VOL_BUFFER_FILE, "w") as f:
                json.dump([{"t": t, "p": p} for t, p in self.price_buffer], f)
        except:
            pass

    # ═══════════════════════════════════════════════════════════════
    # MARKET DATA
    # ═══════════════════════════════════════════════════════════════

    def _get_binance_spot(self):
        """Get BTC/USDT spot from Binance. Fast, reliable."""
        try:
            r = requests.get(
                "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT",
                timeout=2,
            )
            return float(r.json()["price"])
        except:
            return None

    def _get_multi_exchange_spot(self):
        """Get BTC spot from multiple exchanges. Returns dict {exchange: price}.
        Uses direct REST for speed — ccxt fetch_ticker is slow on first call."""
        results = {}

        def fetch_binance():
            try:
                r = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=2)
                return "binance", float(r.json()["price"])
            except:
                return "binance", None

        def fetch_bybit():
            try:
                r = requests.get("https://api.bybit.com/v5/market/tickers?category=spot&symbol=BTCUSDT", timeout=2)
                data = r.json()
                return "bybit", float(data["result"]["list"][0]["lastPrice"])
            except:
                return "bybit", None

        def fetch_coinbase():
            try:
                r = requests.get("https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=2)
                return "coinbase", float(r.json()["data"]["amount"])
            except:
                return "coinbase", None

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            futures = [
                pool.submit(fetch_binance),
                pool.submit(fetch_bybit),
                pool.submit(fetch_coinbase),
            ]
            for f in concurrent.futures.as_completed(futures, timeout=5):
                try:
                    name, price = f.result(timeout=1)
                    if price:
                        results[name] = price
                except:
                    pass
        return results

    def _get_binance_candle_open(self, window_ts):
        """Get the 5m candle open from Binance for a specific window."""
        try:
            r = requests.get(
                "https://api.binance.com/api/v3/klines",
                params={"symbol": "BTCUSDT", "interval": "5m", "startTime": window_ts * 1000, "limit": 1},
                timeout=2,
            )
            candles = r.json()
            if candles:
                return float(candles[0][1])
        except:
            pass
        return None

    def _find_market(self, window_ts):
        """Find the Polymarket event/market for a BTC 5-min window."""
        slug = f"btc-updown-5m-{window_ts}"
        try:
            r = requests.get(f"{GAMMA_BASE}/events", params={"slug": slug}, timeout=5)
            if r.status_code != 200:
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
        except Exception as e:
            self.log(f"⚠️ Market lookup failed: {e}")
            return None

    def _get_orderbook(self, token_id):
        """Fetch CLOB orderbook for a token. Returns (asks_sorted_asc, bids_sorted_desc)."""
        try:
            r = requests.get(f"{CLOB_BASE}/book", params={"token_id": token_id}, timeout=5)
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
        except Exception as e:
            self.log(f"⚠️ Book fetch failed: {e}")
            return [], []

    def _get_book_depth(self, asks, max_price):
        """Total available shares at or below max_price."""
        return sum(size for price, size in asks if price <= max_price)

    # ═══════════════════════════════════════════════════════════════
    # VOLATILITY
    # ═══════════════════════════════════════════════════════════════

    def _update_vol_buffer(self, price):
        """Add a price point to the volatility buffer."""
        self.price_buffer.append((time.time(), price))

    def _calc_current_vol(self):
        """Calculate current 1-hour rolling volatility (std dev of 5-min returns).
        Uses last 12 data points (12 * 5min = 1 hour)."""
        if len(self.price_buffer) < 3:
            return None
        # Use last 12 entries or whatever we have
        recent = list(self.price_buffer)[-12:]
        returns = []
        for i in range(1, len(recent)):
            ret = (recent[i][1] - recent[i - 1][1]) / recent[i - 1][1]
            returns.append(abs(ret))
        if not returns:
            return None
        return statistics.mean(returns)  # Average absolute return as vol proxy

    def _get_vol_adjusted_min_drift(self):
        """Get volatility-adjusted minimum drift threshold."""
        if not USE_VOL_ADJUSTED_DRIFT:
            return BASE_MIN_DRIFT_PCT

        current_vol = self._calc_current_vol()
        if current_vol is None or self.avg_vol is None or self.avg_vol == 0:
            return BASE_MIN_DRIFT_PCT

        # Scale: high vol = require bigger drift, low vol = accept smaller drift
        ratio = current_vol / self.avg_vol
        adjusted = BASE_MIN_DRIFT_PCT * max(ratio, 0.5)  # Floor at 50% of base
        return adjusted

    def _update_avg_vol(self):
        """Update long-run average volatility baseline."""
        current = self._calc_current_vol()
        if current is None:
            return
        if self.avg_vol is None:
            self.avg_vol = current
        else:
            # Exponential moving average with slow decay
            self.avg_vol = 0.99 * self.avg_vol + 0.01 * current

    # ═══════════════════════════════════════════════════════════════
    # STREAK FILTER
    # ═══════════════════════════════════════════════════════════════

    def _get_streak(self):
        """Get current streak: (count, direction).
        count=3, direction='up' means last 3 outcomes were all 'up'."""
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

    def _record_outcome(self, outcome):
        """Record a resolution outcome for streak tracking."""
        self.streak_history.append(outcome)
        self._save_state()

    # ═══════════════════════════════════════════════════════════════
    # ORACLE LAG DETECTION
    # ═══════════════════════════════════════════════════════════════

    def _detect_oracle_lag(self, spot_prices):
        """Detect if multi-exchange consensus diverges from single source.
        Returns True if potential oracle lag detected.

        Logic: If Binance, Bybit, and Coinbase agree on direction
        but have spread > threshold, Chainlink (which aggregates with delay)
        may not have caught up yet.
        """
        if not USE_ORACLE_LAG:
            return False

        prices = list(spot_prices.values())
        if len(prices) < 2:
            return False

        # Check if all exchanges agree on direction from mean
        mean_price = statistics.mean(prices)
        spread_pct = (max(prices) - min(prices)) / mean_price * 100

        # If spread is > 0.01% across exchanges, there may be latency
        # This is a rough proxy — real oracle lag would compare Chainlink on-chain
        return spread_pct > 0.01

    # ═══════════════════════════════════════════════════════════════
    # CONTINUATION PROBABILITY MODEL
    # ═══════════════════════════════════════════════════════════════

    def _estimate_continuation_prob(self, drift_pct, time_left_sec):
        """Estimate probability that current drift direction holds through resolution.

        Model: Brownian motion with drift. The larger the move and the less time
        remaining, the harder it is to reverse.

        drift_pct: absolute drift as decimal (0.15% = 0.0015)
        time_left_sec: seconds until window closes

        Returns: probability [0.50, 0.98]
        """
        # Normalize time remaining as fraction of window
        time_frac = time_left_sec / 300.0

        # Empirical model (will be calibrated with shadow data):
        # - Drift contribution: bigger drift = higher prob
        #   At 0.15% drift, ~180 * 0.0015 = 0.27
        #   At 0.30% drift, ~180 * 0.0030 = 0.54
        # - Time penalty: more time left = more chance of reversal
        #   At 30s left (0.1 frac): penalty = 0.1 * 0.05 = 0.005
        #   At 60s left (0.2 frac): penalty = 0.2 * 0.05 = 0.01
        drift_contrib = abs(drift_pct) * 180
        time_penalty = time_frac * 0.05

        prob = 0.50 + drift_contrib - time_penalty
        return max(0.50, min(prob, 0.98))

    # ═══════════════════════════════════════════════════════════════
    # TRADE SIZING
    # ═══════════════════════════════════════════════════════════════

    def _calc_trade_size(self, edge, streak_reversal=False):
        """Calculate trade size based on edge and bankroll.
        Higher edge = bigger size (Kelly-lite).
        """
        # Base: RISK_PER_TRADE * balance
        base = RISK_PER_TRADE * self.balance
        # Scale with edge: 7% edge = 1x, 14% edge = 2x
        edge_multiplier = edge / MIN_EDGE
        size = base * edge_multiplier

        if streak_reversal:
            size *= STREAK_SIZE_BOOST

        return max(MIN_TRADE_SIZE, min(size, MAX_TRADE_SIZE, self.balance * 0.05))

    # ═══════════════════════════════════════════════════════════════
    # ORDER EXECUTION
    # ═══════════════════════════════════════════════════════════════

    def _place_order(self, token_id, price, size, market):
        """Place a FOK buy order on the CLOB. Returns order result or None."""
        if self.mode != "live":
            return {"order_id": f"{self.mode}_{int(time.time())}", "status": "matched"}

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
            signed = self.client.create_market_order(order_args, options)
            resp = self.client.post_order(signed, orderType=OrderType.FOK)

            if resp and resp.get("success"):
                return {
                    "order_id": resp.get("orderID", ""),
                    "status": resp.get("status", "matched"),
                }
            else:
                self.log(f"❌ Order rejected: {resp}")
                return None
        except Exception as e:
            self.log(f"❌ Order error: {e}")
            return None

    # ═══════════════════════════════════════════════════════════════
    # RESOLUTION TRACKER
    # ═══════════════════════════════════════════════════════════════

    def _check_resolutions(self):
        """Check if any pending trades have resolved."""
        conn = sqlite3.connect(DB_PATH)
        pending = conn.execute(
            "SELECT id, window_ts, side, buy_price, shares, cost_usdc FROM trades WHERE resolved = 0"
        ).fetchall()

        for trade_id, window_ts, side, buy_price, shares, cost in pending:
            # Check if this window has resolved
            slug = f"btc-updown-5m-{window_ts}"
            try:
                r = requests.get(f"{GAMMA_BASE}/events", params={"slug": slug}, timeout=5)
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

                # Check if actually resolved (one side = 1.0)
                if up_price < 0.99 and down_price < 0.99:
                    continue  # Not yet resolved

                outcome = "up" if up_price > 0.5 else "down"
                won = 1 if outcome == side else 0
                pnl = (shares * 1.0 - cost) if won else -cost

                # Get BTC close price
                btc_close = self._get_binance_candle_open(window_ts + 300)  # Next candle open ≈ this candle close

                conn.execute(
                    "UPDATE trades SET resolved=1, won=?, pnl=?, resolution_price=?, btc_close=? WHERE id=?",
                    (won, pnl, up_price, btc_close, trade_id),
                )

                # Record resolution
                conn.execute(
                    "INSERT OR REPLACE INTO resolutions (window_ts, outcome, btc_close, resolved_at) VALUES (?, ?, ?, ?)",
                    (window_ts, outcome, btc_close, datetime.now(PST).isoformat()),
                )
                conn.commit()

                # Update streak
                self._record_outcome(outcome)

                # Update stats
                if won:
                    self.stats["trades_won"] += 1
                    self.stats["total_pnl"] += pnl
                    self.log(f"✅ WIN window {window_ts} | {side.upper()} | +${pnl:.2f}")
                else:
                    self.stats["trades_lost"] += 1
                    self.stats["total_pnl"] += pnl
                    self.log(f"❌ LOSS window {window_ts} | {side.upper()} | -${abs(pnl):.2f}")

                if self.mode == "paper":
                    self.balance += (shares if won else 0)

            except Exception as e:
                self.log(f"⚠️ Resolution check error: {e}")
                continue

        conn.close()

    # ═══════════════════════════════════════════════════════════════
    # MAIN SIGNAL LOGIC
    # ═══════════════════════════════════════════════════════════════

    def _evaluate_window(self, window_ts, time_left):
        """
        Evaluate whether to enter a trade for the current window.
        Returns a signal dict or None.
        """
        # 1. Get BTC open price for this window
        btc_open = self._get_binance_candle_open(window_ts)
        if btc_open is None:
            return None

        # 2. Get current multi-exchange spot prices
        spot_prices = self._get_multi_exchange_spot()
        binance_spot = spot_prices.get("binance")
        if binance_spot is None:
            # Fallback to direct Binance API
            binance_spot = self._get_binance_spot()
        if binance_spot is None:
            return None

        # Use multi-exchange average as best estimate
        avg_spot = statistics.mean(spot_prices.values()) if spot_prices else binance_spot

        # Update vol buffer
        self._update_vol_buffer(avg_spot)
        self._update_avg_vol()

        # 3. Calculate drift
        drift_pct = (avg_spot - btc_open) / btc_open  # Signed decimal
        abs_drift = abs(drift_pct)

        # 4. Volatility-adjusted minimum drift
        vol_min = self._get_vol_adjusted_min_drift()
        current_vol = self._calc_current_vol()

        # 5. Find market and get orderbooks
        market = self._find_market(window_ts)
        if market is None or market.get("closed"):
            return None

        up_asks, up_bids = self._get_orderbook(market["token_ids"]["up"])
        down_asks, down_bids = self._get_orderbook(market["token_ids"]["down"])

        # Best prices
        up_best_ask = up_asks[0][0] if up_asks else None
        up_best_bid = up_bids[0][0] if up_bids else None
        down_best_ask = down_asks[0][0] if down_asks else None
        down_best_bid = down_bids[0][0] if down_bids else None

        # Available depth up to MAX_BUY_PRICE
        up_depth = self._get_book_depth(up_asks, MAX_BUY_PRICE)
        down_depth = self._get_book_depth(down_asks, MAX_BUY_PRICE)

        # 6. Determine favored side
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

        # 7. Continuation probability model
        model_prob = self._estimate_continuation_prob(drift_pct, time_left)

        # 8. Streak filter
        streak_count, streak_dir = self._get_streak()
        streak_reversal = False

        if USE_STREAK_FILTER and streak_count >= STREAK_THRESHOLD and streak_dir is not None:
            # Streak is long — consider contrarian play
            # If drift agrees with streak, flip to contrarian
            if favored == streak_dir:
                # Drift is WITH the streak. Contrarian says: fade it.
                # Flip model_prob to favor the other side
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
                self.log(f"🔄 STREAK REVERSAL: {streak_count}x {streak_dir}, flipping to {favored}")

        # 9. Oracle lag detection
        oracle_lag = self._detect_oracle_lag(spot_prices)
        if oracle_lag and USE_ORACLE_LAG:
            model_prob = min(model_prob + LAG_EDGE_BONUS, 0.98)

        # 10. Calculate edge
        implied_prob = favored_ask if favored_ask else 0.99
        edge = model_prob - implied_prob

        # Build observation record
        obs = {
            "window_ts": window_ts,
            "time_left": time_left,
            "btc_open": btc_open,
            "btc_spot": avg_spot,
            "drift_pct": drift_pct,
            "abs_drift": abs_drift,
            "vol_min": vol_min,
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
            "signal_fired": False,
            "trade_taken": False,
        }

        # ── CHECK ALL ENTRY RULES ──

        # Rule 1: Time — already enforced by caller (30-60s window)

        # Rule 2: Drift ≥ volatility-adjusted minimum
        if abs_drift < vol_min:
            self._record_observation(obs)
            return None

        # Rule 3: Favored-side best ask ≤ MAX_BUY_PRICE
        if favored_ask is None or favored_ask > MAX_BUY_PRICE:
            self._record_observation(obs)
            return None

        # Rule 4: Edge ≥ MIN_EDGE
        if edge < MIN_EDGE:
            self._record_observation(obs)
            return None

        # Rule 5: Sufficient depth (at least our trade size worth of shares)
        if favored_depth < MIN_TRADE_SIZE:
            self._record_observation(obs)
            return None

        # ALL RULES PASSED — signal fires
        obs["signal_fired"] = True
        self.stats["signals_generated"] += 1

        # Calculate size
        trade_size = self._calc_trade_size(edge, streak_reversal)
        # Don't exceed available depth
        trade_size = min(trade_size, favored_depth * favored_ask)

        # Find the actual fill price (walk the book)
        fill_price, fill_shares = self._simulate_fill(favored_asks, trade_size)
        if fill_shares < MIN_TRADE_SIZE:
            self._record_observation(obs)
            return None

        obs["trade_taken"] = True
        obs["buy_price"] = fill_price
        obs["shares"] = fill_shares
        obs["cost"] = fill_price * fill_shares

        self._record_observation(obs)
        return obs

    def _simulate_fill(self, asks, target_spend):
        """Walk the ask side of the book to simulate a market buy.
        Returns (avg_fill_price, total_shares) for up to target_spend USD.
        Only considers asks ≤ MAX_BUY_PRICE."""
        total_shares = 0
        total_cost = 0

        for price, size in asks:
            if price > MAX_BUY_PRICE:
                break
            remaining_budget = target_spend - total_cost
            if remaining_budget <= 0:
                break
            affordable_shares = remaining_budget / price
            take = min(size, affordable_shares)
            total_shares += take
            total_cost += take * price

        avg_price = total_cost / total_shares if total_shares > 0 else 0
        return avg_price, total_shares

    def _record_observation(self, obs):
        """Log an observation to SQLite."""
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute(
                """INSERT INTO observations
                   (timestamp, window_ts, time_left_sec, btc_open, btc_spot, drift_pct,
                    vol_1h, vol_adjusted_min, up_best_ask, up_best_bid, down_best_ask, down_best_bid,
                    up_ask_depth_50, down_ask_depth_50, favored_side, favored_ask,
                    model_prob, implied_prob, edge, streak_count, streak_direction,
                    oracle_lag_detected, signal_fired, trade_taken, buy_price, shares, cost_usdc)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    datetime.now(PST).isoformat(), obs["window_ts"], obs["time_left"],
                    obs["btc_open"], obs["btc_spot"], obs["drift_pct"],
                    obs.get("current_vol"), obs["vol_min"],
                    obs["up_best_ask"], obs["up_best_bid"],
                    obs["down_best_ask"], obs["down_best_bid"],
                    obs["up_depth"], obs["down_depth"],
                    obs["favored"], obs["favored_ask"],
                    obs["model_prob"], obs["implied_prob"], obs["edge"],
                    obs["streak_count"], obs["streak_dir"],
                    1 if obs["oracle_lag"] else 0,
                    1 if obs["signal_fired"] else 0,
                    1 if obs["trade_taken"] else 0,
                    obs.get("buy_price"), obs.get("shares"), obs.get("cost"),
                ),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            self.log(f"⚠️ DB write error: {e}")

    def _record_trade(self, obs):
        """Record a trade to the trades table."""
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute(
                """INSERT INTO trades
                   (timestamp, window_ts, side, buy_price, shares, cost_usdc,
                    model_prob, edge, order_id, btc_open)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    datetime.now(PST).isoformat(), obs["window_ts"], obs["favored"],
                    obs["buy_price"], obs["shares"], obs["cost"],
                    obs["model_prob"], obs["edge"],
                    obs.get("order_id", ""), obs["btc_open"],
                ),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            self.log(f"⚠️ Trade DB write error: {e}")

    # ═══════════════════════════════════════════════════════════════
    # MAIN LOOP
    # ═══════════════════════════════════════════════════════════════

    def run(self):
        """Main event loop. Watches every 5-min window."""
        self.log(f"🎯 Late Drift Sniper v1 | Mode: {self.mode.upper()} | Size: ${self.trade_size}")
        self.log(f"   Min drift: {BASE_MIN_DRIFT_PCT*100:.2f}% | Max price: ${MAX_BUY_PRICE} | Min edge: {MIN_EDGE*100:.0f}%")
        self.log(f"   Streak filter: {'ON' if USE_STREAK_FILTER else 'OFF'} | Vol adjust: {'ON' if USE_VOL_ADJUSTED_DRIFT else 'OFF'} | Oracle lag: {'ON' if USE_ORACLE_LAG else 'OFF'}")

        traded_windows = set()

        while self.running:
            try:
                now = time.time()
                window_ts = int(now // 300) * 300
                window_end = window_ts + 300
                time_left = window_end - now

                # Phase 1: Wait for entry window (T-60s to T-30s)
                if time_left > ENTRY_WINDOW_START:
                    # Too early — sleep until entry window opens
                    sleep_for = time_left - ENTRY_WINDOW_START - 1
                    if sleep_for > 10:
                        # Check resolutions while we wait
                        self._check_resolutions()
                        self._save_state()
                    if sleep_for > 0:
                        time.sleep(min(sleep_for, 30))
                    continue

                # Phase 2: Entry window active
                if ENTRY_WINDOW_END <= time_left <= ENTRY_WINDOW_START:
                    if window_ts not in traded_windows:
                        self.stats["windows_observed"] += 1

                        signal = self._evaluate_window(window_ts, time_left)

                        if signal and signal.get("trade_taken"):
                            # EXECUTE
                            token_id_key = signal["favored"]  # 'up' or 'down'
                            market = self._find_market(window_ts)

                            if market and not market.get("closed"):
                                token_id = market["token_ids"][token_id_key]
                                result = self._place_order(
                                    token_id, signal["buy_price"],
                                    signal["shares"], market,
                                )
                                if result:
                                    signal["order_id"] = result.get("order_id", "")
                                    self._record_trade(signal)
                                    traded_windows.add(window_ts)
                                    self.stats["trades_taken"] += 1

                                    if self.mode == "paper":
                                        self.balance -= signal["cost"]

                                    self.log(
                                        f"🎯 TRADE {signal['favored'].upper()} | "
                                        f"drift {signal['drift_pct']*100:+.3f}% | "
                                        f"ask ${signal['favored_ask']:.2f} → fill ${signal['buy_price']:.3f} | "
                                        f"edge {signal['edge']*100:.1f}% | "
                                        f"{signal['shares']:.1f} shares @ ${signal['cost']:.2f}"
                                    )
                                else:
                                    self.log(f"❌ Order failed for window {window_ts}")
                            else:
                                self.log(f"⚠️ Market unavailable for execution")
                        elif signal:
                            # Signal fired but trade not taken (insufficient depth etc)
                            pass
                        else:
                            # No signal — log summary at T-45s (midpoint)
                            if 43 <= time_left <= 47:
                                spot = self._get_binance_spot()
                                p0 = self._get_binance_candle_open(window_ts)
                                if spot and p0:
                                    drift = (spot - p0) / p0 * 100
                                    self.log(
                                        f"👀 Window {window_ts} | T-{time_left:.0f}s | "
                                        f"drift {drift:+.3f}% | "
                                        f"W{self.stats['trades_won']}-L{self.stats['trades_lost']} "
                                        f"(${self.stats['total_pnl']:+.2f})"
                                    )

                    time.sleep(POLL_INTERVAL)
                    continue

                # Phase 3: Past entry window (T < 30s) — wait for next window
                if time_left < ENTRY_WINDOW_END:
                    # Sleep until next window's entry period
                    next_window_entry = window_end + (300 - ENTRY_WINDOW_START)
                    sleep_for = next_window_entry - now - 1
                    if sleep_for > 5:
                        self._check_resolutions()
                        self._save_state()
                    time.sleep(max(sleep_for, 1))
                    continue

            except KeyboardInterrupt:
                self.log("🛑 Shutting down...")
                self.running = False
            except Exception as e:
                self.log(f"💥 Loop error: {e}")
                time.sleep(5)

        # Final save
        self._save_state()
        self._print_summary()

    def _print_summary(self):
        """Print session summary."""
        s = self.stats
        total = s["trades_won"] + s["trades_lost"]
        wr = s["trades_won"] / total * 100 if total else 0
        self.log("=" * 60)
        self.log(f"📊 SESSION SUMMARY")
        self.log(f"   Windows observed: {s['windows_observed']}")
        self.log(f"   Signals generated: {s['signals_generated']}")
        self.log(f"   Trades taken: {s['trades_taken']}")
        self.log(f"   Resolved: {total} ({s['trades_won']}W-{s['trades_lost']}L, {wr:.1f}% WR)")
        self.log(f"   P&L: ${s['total_pnl']:+.2f}")
        self.log(f"   Balance: ${self.balance:.2f}")
        self.log("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="Late Drift Sniper — Polymarket BTC 5-min")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--shadow", action="store_true", help="Shadow mode: observe and log, no trades")
    group.add_argument("--paper", action="store_true", help="Paper mode: simulate trades at real prices")
    group.add_argument("--live", action="store_true", help="Live mode: real money orders")
    parser.add_argument("--size", type=float, default=None, help="Trade size in USD")
    args = parser.parse_args()

    mode = "shadow" if args.shadow else ("paper" if args.paper else "live")

    bot = LateDriftSniper(mode=mode, trade_size=args.size)

    # Graceful shutdown
    def handle_signal(sig, frame):
        bot.log("🛑 Signal received, shutting down...")
        bot.running = False
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    bot.run()


if __name__ == "__main__":
    main()
