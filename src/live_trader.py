"""
🏞 The Outsiders — LIVE Trading Engine

Multi-strategy live trading on Polymarket BTC 5-min markets.
Uses real USDC, places real orders via the CLOB API.

Strategies:
1. ⚡ Momentum
2. 🔄 Mean Reversion  
3. 📊 OB Imbalance
4. 🧠 Smart Money
"""
import time
import json
import os
import signal
import sqlite3
import sys
import requests
from datetime import datetime, timezone, timedelta
from dotenv import dotenv_values

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    OrderArgs, PartialCreateOrderOptions, BalanceAllowanceParams
)
from py_clob_client.order_builder.constants import BUY

from src.data.polymarket import get_full_market_snapshot, check_market_resolution, find_btc_5min_market
from src.data.price_feed import get_recent_candles, fetch_kraken_ticker, fetch_kraken_orderbook
from src.strategies.btc_5min import (
    generate_signal as momentum_signal,
    format_signal_message as momentum_format,
)
from src.strategies.trend_rider import (
    generate_signal as trend_rider_signal,
    format_signal_message as trend_rider_format,
    DEFAULTS as TREND_RIDER_DEFAULTS,
)
from src.strategies.htf_context import get_htf_context, htf_agrees_with
from src.strategies.mean_reversion import (
    generate_signal as meanrev_signal,
    format_signal_message as meanrev_format,
    DEFAULT_PARAMS as MEANREV_DEFAULTS,
)
from src.strategies.orderbook_imbalance import (
    generate_signal as ob_signal,
    format_signal_message as ob_format,
    DEFAULT_PARAMS as OB_DEFAULTS,
)
from src.strategies.smart_money import (
    generate_signal as smart_signal,
    format_signal_message as smart_format,
    DEFAULT_PARAMS as SMART_DEFAULTS,
)
from src.database import init_db, insert_trade, close_trade, DB_PATH, get_connection
from src.data.binance import get_derivatives_signals
from src.ml.features import extract_features, add_cross_strategy_features
from src.ml.meta_learner import MetaLearner
from src.redeemer import Redeemer

PST = timezone(timedelta(hours=-8))
ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
PARAMS_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "best_params.json")


def load_momentum_params():
    if os.path.exists(PARAMS_PATH):
        with open(PARAMS_PATH) as f:
            data = json.load(f)
        return data.get("params", {})
    return {
        "lookback_minutes": 15, "min_edge_pct": 3.0, "take_profit_pct": 70.0,
        "stop_loss_pct": 20.0, "risk_per_trade_pct": 5.0, "max_open_trades": 1,
        "w_momentum": 0.50, "w_trend": 0.20, "w_last_candle": 0.20,
        "w_orderbook": 0.15, "w_volatility": 0.10,
        "vol_low_threshold": 0.0005, "vol_high_threshold": 0.002,
        "momentum_strong_threshold": 0.6,
    }


STRATEGY_CONFIG = {
    "trend_rider": {
        "name": "btc_5min_trend_rider_LIVE",
        "display": "📈 Trend Rider",
        "load_params": lambda: TREND_RIDER_DEFAULTS.copy(),
    },
    # REPLACED by Trend Rider — backtested 56% WR (151 trades), 73% OOS (37 trades)
    # "momentum": {
    #     "name": "btc_5min_momentum_LIVE",
    #     "display": "⚡ Momentum",
    #     "load_params": load_momentum_params,
    # },
    # PAUSED — 20% WR on v3 (10 trades), -$74 across v2+v3. Revisit later.
    # "mean_reversion": {
    #     "name": "btc_5min_meanrev_LIVE",
    #     "display": "🔄 Mean Reversion",
    #     "load_params": lambda: MEANREV_DEFAULTS.copy(),
    # },
    "orderbook_imbalance": {
        "name": "btc_5min_ob_imbalance_LIVE",
        "display": "📊 OB Imbalance",
        "load_params": lambda: OB_DEFAULTS.copy(),
    },
    "smart_money": {
        "name": "btc_5min_smart_money_LIVE",
        "display": "🧠 Smart Money",
        "load_params": lambda: SMART_DEFAULTS.copy(),
    },
}


class StrategyState:
    def __init__(self, key, config):
        self.key = key
        self.name = config["name"]
        self.display = config["display"]
        self.params = config["load_params"]()
        self.open_trades = []
        self.traded_slugs = set()
        self.wins = 0
        self.losses = 0
        self.trade_count = 0
        self.total_pnl = 0.0
        self.consecutive_losses = 0
        self.cooldown_skips = 0  # Trades to skip after loss streak
        self.kelly_fraction = 0.04  # Default 4%, updated by Kelly calc
        self.kelly_last_calc = 0  # Trade count at last Kelly recalc
        self.recent_outcomes = []  # Last N outcome directions for ML features


