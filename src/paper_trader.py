"""
Paper Trading Engine v2 — Multi-Strategy

Runs 3 strategies simultaneously against REAL Polymarket data:
1. BTC 5-Min Momentum (original)
2. Mean Reversion
3. Orderbook Imbalance

Each strategy maintains independent positions and tracking.
"""
import time
import json
import os
import signal
import sys
from datetime import datetime, timezone
from src.data.polymarket import get_full_market_snapshot, check_market_resolution
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
from src.database import init_db, insert_trade, close_trade, get_trades, get_performance_summary

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
        "name": "btc_5min_momentum",
        "display": "⚡ Momentum",
        "load_params": load_momentum_params,
    },
    "mean_reversion": {
        "name": "btc_5min_meanrev",
        "display": "🔄 Mean Reversion",
        "load_params": lambda: MEANREV_DEFAULTS.copy(),
    },
    "orderbook_imbalance": {
        "name": "btc_5min_ob_imbalance",
        "display": "📊 OB Imbalance",
        "load_params": lambda: OB_DEFAULTS.copy(),
    },
    "smart_money": {
        "name": "btc_5min_smart_money",
        "display": "🧠 Smart Money",
        "load_params": lambda: SMART_DEFAULTS.copy(),
    },
}


class StrategyState:
    """Track state for a single strategy."""
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


