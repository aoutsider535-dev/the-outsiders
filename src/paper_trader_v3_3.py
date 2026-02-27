"""
🏞 The Outsiders — Paper Trader v3.3 (TRUE MIRROR of v3.1)

Watches v3.1's database for new trades and places the EXACT OPPOSITE.
When v3.1 bets UP, v3.3 bets DOWN on the same market. No independent signals.
This is a pure mirror test — same markets, same timing, opposite direction.
"""
import time
import json
import os
import signal as sig_module
import sqlite3
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.data.polymarket import check_market_resolution

PST = timezone(timedelta(hours=-8))
V31_DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "paper_v3_1.db")
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "paper_v3_3.db")

TAKER_FEE_PCT = 0.02
SLIPPAGE_PCT = 0.005
STARTING_BALANCE = 100.0


class PaperTraderV33:
    def __init__(self):
        self.balance = STARTING_BALANCE
        self.running = True
        self.open_trades = []
        self.mirrored_ids = set()  # v3.1 trade IDs we've already mirrored
        self.wins = 0
        self.losses = 0
        self.total_pnl = 0.0
        self.trade_count = 0

        self._init_db()
        self._load_mirrored_ids()
        self.balance = self._recover_balance()
        sig_module.signal(sig_module.SIGINT, self._shutdown)
        sig_module.signal(sig_module.SIGTERM, self._shutdown)

    def _shutdown(self, signum, frame):
        print("\n⏹️  Shutting down Paper Trader v3.3...")
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
                v31_trade_id INTEGER,
                timestamp INTEGER, market_id TEXT, strategy TEXT,
                direction TEXT, original_direction TEXT,
                entry_price REAL, exit_price REAL,
                quantity REAL, pnl REAL, pnl_pct REAL,
                edge_pct REAL, confidence REAL, signal_data TEXT,
                status TEXT DEFAULT 'open', exit_reason TEXT,
                created_at TEXT, closed_at TEXT,
                fees REAL DEFAULT 0, slippage REAL DEFAULT 0
            )
        """)
        conn.commit()
        conn.close()

    def _load_mirrored_ids(self):
        """Load already-mirrored v3.1 trade IDs to avoid duplicates."""
        try:
            conn = sqlite3.connect(DB_PATH)
            rows = conn.execute("SELECT v31_trade_id FROM trades WHERE v31_trade_id IS NOT NULL").fetchall()
            self.mirrored_ids = {r[0] for r in rows}
            conn.close()
            if self.mirrored_ids:
                self.log(f"📋 Loaded {len(self.mirrored_ids)} already-mirrored trade IDs")
        except:
            pass

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

    def _load_open_trades(self):
        """Reload open trades from DB on startup."""
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM trades WHERE status='open'").fetchall()
            conn.close()
            for r in rows:
                self.open_trades.append({
                    "id": r["id"],
                    "v31_trade_id": r["v31_trade_id"],
                    "direction": r["direction"],
                    "original_direction": r["original_direction"],
                    "entry_price": r["entry_price"],
                    "quantity": r["quantity"],
                    "fees": r["fees"] or 0,
                    "market_slug": r["market_id"],
                    "timestamp": r["timestamp"],
                })
            if self.open_trades:
                self.log(f"📋 Recovered {len(self.open_trades)} open trades")
        except:
            pass

    def _get_new_v31_trades(self):
        """Poll v3.1's DB for new trades we haven't mirrored yet.
        Only grabs trades opened in the last 10 minutes to avoid mirroring history."""
        try:
            cutoff = int(time.time()) - 600  # last 10 minutes
            conn = sqlite3.connect(V31_DB)
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT * FROM trades 
                WHERE strategy LIKE '%htf_trend%'
                AND timestamp > ?
                ORDER BY id
            """, (cutoff,)).fetchall()
            conn.close()
            return [dict(r) for r in rows if r["id"] not in self.mirrored_ids]
        except Exception as e:
            self.log(f"⚠️ Error reading v3.1 DB: {e}")
            return []

    def _mirror_trade(self, v31_trade):
        """Place the opposite trade of a v3.1 trade."""
        original_dir = v31_trade["direction"]
        flipped_dir = "down" if original_dir == "up" else "up"

        # Use the same entry price structure but for opposite direction
        # v3.1 bought at entry_price for their direction; we buy at ~(1-entry_price) for opposite
        # But in binary markets both sides are independently priced, so approximate
        entry_price = 1.0 - v31_trade["entry_price"] if v31_trade["entry_price"] else 0.5
        entry_price = max(0.01, min(0.99, entry_price))

        slippage = entry_price * SLIPPAGE_PCT
        fill = min(entry_price + slippage, 0.99)

        # Fixed $5 sizing (don't inherit v3.1's shrinking Kelly)
        trade_size = 5.0
        if self.balance < trade_size:
            self.log(f"   ⚠️ Mirror: No funds (${self.balance:.2f} < ${trade_size:.2f})")
            return

        qty = trade_size / fill if fill > 0 else 0
        cost = fill * qty
        fee = cost * TAKER_FEE_PCT

        conn = sqlite3.connect(DB_PATH)
        cur = conn.execute(
            """INSERT INTO trades (v31_trade_id, timestamp, market_id, strategy, direction, original_direction,
               entry_price, quantity, edge_pct, confidence, signal_data, status, created_at, fees, slippage)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)""",
            (v31_trade["id"], v31_trade["timestamp"], v31_trade["market_id"],
             "btc_5min_mirror_inv_V33", flipped_dir, original_dir,
             fill, qty, v31_trade.get("edge_pct", 0), v31_trade.get("confidence", 0),
             v31_trade.get("signal_data", "{}"),
             datetime.now(timezone.utc).isoformat(), fee, slippage)
        )
        tid = cur.lastrowid
        conn.commit()
        conn.close()

        self.balance -= (cost + fee)
        self.open_trades.append({
            "id": tid,
            "v31_trade_id": v31_trade["id"],
            "direction": flipped_dir,
            "original_direction": original_dir,
            "entry_price": fill,
            "quantity": qty,
            "fees": fee,
            "market_slug": v31_trade["market_id"],
            "timestamp": v31_trade["timestamp"],
        })
        self.mirrored_ids.add(v31_trade["id"])

        emoji = "🟢" if flipped_dir == "up" else "🔴"
        self.log(f"   {emoji} 🪞 MIRROR: v3.1 bet {original_dir.upper()} → we bet {flipped_dir.upper()} "
                 f"@ ${fill:.3f} | Qty: {qty:.1f} | ${cost + fee:.2f} | {v31_trade['market_id'][:40]}")

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
                self.log(f"   ⏰ Mirror stuck {age_min:.0f}min | ${pnl:+.2f}")
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

            orig = trade.get("original_direction", "?").upper()
            emoji = "✅" if won else "❌"
            self.log(f"   {emoji} 🪞 MIRROR: v3.1 said {orig} → we bet {trade['direction'].upper()} → "
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

    def run(self):
        print("🏞 The Outsiders — Paper Trader v3.3 (TRUE MIRROR of v3.1)")
        print("=" * 60)
        print("🪞 Watches v3.1 DB → takes EXACT OPPOSITE of every HTF Trend trade")
        print(f"💰 Starting balance: ${self.balance:.2f}")
        print(f"📊 Fees: {TAKER_FEE_PCT*100}% + {SLIPPAGE_PCT*100}% slippage")
        print(f"📂 Watching: {V31_DB}")
        print("=" * 60 + "\n")

        self._load_open_trades()

        while self.running:
            try:
                # Check resolutions first
                self._check_open_trades()

                # Poll v3.1 for new trades to mirror
                new_trades = self._get_new_v31_trades()
                for t in new_trades:
                    self._mirror_trade(t)

                time.sleep(15)  # Poll faster than v3.1's 30s to catch trades quickly
            except KeyboardInterrupt:
                break
            except Exception as e:
                self.log(f"❌ Error: {e}")
                import traceback; traceback.print_exc()
                time.sleep(60)

        self._print_summary()

    def _print_summary(self):
        total = self.wins + self.losses
        wr = self.wins / total * 100 if total > 0 else 0
        print("\n" + "=" * 60)
        print("📊 Paper Trader v3.3 (True Mirror) — Summary")
        print("=" * 60)
        print(f"  Trades: {total} ({self.wins}W/{self.losses}L)")
        print(f"  Win Rate: {wr:.1f}%")
        print(f"  P&L: ${self.total_pnl:+.2f}")
        print(f"  Balance: ${self.balance:.2f}")


if __name__ == "__main__":
    trader = PaperTraderV33()
    trader.run()
