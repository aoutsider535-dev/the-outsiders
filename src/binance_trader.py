#!/usr/bin/env python3
"""
Binance TA Trader — Paper Mode
Trades BTC spot on Binance using technical analysis with TP/SL.
Unlike Polymarket binary bets, this uses continuous pricing with proper risk mgmt.

Strategy: EMA 9/21 crossover + RSI filter
- LONG: EMA9 crosses above EMA21, RSI < 70
- SHORT: EMA9 crosses below EMA21, RSI > 30
- TP/SL: configurable ratio (default 2:1)
- Exit: first to hit TP, SL, or timeout (20 candles)

Phase 1: Paper trading only (no API keys needed)
Phase 2: Live trading with Binance API (future)
"""

import time
import json
import sqlite3
import requests
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

# === CONFIG ===
SYMBOL = "BTCUSDT"
INTERVAL = "4h"          # Best backtest results on 4h
TRADE_SIZE = 100.0        # Paper USD per trade
TP_PCT = 0.5              # Take profit %
SL_PCT = 0.25             # Stop loss % (2:1 reward/risk)
EMA_FAST = 9
EMA_SLOW = 21
RSI_PERIOD = 14
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30
MAX_CANDLES_HOLD = 20     # Timeout: close after N candles
POLL_INTERVAL = 60        # Check every 60 seconds

PST = timezone(timedelta(hours=-7))
DB_PATH = Path(__file__).parent.parent / "data" / "binance_paper.db"
LOG_PATH = Path(__file__).parent.parent / "data" / "binance_trader.log"

# === LOGGING ===
logger = logging.getLogger("binance_trader")
logger.setLevel(logging.INFO)
fh = logging.FileHandler(LOG_PATH)
fh.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
logger.addHandler(fh)
sh = logging.StreamHandler()
sh.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
logger.addHandler(sh)


def log(msg):
    logger.info(msg)


