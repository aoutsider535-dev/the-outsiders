"""
🏞 The Outsiders — Paper Trader v3.2 (INVERSE HTF Trend)

Takes the HTF Trend Follow signal and FLIPS it.
If HTF says UP → we bet DOWN, and vice versa.

Hypothesis: HTF Trend on v3.1 shows 36% WR (64% inverse).
This tests whether systematically inverting it is profitable.
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

PST = timezone(timedelta(hours=-8))
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "paper_v3_2.db")

TAKER_FEE_PCT = 0.02
SLIPPAGE_PCT = 0.005
STARTING_BALANCE = 100.0


class PaperTraderV32:
    def __init__(self, max_open=5, trade_size_min=1.0, trade_size_max=50.0):
        self.balance = self._recover_balance()
        self.trade_size_min = trade_size_min
        self.trade_size_max = trade_size_max
        self.max_open = max_open
        self.running = True
        self.open_trades = []
        self.traded_slugs = set()
        self.wins = 0
        self.losses = 0
        self.total_pnl = 0.0
        self.kelly_fraction = 0.04
        self.kelly_last_calc = 0
        self.trade_count = 0
        self.last_htf = None
        self.htf_fetch_time = 0
        self.params = HTF_TREND_DEFAULTS.copy()

        self._init_db()
        sig_module.signal(sig_module.SIGINT, self._shutdown)
        sig_module.signal(sig_module.SIGTERM, self._shutdown)

    def _shutdown(self, signum, frame):
        print("\n⏹️  Shutting down Paper Trader v3.2...")
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
                fees REAL DEFAULT 0, slippage REAL DEFAULT 0,
                original_direction TEXT
            )
        """)
        conn.commit()
        conn.close()

    def _recover_balance(self):
        try:
            conn = sqlite3.connect(DB_PATH)
            pnl = conn.execute("SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE status='closed'").fetchone()[0]
            open_cost = conn.execute(
                "SELECT COALESCE(SUM(entry_price * quantity + fees), 0) FROM trades WHERE status='open'"
            ).fetchone()[0]
            conn.close()
            bal = STARTING_BALANCE + pnl - open_cost
            if abs(bal - STARTING_BALANCE) > 0.01:
                print(f"💰 Recovered balance from DB: ${bal:.2f} (P&L: ${pnl:+.2f}, locked: ${open_cost:.2f})")
            return bal
        except:
            return STARTING_BALANCE

    def _get_htf(self):
        now = time.time()
        if self.last_htf and (now - self.htf_fetch_time) < 300:
            return self.last_htf
        self.last_htf = get_htf_context()
        self.htf_fetch_time = now
        return self.last_htf

    def _calc_trade_size(self):
        if self.balance <= 0:
            return self.trade_size_min
        size = self.balance * self.kelly_fraction
        return max(self.trade_size_min, min(self.trade_size_max, round(size, 2)))

    def _maybe_recalc_kelly(self):
        trades_since = self.trade_count - self.kelly_last_calc
        if trades_since < 20 and self.kelly_last_calc > 0:
            return
        try:
            conn = sqlite3.connect(DB_PATH)
            rows = conn.execute(
                "SELECT pnl, quantity FROM trades WHERE status='closed'"
            ).fetchall()
            conn.close()
        except:
            return
        if len(rows) < 15:
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
        old = self.kelly_fraction
        self.kelly_fraction = max(0.01, min(0.15, half_kelly))
        self.kelly_last_calc = self.trade_count
        if abs(old - self.kelly_fraction) > 0.001:
            self.log(f"📐 Kelly: {old*100:.1f}% → {self.kelly_fraction*100:.1f}% (n={len(rows)})")

    def _check_open_trades(self):
        remaining = []
        now = int(time.time())
        for trade in self.open_trades:
            age_min = (now - trade.get("timestamp", now)) / 60
            if age_min > 30:
                pnl = -(trade["quantity"] * trade["entry_price"])
                self.losses += 1
                self.total_pnl += pnl
                self.trade_count += 1
                self.balance += pnl
                self._close_trade(trade["id"], 0.0, pnl, -100.0, "stuck_timeout")
                self.log(f"   ⏰ Stuck {age_min:.0f}min | ${pnl:+.2f}")
                continue

            resolution = check_market_resolution(trade["market_slug"])
            if resolution is None or not resolution.get("resolved") or not resolution.get("winner"):
                remaining.append(trade)
                continue

            winner = resolution["winner"]
            won = (trade["direction"] == winner)

            if won:
                gross = trade["quantity"] * (1.0 - trade["entry_price"])
                pnl = gross - trade.get("fees", 0)
                pnl_pct = (pnl / (trade["entry_price"] * trade["quantity"])) * 100
                self.wins += 1
            else:
                pnl = -(trade["quantity"] * trade["entry_price"]) - trade.get("fees", 0)
                pnl_pct = -100.0
                self.losses += 1

            self.total_pnl += pnl
            self.trade_count += 1
            self.balance += pnl
            self._close_trade(trade["id"], 1.0 if won else 0.0, pnl, pnl_pct, "win" if won else "loss")

            emoji = "✅" if won else "❌"
            orig = trade.get("original_direction", "?").upper()
            self.log(f"   {emoji} 🔄 INV HTF: HTF said {orig} → we bet {trade['direction'].upper()} → "
                     f"{'WON' if won else 'LOST'} | ${pnl:+.2f} | Bal: ${self.balance:.2f} | "
                     f"{self.wins}W-{self.losses}L")

        self.open_trades = remaining

    def _close_trade(self, tid, exit_price, pnl, pnl_pct, reason):
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "UPDATE trades SET status='closed', exit_price=?, pnl=?, pnl_pct=?, exit_reason=?, closed_at=? WHERE id=?",
            (exit_price, pnl, pnl_pct, reason, datetime.now(timezone.utc).isoformat(), tid)
        )
        conn.commit()
        conn.close()

    def _place_trade(self, sig, snapshot, flipped_direction, original_direction):
        self._maybe_recalc_kelly()
        trade_size = self._calc_trade_size()
        entry_price = snapshot["up_price"] if flipped_direction == "up" else snapshot["down_price"]
        slippage = entry_price * SLIPPAGE_PCT
        fill = min(entry_price + slippage, 0.99)
        qty = trade_size / fill if fill > 0 else 0
        cost = fill * qty
        fee = cost * TAKER_FEE_PCT

        conn = sqlite3.connect(DB_PATH)
        cur = conn.execute(
            """INSERT INTO trades (timestamp, market_id, strategy, direction, entry_price,
               quantity, edge_pct, confidence, signal_data, status, created_at, fees, slippage, original_direction)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)""",
            (int(time.time()), snapshot["slug"], "btc_5min_inv_htf_V32",
             flipped_direction, fill, qty, sig.edge_pct, sig.confidence,
             json.dumps(sig.to_dict()),
             datetime.now(timezone.utc).isoformat(), fee, slippage, original_direction)
        )
        tid = cur.lastrowid
        conn.commit()
        conn.close()

        self.balance -= (cost + fee)
        self.open_trades.append({
            "id": tid, "direction": flipped_direction, "entry_price": fill,
            "quantity": qty, "cost": cost, "fees": fee,
            "market_slug": snapshot["slug"], "timestamp": int(time.time()),
            "original_direction": original_direction,
        })

        emoji = "🟢" if flipped_direction == "up" else "🔴"
        self.log(f"   {emoji} 🔄 INV HTF PAPER: HTF said {original_direction.upper()} → "
                 f"we bet {flipped_direction.upper()} @ ${fill:.3f} | Qty: {qty:.1f} | "
                 f"${cost + fee:.2f} | Kelly: {self.kelly_fraction*100:.1f}%")

    def run(self):
        print("🏞 The Outsiders — Paper Trader v3.2 (INVERSE HTF Trend)")
        print("=" * 60)
        print("🔄 Takes HTF Trend signal and FLIPS the direction")
        print(f"💰 Starting balance: ${STARTING_BALANCE:.2f}")
        print(f"📊 Fees: {TAKER_FEE_PCT*100}% + {SLIPPAGE_PCT*100}% slippage")
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

        self._check_open_trades()

        if snapshot["slug"] in self.traded_slugs:
            return

        htf = self._get_htf()
        sig = htf_trend_signal(
            market_up_price=snapshot["up_price"],
            market_down_price=snapshot["down_price"],
            htf_context=htf, params=self.params,
        )

        self.log(f"   📈 HTF Trend: {htf_trend_format(sig)}")

        if sig.should_trade:
            # THE FLIP — opposite of whatever HTF says
            original = sig.direction
            flipped = "down" if original == "up" else "up"

            can_trade = len(self.open_trades) < self.max_open
            has_funds = self.balance >= self._calc_trade_size()

            if can_trade and has_funds:
                self.log(f"   🔄 INVERTING: HTF says {original.upper()} → betting {flipped.upper()}")
                self._place_trade(sig, snapshot, flipped, original)
            elif not has_funds:
                self.log(f"   ⚠️ Signal but no funds (${self.balance:.2f})")

        self.traded_slugs.add(snapshot["slug"])

    def _print_summary(self):
        total = self.wins + self.losses
        wr = self.wins / total * 100 if total > 0 else 0
        print("\n" + "=" * 60)
        print("📊 Paper Trader v3.2 (Inverse HTF) — Summary")
        print("=" * 60)
        print(f"  Trades: {total} ({self.wins}W/{self.losses}L)")
        print(f"  Win Rate: {wr:.1f}%")
        print(f"  P&L: ${self.total_pnl:+.2f}")
        print(f"  Balance: ${self.balance:.2f}")


if __name__ == "__main__":
    trader = PaperTraderV32()
    trader.run()
