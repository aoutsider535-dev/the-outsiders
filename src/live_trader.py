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
from src.database import init_db, insert_trade, close_trade
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
    "mean_reversion": {
        "name": "btc_5min_meanrev_LIVE",
        "display": "🔄 Mean Reversion",
        "load_params": lambda: MEANREV_DEFAULTS.copy(),
    },
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
    
    def _calc_trade_size(self, balance=None):
        """Calculate dynamic trade size: trade_size_pct% of balance, clamped to [min, max]."""
        if balance is None:
            balance = self._get_balance()
        if balance is None or balance <= 0:
            return self.trade_size_min
        size = balance * (self.trade_size_pct / 100)
        return max(self.trade_size_min, min(self.trade_size_max, round(size, 2)))
    
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
        current_size = self._calc_trade_size(balance)
        self.log(f"📊 Trade size: {self.trade_size_pct}% of balance = ${current_size:.2f} (min ${self.trade_size_min}, max ${self.trade_size_max})")
        self.log(f"📡 Strategies:")
        for s in self.strategies.values():
            p = s.params
            self.log(f"   {s.display}: TP={p['take_profit_pct']}% | "
                    f"SL={p['stop_loss_pct']}% | Edge>={p['min_edge_pct']}%")
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
        
        # Get market data
        candles = get_recent_candles(minutes=20)
        ticker = fetch_kraken_ticker()
        kraken_book = fetch_kraken_orderbook()
        
        if not candles or len(candles) < 5:
            self.log(f"⏳ Waiting for candle data ({len(candles) if candles else 0})")
            return
        
        up_ob = snapshot.get("up_orderbook")
        down_ob = snapshot.get("down_orderbook")
        
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
            
            # Check balance and calculate dynamic trade size
            balance = self._get_balance()
            self.trade_size = self._calc_trade_size(balance)
            has_funds = balance is not None and balance >= self.trade_size
            
            # Loss streak cooldown check (Smart Money)
            in_cooldown = state.cooldown_skips > 0

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
            "signal_data": sig.to_dict() if hasattr(sig, 'to_dict') else {},
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
            
            # Auto-claim winnings
            if won and self.redeemer and trade.get("condition_id"):
                try:
                    result = self.redeemer.redeem(trade["condition_id"])
                    if result.get("success"):
                        self.log(f"💰 Auto-claimed! tx: {result['tx_hash'][:16]}...")
                    elif result.get("error") == "rate_limited":
                        self.log(f"⏸️ Claim rate-limited, will retry later")
                    else:
                        self.log(f"⚠️ Claim failed: {result.get('error', 'unknown')}")
                except Exception as e:
                    self.log(f"⚠️ Auto-claim error (funds safe, claim manually): {e}")
            
            emoji = "✅" if won else "❌"
            balance = self._get_balance()
            bal_str = f"${balance:,.2f}" if balance else "?"
            
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
