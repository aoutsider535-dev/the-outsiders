"""
🏞 The Outsiders — Paper Trader v3.1

Tests 3 NEW OOS-validated strategies with HTF overlay:
1. 📈 HTF Trend Follow — trade with 1hr+4hr macro trend (53.8% WR, highest volume)
2. 🔁 Streak Reversal — fade DOWN streaks of 3+ (65.2% WR)
3. 🎯 Combined Streak+Vol — streak + volume exhaustion (81.2% WR, sniper)

All strategies enhanced with higher timeframe (1hr/4hr) context.
"""
import time
import json
import os
import signal as sig_module
import sqlite3
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.data.polymarket import get_full_market_snapshot, check_market_resolution
from src.data.price_feed import get_recent_candles

from src.strategies.htf_context import get_htf_context
from src.strategies.htf_trend import (
    generate_signal as htf_trend_signal,
    format_signal_message as htf_trend_format,
    DEFAULTS as HTF_TREND_DEFAULTS,
)
from src.strategies.streak_reversal import (
    generate_signal as streak_signal,
    format_signal_message as streak_format,
    update_outcome as streak_update,
    DEFAULTS as STREAK_DEFAULTS,
)
from src.strategies.combined_streak_vol import (
    generate_signal as combo_signal,
    format_signal_message as combo_format,
    DEFAULTS as COMBO_DEFAULTS,
)

PST = timezone(timedelta(hours=-8))
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "paper_v3_1.db")

TAKER_FEE_PCT = 0.02
SLIPPAGE_PCT = 0.005

STRATEGY_CONFIG = {
    "htf_trend": {
        "name": "btc_5min_htf_trend_V3",
        "display": "📈 HTF Trend",
        "params": HTF_TREND_DEFAULTS.copy(),
    },
    "streak_reversal": {
        "name": "btc_5min_streak_rev_V3",
        "display": "🔁 Streak Reversal",
        "params": STREAK_DEFAULTS.copy(),
    },
    "combined": {
        "name": "btc_5min_combo_V3",
        "display": "🎯 Combo Streak+Vol",
        "params": COMBO_DEFAULTS.copy(),
    },
}


class StrategyState:
    def __init__(self, key, config):
        self.key = key
        self.name = config["name"]
        self.display = config["display"]
        self.params = config["params"]
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


