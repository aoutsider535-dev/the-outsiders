"""
🏞 The Outsiders — Paper Trader v3 (Contrarian / Inverse)

Runs the SAME strategies as Paper Trader v2 but INVERTS every trade direction.
If v2 says BUY UP → v3 buys DOWN, and vice versa.

The thesis: if v2 strategies are consistently wrong, v3 should be consistently right.
This is a zero-cost way to test whether the signals have information content
(even if the direction is backwards).

Strategies (inverted):
1. 📈↩️ Trend Rider Inverse — fades the trend signal
2. 🎯↩️ VWAP Sniper Inverse — fades the mean reversion signal
3. ⚡↩️ Vol Spike Inverse — fades the volatility breakout signal
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
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "paper_v3.db")

# Same realistic fee simulation as v2
TAKER_FEE_PCT = 0.02
SLIPPAGE_PCT = 0.005

STRATEGY_CONFIG = {
    "trend_rider_inv": {
        "name": "btc_5min_trend_rider_INV",
        "display": "📈↩️ Trend Rider INV",
        "params": TREND_RIDER_DEFAULTS.copy(),
        "signal_fn": trend_rider_signal,
        "format_fn": trend_rider_format,
    },
    "vwap_sniper_inv": {
        "name": "btc_5min_vwap_sniper_INV",
        "display": "🎯↩️ VWAP Sniper INV",
        "params": VWAP_SNIPER_DEFAULTS.copy(),
        "signal_fn": vwap_sniper_signal,
        "format_fn": vwap_sniper_format,
    },
    "volatility_spike_inv": {
        "name": "btc_5min_vol_spike_INV",
        "display": "⚡↩️ Vol Spike INV",
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
        self.kelly_fraction = 0.04
        self.kelly_last_calc = 0


class PaperTraderV3:
    """Inverse/Contrarian paper trader — flips every signal from v2 strategies."""

    STARTING_BALANCE = 100.0
    KELLY_RECALC_INTERVAL = 20
    KELLY_MIN_TRADES = 15
    KELLY_MIN_FRACTION = 0.01
    KELLY_MAX_FRACTION = 0.15

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
        print("\n⏹️  Shutting down Paper Trader v3 (Inverse)...")
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
                original_direction TEXT,
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
        conn.commit()
        conn.close()

    def _calc_trade_size(self, strategy_key=None):
        if self.balance <= 0:
            return self.trade_size_min
        fraction = 0.04
        if strategy_key and strategy_key in self.strategies:
            state = self.strategies[strategy_key]
            self._maybe_recalc_kelly(state)
            fraction = state.kelly_fraction
        size = self.balance * fraction
        return max(self.trade_size_min, min(self.trade_size_max, round(size, 2)))

    def _maybe_recalc_kelly(self, state):
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
            """INSERT INTO trades (timestamp, market_id, strategy, direction, original_direction,
               entry_price, quantity, edge_pct, confidence, signal_data, status, created_at, fees, slippage)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)""",
            (data["timestamp"], data["market_id"], data["strategy"], data["direction"],
             data["original_direction"], data["entry_price"], data["quantity"],
             data["edge_pct"], data["confidence"],
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
        remaining = []
        now = int(time.time())

        for trade in state.open_trades:
            slug = trade["market_slug"]
            age_min = (now - trade.get("timestamp", now)) / 60
            if age_min > 30:
                pnl = -(trade["quantity"] * trade["entry_price"])
                state.losses += 1
                state.total_pnl += pnl
                state.trade_count += 1
                state.consecutive_losses += 1
                self.balance += pnl
                self._close_trade(trade["id"], 0.0, pnl, -100.0, "stuck_timeout")
                self.log(f"   ⏰ {state.display}: Stuck trade closed after {age_min:.0f}min | PnL: ${pnl:+.2f}")
                continue

            resolution = check_market_resolution(slug)
            if resolution is None:
                remaining.append(trade)
                continue

            # resolution is a dict: {"resolved": bool, "winner": "up"/"down"/None, ...}
            if not resolution.get("resolved") or not resolution.get("winner"):
                remaining.append(trade)
                continue
            actual_winner = resolution["winner"]
            won = (trade["direction"] == actual_winner)

            if won:
                gross_pnl = trade["quantity"] * (1.0 - trade["entry_price"])
                pnl = gross_pnl - trade.get("fees", 0)
                pnl_pct = (pnl / (trade["entry_price"] * trade["quantity"])) * 100
                exit_reason = "win"
                state.wins += 1
                state.consecutive_losses = 0
            else:
                pnl = -(trade["quantity"] * trade["entry_price"]) - trade.get("fees", 0)
                pnl_pct = -100.0
                exit_reason = "loss"
                state.losses += 1
                state.consecutive_losses += 1

            state.total_pnl += pnl
            state.trade_count += 1
            self.balance += pnl
            self._close_trade(trade["id"], 1.0 if won else 0.0, pnl, pnl_pct, exit_reason)

            orig = trade.get("original_direction", "?").upper()
            emoji = "✅" if won else "❌"
            self.log(f"   {emoji} {state.display}: {orig}→{trade['direction'].upper()} → "
                     f"{'WON' if won else 'LOST'} | PnL: ${pnl:+.2f} | "
                     f"Balance: ${self.balance:.2f} | Record: {state.wins}W-{state.losses}L")

        state.open_trades = remaining

    def _flip_direction(self, direction):
        """The core inversion: up→down, down→up."""
        return "down" if direction == "up" else "up"

    def run(self):
        print("🏞 The Outsiders — Paper Trader v3 (INVERSE / CONTRARIAN)")
        print("=" * 60)
        print("🔄 Every signal is FLIPPED — if v2 buys UP, v3 buys DOWN")
        print(f"💰 Starting balance: ${self.STARTING_BALANCE:.2f}")
        print(f"📊 Simulating: {TAKER_FEE_PCT*100}% fees + {SLIPPAGE_PCT*100}% slippage")
        print("=" * 60)

        for s in self.strategies.values():
            self._maybe_recalc_kelly(s)

        self.log("📡 Strategies (INVERTED, Half Kelly):")
        for s in self.strategies.values():
            p = s.params
            size = self._calc_trade_size(strategy_key=s.key)
            self.log(f"   {s.display}: Edge>={p['min_edge_pct']}% | "
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
        snapshot = get_full_market_snapshot()
        if not snapshot:
            return

        for state in self.strategies.values():
            self._check_open_trades(state, snapshot)

        candles = get_recent_candles(minutes=30)
        if not candles or len(candles) < 5:
            return

        kraken_book = fetch_kraken_orderbook()

        # Liquidity check
        up_ob = snapshot.get("up_orderbook")
        down_ob = snapshot.get("down_orderbook")
        if up_ob and down_ob:
            total_depth = (up_ob.get("bid_depth", 0) + up_ob.get("ask_depth", 0) +
                          down_ob.get("bid_depth", 0) + down_ob.get("ask_depth", 0))
            if total_depth < 50:
                self.log(f"⚠️ Thin orderbook (depth: ${total_depth:.0f}) — skipping cycle")
                return

        total_open = self._total_open()

        for key, state in self.strategies.items():
            if snapshot["slug"] in state.traded_slugs:
                continue

            sig = state.signal_fn(
                candles=candles,
                market_up_price=snapshot["up_price"],
                market_down_price=snapshot["down_price"],
                kraken_orderbook=kraken_book,
                params=state.params,
            )

            # Log the original signal for transparency
            fmt = state.format_fn(sig)
            self.log(f"   {state.display} (original): {fmt}")

            can_trade = total_open < self.max_total
            strategy_cap = len(state.open_trades) < self.max_per_strategy
            trade_size = self._calc_trade_size(strategy_key=key)
            has_funds = self.balance >= trade_size
            in_cooldown = state.cooldown_skips > 0

            if sig.should_trade and can_trade and strategy_cap and has_funds and not in_cooldown:
                # *** THE FLIP ***
                original_direction = sig.direction
                inverted_direction = self._flip_direction(original_direction)

                # Use the INVERTED direction's market price
                entry_price = snapshot["up_price"] if inverted_direction == "up" else snapshot["down_price"]

                slippage = entry_price * SLIPPAGE_PCT
                fill_price = min(entry_price + slippage, 0.99)
                quantity = trade_size / fill_price if fill_price > 0 else 0
                cost = fill_price * quantity
                fee = cost * TAKER_FEE_PCT
                total_cost = cost + fee

                trade_data = {
                    "timestamp": int(time.time()),
                    "market_id": snapshot["slug"],
                    "strategy": state.name,
                    "direction": inverted_direction,
                    "original_direction": original_direction,
                    "entry_price": fill_price,
                    "quantity": quantity,
                    "edge_pct": sig.edge_pct,
                    "confidence": sig.confidence,
                    "signal_data": sig.to_dict(),
                    "fees": fee,
                    "slippage": slippage,
                }

                trade_id = self._insert_trade(trade_data)
                self.balance -= total_cost

                state.open_trades.append({
                    "id": trade_id,
                    "direction": inverted_direction,
                    "original_direction": original_direction,
                    "entry_price": fill_price,
                    "quantity": quantity,
                    "cost": cost,
                    "fees": fee,
                    "market_slug": snapshot["slug"],
                    "timestamp": int(time.time()),
                })

                orig_emoji = "🟢" if original_direction == "up" else "🔴"
                inv_emoji = "🔴" if original_direction == "up" else "🟢"
                self.log(f"   🔄 {state.display}: {orig_emoji}{original_direction.upper()} → "
                         f"{inv_emoji}{inverted_direction.upper()} @ ${fill_price:.3f} | "
                         f"Qty: {quantity:.1f} | Cost: ${total_cost:.2f} | Edge: {sig.edge_pct:.1f}%")
                total_open += 1

            elif sig.should_trade and in_cooldown:
                state.cooldown_skips -= 1
                self.log(f"   🛑 {state.display}: Skipping (cooldown, {state.cooldown_skips} skips left)")
            elif sig.should_trade and not has_funds:
                self.log(f"   ⚠️ {state.display}: Signal but no funds (${self.balance:.2f})")

            state.traded_slugs.add(snapshot["slug"])

    def _print_summary(self):
        print("\n" + "=" * 60)
        print("📊 Paper Trader v3 (INVERSE) — Final Summary")
        print("=" * 60)
        for s in self.strategies.values():
            t = s.wins + s.losses
            wr = s.wins / t * 100 if t > 0 else 0
            print(f"  {s.display}: {t} trades ({s.wins}W / {s.losses}L) | "
                  f"WR: {wr:.1f}% | P&L: ${s.total_pnl:+.2f}")
        total_pnl = sum(s.total_pnl for s in self.strategies.values())
        print(f"\n  Total P&L: ${total_pnl:+.2f} (after fees + slippage)")
        print(f"  Balance: ${self.balance:.2f} (started ${self.STARTING_BALANCE:.2f})")
        pct = (self.balance - self.STARTING_BALANCE) / self.STARTING_BALANCE * 100
        print(f"  Return: {pct:+.1f}%")


if __name__ == "__main__":
    trader = PaperTraderV3()
    trader.run()