class LiveTrader:
    def __init__(self, trade_size=5.0, max_per_strategy=5, max_total=15,
                 trade_size_pct=4.0, trade_size_min=5.0, trade_size_max=50.0):
        self.trade_size = trade_size  # USD per trade (fallback)
        self.trade_size_pct = trade_size_pct  # % of balance per trade
        self.trade_size_min = trade_size_min
        self.trade_size_max = trade_size_max
        self.max_per_strategy = max_per_strategy
        self.max_total = max_total
        self.running = True
        self.strategies = {}
        self.client = None
        self.redeemer = None
        self.initial_balance = 0.0
        self.ml = MetaLearner(DB_PATH)
        
        # Daily loss circuit breaker
        self.daily_loss_limit_pct = 15.0   # Stop trading if daily loss exceeds 15% of starting balance
        self.daily_loss_limit_abs = 25.0   # Or $25 absolute, whichever is hit first
        self.daily_pnl = 0.0
        self.daily_start_balance = 0.0
        self.daily_date = None
        self.circuit_breaker_tripped = False
        
        for key, config in STRATEGY_CONFIG.items():
            self.strategies[key] = StrategyState(key, config)
        
        init_db()
        signal.signal(signal.SIGINT, self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)
    
    def _shutdown(self, signum, frame):
        print("\n⏹️  Shutting down live trader...")
        self.running = False
    
    def log(self, msg):
        ts = datetime.now(timezone.utc).astimezone(PST).strftime("%I:%M:%S %p")
        print(f"[{ts}] {msg}")
    
    def _init_client(self):
        """Initialize Polymarket CLOB client."""
        config = dotenv_values(ENV_PATH)
        pk = config.get("POLYGON_PRIVATE_KEY", "")
        addr = config.get("POLYGON_WALLET_ADDRESS", "")
        
        if not pk or not addr:
            raise ValueError("Missing POLYGON_PRIVATE_KEY or POLYGON_WALLET_ADDRESS in .env")
        
        host = "https://clob.polymarket.com"
        client = ClobClient(host, key=pk, chain_id=137)
        creds = client.create_or_derive_api_creds()
        self.client = ClobClient(
            host, key=pk, chain_id=137, creds=creds,
            signature_type=1, funder=addr
        )
        self.funder = addr
        
        # Initialize redeemer for auto-claiming winnings
        try:
            self.redeemer = Redeemer(pk, addr)
            self.log("✅ Redeemer initialized (auto-claim enabled)")
        except Exception as e:
            self.log(f"⚠️ Redeemer init failed (claims disabled): {e}")
            self.redeemer = None
        
        return True
    
    def _check_geoblock(self):
        """Verify we're not geoblocked."""
        try:
            r = requests.get("https://polymarket.com/api/geoblock", timeout=10)
            data = r.json()
            if data.get("blocked"):
                self.log(f"🚫 GEOBLOCKED from {data.get('country')}! VPN may be off.")
                return False
            return True
        except:
            self.log("⚠️ Could not check geoblock, proceeding cautiously")
            return True
    
    def _get_balance(self):
        """Get current USDC balance."""
        try:
            bal = self.client.get_balance_allowance(
                BalanceAllowanceParams(asset_type="COLLATERAL")
            )
            raw = int(bal.get("balance", 0))
            return raw / 1e6
        except Exception as e:
            self.log(f"⚠️ Balance check error: {e}")
            return None
    
    def _calc_trade_size(self, balance=None, strategy_key=None):
        """Calculate dynamic trade size using Half Kelly per strategy, clamped to [min, max].
        Kelly fraction is recalculated every KELLY_RECALC_INTERVAL trades per strategy."""
        if balance is None:
            balance = self._get_balance()
        if balance is None or balance <= 0:
            return self.trade_size_min
        
        # Use strategy-specific Kelly fraction if available
        fraction = self.trade_size_pct / 100  # fallback to flat %
        if strategy_key and strategy_key in self.strategies:
            state = self.strategies[strategy_key]
            self._maybe_recalc_kelly(state)
            fraction = state.kelly_fraction
        
        size = balance * fraction
        return max(self.trade_size_min, min(self.trade_size_max, round(size, 2)))
    
    # Kelly Criterion constants
    KELLY_RECALC_INTERVAL = 20  # Recalc every N trades (move to 50 after 100+ trades)
    KELLY_MIN_TRADES = 15       # Need at least this many trades to calculate
    KELLY_MIN_FRACTION = 0.01   # 1% floor
    KELLY_MAX_FRACTION = 0.15   # 15% cap
    
    def _maybe_recalc_kelly(self, state):
        """Recalculate Half Kelly fraction for a strategy if enough new trades."""
        trades_since = state.trade_count - state.kelly_last_calc
        if trades_since < self.KELLY_RECALC_INTERVAL and state.kelly_last_calc > 0:
            return
        
        # Query real on-chain trades first, fall back to DB trades
        try:
            conn = sqlite3.connect(DB_PATH)
            # Prefer real_trades (CSV-verified, no phantom trades)
            try:
                rows = conn.execute(
                    "SELECT pnl, usdc_spent FROM real_trades WHERE strategy = ? AND won IS NOT NULL",
                    (state.name,)
                ).fetchall()
            except Exception:
                rows = []
            if len(rows) < self.KELLY_MIN_TRADES:
                # Fall back to trades table for new trades since last CSV import
                rows = conn.execute(
                    "SELECT pnl, quantity * entry_price FROM trades WHERE strategy = ? AND status = 'closed' AND is_simulated = 0",
                    (state.name,)
                ).fetchall()
            conn.close()
        except Exception as e:
            self.log(f"⚠️ Kelly calc error: {e}")
            return
        
        if len(rows) < self.KELLY_MIN_TRADES:
            return  # Not enough data, keep default
        
        wins, losses = [], []
        for pnl, cost in rows:
            if cost and cost > 0:
                ret = pnl / cost  # return per dollar spent
                if pnl > 0:
                    wins.append(ret)
                else:
                    losses.append(abs(ret))
        
        if not wins or not losses:
            return
        
        p = len(wins) / len(rows)  # win rate
        avg_win = sum(wins) / len(wins)
        avg_loss = sum(losses) / len(losses)
        b = avg_win / avg_loss if avg_loss > 0 else 1.0  # win/loss ratio
        
        full_kelly = p - (1 - p) / b
        half_kelly = full_kelly / 2
        
        old = state.kelly_fraction
        state.kelly_fraction = max(self.KELLY_MIN_FRACTION, min(self.KELLY_MAX_FRACTION, half_kelly))
        state.kelly_last_calc = state.trade_count
        
        if abs(old - state.kelly_fraction) > 0.001:
            self.log(f"📐 {state.display} Kelly update: {old*100:.1f}% → {state.kelly_fraction*100:.1f}% "
                     f"(WR={p*100:.0f}%, W/L={b:.2f}x, n={len(rows)})")
    
    def _recover_open_trades(self):
        """Load open LIVE trades from DB into strategy state on startup.
        This prevents trades from becoming orphaned after a restart."""
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            open_trades = conn.execute(
                "SELECT id, strategy, market_id, direction, entry_price, quantity, signal_data "
                "FROM trades WHERE strategy LIKE '%_LIVE' AND status = 'open' AND is_simulated = 0"
            ).fetchall()
            conn.close()
            
            now = int(time.time())
            recovered = 0
            cleaned = 0
            
            for t in open_trades:
                # Parse signal_data for condition_id
                sig_data = {}
                try:
                    import json as _json
                    sig_data = _json.loads(t["signal_data"]) if t["signal_data"] else {}
                except Exception:
                    pass
                
                # If trade is >30 min old, it's stuck — auto-clean
                if now - (t["id"] * 0) > 0:  # check timestamp from market_id
                    try:
                        mid_ts = int(t["market_id"].split("-")[-1])
                        age_min = (now - mid_ts) / 60
                        if age_min > 30:
                            conn2 = sqlite3.connect(DB_PATH)
                            conn2.execute(
                                "UPDATE trades SET status='closed', exit_reason='stuck_startup', pnl=0, closed_at=datetime('now') WHERE id=?",
                                (t["id"],))
                            conn2.commit()
                            conn2.close()
                            cleaned += 1
                            continue
                    except Exception:
                        pass
                
                # Find matching strategy
                strat_key = None
                for key, state in self.strategies.items():
                    if state.name == t["strategy"]:
                        strat_key = key
                        break
                
                if strat_key:
                    state = self.strategies[strat_key]
                    state.open_trades.append({
                        "id": t["id"],
                        "order_id": None,
                        "direction": t["direction"],
                        "entry_price": t["entry_price"],
                        "quantity": t["quantity"],
                        "cost": t["entry_price"] * t["quantity"],
                        "market_slug": t["market_id"],
                        "token_id": None,
                        "condition_id": sig_data.get("condition_id"),
                        "window_end_ts": int(t["market_id"].split("-")[-1]) + 300,
                    })
                    recovered += 1
            
            if recovered or cleaned:
                self.log(f"🔄 Recovered {recovered} open trades from DB, cleaned {cleaned} stuck trades")
        except Exception as e:
            self.log(f"⚠️ Trade recovery error: {e}")
    
    def _total_open(self):
        return sum(len(s.open_trades) for s in self.strategies.values())
    
    def _total_wins(self):
        return sum(s.wins for s in self.strategies.values())
    
    def _total_losses(self):
        return sum(s.losses for s in self.strategies.values())
    
    def _place_order(self, token_id, price, size, condition_id):
        """Place a real BUY order on Polymarket."""
        try:
            # Get market details for tick size
            clob_market = self.client.get_market(condition_id)
            tick_size = str(clob_market.get("minimum_tick_size", "0.01"))
            neg_risk = clob_market.get("neg_risk", False)
            
            # Round price to tick size
            tick = float(tick_size)
            rounded_price = round(round(price / tick) * tick, 2)
            
            order_args = OrderArgs(
                token_id=token_id,
                price=rounded_price,
                size=round(size, 2),
                side=BUY,
            )
            options = PartialCreateOrderOptions(
                tick_size=tick_size,
                neg_risk=neg_risk,
            )
            
            resp = self.client.create_and_post_order(order_args, options)
            
            if resp and resp.get("success"):
                return {
                    "order_id": resp.get("orderID"),
                    "status": resp.get("status"),
                    "price": rounded_price,
                    "size": round(size, 2),
                }
            else:
                self.log(f"⚠️ Order not successful: {resp}")
                return None
                
        except Exception as e:
            self.log(f"❌ Order placement failed: {e}")
            return None
    
    def run(self):
        """Main live trading loop."""
        print("🏞 The Outsiders — LIVE Trading Engine")
        print("=" * 60)
        print("💰 REAL MONEY MODE — Every trade uses real USDC!")
        print("=" * 60)
        
        # Initialize client
        self.log("Connecting to Polymarket...")
        if not self._init_client():
            print("❌ Failed to initialize client")
            return
        self.log("✅ Connected to Polymarket CLOB")
        
        # Check geoblock
        if not self._check_geoblock():
            print("❌ Geoblocked! Connect VPN to Romania first.")
            return
        self.log("✅ Not geoblocked")
        
        # Check balance
        balance = self._get_balance()
        if balance is None or balance < self.trade_size_min:
            print(f"❌ Insufficient balance: ${balance}. Need at least ${self.trade_size_min}")
            return
        
        self.initial_balance = balance
        self.daily_start_balance = balance
        self.daily_date = datetime.now(PST).date()
        self.log(f"💰 Balance: ${balance:,.2f}")
        
        # Recover open trades from DB (prevents stuck trades after restart)
        self._recover_open_trades()
        
        # Initialize Kelly fractions on startup
        for s in self.strategies.values():
            self._maybe_recalc_kelly(s)
        self.log(f"📡 Strategies (Half Kelly sizing):")
        for s in self.strategies.values():
            p = s.params
            size = self._calc_trade_size(balance, strategy_key=s.key)
            self.log(f"   {s.display}: TP={p['take_profit_pct']}% | "
                    f"SL={p['stop_loss_pct']}% | Edge>={p['min_edge_pct']}% | "
                    f"Kelly={s.kelly_fraction*100:.1f}% (${size:.2f})")
        self.log(f"🔒 Max positions: {self.max_per_strategy}/strategy, {self.max_total} total")
        ml_stats = self.ml.get_stats()
        if ml_stats["is_exploring"]:
            self.log(f"🧠 ML Meta-Learner: EXPLORING ({ml_stats['total_samples']}/{30} trades until active)")
        else:
            self.log(f"🧠 ML Meta-Learner: ACTIVE v{ml_stats['model_version']} | "
                     f"Taken WR: {ml_stats['taken']['wr']:.0%} | Skipped WR: {ml_stats['skipped']['wr']:.0%}")
        print("=" * 60)
        print("Press Ctrl+C to stop\n")
        
        # Track geoblock checks
        last_geocheck = 0
        
        while self.running:
            try:
                # Check geoblock every 5 minutes
                now = time.time()
                if now - last_geocheck > 300:
                    if not self._check_geoblock():
                        self.log("⏸️ Pausing — geoblocked. Will retry in 60s...")
                        time.sleep(60)
                        continue
                    last_geocheck = now
                
                self._tick()
                
                for _ in range(30):
                    if not self.running:
                        break
                    time.sleep(1)
                    
            except Exception as e:
                self.log(f"❌ Error: {e}")
                import traceback
                traceback.print_exc()
                time.sleep(10)
        
        self._print_summary()
    
    def _tick(self):
        """Single trading iteration."""
        snapshot = get_full_market_snapshot()
        if not snapshot:
            self.log("⏳ No active market found, waiting...")
            return
        
        # Auto-resolve stuck trades (>15 min old on 5-min markets)
        # Try resolution first; only zero out if resolution unavailable after 45 min
        now_ts = int(time.time())
        for s in self.strategies.values():
            stuck = [t for t in s.open_trades if now_ts - t.get("window_end_ts", now_ts) > 900]
            for t in stuck:
                age_min = (now_ts - t.get("window_end_ts", now_ts)) // 60
                try:
                    # Try to resolve properly first
                    resolution = check_market_resolution(t["market_slug"])
                    if resolution and resolution.get("resolved") and resolution.get("winner"):
                        # Proper resolution — let _check_open_trades handle it
                        self.log(f"🔍 {s.display}: Stuck trade #{t['id']} ({age_min}min) has resolution — will resolve next cycle")
                        continue
                    
                    # No resolution available — zero out only if very old (>45 min)
                    if age_min > 45:
                        conn = sqlite3.connect(DB_PATH)
                        conn.execute(
                            "UPDATE trades SET status='closed', exit_reason='stuck_auto', pnl=0, closed_at=datetime('now') WHERE id=?",
                            (t["id"],))
                        conn.commit()
                        conn.close()
                        s.open_trades.remove(t)
                        self.log(f"🧹 {s.display}: Auto-cleaned stuck trade #{t['id']} ({age_min}min old, no resolution)")
                    else:
                        self.log(f"⏳ {s.display}: Trade #{t['id']} waiting for resolution ({age_min}min old)")
                except Exception as e:
                    self.log(f"⚠️ Stuck cleanup error: {e}")
        
        # Check open trades for resolution
        for s in self.strategies.values():
            self._check_open_trades(s, snapshot)
        
        # Resolve ML shadow trades (skipped by ML — track what would have happened)
        self._resolve_ml_shadows()
        
        # Get market data
        candles = get_recent_candles(minutes=35)  # Trend Rider needs 23+ candles (EMA21 + 2)
        ticker = fetch_kraken_ticker()
        kraken_book = fetch_kraken_orderbook()
        
        if not candles or len(candles) < 5:
            self.log(f"⏳ Waiting for candle data ({len(candles) if candles else 0})")
            return
        
        up_ob = snapshot.get("up_orderbook")
        down_ob = snapshot.get("down_orderbook")
        
        # Liquidity check — skip if Polymarket orderbook is empty/thin
        if up_ob and down_ob:
            total_depth = up_ob.get("bid_depth", 0) + up_ob.get("ask_depth", 0) + \
                          down_ob.get("bid_depth", 0) + down_ob.get("ask_depth", 0)
            if total_depth < 50:  # Less than $50 total depth = too thin
                self.log(f"⚠️ Thin orderbook (depth: ${total_depth:.0f}) — skipping cycle")
                return
        
        orderbook_data = None
        if up_ob and down_ob:
            orderbook_data = {
                "imbalance": snapshot.get("ob_imbalance", 0),
                "bid_depth": up_ob["bid_depth"],
                "ask_depth": up_ob["ask_depth"],
            }
        elif kraken_book:
            orderbook_data = kraken_book
        
        # Fetch Binance derivatives signals (funding rate + open interest)
        btc_dir = None
        if candles and len(candles) >= 2:
            btc_dir = "up" if candles[-1]["close"] > candles[-2]["close"] else "down"
        derivatives = get_derivatives_signals(btc_direction=btc_dir)
        
        # Log market state
        self.log(f"📊 {snapshot['title']}")
        self.log(f"   Polymarket: Up={snapshot['up_price']:.3f} | Down={snapshot['down_price']:.3f}")
        if ticker:
            self.log(f"   BTC: ${ticker['last']:,.2f}")
        if derivatives:
            self.log(f"   📡 Derivatives: {derivatives['description']} | Score: {derivatives['combined_score']:+.2f}")
        
        total_open = self._total_open()
        if total_open > 0:
            self.log(f"   📍 Total open: {total_open}")
        
        # Daily reset check
        today = datetime.now(PST).date()
        if today != self.daily_date:
            self.log(f"📅 New day — resetting daily P&L (yesterday: ${self.daily_pnl:+.2f})")
            self.daily_pnl = 0.0
            self.daily_start_balance = self._get_balance() or self.daily_start_balance
            self.daily_date = today
            self.circuit_breaker_tripped = False
        
        # Circuit breaker check (DISABLED — letting ML learn)
        # if self.circuit_breaker_tripped:
        #     self.log(f"🛑 Circuit breaker ACTIVE — daily loss ${self.daily_pnl:+.2f}. No new trades until tomorrow.")
        #     return
        
        # ═══════════════════════════════════════════════════════════
        # PHASE 1: Generate ALL signals and collect candidates
        # ═══════════════════════════════════════════════════════════
        try:
            _htf_ctx = get_htf_context()
        except Exception:
            _htf_ctx = None
        
        all_signals = []  # Collect all signals for cross-strategy context
        candidates = []   # Tradeable candidates for ML ranking
        
        for key, state in self.strategies.items():
            if snapshot["slug"] in state.traded_slugs:
                continue
            
            sig = self._generate_signal(key, state, candles, orderbook_data, 
                                         snapshot, up_ob, down_ob, kraken_book,
                                         derivatives=derivatives)
            if sig is None:
                continue
            
            fmt = self._format_signal(key, sig)
            self.log(f"   {state.display}: {fmt}")
            
            # Record signal for cross-strategy context (even non-trading ones)
            all_signals.append({
                "strategy_key": key,
                "direction": sig.direction if sig.direction else "",
                "confidence": sig.confidence if hasattr(sig, 'confidence') else 0,
                "edge_pct": sig.edge_pct if hasattr(sig, 'edge_pct') else 0,
                "should_trade": sig.should_trade,
            })
            
            if not sig.should_trade:
                state.traded_slugs.add(snapshot["slug"])
                continue
            
            # Check basic eligibility
            can_trade = total_open < self.max_total
            strategy_cap = len(state.open_trades) < self.max_per_strategy
            balance = self._get_balance()
            self.trade_size = self._calc_trade_size(balance, strategy_key=key)
            has_funds = balance is not None and balance >= self.trade_size
            in_cooldown = state.cooldown_skips > 0
            
            if hasattr(sig, 'shadow_trade') and sig.shadow_trade:
                self.log(f"   👻 {state.display}: Edge {sig.edge_pct:.1f}% above cap — skipped")
                state.traded_slugs.add(snapshot["slug"])
                continue
            
            if not can_trade:
                self.log(f"   ⚠️ {state.display}: Signal but at max positions ({self.max_total})")
                state.traded_slugs.add(snapshot["slug"])
                continue
            if not strategy_cap:
                self.log(f"   ⚠️ {state.display}: Signal but strategy cap ({self.max_per_strategy})")
                state.traded_slugs.add(snapshot["slug"])
                continue
            if not has_funds:
                self.log(f"   ⚠️ {state.display}: Signal but insufficient funds (${balance:.2f})")
                state.traded_slugs.add(snapshot["slug"])
                continue
            if in_cooldown:
                state.cooldown_skips -= 1
                self.log(f"   🛑 {state.display}: Skipping (cooldown, {state.cooldown_skips} skips left)")
                state.traded_slugs.add(snapshot["slug"])
                continue
            
            # Extract ML features
            _entry = snapshot["up_price"] if sig.direction == "up" else snapshot["down_price"]
            try:
                ml_features = extract_features(
                    candles, sig, key, state,
                    market_snapshot={
                        "up_price": snapshot["up_price"],
                        "down_price": snapshot["down_price"],
                        "up_orderbook": up_ob,
                        "down_orderbook": down_ob,
                    },
                    htf_context=_htf_ctx,
                    derivatives=derivatives,
                    orderbook=orderbook_data,
                )
            except Exception as e:
                self.log(f"   ⚠️ ML feature extraction error: {e}")
                ml_features = None
            
            candidates.append({
                "key": key,
                "state": state,
                "sig": sig,
                "entry_price": _entry,
                "trade_size": self.trade_size,
                "features": ml_features,
                "snapshot": snapshot,
            })
        
        # ═══════════════════════════════════════════════════════════
        # PHASE 2: Add cross-strategy context and let ML rank
        # ═══════════════════════════════════════════════════════════
        if not candidates:
            for key, state in self.strategies.items():
                state.traded_slugs.add(snapshot["slug"])
            return
        
        # Enrich features with cross-strategy signals
        for c in candidates:
            if c["features"]:
                add_cross_strategy_features(c["features"], all_signals)
        
        # ML ranking: evaluate all candidates, sort by expected value
        ranked = []
        for c in candidates:
            try:
                ml_approved, ml_prob, ml_reason = self.ml.should_trade(
                    c["features"], c["key"], 
                    entry_price=c["entry_price"], trade_size=c["trade_size"]
                )
                ranked.append({**c, "ml_approved": ml_approved, "ml_prob": ml_prob, "ml_reason": ml_reason})
            except Exception as e:
                self.log(f"   ⚠️ ML error for {c['state'].display}: {e}")
                ranked.append({**c, "ml_approved": True, "ml_prob": 0.5, "ml_reason": f"ML error: {e}"})
        
        # Sort by ML probability (best first) — highest EV trades execute first
        ranked.sort(key=lambda x: x["ml_prob"], reverse=True)
        
        # Log the ranking
        if len(ranked) > 1:
            rank_str = " > ".join(
                f"{r['state'].display}({r['ml_prob']:.0%})" for r in ranked
            )
            self.log(f"   🏆 ML Ranking: {rank_str}")
        
        # ═══════════════════════════════════════════════════════════
        # PHASE 3: Execute best trades, shadow track the rest
        # ═══════════════════════════════════════════════════════════
        for r in ranked:
            state = r["state"]
            sig = r["sig"]
            
            if r["ml_approved"]:
                self.log(f"   {r['ml_reason']}")
                self._execute_live_trade(state, sig, r["snapshot"])
            else:
                self.log(f"   {r['ml_reason']}")
                self.log(f"   🧠 {state.display}: ML blocked")
                # Shadow track
                try:
                    conn = sqlite3.connect(DB_PATH)
                    conn.execute("""
                        INSERT INTO ml_shadow_trades 
                        (strategy, direction, entry_price, market_slug, ml_prob, features, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (r["key"], sig.direction, r["entry_price"],
                          snapshot["slug"], r["ml_prob"],
                          json.dumps(r["features"]) if r["features"] else "{}",
                          datetime.now(timezone.utc).isoformat()))
                    conn.commit()
                    conn.close()
                except Exception:
                    pass
            
            state.traded_slugs.add(snapshot["slug"])
    
    def _generate_signal(self, key, state, candles, orderbook_data, 
                          snapshot, up_ob, down_ob, kraken_book, derivatives=None):
        """Generate signal for a specific strategy."""
        common = dict(
            orderbook_data=orderbook_data,
            market_up_price=snapshot["up_price"],
            market_down_price=snapshot["down_price"],
            params=state.params,
            timestamp=int(time.time()),
        )
        
        if key == "trend_rider":
            sig = trend_rider_signal(candles=candles, **common)
            # HTF filter: only trade when 1hr trend agrees (55.1% → 56.0% WR boost)
            if sig and sig.should_trade:
                htf_ctx = get_htf_context()
                if not htf_agrees_with(sig.direction, htf_ctx):
                    sig.should_trade = False
                    sig.reason = (f"⏸️ TrendRider: HTF disagrees | 1H: {htf_ctx.get('h1_trend', 0):+.2f} "
                                  f"| Signal was {sig.direction.upper()}")
                else:
                    self.log(f"   🌐 HTF confirms {sig.direction.upper()} | {htf_ctx.get('description', '')}")
        elif key == "momentum":
            sig = momentum_signal(candles=candles[-state.params.get("lookback_minutes", 15):], **common)
        elif key == "mean_reversion":
            sig = meanrev_signal(candles=candles, **common)
        elif key == "orderbook_imbalance":
            sig = ob_signal(candles=candles, up_orderbook=up_ob, down_orderbook=down_ob,
                           kraken_orderbook=kraken_book, **common)
        elif key == "smart_money":
            sig = smart_signal(candles=candles, **common)
        else:
            return None
        
        # Apply derivatives overlay (funding rate + open interest)
        if sig and derivatives and sig.should_trade:
            sig = self._apply_derivatives_overlay(sig, derivatives)
        
        return sig
    
    def _apply_derivatives_overlay(self, sig, derivatives):
        """Adjust signal confidence/edge based on Binance derivatives data.
        
        Funding rate and OI either CONFIRM or CONTRADICT the signal direction.
        - Confirmation: boost edge by up to +3%
        - Contradiction: reduce edge by up to -3%, may kill the trade
        """
        deriv_score = derivatives["combined_score"]  # -1 (bearish) to +1 (bullish)
        
        # How well does derivatives data align with signal direction?
        if sig.direction == "up":
            alignment = deriv_score  # Positive = bullish = confirms UP
        else:
            alignment = -deriv_score  # Negative combined = bearish = confirms DOWN
        
        # Adjust edge: ±3% max based on alignment
        edge_adjustment = alignment * 3.0
        original_edge = sig.edge_pct
        sig.edge_pct += edge_adjustment
        
        # Adjust confidence proportionally
        conf_adjustment = alignment * 0.03  # ±3% confidence
        sig.confidence += conf_adjustment
        
        # Store derivatives data in signal for logging/DB
        if hasattr(sig, 'to_dict'):
            orig_to_dict = sig.to_dict
            def enhanced_to_dict():
                d = orig_to_dict()
                d["funding_score"] = derivatives.get("funding_score", 0)
                d["oi_score"] = derivatives.get("oi_score", 0)
                d["deriv_combined"] = deriv_score
                d["deriv_edge_adj"] = edge_adjustment
                d["funding_rate_pct"] = derivatives.get("funding_rate_pct")
                d["oi_change_pct"] = derivatives.get("oi_change_pct")
                return d
            sig.to_dict = enhanced_to_dict
        
        # If derivatives strongly contradict (alignment < -0.5), reduce should_trade
        if alignment < -0.5 and sig.edge_pct < sig.confidence * 0.5:
            sig.should_trade = False
            sig.reason = f"⚠️ Derivatives contradiction ({derivatives['description']}) | " + sig.reason
        
        return sig
    
    def _format_signal(self, key, sig):
        fmts = {"trend_rider": trend_rider_format, "momentum": momentum_format,
                "mean_reversion": meanrev_format,
                "orderbook_imbalance": ob_format, "smart_money": smart_format}
        return fmts.get(key, str)(sig)
    
    def _record_shadow_trade(self, state, sig, snapshot):
        """Record a shadow trade to DB for tracking capped-trade outcomes."""
        try:
            conn = get_connection()
            direction = sig.direction
            entry_price = snapshot[f"{direction}_price"]
            signal_data = sig.to_dict() if hasattr(sig, 'to_dict') else {}
            signal_data["condition_id"] = snapshot.get("condition_id", "")
            signal_data["shadow"] = True
            conn.execute(
                """INSERT INTO trades (timestamp, market_id, strategy, side, direction,
                   entry_price, quantity, edge_pct, confidence, signal_data, status, 
                   is_simulated, exit_reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', 0, 'shadow')""",
                (int(snapshot["timestamp"]), snapshot["slug"],
                 state.strategy_name + "_SHADOW", "buy", direction,
                 entry_price, 0, sig.edge_pct, sig.confidence,
                 json.dumps(signal_data))
            )
            conn.commit()
            conn.close()
        except Exception as e:
            self.log(f"   ⚠️ Shadow trade recording failed: {e}")

    def _resolve_shadow_trades(self, current_snapshot):
        """Resolve shadow trades from DB to track what capped trades would have done."""
        try:
            conn = get_connection()
            shadows = conn.execute(
                "SELECT id, market_id, direction, entry_price FROM trades WHERE status='open' AND strategy LIKE '%_SHADOW'"
            ).fetchall()
            
            for trade_id, slug, direction, entry_price in shadows:
                if slug == current_snapshot.get("slug"):
                    continue  # Still active market
                
                resolution = check_market_resolution(slug)
                if resolution is None or not resolution.get("resolved"):
                    continue
                
                actual_winner = resolution.get("winner")
                if actual_winner is None:
                    conn.execute("UPDATE trades SET status='closed', exit_reason='shadow_unknown' WHERE id=?", (trade_id,))
                    continue
                
                won = (direction == actual_winner)
                if won:
                    pnl = 1.0 - entry_price  # Normalized per-share
                    reason = "shadow_win"
                else:
                    pnl = -entry_price
                    reason = "shadow_loss"
                
                conn.execute(
                    "UPDATE trades SET status='closed', exit_price=?, pnl=?, pnl_pct=?, exit_reason=?, closed_at=? WHERE id=?",
                    (1.0 if won else 0.0, pnl, (pnl / entry_price) * 100, reason, int(datetime.now().timestamp()), trade_id)
                )
                emoji = "✅" if won else "❌"
                self.log(f"👻 SHADOW resolved: {direction.upper()} @ {entry_price:.3f} → {emoji} {'WON' if won else 'LOST'} (would have been ${pnl:+.2f})")
            
            conn.commit()
            conn.close()
        except Exception as e:
            self.log(f"⚠️ Shadow resolution error: {e}")

    def _execute_live_trade(self, state, sig, snapshot):
        """Execute a REAL trade on Polymarket."""
        direction = sig.direction
        entry_price = snapshot["up_price"] if direction == "up" else snapshot["down_price"]
        
        # Calculate size: $5 worth of tokens
        quantity = self.trade_size / entry_price if entry_price > 0 else 0
        
        # Pick the right token
        token_id = snapshot["token_ids"]["up"] if direction == "up" else snapshot["token_ids"]["down"]
        
        # Place the REAL order
        self.log(f"💸 {state.display}: Placing REAL order...")
        order = self._place_order(token_id, entry_price, quantity, snapshot["condition_id"])
        
        if not order:
            self.log(f"   ❌ Order failed — skipping")
            return
        
        # Verify order filled (wait up to 5s, check fill status)
        order_id = order.get("order_id")
        fill_price = order["price"]
        fill_size = order["size"]
        filled = False
        
        if order_id:
            for attempt in range(5):
                time.sleep(1)
                try:
                    order_data = self.client.get_order(order_id)
                    if order_data:
                        status = order_data.get("status", "")
                        size_matched = float(order_data.get("size_matched", 0))
                        if size_matched > 0:
                            fill_size = size_matched
                            fill_price = float(order_data.get("price", fill_price))
                            filled = True
                            self.log(f"   ✅ Order filled: {size_matched:.1f} @ ${fill_price:.3f}")
                            break
                        elif status in ("CANCELED", "EXPIRED"):
                            self.log(f"   ⚠️ Order {status} — not filled, skipping")
                            return
                except Exception as e:
                    self.log(f"   ⚠️ Fill check error: {e}")
            
            if not filled:
                # Check one more time with extended wait
                time.sleep(2)
                try:
                    order_data = self.client.get_order(order_id)
                    size_matched = float(order_data.get("size_matched", 0)) if order_data else 0
                    if size_matched > 0:
                        fill_size = size_matched
                        fill_price = float(order_data.get("price", fill_price))
                        filled = True
                    else:
                        self.log(f"   ⚠️ Order not filled after 7s — canceling and skipping")
                        try:
                            self.client.cancel_orders([order_id])
                        except Exception:
                            pass
                        return
                except Exception as e:
                    self.log(f"   ❌ Could not verify fill: {e} — canceling and skipping (NOT recording)")
                    try:
                        self.client.cancel_orders([order_id])
                    except Exception:
                        pass
                    return
        
        if not filled:
            self.log(f"   ❌ No order_id returned — cannot verify fill, skipping")
            return
        
        # Record in database (ONLY if fill is confirmed)
        trade_data = {
            "timestamp": int(time.time()),
            "market_id": snapshot["slug"],
            "strategy": state.name,
            "side": "buy",
            "direction": direction,
            "entry_price": fill_price,
            "quantity": fill_size,
            "edge_pct": sig.edge_pct,
            "confidence": sig.confidence,
            "signal_data": {**(sig.to_dict() if hasattr(sig, 'to_dict') else {}), "condition_id": snapshot["condition_id"]},
            "is_simulated": 0,  # REAL TRADE!
        }
        
        trade_id = insert_trade(trade_data)
        
        state.open_trades.append({
            "id": trade_id,
            "order_id": order_id,
            "direction": direction,
            "entry_price": fill_price,
            "quantity": fill_size,
            "cost": fill_price * fill_size,
            "market_slug": snapshot["slug"],
            "token_id": token_id,
            "condition_id": snapshot["condition_id"],
            "window_end_ts": snapshot["window_end_ts"],
        })
        
        emoji = "🟢" if direction == "up" else "🔴"
        fill_note = " (verified)" if filled else " (unverified)"
        self.log(f"{emoji} 💰 {state.display} LIVE TRADE: BUY {direction.upper()} "
                f"@ ${fill_price:.3f} | Qty: {fill_size:.1f} | "
                f"Cost: ${fill_price * fill_size:.2f} | Edge: {sig.edge_pct:.1f}%{fill_note}")
    
    def _resolve_ml_shadows(self):
        """Resolve ML shadow trades to validate the model's skip decisions."""
        try:
            conn = sqlite3.connect(DB_PATH)
            pending = conn.execute(
                "SELECT id, direction, market_slug FROM ml_shadow_trades WHERE outcome IS NULL"
            ).fetchall()
            
            for row in pending:
                shadow_id, direction, slug = row
                resolution = check_market_resolution(slug)
                if resolution and resolution.get("resolved") and resolution.get("winner"):
                    won = 1 if direction == resolution["winner"] else 0
                    conn.execute(
                        "UPDATE ml_shadow_trades SET outcome=?, resolved_at=? WHERE id=?",
                        (won, datetime.now(timezone.utc).isoformat(), shadow_id)
                    )
                    emoji = "✅" if won else "❌"
                    self.log(f"   👻 ML shadow: {direction.upper()} → {emoji} {'would have WON' if won else 'would have LOST'} (ML skipped, P={0:.0%})")
                    
                    # Also feed to ML for learning (as a skip outcome)
                    self.ml.record_outcome(shadow_id * -1, bool(won), 0)  # Negative ID to distinguish
            
            conn.commit()
            conn.close()
        except Exception:
            pass
    
    def _check_open_trades(self, state, current_snapshot):
        """Check open trades for resolution."""
        if not state.open_trades:
            return
        
        resolved_indices = []
        
        for i, trade in enumerate(state.open_trades):
            # Skip if this trade's market window hasn't ended yet
            if trade.get("window_end_ts") and int(time.time()) < trade["window_end_ts"]:
                continue
            
            resolution = check_market_resolution(trade["market_slug"])
            
            if resolution is None:
                continue
            
            if not resolution.get("resolved"):
                self.log(f"⏳ {state.display}: Waiting for {trade['market_slug']}...")
                continue
            
            actual_winner = resolution.get("winner")
            if actual_winner is None:
                self.log(f"⚠️ {state.display}: Winner unknown for {trade['market_slug']}")
                resolved_indices.append(i)
                continue
            
            won = (trade["direction"] == actual_winner)
            
            # Real P&L for binary options:
            # Win: tokens worth $1 each, profit = quantity * (1 - entry_price)
            # Loss: tokens worth $0, loss = quantity * entry_price (= cost)
            if won:
                pnl = trade["quantity"] * (1.0 - trade["entry_price"])
                pnl_pct = ((1.0 / trade["entry_price"]) - 1.0) * 100
                exit_reason = "win_live"
                state.wins += 1
                state.consecutive_losses = 0
            else:
                pnl = -trade["cost"]
                pnl_pct = -100.0
                exit_reason = "loss_live"
                state.losses += 1
                state.consecutive_losses += 1
                # Smart Money: after 2+ consecutive losses, skip next 2 signals
                if state.key == "smart_money" and state.consecutive_losses >= 2:
                    state.cooldown_skips = 2
                    self.log(f"   🛑 {state.display}: {state.consecutive_losses} losses in a row → cooling down (skip 2)")
                elif state.key in ("momentum", "trend_rider") and state.consecutive_losses >= 3:
                    state.cooldown_skips = 1
                    self.log(f"   🛑 {state.display}: {state.consecutive_losses} losses in a row → cooling down (skip 1)")
            
            state.total_pnl += pnl
            state.trade_count += 1
            self.daily_pnl += pnl
            
            # Check circuit breaker
            loss_pct = abs(self.daily_pnl) / self.daily_start_balance * 100 if self.daily_start_balance > 0 else 0
            if self.daily_pnl < 0 and (loss_pct >= self.daily_loss_limit_pct or abs(self.daily_pnl) >= self.daily_loss_limit_abs):
                self.circuit_breaker_tripped = True
                self.log(f"🚨🛑 CIRCUIT BREAKER TRIPPED! Daily loss: ${self.daily_pnl:+.2f} ({loss_pct:.1f}%). No more trades today.")
            
            close_trade(trade["id"], 1.0 if won else 0.0, pnl, pnl_pct, exit_reason)
            
            # Feed outcome to ML meta-learner
            try:
                self.ml.record_outcome(trade["id"], won, pnl)
                state.recent_outcomes.append(actual_winner)
                state.recent_outcomes = state.recent_outcomes[-20:]  # Keep last 20
            except Exception:
                pass
            
            # Auto-redeem resolved positions (wins get USDC back, losses clear the position)
            if self.redeemer and trade.get("condition_id"):
                try:
                    result = self.redeemer.redeem(trade["condition_id"])
                    if result.get("success"):
                        label = "💰 Auto-claimed" if won else "🧹 Auto-cleared loss"
                        self.log(f"{label}! tx: {result['tx_hash'][:16]}...")
                    elif result.get("error") == "rate_limited":
                        self.log(f"⏸️ Redeem rate-limited, will retry later")
                    else:
                        self.log(f"⚠️ Redeem failed: {result.get('error', 'unknown')}")
                except Exception as e:
                    self.log(f"⚠️ Auto-redeem error (claim manually): {e}")
            
            emoji = "✅" if won else "❌"
            balance = self._get_balance()
            bal_str = f"${balance:,.2f}" if balance else "?"
            
            # Record real balance for equity curve
            if balance is not None:
                try:
                    conn = sqlite3.connect(DB_PATH)
                    conn.execute("CREATE TABLE IF NOT EXISTS balance_history (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp INTEGER, balance REAL, source TEXT DEFAULT 'live')")
                    conn.execute("INSERT INTO balance_history (timestamp, balance) VALUES (?, ?)", (int(time.time()), balance))
                    conn.commit()
                    conn.close()
                except Exception:
                    pass
            
            self.log(f"{emoji} 💰 {state.display}: Bet {trade['direction'].upper()}, "
                    f"resolved {actual_winner.upper()} | "
                    f"{'WON' if won else 'LOST'} | "
                    f"PnL: ${pnl:+.2f} ({pnl_pct:+.1f}%) | "
                    f"Balance: {bal_str} | "
                    f"Record: {self._total_wins()}W-{self._total_losses()}L")
            
            resolved_indices.append(i)
        
        for i in sorted(resolved_indices, reverse=True):
            state.open_trades.pop(i)
    
    def _print_summary(self):
        total_w = self._total_wins()
        total_l = self._total_losses()
        balance = self._get_balance() or 0
        
        print("\n" + "=" * 60)
        print("🏞 LIVE TRADING SESSION SUMMARY")
        print("=" * 60)
        
        for s in self.strategies.values():
            t = s.trade_count
            wr = (s.wins / t * 100) if t > 0 else 0
            print(f"\n{s.display}:")
            print(f"  Trades: {t} ({s.wins}W / {s.losses}L) | WR: {wr:.1f}% | P&L: ${s.total_pnl:+.2f}")
        
        print(f"\n{'─' * 40}")
        print(f"TOTAL: {total_w + total_l} trades ({total_w}W / {total_l}L)")
        print(f"Starting Balance: ${self.initial_balance:,.2f}")
        print(f"Current Balance: ${balance:,.2f}")
        change = balance - self.initial_balance
        pct = (change / self.initial_balance * 100) if self.initial_balance > 0 else 0
        print(f"P&L: ${change:+,.2f} ({pct:+.1f}%)")
        print("=" * 60)


if __name__ == "__main__":
    trade_size = float(sys.argv[1]) if len(sys.argv) > 1 else 5.0
    trader = LiveTrader(trade_size=trade_size)
    trader.run()
