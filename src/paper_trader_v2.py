"""
🏞 The Outsiders — Paper Trader v2

Tests 3 new uncorrelated strategies on Polymarket BTC 5-min markets.
No real money — tracks simulated P&L against real market outcomes.
Mimics live trader as closely as possible (Kelly sizing, fees, slippage, cooldowns).

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

# Realistic fee simulation
TAKER_FEE_PCT = 0.02  # 2% taker fee on entry
SLIPPAGE_PCT = 0.005   # 0.5% avg slippage (limit orders sometimes fill worse)

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
        self.consecutive_losses = 0
        self.cooldown_skips = 0
        # Half Kelly
        self.kelly_fraction = 0.04  # Default 4%, updated by Kelly calc
        self.kelly_last_calc = 0


class PaperTraderV2:
    STARTING_BALANCE = 100.0

    # Kelly constants (same as live)
    KELLY_RECALC_INTERVAL = 20
    KELLY_MIN_TRADES = 15
    KELLY_MIN_FRACTION = 0.01   # 1% floor
    KELLY_MAX_FRACTION = 0.15   # 15% cap

    def __init__(self, max_per_strategy=5, max_total=15,
                 trade_size_min=5.0, trade_size_max=50.0):
        self.balance = self.STARTING_BALANCE
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
                closed_at TEXT,
                fees REAL DEFAULT 0,
                slippage REAL DEFAULT 0
            )
        """)
        # Add fees/slippage columns if missing (upgrade existing DBs)
        try:
            conn.execute("ALTER TABLE trades ADD COLUMN fees REAL DEFAULT 0")
        except:
            pass
        try:
            conn.execute("ALTER TABLE trades ADD COLUMN slippage REAL DEFAULT 0")
        except:
            pass
        conn.commit()
        conn.close()

    def _calc_trade_size(self, strategy_key=None):
        """Calculate trade size using Half Kelly per strategy."""
        if self.balance <= 0:
            return self.trade_size_min

        fraction = 0.04  # fallback
        if strategy_key and strategy_key in self.strategies:
            state = self.strategies[strategy_key]
            self._maybe_recalc_kelly(state)
            fraction = state.kelly_fraction

        size = self.balance * fraction
        return max(self.trade_size_min, min(self.trade_size_max, round(size, 2)))

    def _maybe_recalc_kelly(self, state):
        """Recalculate Half Kelly for a strategy if enough new trades."""
        trades_since = state.trade_count - state.kelly_last_calc
        if trades_since < self.KELLY_RECALC_INTERVAL and state.kelly_last_calc > 0:
            return

        try:
            conn = sqlite3.connect(DB_PATH)
            rows = conn.execute(
                "SELECT pnl, quantity FROM trades WHERE strategy = ? AND status = 'closed'",
                (state.name,)
            ).fetchall()
            conn.close()
        except Exception as e:
            self.log(f"⚠️ Kelly calc error: {e}")
            return

        if len(rows) < self.KELLY_MIN_TRADES:
            return

        wins, losses = [], []
        for pnl, qty in rows:
            if qty and qty > 0:
                ret = pnl / qty
                if pnl > 0:
                    wins.append(ret)
                else:
                    losses.append(abs(ret))

        if not wins or not losses:
            return

        p = len(wins) / len(rows)
        avg_win = sum(wins) / len(wins)
        avg_loss = sum(losses) / len(losses)
        b = avg_win / avg_loss if avg_loss > 0 else 1.0

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

    def _insert_trade(self, data):
        conn = sqlite3.connect(DB_PATH)
        cur = conn.execute(
            """INSERT INTO trades (timestamp, market_id, strategy, direction, entry_price,
               quantity, edge_pct, confidence, signal_data, status, created_at, fees, slippage)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)""",
            (data["timestamp"], data["market_id"], data["strategy"], data["direction"],
             data["entry_price"], data["quantity"], data["edge_pct"], data["confidence"],
             json.dumps(data.get("signal_data", {})),
             datetime.now(timezone.utc).isoformat(),
             data.get("fees", 0), data.get("slippage", 0))
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
        now = int(time.time())

        for trade in state.open_trades:
            slug = trade["market_slug"]

            # Stuck trade cleanup — if open > 30 min on a 5-min market, force close as loss
            age_min = (now - trade.get("timestamp", now)) / 60
            if age_min > 30:
                pnl = -(trade["quantity"] * trade["entry_price"])
                state.losses += 1
                state.total_pnl += pnl
                state.trade_count += 1
                state.consecutive_losses += 1
                self.balance += pnl
                self._close_trade(trade["id"], 0.0, pnl, -100.0, "stuck_timeout")
                self.log(f"   ⏰ {state.display}: Stuck trade closed after {age_min:.0f}min | "
                         f"PnL: ${pnl:+.2f}")
                continue

            # Check resolution
            resolution = check_market_resolution(slug)
            if resolution is None:
                remaining.append(trade)
                continue

            actual_winner = resolution
            won = (trade["direction"] == actual_winner)

            if won:
                # Net P&L after fees: payout - cost - entry fee
                gross_pnl = trade["quantity"] * (1.0 - trade["entry_price"])
                pnl = gross_pnl - trade.get("fees", 0)
                pnl_pct = (pnl / (trade["entry_price"] * trade["quantity"])) * 100
                exit_reason = "win"
                state.wins += 1
                state.consecutive_losses = 0
            else:
                # Loss = full cost (already paid fees on entry)
                pnl = -(trade["quantity"] * trade["entry_price"]) - trade.get("fees", 0)
                pnl_pct = -100.0
                exit_reason = "loss"
                state.losses += 1
                state.consecutive_losses += 1

            state.total_pnl += pnl
            state.trade_count += 1
            self.balance += pnl

            self._close_trade(trade["id"], 1.0 if won else 0.0, pnl, pnl_pct, exit_reason)

            emoji = "✅" if won else "❌"
            self.log(f"   {emoji} {state.display}: {trade['direction'].upper()} → "
                     f"{'WON' if won else 'LOST'} | PnL: ${pnl:+.2f} (fee: ${trade.get('fees', 0):.2f}) | "
                     f"Balance: ${self.balance:.2f} | Record: {state.wins}W-{state.losses}L")

        state.open_trades = remaining

    def run(self):
        """Main paper trading loop."""
        print("🏞 The Outsiders — Paper Trader v2")
        print("=" * 60)
        print("📝 PAPER TRADING — No real money, real market data!")
        print(f"💰 Starting balance: ${self.STARTING_BALANCE:.2f}")
        print(f"📊 Simulating: {TAKER_FEE_PCT*100}% fees + {SLIPPAGE_PCT*100}% slippage")
        print("=" * 60)

        # Initialize Kelly fractions
        for s in self.strategies.values():
            self._maybe_recalc_kelly(s)

        self.log("📡 Strategies (Half Kelly sizing):")
        for s in self.strategies.values():
            p = s.params
            size = self._calc_trade_size(strategy_key=s.key)
            self.log(f"   {s.display}: TP={p['take_profit_pct']}% | "
                     f"SL={p['stop_loss_pct']}% | Edge>={p['min_edge_pct']}% | "
                     f"Kelly={s.kelly_fraction*100:.1f}% (${size:.2f})")
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

        # Get market data (30 min for EMA21 + BB20)
        candles = get_recent_candles(minutes=30)
        if not candles or len(candles) < 5:
            return

        kraken_book = fetch_kraken_orderbook()

        # Liquidity check (same as live trader)
        up_ob = snapshot.get("up_orderbook")
        down_ob = snapshot.get("down_orderbook")
        if up_ob and down_ob:
            total_depth = (up_ob.get("bid_depth", 0) + up_ob.get("ask_depth", 0) +
                          down_ob.get("bid_depth", 0) + down_ob.get("ask_depth", 0))
            if total_depth < 50:
                self.log(f"⚠️ Thin orderbook (depth: ${total_depth:.0f}) — skipping cycle")
                return

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

            # Kelly-based trade size
            trade_size = self._calc_trade_size(strategy_key=key)
            has_funds = self.balance >= trade_size

            # Loss streak cooldown (same logic as live Smart Money)
            in_cooldown = state.cooldown_skips > 0

            if sig.should_trade and can_trade and strategy_cap and has_funds and not in_cooldown:
                entry_price = snapshot["up_price"] if sig.direction == "up" else snapshot["down_price"]

                # Simulate slippage — actual fill slightly worse
                slippage = entry_price * SLIPPAGE_PCT
                fill_price = min(entry_price + slippage, 0.99)  # Can't exceed $0.99

                quantity = trade_size / fill_price if fill_price > 0 else 0
                cost = fill_price * quantity

                # Simulate taker fee on entry
                fee = cost * TAKER_FEE_PCT
                total_cost = cost + fee

                trade_data = {
                    "timestamp": int(time.time()),
                    "market_id": snapshot["slug"],
                    "strategy": state.name,
                    "direction": sig.direction,
                    "entry_price": fill_price,
                    "quantity": quantity,
                    "edge_pct": sig.edge_pct,
                    "confidence": sig.confidence,
                    "signal_data": sig.to_dict(),
                    "fees": fee,
                    "slippage": slippage,
                }

                trade_id = self._insert_trade(trade_data)
                self.balance -= total_cost  # Reserve funds + fees

                state.open_trades.append({
                    "id": trade_id,
                    "direction": sig.direction,
                    "entry_price": fill_price,
                    "quantity": quantity,
                    "cost": cost,
                    "fees": fee,
                    "market_slug": snapshot["slug"],
                    "timestamp": int(time.time()),
                })

                emoji = "🟢" if sig.direction == "up" else "🔴"
                self.log(f"   {emoji} {state.display} PAPER: {sig.direction.upper()} @ "
                         f"${fill_price:.3f} | Qty: {quantity:.1f} | "
                         f"Cost: ${total_cost:.2f} (fee: ${fee:.2f}) | Edge: {sig.edge_pct:.1f}% | "
                         f"Kelly: {state.kelly_fraction*100:.1f}%")
                total_open += 1

            elif sig.should_trade and in_cooldown:
                state.cooldown_skips -= 1
                self.log(f"   🛑 {state.display}: Skipping (cooldown, {state.cooldown_skips} skips left)")
            elif sig.should_trade and not has_funds:
                self.log(f"   ⚠️ {state.display}: Signal but no funds (${self.balance:.2f})")
            elif sig.should_trade and not can_trade:
                self.log(f"   ⚠️ {state.display}: Signal but at max positions ({self.max_total})")
            elif sig.should_trade and not strategy_cap:
                self.log(f"   ⚠️ {state.display}: Signal but strategy cap ({self.max_per_strategy})")

            state.traded_slugs.add(snapshot["slug"])

    def _print_summary(self):
        """Print final summary."""
        print("\n" + "=" * 60)
        print("📊 Paper Trader v2 — Final Summary")
        print("=" * 60)
        total_fees = 0
        for s in self.strategies.values():
            t = s.wins + s.losses
            wr = s.wins / t * 100 if t > 0 else 0
            print(f"  {s.display}: {t} trades ({s.wins}W / {s.losses}L) | "
                  f"WR: {wr:.1f}% | P&L: ${s.total_pnl:+.2f} | "
                  f"Kelly: {s.kelly_fraction*100:.1f}%")
        total_pnl = sum(s.total_pnl for s in self.strategies.values())
        print(f"\n  Total P&L: ${total_pnl:+.2f} (after fees + slippage)")
        print(f"  Balance: ${self.balance:.2f} (started ${self.STARTING_BALANCE:.2f})")
        pct = (self.balance - self.STARTING_BALANCE) / self.STARTING_BALANCE * 100
        print(f"  Return: {pct:+.1f}%")


if __name__ == "__main__":
    trader = PaperTraderV2()
    trader.run()
