"""
🏞 The Outsiders — Paper Trader v2

Tests 3 new uncorrelated strategies on Polymarket BTC 5-min markets.
No real money — tracks simulated P&L against real market outcomes.

Strategies:
1. 📈 Trend Rider — EMA crossover + VWAP (bullish/bearish trends)
2. 🎯 VWAP Sniper — VWAP deviation + RSI extremes (ranging markets)
3. ⚡ Volatility Spike — Volume surge + BB breakout (volatility events)
"""
import time
import json
import os
import signal
import sqlite3
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.data.polymarket import get_full_market_snapshot, check_market_resolution
from src.data.price_feed import get_recent_candles, fetch_kraken_ticker, fetch_kraken_orderbook

from src.strategies.trend_rider import (
    generate_signal as trend_rider_signal,
    format_signal_message as trend_rider_format,
    DEFAULTS as TREND_RIDER_DEFAULTS,
)
from src.strategies.vwap_sniper import (
    generate_signal as vwap_sniper_signal,
    format_signal_message as vwap_sniper_format,
    DEFAULTS as VWAP_SNIPER_DEFAULTS,
)
from src.strategies.volatility_spike import (
    generate_signal as vol_spike_signal,
    format_signal_message as vol_spike_format,
    DEFAULTS as VOL_SPIKE_DEFAULTS,
)

PST = timezone(timedelta(hours=-8))
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "paper_v2.db")

STRATEGY_CONFIG = {
    "trend_rider": {
        "name": "btc_5min_trend_rider_V2",
        "display": "📈 Trend Rider",
        "params": TREND_RIDER_DEFAULTS.copy(),
        "signal_fn": trend_rider_signal,
        "format_fn": trend_rider_format,
    },
    "vwap_sniper": {
        "name": "btc_5min_vwap_sniper_V2",
        "display": "🎯 VWAP Sniper",
        "params": VWAP_SNIPER_DEFAULTS.copy(),
        "signal_fn": vwap_sniper_signal,
        "format_fn": vwap_sniper_format,
    },
    "volatility_spike": {
        "name": "btc_5min_vol_spike_V2",
        "display": "⚡ Vol Spike",
        "params": VOL_SPIKE_DEFAULTS.copy(),
        "signal_fn": vol_spike_signal,
        "format_fn": vol_spike_format,
    },
}


class StrategyState:
    def __init__(self, key, config):
        self.key = key
        self.name = config["name"]
        self.display = config["display"]
        self.params = config["params"]
        self.signal_fn = config["signal_fn"]
        self.format_fn = config["format_fn"]
        self.open_trades = []
        self.traded_slugs = set()
        self.wins = 0
        self.losses = 0
        self.trade_count = 0
        self.total_pnl = 0.0