# === DATABASE ===
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT,
        symbol TEXT,
        side TEXT,
        entry_price REAL,
        exit_price REAL,
        tp_price REAL,
        sl_price REAL,
        size_usd REAL,
        pnl REAL,
        exit_reason TEXT,
        candles_held INTEGER,
        ema_fast REAL,
        ema_slow REAL,
        rsi REAL,
        status TEXT DEFAULT 'open'
    )""")
    conn.commit()
    return conn


# === INDICATORS ===
def calc_ema(prices, period):
    if len(prices) < period:
        return None
    k = 2 / (period + 1)
    ema_val = sum(prices[:period]) / period
    for p in prices[period:]:
        ema_val = p * k + ema_val * (1 - k)
    return ema_val


def calc_ema_series(prices, period):
    if len(prices) < period:
        return []
    k = 2 / (period + 1)
    result = [sum(prices[:period]) / period]
    for p in prices[period:]:
        result.append(p * k + result[-1] * (1 - k))
    return result


def calc_rsi(prices, period=14):
    if len(prices) < period + 1:
        return None
    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains = [max(d, 0) for d in deltas]
    losses = [-min(d, 0) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


# === MARKET DATA ===
def get_candles(symbol=SYMBOL, interval=INTERVAL, limit=100):
    """Fetch candles from Binance. No API key needed for market data."""
    r = requests.get(
        "https://api.binance.com/api/v3/klines",
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=10,
    )
    r.raise_for_status()
    return [
        {
            "open_time": c[0],
            "open": float(c[1]),
            "high": float(c[2]),
            "low": float(c[3]),
            "close": float(c[4]),
            "volume": float(c[5]),
            "close_time": c[6],
        }
        for c in r.json()
    ]


def get_price(symbol=SYMBOL):
    """Get current spot price."""
    r = requests.get(
        f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}",
        timeout=5,
    )
    return float(r.json()["price"])


# === SIGNALS ===
def check_signal(candles):
    """Check for EMA crossover + RSI filter.
    Returns ('long', indicators), ('short', indicators), or (None, indicators)."""
    closes = [c["close"] for c in candles]

    ema_fast_series = calc_ema_series(closes, EMA_FAST)
    ema_slow_series = calc_ema_series(closes, EMA_SLOW)
    rsi = calc_rsi(closes, RSI_PERIOD)

    if len(ema_fast_series) < 2 or len(ema_slow_series) < 2 or rsi is None:
        return None, {}

    # Align: use last 2 values
    ef_now = ema_fast_series[-1]
    ef_prev = ema_fast_series[-2]
    es_now = ema_slow_series[-1]
    es_prev = ema_slow_series[-2]

    indicators = {"ema_fast": ef_now, "ema_slow": es_now, "rsi": rsi}

    # Long: EMA fast crosses above slow, RSI not overbought
    if ef_now > es_now and ef_prev <= es_prev and rsi < RSI_OVERBOUGHT:
        return "long", indicators

    # Short: EMA fast crosses below slow, RSI not oversold
    if ef_now < es_now and ef_prev >= es_prev and rsi > RSI_OVERSOLD:
        return "short", indicators

    return None, indicators


# === POSITION MANAGEMENT ===
class PaperTrader:
    def __init__(self):
        self.db = init_db()
        self.balance = 1000.0  # Starting paper balance
        self.position = None  # {side, entry, tp, sl, size, candles, trade_id}
        self._load_state()

    def _load_state(self):
        """Resume open position from DB."""
        row = self.db.execute(
            "SELECT id, side, entry_price, tp_price, sl_price, size_usd, candles_held "
            "FROM trades WHERE status='open' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row:
            self.position = {
                "trade_id": row[0],
                "side": row[1],
                "entry": row[2],
                "tp": row[3],
                "sl": row[4],
                "size": row[5],
                "candles": row[6],
            }
            log(f"📋 Resumed open {self.position['side']} @ ${self.position['entry']:,.2f}")

        # Load balance from last closed trade
        row = self.db.execute(
            "SELECT SUM(pnl) FROM trades WHERE status='closed'"
        ).fetchone()
        if row and row[0]:
            self.balance = 1000.0 + row[0]

    def open_position(self, side, price, indicators):
        if self.position:
            log(f"⚠️ Already in {self.position['side']} position")
            return

        if side == "long":
            tp = price * (1 + TP_PCT / 100)
            sl = price * (1 - SL_PCT / 100)
        else:
            tp = price * (1 - TP_PCT / 100)
            sl = price * (1 + SL_PCT / 100)

        cursor = self.db.execute(
            """INSERT INTO trades (timestamp, symbol, side, entry_price, tp_price, sl_price,
               size_usd, pnl, candles_held, ema_fast, ema_slow, rsi, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?, 'open')""",
            (
                datetime.now(PST).isoformat(),
                SYMBOL,
                side,
                price,
                tp,
                sl,
                TRADE_SIZE,
                indicators.get("ema_fast", 0),
                indicators.get("ema_slow", 0),
                indicators.get("rsi", 0),
            ),
        )
        self.db.commit()

        self.position = {
            "trade_id": cursor.lastrowid,
            "side": side,
            "entry": price,
            "tp": tp,
            "sl": sl,
            "size": TRADE_SIZE,
            "candles": 0,
        }

        log(
            f"{'🟢' if side == 'long' else '🔴'} {side.upper()} {SYMBOL} @ ${price:,.2f} | "
            f"TP ${tp:,.2f} ({TP_PCT}%) | SL ${sl:,.2f} ({SL_PCT}%) | "
            f"Size ${TRADE_SIZE}"
        )

    def check_exit(self, current_price, candle_high, candle_low):
        if not self.position:
            return

        pos = self.position
        pos["candles"] += 1
        reason = None
        exit_price = None

        if pos["side"] == "long":
            if candle_high >= pos["tp"]:
                reason = "TP"
                exit_price = pos["tp"]
            elif candle_low <= pos["sl"]:
                reason = "SL"
                exit_price = pos["sl"]
            elif pos["candles"] >= MAX_CANDLES_HOLD:
                reason = "TIMEOUT"
                exit_price = current_price
        else:  # short
            if candle_low <= pos["tp"]:
                reason = "TP"
                exit_price = pos["tp"]
            elif candle_high >= pos["sl"]:
                reason = "SL"
                exit_price = pos["sl"]
            elif pos["candles"] >= MAX_CANDLES_HOLD:
                reason = "TIMEOUT"
                exit_price = current_price

        if reason:
            self._close_position(exit_price, reason)

    def _close_position(self, exit_price, reason):
        pos = self.position
        if pos["side"] == "long":
            pnl_pct = (exit_price - pos["entry"]) / pos["entry"] * 100
        else:
            pnl_pct = (pos["entry"] - exit_price) / pos["entry"] * 100

        pnl_usd = pos["size"] * pnl_pct / 100
        self.balance += pnl_usd

        self.db.execute(
            """UPDATE trades SET exit_price=?, pnl=?, exit_reason=?, candles_held=?, status='closed'
               WHERE id=?""",
            (exit_price, pnl_usd, reason, pos["candles"], pos["trade_id"]),
        )
        self.db.commit()

        emoji = "✅" if pnl_usd > 0 else "❌"
        log(
            f"{emoji} CLOSE {pos['side'].upper()} @ ${exit_price:,.2f} ({reason}) | "
            f"PnL: ${pnl_usd:+.2f} ({pnl_pct:+.2f}%) | "
            f"Held {pos['candles']} candles | Bal: ${self.balance:,.2f}"
        )

        self.position = None

    def print_stats(self):
        rows = self.db.execute(
            "SELECT COUNT(*), SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END), SUM(pnl) "
            "FROM trades WHERE status='closed'"
        ).fetchone()
        total, wins, net_pnl = rows[0] or 0, rows[1] or 0, rows[2] or 0
        wr = wins / total * 100 if total > 0 else 0
        log(f"📊 {wins}W-{total - wins}L ({wr:.0f}% WR) | Net: ${net_pnl:+.2f} | Bal: ${self.balance:,.2f}")


# === MAIN LOOP ===
def run():
    log(f"📈 Binance TA Trader — PAPER MODE")
    log(f"Strategy: EMA {EMA_FAST}/{EMA_SLOW} + RSI {RSI_PERIOD} | {INTERVAL} candles")
    log(f"TP: {TP_PCT}% | SL: {SL_PCT}% | Size: ${TRADE_SIZE} | Timeout: {MAX_CANDLES_HOLD} candles")

    trader = PaperTrader()
    log(f"💰 Balance: ${trader.balance:,.2f}")

    last_candle_time = 0

    while True:
        try:
            candles = get_candles(limit=50)
            if not candles:
                time.sleep(POLL_INTERVAL)
                continue

            latest = candles[-1]
            current_price = latest["close"]

            # Check exits on current candle
            if trader.position:
                trader.check_exit(current_price, latest["high"], latest["low"])

            # Check for new signals on candle close
            if latest["close_time"] != last_candle_time:
                last_candle_time = latest["close_time"]

                if not trader.position:
                    signal, indicators = check_signal(candles[:-1])  # Use closed candles only
                    if signal:
                        trader.open_position(signal, current_price, indicators)

                # Stats every candle
                trader.print_stats()

            time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            log("🛑 Stopped")
            break
        except Exception as e:
            log(f"⚠️ Error: {e}")
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run()