class PaperTrader:
    def __init__(self, balance=1000.0):
        self.balance = balance
        self.initial_balance = balance
        self.running = True
        self.strategies = {}
        
        for key, config in STRATEGY_CONFIG.items():
            self.strategies[key] = StrategyState(key, config)
        
        init_db()
        signal.signal(signal.SIGINT, self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)
    
    def _shutdown(self, signum, frame):
        print("\n⏹️  Shutting down multi-strategy paper trader...")
        self.running = False
    
    def log(self, msg):
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(f"[{ts}] {msg}")
    
    def _total_open(self):
        return sum(len(s.open_trades) for s in self.strategies.values())
    
    def _total_risk_pct(self):
        total_risk = sum(
            t["risk_amount"] for s in self.strategies.values() for t in s.open_trades
        )
        return (total_risk / self.balance * 100) if self.balance > 0 else 0
    
    def _total_wins(self):
        return sum(s.wins for s in self.strategies.values())
    
    def _total_losses(self):
        return sum(s.losses for s in self.strategies.values())
    
    def _total_trades(self):
        return sum(s.trade_count for s in self.strategies.values())
    
    def run(self):
        print("🏞 The Outsiders — Multi-Strategy Paper Trading Engine v2")
        print("=" * 60)
        print(f"💰 Starting balance: ${self.balance:,.2f}")
        print(f"📡 Strategies:")
        for s in self.strategies.values():
            p = s.params
            print(f"   {s.display}: TP={p['take_profit_pct']}% | "
                  f"SL={p['stop_loss_pct']}% | Edge>={p['min_edge_pct']}%")
        print(f"📡 Using LIVE Polymarket + Kraken data")
        print(f"🧪 Paper trading mode — no real money at risk")
        print("=" * 60)
        print("Press Ctrl+C to stop\n")
        
        while self.running:
            try:
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
        """Single iteration — run all strategies."""
        snapshot = get_full_market_snapshot()
        if not snapshot:
            self.log("⏳ No active market found, waiting...")
            return
        
        # Check open trades for ALL strategies first
        for s in self.strategies.values():
            self._check_open_trades(s, snapshot)
        
        # Get shared market data
        candles = get_recent_candles(minutes=20)  # max lookback across strategies
        ticker = fetch_kraken_ticker()
        kraken_book = fetch_kraken_orderbook()
        
        if not candles or len(candles) < 5:
            self.log(f"⏳ Waiting for candle data ({len(candles) if candles else 0} candles)")
            return
        
        # Polymarket orderbook data
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
        
        # Log market state once
        self.log(f"📊 {snapshot['title']}")
        self.log(f"   Polymarket: Up={snapshot['up_price']:.3f} | Down={snapshot['down_price']:.3f}")
        if ticker:
            self.log(f"   BTC: ${ticker['last']:,.2f}")
        total_open = self._total_open()
        if total_open > 0:
            self.log(f"   📍 Total open: {total_open} | Risk: {self._total_risk_pct():.1f}%")
        
        # Run each strategy
        for key, state in self.strategies.items():
            if snapshot["slug"] in state.traded_slugs:
                continue
            
            sig = self._generate_strategy_signal(
                key, state, candles, orderbook_data, snapshot,
                up_ob, down_ob, kraken_book
            )
            
            if sig is None:
                continue
            
            fmt = self._format_strategy_signal(key, sig)
            self.log(f"   {state.display}: {fmt}")
            
            # Check if we can trade (global limits)
            can_trade = total_open < 12  # max 12 total across all strategies (3 per strategy x 4)
            risk_ok = self._total_risk_pct() < 20  # 20% total risk cap
            strategy_cap = len(state.open_trades) < 3  # max 3 per strategy
            
            if sig.should_trade and can_trade and risk_ok and strategy_cap:
                self._execute_paper_trade(state, sig, snapshot)
                total_open += 1
            elif sig.should_trade and not can_trade:
                self.log(f"   ⚠️ {state.display}: Signal but at max positions (12)")
            elif sig.should_trade and not risk_ok:
                self.log(f"   ⚠️ {state.display}: Signal but risk cap reached")
            elif sig.should_trade and not strategy_cap:
                self.log(f"   ⚠️ {state.display}: Signal but strategy cap reached (2)")
            
            state.traded_slugs.add(snapshot["slug"])
    
    def _generate_strategy_signal(self, key, state, candles, orderbook_data,
                                   snapshot, up_ob, down_ob, kraken_book):
        """Generate signal for a specific strategy."""
        if key == "momentum":
            return momentum_signal(
                candles=candles[-state.params.get("lookback_minutes", 15):],
                orderbook_data=orderbook_data,
                market_up_price=snapshot["up_price"],
                market_down_price=snapshot["down_price"],
                params=state.params,
                timestamp=int(time.time()),
            )
        elif key == "mean_reversion":
            return meanrev_signal(
                candles=candles,
                orderbook_data=orderbook_data,
                market_up_price=snapshot["up_price"],
                market_down_price=snapshot["down_price"],
                params=state.params,
                timestamp=int(time.time()),
            )
        elif key == "orderbook_imbalance":
            return ob_signal(
                candles=candles,
                orderbook_data=orderbook_data,
                market_up_price=snapshot["up_price"],
                market_down_price=snapshot["down_price"],
                params=state.params,
                timestamp=int(time.time()),
                up_orderbook=up_ob,
                down_orderbook=down_ob,
                kraken_orderbook=kraken_book,
            )
        elif key == "smart_money":
            return smart_signal(
                candles=candles,
                orderbook_data=orderbook_data,
                market_up_price=snapshot["up_price"],
                market_down_price=snapshot["down_price"],
                params=state.params,
                timestamp=int(time.time()),
            )
        return None
    
    def _format_strategy_signal(self, key, sig):
        """Format signal for logging."""
        if key == "momentum":
            return momentum_format(sig)
        elif key == "mean_reversion":
            return meanrev_format(sig)
        elif key == "orderbook_imbalance":
            return ob_format(sig)
        elif key == "smart_money":
            return smart_format(sig)
        return str(sig)
    
    def _execute_paper_trade(self, state, sig, snapshot):
        """Execute a simulated trade for a specific strategy."""
        entry_price = snapshot["up_price"] if sig.direction == "up" else snapshot["down_price"]
        risk_amount = self.balance * (state.params["risk_per_trade_pct"] / 100)
        quantity = risk_amount / entry_price if entry_price > 0 else 0
        
        trade_data = {
            "timestamp": int(time.time()),
            "market_id": snapshot["slug"],
            "strategy": state.name,
            "side": "buy",
            "direction": sig.direction,
            "entry_price": entry_price,
            "quantity": quantity,
            "edge_pct": sig.edge_pct,
            "confidence": sig.confidence,
            "signal_data": sig.to_dict() if hasattr(sig, 'to_dict') else {},
            "is_simulated": 1,
        }
        
        trade_id = insert_trade(trade_data)
        
        state.open_trades.append({
            "id": trade_id,
            "direction": sig.direction,
            "entry_price": entry_price,
            "quantity": quantity,
            "risk_amount": risk_amount,
            "market_slug": snapshot["slug"],
            "window_end_ts": snapshot["window_end_ts"],
        })
        
        emoji = "🟢" if sig.direction == "up" else "🔴"
        self.log(f"{emoji} {state.display} TRADE: BUY {sig.direction.upper()} "
                f"@ ${entry_price:.3f} | Qty: {quantity:.1f} | "
                f"Risk: ${risk_amount:.2f} | Edge: {sig.edge_pct:.1f}%")
    
    def _check_open_trades(self, state, current_snapshot):
        """Check open trades for a specific strategy."""
        if not state.open_trades:
            return
        
        resolved_indices = []
        
        for i, trade in enumerate(state.open_trades):
            if current_snapshot["slug"] == trade["market_slug"]:
                continue
            
            resolution = check_market_resolution(trade["market_slug"])
            
            if resolution is None:
                self.log(f"⚠️ {state.display}: Could not check {trade['market_slug']}")
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
            
            if won:
                pnl = trade["risk_amount"] * (state.params["take_profit_pct"] / 100)
                pnl_pct = state.params["take_profit_pct"]
                exit_reason = "win_real"
                state.wins += 1
            else:
                pnl = -trade["risk_amount"] * (state.params["stop_loss_pct"] / 100)
                pnl_pct = -state.params["stop_loss_pct"]
                exit_reason = "loss_real"
                state.losses += 1
            
            self.balance += pnl
            state.total_pnl += pnl
            state.trade_count += 1
            
            close_trade(trade["id"], 1.0 if won else 0.0, pnl, pnl_pct, exit_reason)
            
            emoji = "✅" if won else "❌"
            self.log(f"{emoji} {state.display}: Bet {trade['direction'].upper()}, "
                    f"resolved {actual_winner.upper()} | "
                    f"{'WON' if won else 'LOST'} | "
                    f"PnL: ${pnl:+.2f} ({pnl_pct:+.1f}%) | "
                    f"Balance: ${self.balance:,.2f} | "
                    f"Record: {self._total_wins()}W-{self._total_losses()}L")
            
            resolved_indices.append(i)
        
        for i in sorted(resolved_indices, reverse=True):
            state.open_trades.pop(i)
    
    def _print_summary(self):
        total_w = self._total_wins()
        total_l = self._total_losses()
        total_t = self._total_trades()
        
        print("\n" + "=" * 60)
        print("🏞 MULTI-STRATEGY PAPER TRADING SUMMARY")
        print("=" * 60)
        
        for s in self.strategies.values():
            t = s.trade_count
            wr = (s.wins / t * 100) if t > 0 else 0
            print(f"\n{s.display}:")
            print(f"  Trades: {t} ({s.wins}W / {s.losses}L) | WR: {wr:.1f}% | P&L: ${s.total_pnl:+.2f}")
        
        print(f"\n{'─' * 40}")
        print(f"TOTAL: {total_t} trades ({total_w}W / {total_l}L)")
        wr = (total_w / total_t * 100) if total_t > 0 else 0
        print(f"Win Rate: {wr:.1f}%")
        print(f"Final Balance: ${self.balance:,.2f} "
              f"({((self.balance - self.initial_balance) / self.initial_balance * 100):+.1f}%)")
        print("=" * 60)


def run_paper_trading(balance=1000.0):
    trader = PaperTrader(balance=balance)
    trader.run()


if __name__ == "__main__":
    balance = float(sys.argv[1]) if len(sys.argv) > 1 else 1000.0
    run_paper_trading(balance=balance)