class PaperTraderV31:
    STARTING_BALANCE = 100.0
    KELLY_RECALC_INTERVAL = 20
    KELLY_MIN_TRADES = 15
    KELLY_MIN_FRACTION = 0.01
    KELLY_MAX_FRACTION = 0.15

    def __init__(self, max_per_strategy=5, max_total=15,
                 trade_size_min=1.0, trade_size_max=50.0):
        self.balance = self.STARTING_BALANCE
        self.trade_size_min = trade_size_min
        self.trade_size_max = trade_size_max
        self.max_per_strategy = max_per_strategy
        self.max_total = max_total
        self.running = True
        self.strategies = {}
        self.last_htf = None
        self.htf_fetch_time = 0

        for key, config in STRATEGY_CONFIG.items():
            self.strategies[key] = StrategyState(key, config)

        self._init_db()
        sig_module.signal(sig_module.SIGINT, self._shutdown)
        sig_module.signal(sig_module.SIGTERM, self._shutdown)

    def _shutdown(self, signum, frame):
        print("\n⏹️  Shutting down Paper Trader v3.1...")
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
                timestamp INTEGER, market_id TEXT, strategy TEXT,
                direction TEXT, entry_price REAL, exit_price REAL,
                quantity REAL, pnl REAL, pnl_pct REAL,
                edge_pct REAL, confidence REAL, signal_data TEXT,
                status TEXT DEFAULT 'open', exit_reason TEXT,
                created_at TEXT, closed_at TEXT,
                fees REAL DEFAULT 0, slippage REAL DEFAULT 0
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
        except:
            return
        if len(rows) < self.KELLY_MIN_TRADES:
            return
        wins, losses = [], []
        for pnl, qty in rows:
            if qty and qty > 0:
                ret = pnl / qty
                (wins if pnl > 0 else losses).append(abs(ret))
        if not wins or not losses:
            return
        p = len(wins) / len(rows)
        b = (sum(wins) / len(wins)) / (sum(losses) / len(losses))
        half_kelly = (p - (1 - p) / b) / 2
        old = state.kelly_fraction
        state.kelly_fraction = max(self.KELLY_MIN_FRACTION, min(self.KELLY_MAX_FRACTION, half_kelly))
        state.kelly_last_calc = state.trade_count
        if abs(old - state.kelly_fraction) > 0.001:
            self.log(f"📐 {state.display} Kelly: {old*100:.1f}% → {state.kelly_fraction*100:.1f}% (n={len(rows)})")

    def _total_open(self):
        return sum(len(s.open_trades) for s in self.strategies.values())

    def _get_htf(self):
        """Get HTF context, cached for 5 minutes."""
        now = time.time()
        if self.last_htf and (now - self.htf_fetch_time) < 300:
            return self.last_htf
        self.last_htf = get_htf_context()
        self.htf_fetch_time = now
        return self.last_htf

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
        tid = cur.lastrowid
        conn.commit()
        conn.close()
        return tid

    def _close_trade(self, tid, exit_price, pnl, pnl_pct, reason):
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "UPDATE trades SET status='closed', exit_price=?, pnl=?, pnl_pct=?, exit_reason=?, closed_at=? WHERE id=?",
            (exit_price, pnl, pnl_pct, reason, datetime.now(timezone.utc).isoformat(), tid)
        )
        conn.commit()
        conn.close()

    def _check_open_trades(self, state):
        remaining = []
        now = int(time.time())
        for trade in state.open_trades:
            age_min = (now - trade.get("timestamp", now)) / 60
            if age_min > 30:
                pnl = -(trade["quantity"] * trade["entry_price"])
                state.losses += 1
                state.total_pnl += pnl
                state.trade_count += 1
                state.consecutive_losses += 1
                self.balance += pnl
                self._close_trade(trade["id"], 0.0, pnl, -100.0, "stuck_timeout")
                self.log(f"   ⏰ {state.display}: Stuck {age_min:.0f}min | ${pnl:+.2f}")
                # Update streak on loss
                streak_update("down" if trade["direction"] == "up" else "up")
                continue

            resolution = check_market_resolution(trade["market_slug"])
            if resolution is None:
                remaining.append(trade)
                continue
            if not resolution.get("resolved") or not resolution.get("winner"):
                remaining.append(trade)
                continue

            winner = resolution["winner"]
            won = (trade["direction"] == winner)

            if won:
                gross = trade["quantity"] * (1.0 - trade["entry_price"])
                pnl = gross - trade.get("fees", 0)
                pnl_pct = (pnl / (trade["entry_price"] * trade["quantity"])) * 100
                state.wins += 1
                state.consecutive_losses = 0
            else:
                pnl = -(trade["quantity"] * trade["entry_price"]) - trade.get("fees", 0)
                pnl_pct = -100.0
                state.losses += 1
                state.consecutive_losses += 1

            state.total_pnl += pnl
            state.trade_count += 1
            self.balance += pnl
            self._close_trade(trade["id"], 1.0 if won else 0.0, pnl, pnl_pct, "win" if won else "loss")

            # Update streak tracker with the actual outcome
            streak_update(winner)

            emoji = "✅" if won else "❌"
            self.log(f"   {emoji} {state.display}: {trade['direction'].upper()} → "
                     f"{'WON' if won else 'LOST'} | ${pnl:+.2f} | Bal: ${self.balance:.2f} | "
                     f"{state.wins}W-{state.losses}L")

        state.open_trades = remaining

    def _place_trade(self, state, sig, snapshot, htf):
        trade_size = self._calc_trade_size(strategy_key=state.key)
        entry_price = snapshot["up_price"] if sig.direction == "up" else snapshot["down_price"]
        slippage = entry_price * SLIPPAGE_PCT
        fill = min(entry_price + slippage, 0.99)
        qty = trade_size / fill if fill > 0 else 0
        cost = fill * qty
        fee = cost * TAKER_FEE_PCT

        tid = self._insert_trade({
            "timestamp": int(time.time()), "market_id": snapshot["slug"],
            "strategy": state.name, "direction": sig.direction,
            "entry_price": fill, "quantity": qty,
            "edge_pct": sig.edge_pct, "confidence": sig.confidence,
            "signal_data": sig.to_dict(), "fees": fee, "slippage": slippage,
        })
        self.balance -= (cost + fee)
        state.open_trades.append({
            "id": tid, "direction": sig.direction, "entry_price": fill,
            "quantity": qty, "cost": cost, "fees": fee,
            "market_slug": snapshot["slug"], "timestamp": int(time.time()),
        })

        emoji = "🟢" if sig.direction == "up" else "🔴"
        self.log(f"   {emoji} {state.display} PAPER: {sig.direction.upper()} @ "
                 f"${fill:.3f} | Qty: {qty:.1f} | ${cost + fee:.2f} | "
                 f"Edge: {sig.edge_pct:.1f}% | Kelly: {state.kelly_fraction*100:.1f}%")

    def run(self):
        print("🏞 The Outsiders — Paper Trader v3.1 (HTF Enhanced)")
        print("=" * 60)
        print("📝 3 NEW OOS-validated strategies with 1hr/4hr overlay")
        print(f"💰 Starting balance: ${self.STARTING_BALANCE:.2f}")
        print(f"📊 Fees: {TAKER_FEE_PCT*100}% + {SLIPPAGE_PCT*100}% slippage")
        print("=" * 60)

        for s in self.strategies.values():
            size = self._calc_trade_size(strategy_key=s.key)
            self.log(f"   {s.display}: Kelly={s.kelly_fraction*100:.1f}% (${size:.2f})")
        self.log(f"🔒 Max: {self.max_per_strategy}/strategy, {self.max_total} total")
        print("=" * 60 + "\n")

        while self.running:
            try:
                self._run_cycle()
                time.sleep(30)
            except KeyboardInterrupt:
                break
            except Exception as e:
                self.log(f"❌ Error: {e}")
                import traceback; traceback.print_exc()
                time.sleep(60)

        self._print_summary()

    def _run_cycle(self):
        snapshot = get_full_market_snapshot()
        if not snapshot:
            return

        for state in self.strategies.values():
            self._check_open_trades(state)

        candles = get_recent_candles(minutes=30)
        if not candles or len(candles) < 5:
            return

        # Normalize candles for combo strategy
        norm_candles = []
        for c in candles:
            nc = dict(c)
            if "volume" not in nc and "volumefrom" in nc:
                nc["volume"] = nc["volumefrom"]
            elif "volume" not in nc:
                nc["volume"] = 0
            norm_candles.append(nc)

        htf = self._get_htf()
        total_open = self._total_open()

        for key, state in self.strategies.items():
            if snapshot["slug"] in state.traded_slugs:
                continue

            can_trade = total_open < self.max_total and len(state.open_trades) < self.max_per_strategy
            trade_size = self._calc_trade_size(strategy_key=key)
            has_funds = self.balance >= trade_size

            # Generate signal
            if key == "htf_trend":
                sig = htf_trend_signal(
                    market_up_price=snapshot["up_price"],
                    market_down_price=snapshot["down_price"],
                    htf_context=htf, params=state.params,
                )
            elif key == "streak_reversal":
                sig = streak_signal(
                    market_up_price=snapshot["up_price"],
                    market_down_price=snapshot["down_price"],
                    htf_context=htf, params=state.params,
                )
            elif key == "combined":
                sig = combo_signal(
                    candles=norm_candles,
                    market_up_price=snapshot["up_price"],
                    market_down_price=snapshot["down_price"],
                    htf_context=htf, params=state.params,
                )
            else:
                continue

            # Log
            fmt_fn = {"htf_trend": htf_trend_format, "streak_reversal": streak_format,
                      "combined": combo_format}
            self.log(f"   {state.display}: {fmt_fn[key](sig)}")

            if sig.should_trade and can_trade and has_funds:
                self._place_trade(state, sig, snapshot, htf)
                total_open += 1
            elif sig.should_trade and not has_funds:
                self.log(f"   ⚠️ {state.display}: Signal but no funds (${self.balance:.2f})")

            state.traded_slugs.add(snapshot["slug"])

    def _print_summary(self):
        print("\n" + "=" * 60)
        print("📊 Paper Trader v3.1 — Summary")
        print("=" * 60)
        for s in self.strategies.values():
            t = s.wins + s.losses
            wr = s.wins / t * 100 if t > 0 else 0
            print(f"  {s.display}: {t} trades ({s.wins}W/{s.losses}L) | "
                  f"WR: {wr:.1f}% | P&L: ${s.total_pnl:+.2f}")
        total = sum(s.total_pnl for s in self.strategies.values())
        print(f"\n  Total: ${total:+.2f} | Balance: ${self.balance:.2f}")


if __name__ == "__main__":
    trader = PaperTraderV31()
    trader.run()
