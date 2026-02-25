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
    "momentum": {
        "name": "btc_5min_momentum_LIVE",
        "display": "⚡ Momentum",
        "load_params": load_momentum_params,
    },
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
        
        # Query recent closed trades for this strategy
        try:
            conn = sqlite3.connect(DB_PATH)
            rows = conn.execute(
                "SELECT pnl, quantity FROM trades WHERE strategy = ? AND status = 'closed' AND is_simulated = 0",
                (state.name,)
            ).fetchall()
            conn.close()
        except Exception as e:
            self.log(f"⚠️ Kelly calc error: {e}")
            return
        
        if len(rows) < self.KELLY_MIN_TRADES:
            return  # Not enough data, keep default
        
        wins, losses = [], []
        for pnl, qty in rows:
            if qty and qty > 0:
                ret = pnl / qty  # return per dollar
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
        self.log(f"💰 Balance: ${balance:,.2f}")
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
        
        # Check open trades for resolution
        for s in self.strategies.values():
            self._check_open_trades(s, snapshot)
        
        # Resolve shadow trades
        self._resolve_shadow_trades(snapshot)
        
        # Get market data
        candles = get_recent_candles(minutes=20)
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
        
        # Log market state
        self.log(f"📊 {snapshot['title']}")
        self.log(f"   Polymarket: Up={snapshot['up_price']:.3f} | Down={snapshot['down_price']:.3f}")
        if ticker:
            self.log(f"   BTC: ${ticker['last']:,.2f}")
        
        total_open = self._total_open()
        if total_open > 0:
            self.log(f"   📍 Total open: {total_open}")
        
        # Run each strategy
        for key, state in self.strategies.items():
            if snapshot["slug"] in state.traded_slugs:
                continue
            
            sig = self._generate_signal(key, state, candles, orderbook_data, 
                                         snapshot, up_ob, down_ob, kraken_book)
            if sig is None:
                continue
            
            fmt = self._format_signal(key, sig)
            self.log(f"   {state.display}: {fmt}")
            
            can_trade = total_open < self.max_total
            strategy_cap = len(state.open_trades) < self.max_per_strategy
            
            # Check balance and calculate dynamic trade size (Kelly per strategy)
            balance = self._get_balance()
            self.trade_size = self._calc_trade_size(balance, strategy_key=key)
            has_funds = balance is not None and balance >= self.trade_size
            
            # Loss streak cooldown check (Smart Money)
            in_cooldown = state.cooldown_skips > 0

            # Shadow trade logging — track capped trades for validation
            if hasattr(sig, 'shadow_trade') and sig.shadow_trade:
                self.log(f"   👻 {state.display}: SHADOW trade {sig.direction.upper()} @ edge {sig.edge_pct:.1f}% (capped, tracking outcome)")
                self._record_shadow_trade(state, sig, snapshot)
            
            if sig.should_trade and can_trade and strategy_cap and has_funds and not in_cooldown:
                self._execute_live_trade(state, sig, snapshot)
            elif sig.should_trade and in_cooldown:
                state.cooldown_skips -= 1
                self.log(f"   🛑 {state.display}: Skipping (cooldown, {state.cooldown_skips} skips left)")
                total_open += 1
            elif sig.should_trade and not has_funds:
                self.log(f"   ⚠️ {state.display}: Signal but insufficient funds (${balance:.2f})")
            elif sig.should_trade and not can_trade:
                self.log(f"   ⚠️ {state.display}: Signal but at max positions ({self.max_total})")
            elif sig.should_trade and not strategy_cap:
                self.log(f"   ⚠️ {state.display}: Signal but strategy cap ({self.max_per_strategy})")
            
            state.traded_slugs.add(snapshot["slug"])
    
    def _generate_signal(self, key, state, candles, orderbook_data, 
                          snapshot, up_ob, down_ob, kraken_book):
        """Generate signal for a specific strategy."""
        common = dict(
            orderbook_data=orderbook_data,
            market_up_price=snapshot["up_price"],
            market_down_price=snapshot["down_price"],
            params=state.params,
            timestamp=int(time.time()),
        )
        
        if key == "momentum":
            return momentum_signal(candles=candles[-state.params.get("lookback_minutes", 15):], **common)
        elif key == "mean_reversion":
            return meanrev_signal(candles=candles, **common)
        elif key == "orderbook_imbalance":
            return ob_signal(candles=candles, up_orderbook=up_ob, down_orderbook=down_ob,
                           kraken_orderbook=kraken_book, **common)
        elif key == "smart_money":
            return smart_signal(candles=candles, **common)
        return None
    
    def _format_signal(self, key, sig):
        fmts = {"momentum": momentum_format, "mean_reversion": meanrev_format,
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
        
        # Record in database
        trade_data = {
            "timestamp": int(time.time()),
            "market_id": snapshot["slug"],
            "strategy": state.name,
            "side": "buy",
            "direction": direction,
            "entry_price": order["price"],
            "quantity": order["size"],
            "edge_pct": sig.edge_pct,
            "confidence": sig.confidence,
            "signal_data": {**(sig.to_dict() if hasattr(sig, 'to_dict') else {}), "condition_id": snapshot["condition_id"]},
            "is_simulated": 0,  # REAL TRADE!
        }
        
        trade_id = insert_trade(trade_data)
        
        state.open_trades.append({
            "id": trade_id,
            "order_id": order["order_id"],
            "direction": direction,
            "entry_price": order["price"],
            "quantity": order["size"],
            "cost": order["price"] * order["size"],
            "market_slug": snapshot["slug"],
            "token_id": token_id,
            "condition_id": snapshot["condition_id"],
            "window_end_ts": snapshot["window_end_ts"],
        })
        
        emoji = "🟢" if direction == "up" else "🔴"
        self.log(f"{emoji} 💰 {state.display} LIVE TRADE: BUY {direction.upper()} "
                f"@ ${order['price']:.3f} | Qty: {order['size']:.1f} | "
                f"Cost: ${order['price'] * order['size']:.2f} | Edge: {sig.edge_pct:.1f}%")
    
    def _check_open_trades(self, state, current_snapshot):
        """Check open trades for resolution."""
        if not state.open_trades:
            return
        
        resolved_indices = []
        
        for i, trade in enumerate(state.open_trades):
            if current_snapshot["slug"] == trade["market_slug"]:
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
            
            state.total_pnl += pnl
            state.trade_count += 1
            
            close_trade(trade["id"], 1.0 if won else 0.0, pnl, pnl_pct, exit_reason)
            
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