class PaperTraderV2:
    STARTING_BALANCE = 100.0  # Simulated starting balance

    def __init__(self, max_per_strategy=5, max_total=15,
                 trade_size_pct=4.0, trade_size_min=5.0, trade_size_max=50.0):
        self.balance = self.STARTING_BALANCE
        self.trade_size_pct = trade_size_pct
        self.trade_size_min = trade_size_min
        self.trade_size_max = trade_size_max
        self.max_per_strategy = max_per_strategy
        self.max_total = max_total
        self.running = True
        self.strategies = {}

        for key, config in STRATEGY_CONFIG.items():
            self.strategies[key] = StrategyState(key, config)

        self._init_db()
        signal.signal(signal.SIGINT, self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)

    def _shutdown(self, signum, frame):
        print("\n⏹️  Shutting down Paper Trader v2...")
        self.running = False

    def log(self, msg):
        ts = datetime.now(timezone.utc).astimezone(PST).strftime("%I:%M:%S %p")
        print(f"[{ts}] {msg}")

    def _init_db(self):
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER,
                market_id TEXT,
                strategy TEXT,
                direction TEXT,
                entry_price REAL,
                exit_price REAL,
                quantity REAL,
                pnl REAL,
                pnl_pct REAL,
                edge_pct REAL,
                confidence REAL,
                signal_data TEXT,
                status TEXT DEFAULT 'open',
                exit_reason TEXT,
                created_at TEXT,
                closed_at TEXT
            )
        """)
        conn.commit()
        conn.close()

    def _calc_trade_size(self):
        size = self.balance * (self.trade_size_pct / 100)
        return max(self.trade_size_min, min(self.trade_size_max, round(size, 2)))

    def _total_open(self):
        return sum(len(s.open_trades) for s in self.strategies.values())

    def _insert_trade(self, data):
        conn = sqlite3.connect(DB_PATH)
        cur = conn.execute(
            """INSERT INTO trades (timestamp, market_id, strategy, direction, entry_price,
               quantity, edge_pct, confidence, signal_data, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)""",
            (data["timestamp"], data["market_id"], data["strategy"], data["direction"],
             data["entry_price"], data["quantity"], data["edge_pct"], data["confidence"],
             json.dumps(data.get("signal_data", {})),
             datetime.now(timezone.utc).isoformat())
        )
        trade_id = cur.lastrowid
        conn.commit()
        conn.close()
        return trade_id

    def _close_trade(self, trade_id, exit_price, pnl, pnl_pct, exit_reason):
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "UPDATE trades SET status='closed', exit_price=?, pnl=?, pnl_pct=?, exit_reason=?, closed_at=? WHERE id=?",
            (exit_price, pnl, pnl_pct, exit_reason, datetime.now(timezone.utc).isoformat(), trade_id)
        )
        conn.commit()
        conn.close()

    def _check_open_trades(self, state, snapshot):
        """Check if any open trades have resolved."""
        remaining = []
        for trade in state.open_trades:
            slug = trade["market_slug"]

            # Check resolution
            resolution = check_market_resolution(slug)
            if resolution is None:
                remaining.append(trade)
                continue

            actual_winner = resolution
            won = (trade["direction"] == actual_winner)

            if won:
                pnl = trade["quantity"] * (1.0 - trade["entry_price"])
                pnl_pct = ((1.0 / trade["entry_price"]) - 1.0) * 100
                exit_reason = "win"
                state.wins += 1
            else:
                pnl = -(trade["quantity"] * trade["entry_price"])
                pnl_pct = -100.0
                exit_reason = "loss"
                state.losses += 1

            state.total_pnl += pnl
            state.trade_count += 1
            self.balance += pnl

            self._close_trade(trade["id"], 1.0 if won else 0.0, pnl, pnl_pct, exit_reason)

            emoji = "✅" if won else "❌"
            self.log(f"   {emoji} {state.display}: {trade['direction'].upper()} → "
                     f"{'WON' if won else 'LOST'} | PnL: ${pnl:+.2f} | "
                     f"Balance: ${self.balance:.2f} | Record: {state.wins}W-{state.losses}L")

        state.open_trades = remaining

    def run(self):
        """Main paper trading loop."""
        print("🏞 The Outsiders — Paper Trader v2")
        print("=" * 60)
        print("📝 PAPER TRADING — No real money, real market data!")
        print(f"💰 Starting balance: ${self.STARTING_BALANCE:.2f}")
        print("=" * 60)

        self.log("📡 Strategies:")
        for s in self.strategies.values():
            p = s.params
            self.log(f"   {s.display}: TP={p['take_profit_pct']}% | "
                     f"SL={p['stop_loss_pct']}% | Edge>={p['min_edge_pct']}%")
        self.log(f"🔒 Max positions: {self.max_per_strategy}/strategy, {self.max_total} total")
        print("=" * 60)
        print("Press Ctrl+C to stop\n")

        while self.running:
            try:
                self._run_cycle()
                time.sleep(30)
            except KeyboardInterrupt:
                break
            except Exception as e:
                self.log(f"❌ Cycle error: {e}")
                time.sleep(60)

        self._print_summary()

    def _run_cycle(self):
        """Single trading cycle."""
        snapshot = get_full_market_snapshot()
        if not snapshot:
            return

        # Check open trades for resolution
        for state in self.strategies.values():
            self._check_open_trades(state, snapshot)

        # Get market data
        candles = get_recent_candles(minutes=30)  # Need more for EMA21 + BB20
        if not candles or len(candles) < 5:
            return

        kraken_book = fetch_kraken_orderbook()

        total_open = self._total_open()

        # Run each strategy
        for key, state in self.strategies.items():
            if snapshot["slug"] in state.traded_slugs:
                continue

            # Generate signal
            sig = state.signal_fn(
                candles=candles,
                market_up_price=snapshot["up_price"],
                market_down_price=snapshot["down_price"],
                kraken_orderbook=kraken_book,
                params=state.params,
            )

            fmt = state.format_fn(sig)
            self.log(f"   {state.display}: {fmt}")

            can_trade = total_open < self.max_total
            strategy_cap = len(state.open_trades) < self.max_per_strategy
            trade_size = self._calc_trade_size()
            has_funds = self.balance >= trade_size

            if sig.should_trade and can_trade and strategy_cap and has_funds:
                entry_price = snapshot["up_price"] if sig.direction == "up" else snapshot["down_price"]
                quantity = trade_size / entry_price if entry_price > 0 else 0
                cost = entry_price * quantity

                trade_data = {
                    "timestamp": int(time.time()),
                    "market_id": snapshot["slug"],
                    "strategy": state.name,
                    "direction": sig.direction,
                    "entry_price": entry_price,
                    "quantity": quantity,
                    "edge_pct": sig.edge_pct,
                    "confidence": sig.confidence,
                    "signal_data": sig.to_dict(),
                }

                trade_id = self._insert_trade(trade_data)
                self.balance -= cost  # Reserve funds

                state.open_trades.append({
                    "id": trade_id,
                    "direction": sig.direction,
                    "entry_price": entry_price,
                    "quantity": quantity,
                    "cost": cost,
                    "market_slug": snapshot["slug"],
                })

                emoji = "🟢" if sig.direction == "up" else "🔴"
                self.log(f"   {emoji} {state.display} PAPER: {sig.direction.upper()} @ "
                         f"${entry_price:.3f} | Qty: {quantity:.1f} | "
                         f"Cost: ${cost:.2f} | Edge: {sig.edge_pct:.1f}%")
                total_open += 1

            elif sig.should_trade and not has_funds:
                self.log(f"   ⚠️ {state.display}: Signal but no funds (${self.balance:.2f})")

            state.traded_slugs.add(snapshot["slug"])

    def _print_summary(self):
        """Print final summary."""
        print("\n" + "=" * 60)
        print("📊 Paper Trader v2 — Final Summary")
        print("=" * 60)
        for s in self.strategies.values():
            t = s.wins + s.losses
            wr = s.wins / t * 100 if t > 0 else 0
            print(f"  {s.display}: {t} trades ({s.wins}W / {s.losses}L) | "
                  f"WR: {wr:.1f}% | P&L: ${s.total_pnl:+.2f}")
        total_pnl = sum(s.total_pnl for s in self.strategies.values())
        print(f"\n  Total P&L: ${total_pnl:+.2f}")
        print(f"  Balance: ${self.balance:.2f} (started ${self.STARTING_BALANCE:.2f})")
        pct = (self.balance - self.STARTING_BALANCE) / self.STARTING_BALANCE * 100
        print(f"  Return: {pct:+.1f}%")


if __name__ == "__main__":
    trader = PaperTraderV2()
    trader.run()
