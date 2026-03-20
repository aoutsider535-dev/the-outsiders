#!/usr/bin/env python3
"""
═══════════════════════════════════════════════════════════════════════════════
🎯 THE OUTSIDERS — Late Drift Sniper Backtester
═══════════════════════════════════════════════════════════════════════════════

COMPLETE standalone backtester that replays the exact v3 drift sniper strategy
against historical data. Reuses all strategy functions from sniper_v3.py.

WHAT IT DOES:
  For every historical BTC 5-minute window:
    1. Reconstruct BTC open price and spot at T-45s from Binance 1m candles
    2. Calculate drift (same formula as live)
    3. Apply vol-adjusted threshold, streak filter, oracle lag detection
    4. Check all entry conditions (drift, price, edge, depth)
    5. Simulate FOK fill at estimated/real book prices
    6. Resolve against actual BTC outcome
    7. Track equity curve, stats, filter effectiveness

DATA SOURCES:
  - Binance 1m klines (always available, free)
  - poly_data CSV (optional, for real Polymarket fill prices)
  - PolyBackTest API (optional, for exact orderbook snapshots)

USAGE:
  python src/drift_backtester.py --days 30
  python src/drift_backtester.py --start-date 2026-02-01 --end-date 2026-03-01
  python src/drift_backtester.py --days 7 --no-streak --no-vol-adjust
  python src/drift_backtester.py --days 30 --download-poly-data

OUTPUT:
  - Console summary with full stats
  - data/backtest_results.md (markdown report)
  - data/backtest_equity_curve.png (matplotlib chart)
  - data/backtest_trades.csv (every signal/trade detail)

═══════════════════════════════════════════════════════════════════════════════
"""

import os
import sys
import json
import time
import logging
import argparse
import statistics
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Tuple

import pandas as pd
import numpy as np

# ─── Path setup ──────────────────────────────────────────────────────────────
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

# Import strategy functions from the live sniper
from src.sniper_v3 import (
    LateDriftSniperV3,
    BASE_MIN_DRIFT_PCT,
    MAX_BUY_PRICE,
    MIN_EDGE,
    MIN_DEPTH_SHARES,
    USE_STREAK_FILTER,
    USE_VOL_ADJUSTED_DRIFT,
    USE_ORACLE_LAG,
    STREAK_THRESHOLD,
    LAG_EDGE_BONUS,
    RISK_PER_TRADE,
    MIN_TRADE_SIZE,
    MAX_TRADE_SIZE,
    DEFAULT_TRADE_SIZE,
    MAX_BANKROLL_RISK,
    VOL_LOOKBACK,
    VOL_EMA_DECAY,
    ENTRY_WINDOW_START,
    ENTRY_WINDOW_END,
)

from src.backtest_data_utils import (
    fetch_binance_klines,
    get_btc_candle_open,
    get_btc_candle_close,
    get_btc_price_at,
    estimate_pm_book_price,
    load_btc_5m_fills,
    download_poly_data,
    get_polybacktest_client,
    generate_windows,
    determine_outcome,
    DATA_DIR,
    CACHE_DIR,
)

# ─── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("drift_sniper.backtester")

PST = timezone(timedelta(hours=-7))

# Output paths
RESULTS_MD = os.path.join(DATA_DIR, "backtest_results.md")
EQUITY_PNG = os.path.join(DATA_DIR, "backtest_equity_curve.png")
TRADES_CSV = os.path.join(DATA_DIR, "backtest_trades.csv")


# ═══════════════════════════════════════════════════════════════════════════════
# BACKTEST ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

class BacktestEngine:
    """
    Replays the drift sniper strategy against historical data.

    Creates a LateDriftSniperV3 instance in 'shadow' mode and uses its
    strategy methods directly. This guarantees exact parity with live.

    State management:
      - Volatility buffer: updated every window (same as live)
      - Streak history: updated with each resolution (same as live)
      - Balance: tracked for Kelly-lite sizing (starts at $1000)
    """

    def __init__(
        self,
        start_ts: int,
        end_ts: int,
        initial_balance: float = 1000.0,
        trade_size: float = DEFAULT_TRADE_SIZE,
        use_streak: bool = USE_STREAK_FILTER,
        use_vol_adjust: bool = USE_VOL_ADJUSTED_DRIFT,
        use_oracle_lag: bool = USE_ORACLE_LAG,
        use_poly_data: bool = False,
        use_polybacktest: bool = False,
        verbose: bool = False,
    ):
        self.start_ts = start_ts
        self.end_ts = end_ts
        self.initial_balance = initial_balance
        self.trade_size = trade_size

        # Filter toggles
        self.use_streak = use_streak
        self.use_vol_adjust = use_vol_adjust
        self.use_oracle_lag = use_oracle_lag
        self.use_poly_data = use_poly_data
        self.use_polybacktest = use_polybacktest
        self.verbose = verbose

        # Strategy engine (reuses all v3 logic)
        self.strategy = LateDriftSniperV3(mode="shadow", trade_size=trade_size)
        self.strategy.balance = initial_balance

        # Data
        self.candles_df: Optional[pd.DataFrame] = None
        self.poly_fills_df: Optional[pd.DataFrame] = None
        self.pbt_client = None  # PolyBackTest client

        # Results storage
        self.trades: List[dict] = []
        self.all_signals: List[dict] = []  # Every window evaluation
        self.equity_curve: List[Tuple[int, float]] = []

    def load_data(self):
        """
        Load all required historical data.

        1. Binance 1m candles (always)
        2. poly_data fills (if --use-poly-data)
        3. PolyBackTest client (if API key configured)
        """
        log.info("═" * 60)
        log.info("📦 LOADING HISTORICAL DATA")
        log.info("═" * 60)

        # 1. Binance 1-minute candles
        log.info("📊 Fetching Binance 1m candles...")
        self.candles_df = fetch_binance_klines(
            start_ts=self.start_ts - 3600,  # 1hr buffer for vol calc
            end_ts=self.end_ts + 300,        # 5min buffer for last window
            interval="1m",
        )
        if self.candles_df.empty:
            log.error("❌ No Binance data available. Cannot backtest.")
            sys.exit(1)

        log.info(f"   {len(self.candles_df):,} candles loaded")

        # 2. poly_data (optional)
        if self.use_poly_data:
            log.info("📊 Loading poly_data BTC 5m fills...")
            self.poly_fills_df = load_btc_5m_fills(self.start_ts, self.end_ts)
            if self.poly_fills_df is not None:
                log.info(f"   {len(self.poly_fills_df):,} fills loaded")
            else:
                log.warning("   ⚠️ poly_data not available, using estimated book prices")

        # 3. PolyBackTest (optional)
        if self.use_polybacktest:
            self.pbt_client = get_polybacktest_client()
            if self.pbt_client:
                log.info("   ✅ PolyBackTest API connected")
            else:
                log.warning("   ⚠️ No POLYBACKTEST_API_KEY in .env")

    def _get_book_price(
        self, window_ts: int, favored_side: str, drift_pct: float
    ) -> Tuple[Optional[float], float]:
        """
        Get Polymarket book price for the favored side.

        Priority:
          1. PolyBackTest API snapshot (if available)
          2. poly_data fill prices near window end (if loaded)
          3. Drift-based estimation (always available)

        Returns: (best_ask_price, estimated_depth)
        """
        # Tier 3: PolyBackTest API
        if self.pbt_client:
            try:
                # Snapshot at T-45s
                snapshot_ts = window_ts + 255
                snap = self.pbt_client.get_snapshot_at(
                    f"btc-updown-5m-{window_ts}", snapshot_ts
                )
                if snap and "asks" in snap:
                    asks = snap["asks"]
                    if asks:
                        best_ask = float(asks[0][0])
                        depth = sum(float(a[1]) for a in asks if float(a[0]) <= MAX_BUY_PRICE)
                        return best_ask, depth
            except Exception as e:
                log.debug(f"PolyBackTest error: {e}")

        # Tier 2: poly_data fills
        if self.poly_fills_df is not None and not self.poly_fills_df.empty:
            # Get fills near end of this window (last 60 seconds)
            window_end = window_ts + 300
            mask = (
                (self.poly_fills_df["window_ts"] == window_ts) &
                (self.poly_fills_df["timestamp"] >= window_end - 60)
            )
            window_fills = self.poly_fills_df[mask]

            if not window_fills.empty and "price" in window_fills.columns:
                # Use median fill price as best ask estimate
                prices = window_fills["price"].dropna()
                if not prices.empty:
                    # For favored side, we want the ask price
                    # If drift is up, favored = UP token, price should be > 0.50
                    # Filter to relevant side
                    relevant = prices[prices > 0.40] if favored_side == "up" else prices[prices > 0.40]
                    if not relevant.empty:
                        ask_est = relevant.median()
                        depth = float(window_fills["size"].sum()) if "size" in window_fills.columns else 100.0
                        return float(ask_est), depth

        # Tier 1: Drift-based estimation (always available)
        return estimate_pm_book_price(drift_pct)

    def _evaluate_window_backtest(
        self, window_ts: int, candles_df: pd.DataFrame
    ) -> Optional[dict]:
        """
        Evaluate a single 5-minute window for the backtest.

        Replicates the exact logic from LateDriftSniperV3._evaluate_window()
        but uses historical data instead of live feeds.

        Simulates evaluation at T-45s (midpoint of entry window).

        Returns signal dict or None.
        """
        eval_time = window_ts + 255  # T-45s before window close

        # ── Step 1: BTC open price ──
        btc_open = get_btc_candle_open(candles_df, window_ts)
        if btc_open is None:
            return None

        # ── Step 2: BTC spot at T-45s ──
        # Use the 1m candle close at minute 4 (closest to T-45s)
        btc_spot = get_btc_price_at(candles_df, eval_time)
        if btc_spot is None:
            return None

        # ── Step 3: Update volatility (reuse strategy method) ──
        self.strategy._update_vol(btc_spot)
        self.strategy._update_avg_vol()

        # ── Step 4: Calculate drift ──
        drift_pct = (btc_spot - btc_open) / btc_open
        abs_drift = abs(drift_pct)

        # ── Step 5: Vol-adjusted minimum drift ──
        if self.use_vol_adjust:
            vol_min, vol_factor = self.strategy._get_vol_adjusted_min_drift()
        else:
            vol_min = BASE_MIN_DRIFT_PCT
            vol_factor = None

        current_vol = self.strategy._calc_current_vol()

        # ── Step 6: Determine favored side ──
        if drift_pct > 0:
            favored = "up"
        elif drift_pct < 0:
            favored = "down"
        else:
            favored = None

        # ── Step 7: Get book prices ──
        if favored:
            favored_ask, favored_depth = self._get_book_price(
                window_ts, favored, drift_pct
            )
        else:
            favored_ask = None
            favored_depth = 0

        # ── Step 8: Continuation probability (reuse strategy method) ──
        time_left = 45.0  # Simulated at T-45s
        model_prob = self.strategy._estimate_continuation_prob(drift_pct, time_left)

        # ── Step 9: Streak filter (reuse strategy method) ──
        streak_count, streak_dir = self.strategy._get_streak()
        streak_reversal = False

        if (self.use_streak and streak_count >= STREAK_THRESHOLD
                and streak_dir is not None and favored == streak_dir):
            model_prob = 1.0 - model_prob
            favored = "down" if favored == "up" else "up"
            favored_ask, favored_depth = self._get_book_price(
                window_ts, favored, drift_pct
            )
            streak_reversal = True

        # ── Step 10: Oracle lag (approximation for backtest) ──
        # In backtest, we approximate oracle lag by checking if the drift
        # direction changed rapidly (1m candle reversal). This is a rough
        # proxy since we don't have multi-exchange data historically.
        oracle_lag = False
        lag_bonus = 0.0
        if self.use_oracle_lag:
            # Check if the last two 1m candles moved in opposite directions
            # (suggests rapid movement that oracle may lag behind)
            prev_close = get_btc_price_at(candles_df, eval_time - 60)
            if prev_close and btc_spot:
                recent_move = abs(btc_spot - prev_close) / prev_close
                if recent_move > 0.001:  # > 0.1% in 1 minute = fast move
                    oracle_lag = True
                    lag_bonus = LAG_EDGE_BONUS
                    model_prob = min(model_prob + lag_bonus, 0.98)

        # ── Step 11: Calculate edge ──
        implied_prob = favored_ask if favored_ask else 0.99
        edge = model_prob - implied_prob

        # Build signal record
        signal = {
            "window_ts": window_ts,
            "timestamp": datetime.utcfromtimestamp(eval_time).isoformat(),
            "btc_open": btc_open,
            "btc_spot": btc_spot,
            "drift_pct": drift_pct,
            "abs_drift": abs_drift,
            "vol_min": vol_min,
            "vol_factor": vol_factor,
            "current_vol": current_vol,
            "favored": favored,
            "favored_ask": favored_ask,
            "favored_depth": favored_depth,
            "model_prob": model_prob,
            "implied_prob": implied_prob,
            "edge": edge,
            "streak_count": streak_count,
            "streak_dir": streak_dir,
            "streak_reversal": streak_reversal,
            "oracle_lag": oracle_lag,
            "lag_bonus": lag_bonus,
            "signal_fired": False,
            "trade_taken": False,
            "reject_reason": None,
            "buy_price": None,
            "shares": 0,
            "cost": 0,
            "outcome": None,
            "won": None,
            "pnl": 0,
            "time_left": time_left,
        }

        # ══ CHECK ALL ENTRY RULES ══

        # Rule 1: Must have a direction
        if favored is None:
            signal["reject_reason"] = "no drift (exactly flat)"
            return signal

        # Rule 2: Drift ≥ vol-adjusted minimum
        if abs_drift < vol_min:
            signal["reject_reason"] = f"drift {abs_drift*100:.3f}% < min {vol_min*100:.3f}%"
            return signal

        # Rule 3: Book price ≤ MAX_BUY_PRICE
        if favored_ask is None or favored_ask > MAX_BUY_PRICE:
            signal["reject_reason"] = f"ask ${favored_ask} > max ${MAX_BUY_PRICE}"
            return signal

        # Rule 4: Edge ≥ MIN_EDGE
        if edge < MIN_EDGE:
            signal["reject_reason"] = f"edge {edge*100:.1f}% < min {MIN_EDGE*100:.0f}%"
            return signal

        # Rule 5: Sufficient depth
        if favored_depth < MIN_DEPTH_SHARES:
            signal["reject_reason"] = f"depth {favored_depth:.0f} < min {MIN_DEPTH_SHARES:.0f}"
            return signal

        # ALL RULES PASSED
        signal["signal_fired"] = True

        # ── Calculate trade size (reuse strategy method) ──
        trade_size = self.strategy._calc_trade_size(edge, streak_reversal)
        trade_size = min(trade_size, self.trade_size)  # Cap at configured size

        # ── Simulate fill ──
        # Build simplified ask book for fill simulation
        asks = [(favored_ask, favored_depth)]
        fill_price, fill_shares = self.strategy._simulate_fill(asks, trade_size)

        if fill_shares < MIN_TRADE_SIZE / MAX_BUY_PRICE:
            signal["reject_reason"] = f"fill too small ({fill_shares:.1f} shares)"
            return signal

        signal["trade_taken"] = True
        signal["buy_price"] = fill_price
        signal["shares"] = fill_shares
        signal["cost"] = fill_price * fill_shares

        return signal

    def run(self) -> dict:
        """
        Run the full backtest.

        Processes every 5-minute window chronologically, maintaining
        vol buffer and streak history exactly like the live bot.

        Returns a summary dict with all stats.
        """
        self.load_data()

        windows = generate_windows(self.start_ts, self.end_ts)
        total_windows = len(windows)

        log.info(f"\n{'═'*60}")
        log.info(f"🎯 BACKTEST: {total_windows:,} windows")
        log.info(f"   Period: {datetime.utcfromtimestamp(self.start_ts).strftime('%Y-%m-%d')} → "
                 f"{datetime.utcfromtimestamp(self.end_ts).strftime('%Y-%m-%d')}")
        log.info(f"   Filters: streak={'ON' if self.use_streak else 'OFF'} "
                 f"vol_adjust={'ON' if self.use_vol_adjust else 'OFF'} "
                 f"oracle_lag={'ON' if self.use_oracle_lag else 'OFF'}")
        log.info(f"   Balance: ${self.initial_balance:.2f} | Size: ${self.trade_size}")
        log.info(f"   Data: Binance 1m + "
                 f"{'poly_data' if self.use_poly_data else 'estimated'} PM prices"
                 f"{' + PolyBackTest' if self.pbt_client else ''}")
        log.info(f"{'═'*60}\n")

        balance = self.initial_balance
        self.equity_curve = [(self.start_ts, balance)]

        # Progress tracking
        start_time = time.time()
        last_progress = 0

        for i, window_ts in enumerate(windows):
            # Progress every 10%
            pct = (i + 1) / total_windows * 100
            if pct - last_progress >= 10:
                elapsed = time.time() - start_time
                rate = (i + 1) / elapsed if elapsed > 0 else 0
                eta = (total_windows - i - 1) / rate if rate > 0 else 0
                log.info(
                    f"  ⏳ {pct:.0f}% ({i+1:,}/{total_windows:,}) | "
                    f"{rate:.0f} windows/s | ETA: {eta:.0f}s"
                )
                last_progress = pct

            # Evaluate this window
            signal = self._evaluate_window_backtest(window_ts, self.candles_df)

            if signal is None:
                continue  # No data for this window

            # Determine actual outcome
            outcome = determine_outcome(self.candles_df, window_ts)
            signal["outcome"] = outcome

            # If trade was taken, resolve it
            if signal["trade_taken"] and outcome is not None:
                won = outcome == signal["favored"]
                signal["won"] = won

                if won:
                    pnl = signal["shares"] * 1.0 - signal["cost"]
                    # Apply 2% Polymarket fee on winnings
                    pnl *= 0.98
                else:
                    pnl = -signal["cost"]

                signal["pnl"] = pnl
                balance += pnl
                self.strategy.balance = balance

                # Update streak (same as live)
                self.strategy._record_outcome(outcome)

                self.trades.append(signal)
                self.equity_curve.append((window_ts, balance))

                if self.verbose:
                    side = signal['favored'].upper()
                    result = "✅" if won else "❌"
                    log.info(
                        f"  {result} {side} | drift {signal['drift_pct']*100:+.3f}% | "
                        f"edge {signal['edge']*100:.1f}% | "
                        f"P&L ${pnl:+.2f} | bal ${balance:.2f}"
                    )

            elif signal["trade_taken"] is False and outcome is not None:
                # No trade but record outcome for streak tracking
                self.strategy._record_outcome(outcome)

            self.all_signals.append(signal)

        elapsed = time.time() - start_time
        log.info(f"\n✅ Backtest complete in {elapsed:.1f}s ({total_windows/elapsed:.0f} windows/s)")

        # Generate all outputs
        summary = self._compute_stats(balance)
        self._print_summary(summary)
        self._write_results_md(summary)
        self._write_trades_csv()
        self._plot_equity_curve()

        return summary

    def _compute_stats(self, final_balance: float) -> dict:
        """Compute comprehensive backtest statistics."""
        if not self.trades:
            return {
                "total_trades": 0,
                "win_rate": 0,
                "total_pnl": 0,
                "roi": 0,
                "sharpe": 0,
                "max_drawdown": 0,
                "profit_factor": 0,
                "expectancy": 0,
            }

        df = pd.DataFrame(self.trades)
        resolved = df[df["won"].notna()]

        wins = resolved[resolved["won"] == True]
        losses = resolved[resolved["won"] == False]

        total_trades = len(resolved)
        win_count = len(wins)
        loss_count = len(losses)
        win_rate = win_count / total_trades * 100 if total_trades > 0 else 0

        total_pnl = resolved["pnl"].sum()
        gross_profit = wins["pnl"].sum() if not wins.empty else 0
        gross_loss = abs(losses["pnl"].sum()) if not losses.empty else 0

        avg_win = wins["pnl"].mean() if not wins.empty else 0
        avg_loss = losses["pnl"].mean() if not losses.empty else 0

        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
        expectancy = resolved["pnl"].mean() if total_trades > 0 else 0

        roi = (final_balance - self.initial_balance) / self.initial_balance * 100

        # Sharpe ratio (annualized, daily returns)
        if len(self.equity_curve) > 2:
            equity_vals = [e[1] for e in self.equity_curve]
            daily_returns = []
            # Group by day
            daily_pnls = {}
            for t in self.trades:
                day = t["window_ts"] // 86400
                if day not in daily_pnls:
                    daily_pnls[day] = 0
                daily_pnls[day] += t.get("pnl", 0)

            daily_rets = list(daily_pnls.values())
            if daily_rets and statistics.stdev(daily_rets) > 0:
                sharpe = (statistics.mean(daily_rets) / statistics.stdev(daily_rets)) * (365 ** 0.5)
            else:
                sharpe = 0
        else:
            sharpe = 0

        # Max drawdown
        peak = self.initial_balance
        max_dd = 0
        max_dd_pct = 0
        for ts, bal in self.equity_curve:
            if bal > peak:
                peak = bal
            dd = peak - bal
            dd_pct = dd / peak * 100 if peak > 0 else 0
            if dd_pct > max_dd_pct:
                max_dd_pct = dd_pct
                max_dd = dd

        # Max consecutive wins/losses
        max_win_streak = 0
        max_loss_streak = 0
        current_streak = 0
        last_result = None
        for _, row in resolved.iterrows():
            if row["won"] == last_result:
                current_streak += 1
            else:
                current_streak = 1
                last_result = row["won"]
            if row["won"]:
                max_win_streak = max(max_win_streak, current_streak)
            else:
                max_loss_streak = max(max_loss_streak, current_streak)

        # Filter breakdown
        all_signals_df = pd.DataFrame(self.all_signals)
        total_windows = len(all_signals_df)
        signals_fired = len(all_signals_df[all_signals_df["signal_fired"] == True]) if not all_signals_df.empty else 0

        # Rejection breakdown
        rejected = all_signals_df[all_signals_df["reject_reason"].notna()] if not all_signals_df.empty else pd.DataFrame()
        reject_counts = {}
        if not rejected.empty:
            for reason in rejected["reject_reason"]:
                if "drift" in str(reason).lower():
                    key = "Insufficient Drift"
                elif "ask" in str(reason).lower() or "price" in str(reason).lower():
                    key = "Price Too High"
                elif "edge" in str(reason).lower():
                    key = "Insufficient Edge"
                elif "depth" in str(reason).lower():
                    key = "Low Depth"
                else:
                    key = "Other"
                reject_counts[key] = reject_counts.get(key, 0) + 1

        # Performance by streak reversal
        reversals = resolved[resolved["streak_reversal"] == True] if "streak_reversal" in resolved.columns else pd.DataFrame()
        normal = resolved[resolved["streak_reversal"] == False] if "streak_reversal" in resolved.columns else resolved

        reversal_wr = reversals["won"].mean() * 100 if not reversals.empty else 0
        normal_wr = normal["won"].mean() * 100 if not normal.empty else 0

        # Performance by oracle lag
        lag_trades = resolved[resolved["oracle_lag"] == True] if "oracle_lag" in resolved.columns else pd.DataFrame()
        no_lag = resolved[resolved["oracle_lag"] == False] if "oracle_lag" in resolved.columns else resolved

        lag_wr = lag_trades["won"].mean() * 100 if not lag_trades.empty else 0
        no_lag_wr = no_lag["won"].mean() * 100 if not no_lag.empty else 0

        # Edge bucket analysis
        edge_buckets = {}
        if not resolved.empty and "edge" in resolved.columns:
            for _, row in resolved.iterrows():
                edge_pct = row["edge"] * 100
                if edge_pct < 7:
                    bucket = "0-7%"
                elif edge_pct < 10:
                    bucket = "7-10%"
                elif edge_pct < 15:
                    bucket = "10-15%"
                elif edge_pct < 20:
                    bucket = "15-20%"
                else:
                    bucket = "20%+"
                if bucket not in edge_buckets:
                    edge_buckets[bucket] = {"trades": 0, "wins": 0, "pnl": 0}
                edge_buckets[bucket]["trades"] += 1
                if row["won"]:
                    edge_buckets[bucket]["wins"] += 1
                edge_buckets[bucket]["pnl"] += row.get("pnl", 0)

        return {
            "total_windows": total_windows,
            "signals_fired": signals_fired,
            "total_trades": total_trades,
            "win_count": win_count,
            "loss_count": loss_count,
            "win_rate": win_rate,
            "total_pnl": total_pnl,
            "gross_profit": gross_profit,
            "gross_loss": gross_loss,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "profit_factor": profit_factor,
            "expectancy": expectancy,
            "roi": roi,
            "sharpe": sharpe,
            "max_drawdown": max_dd,
            "max_drawdown_pct": max_dd_pct,
            "max_win_streak": max_win_streak,
            "max_loss_streak": max_loss_streak,
            "final_balance": final_balance,
            "reject_counts": reject_counts,
            "signal_rate": signals_fired / total_windows * 100 if total_windows > 0 else 0,
            "reversal_trades": len(reversals),
            "reversal_wr": reversal_wr,
            "normal_wr": normal_wr,
            "lag_trades": len(lag_trades),
            "lag_wr": lag_wr,
            "no_lag_wr": no_lag_wr,
            "edge_buckets": edge_buckets,
        }

    def _print_summary(self, stats: dict):
        """Print detailed backtest summary to console."""
        log.info(f"\n{'═'*60}")
        log.info(f"  📊 BACKTEST RESULTS")
        log.info(f"{'═'*60}")
        log.info(f"  Period: {datetime.utcfromtimestamp(self.start_ts).strftime('%Y-%m-%d')} → "
                 f"{datetime.utcfromtimestamp(self.end_ts).strftime('%Y-%m-%d')}")
        log.info(f"  Windows: {stats['total_windows']:,} | Signals: {stats['signals_fired']} "
                 f"({stats['signal_rate']:.1f}%)")
        log.info(f"")
        log.info(f"  {'─'*56}")
        log.info(f"  PERFORMANCE")
        log.info(f"  {'─'*56}")
        log.info(f"  Trades:         {stats['total_trades']}")
        log.info(f"  Win Rate:       {stats['win_rate']:.1f}% ({stats['win_count']}W-{stats['loss_count']}L)")
        log.info(f"  Total P&L:      ${stats['total_pnl']:+.2f}")
        log.info(f"  ROI:            {stats['roi']:+.1f}%")
        log.info(f"  Profit Factor:  {stats['profit_factor']:.2f}")
        log.info(f"  Expectancy:     ${stats['expectancy']:+.2f}/trade")
        log.info(f"  Sharpe Ratio:   {stats['sharpe']:.2f}")
        log.info(f"  Max Drawdown:   ${stats['max_drawdown']:.2f} ({stats['max_drawdown_pct']:.1f}%)")
        log.info(f"  Avg Win:        ${stats['avg_win']:+.2f}")
        log.info(f"  Avg Loss:       ${stats['avg_loss']:+.2f}")
        log.info(f"  Max Win Streak: {stats['max_win_streak']}")
        log.info(f"  Max Loss Streak: {stats['max_loss_streak']}")
        log.info(f"  Final Balance:  ${stats['final_balance']:.2f}")

        if stats.get("reject_counts"):
            log.info(f"\n  {'─'*56}")
            log.info(f"  REJECTION BREAKDOWN")
            log.info(f"  {'─'*56}")
            for reason, count in sorted(stats["reject_counts"].items(), key=lambda x: -x[1]):
                log.info(f"  {reason:25s} {count:>6,}")

        if stats.get("edge_buckets"):
            log.info(f"\n  {'─'*56}")
            log.info(f"  EDGE BUCKET ANALYSIS")
            log.info(f"  {'─'*56}")
            log.info(f"  {'Bucket':>8}  {'Trades':>7}  {'WR':>7}  {'P&L':>10}")
            for bucket in ["0-7%", "7-10%", "10-15%", "15-20%", "20%+"]:
                if bucket in stats["edge_buckets"]:
                    b = stats["edge_buckets"][bucket]
                    wr = b["wins"] / b["trades"] * 100 if b["trades"] > 0 else 0
                    log.info(f"  {bucket:>8}  {b['trades']:>7}  {wr:>6.1f}%  ${b['pnl']:>+9.2f}")

        log.info(f"\n  {'─'*56}")
        log.info(f"  FILTER EFFECTIVENESS")
        log.info(f"  {'─'*56}")
        if self.use_streak:
            log.info(f"  Streak reversals: {stats['reversal_trades']} trades "
                     f"({stats['reversal_wr']:.1f}% WR vs {stats['normal_wr']:.1f}% normal)")
        else:
            log.info(f"  Streak filter: OFF")
        if self.use_oracle_lag:
            log.info(f"  Oracle lag trades: {stats['lag_trades']} "
                     f"({stats['lag_wr']:.1f}% WR vs {stats['no_lag_wr']:.1f}% no-lag)")
        else:
            log.info(f"  Oracle lag: OFF")

        log.info(f"{'═'*60}")

    def _write_results_md(self, stats: dict):
        """Write backtest results to markdown file."""
        os.makedirs(DATA_DIR, exist_ok=True)

        period = (
            f"{datetime.utcfromtimestamp(self.start_ts).strftime('%Y-%m-%d')} → "
            f"{datetime.utcfromtimestamp(self.end_ts).strftime('%Y-%m-%d')}"
        )

        md = f"""# 🎯 Drift Sniper v3 — Backtest Results

**Period:** {period}
**Generated:** {datetime.now(PST).strftime('%Y-%m-%d %H:%M PST')}

## Configuration
| Parameter | Value |
|-----------|-------|
| Initial Balance | ${self.initial_balance:.2f} |
| Trade Size | ${self.trade_size:.2f} |
| Streak Filter | {'ON' if self.use_streak else 'OFF'} |
| Vol Adjust | {'ON' if self.use_vol_adjust else 'OFF'} |
| Oracle Lag | {'ON' if self.use_oracle_lag else 'OFF'} |
| Data Source | Binance 1m + {'poly_data' if self.use_poly_data else 'estimated'} PM prices |

## Performance Summary
| Metric | Value |
|--------|-------|
| Windows Observed | {stats['total_windows']:,} |
| Signals Fired | {stats['signals_fired']} ({stats['signal_rate']:.1f}%) |
| Trades Taken | {stats['total_trades']} |
| **Win Rate** | **{stats['win_rate']:.1f}%** ({stats['win_count']}W-{stats['loss_count']}L) |
| **Total P&L** | **${stats['total_pnl']:+.2f}** |
| **ROI** | **{stats['roi']:+.1f}%** |
| Profit Factor | {stats['profit_factor']:.2f} |
| Expectancy | ${stats['expectancy']:+.2f}/trade |
| Sharpe Ratio | {stats['sharpe']:.2f} |
| Max Drawdown | ${stats['max_drawdown']:.2f} ({stats['max_drawdown_pct']:.1f}%) |
| Avg Win | ${stats['avg_win']:+.2f} |
| Avg Loss | ${stats['avg_loss']:+.2f} |
| Max Win Streak | {stats['max_win_streak']} |
| Max Loss Streak | {stats['max_loss_streak']} |
| Final Balance | ${stats['final_balance']:.2f} |

## Rejection Breakdown
| Reason | Count |
|--------|-------|
"""
        for reason, count in sorted(stats.get("reject_counts", {}).items(), key=lambda x: -x[1]):
            md += f"| {reason} | {count:,} |\n"

        md += """
## Edge Bucket Analysis
| Bucket | Trades | Win Rate | P&L |
|--------|--------|----------|-----|
"""
        for bucket in ["0-7%", "7-10%", "10-15%", "15-20%", "20%+"]:
            if bucket in stats.get("edge_buckets", {}):
                b = stats["edge_buckets"][bucket]
                wr = b["wins"] / b["trades"] * 100 if b["trades"] > 0 else 0
                md += f"| {bucket} | {b['trades']} | {wr:.1f}% | ${b['pnl']:+.2f} |\n"

        md += f"""
## Filter Effectiveness
| Filter | Trades | Win Rate | Comparison |
|--------|--------|----------|------------|
| Streak reversals | {stats['reversal_trades']} | {stats['reversal_wr']:.1f}% | vs {stats['normal_wr']:.1f}% normal |
| Oracle lag | {stats['lag_trades']} | {stats['lag_wr']:.1f}% | vs {stats['no_lag_wr']:.1f}% no-lag |

## Equity Curve
![Equity Curve](backtest_equity_curve.png)

## Trade Log
See `backtest_trades.csv` for full details.

---
*Generated by The Outsiders — Late Drift Sniper v3 Backtester*
"""
        with open(RESULTS_MD, "w") as f:
            f.write(md)
        log.info(f"📝 Results written to {RESULTS_MD}")

    def _write_trades_csv(self):
        """Write all signals/trades to CSV for analysis."""
        if not self.all_signals:
            return

        df = pd.DataFrame(self.all_signals)

        # Select and order columns
        cols = [
            "window_ts", "timestamp", "btc_open", "btc_spot",
            "drift_pct", "vol_factor", "vol_min",
            "favored", "favored_ask", "favored_depth",
            "model_prob", "implied_prob", "edge",
            "streak_count", "streak_dir", "streak_reversal",
            "oracle_lag", "lag_bonus",
            "signal_fired", "reject_reason", "trade_taken",
            "buy_price", "shares", "cost",
            "outcome", "won", "pnl",
        ]
        available = [c for c in cols if c in df.columns]
        df[available].to_csv(TRADES_CSV, index=False)
        log.info(f"📝 Trades CSV written to {TRADES_CSV} ({len(df):,} rows)")

    def _plot_equity_curve(self):
        """Generate equity curve chart using matplotlib."""
        if len(self.equity_curve) < 2:
            log.warning("⚠️ Not enough data for equity curve")
            return

        try:
            import matplotlib
            matplotlib.use("Agg")  # Non-interactive backend
            import matplotlib.pyplot as plt
            import matplotlib.dates as mdates

            timestamps = [datetime.utcfromtimestamp(t) for t, _ in self.equity_curve]
            balances = [b for _, b in self.equity_curve]

            fig, ax = plt.subplots(figsize=(14, 6))
            fig.patch.set_facecolor("#0a0a0f")
            ax.set_facecolor("#1a1a2e")

            # Equity line
            ax.plot(timestamps, balances, color="#e2b714", linewidth=1.5, label="Equity")

            # Shade wins (green) and losses (red) regions
            ax.fill_between(
                timestamps, self.initial_balance, balances,
                where=[b >= self.initial_balance for b in balances],
                alpha=0.15, color="#00d26a",
            )
            ax.fill_between(
                timestamps, self.initial_balance, balances,
                where=[b < self.initial_balance for b in balances],
                alpha=0.15, color="#ff4757",
            )

            # Baseline
            ax.axhline(
                y=self.initial_balance,
                color="#8892b0", linestyle="--", linewidth=0.8, alpha=0.5,
            )

            # Trade markers
            for trade in self.trades:
                ts = datetime.utcfromtimestamp(trade["window_ts"])
                if trade.get("won"):
                    ax.plot(ts, trade.get("pnl", 0) + self.initial_balance,
                            "^", color="#00d26a", markersize=4, alpha=0.6)

            ax.set_title(
                "🎯 Drift Sniper v3 — Backtest Equity Curve",
                color="#e2b714", fontsize=14, fontweight="bold",
            )
            ax.set_xlabel("Date", color="#8892b0")
            ax.set_ylabel("Balance ($)", color="#8892b0")
            ax.tick_params(colors="#8892b0")
            ax.spines["top"].set_visible(False)
            ax.spines["right"].set_visible(False)
            ax.spines["bottom"].set_color("#2d2d44")
            ax.spines["left"].set_color("#2d2d44")

            # Format x-axis dates
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
            fig.autofmt_xdate()

            # Stats annotation
            total_pnl = balances[-1] - self.initial_balance
            roi = total_pnl / self.initial_balance * 100
            win_count = sum(1 for t in self.trades if t.get("won"))
            total = len(self.trades)
            wr = win_count / total * 100 if total > 0 else 0

            stats_text = (
                f"P&L: ${total_pnl:+.2f} ({roi:+.1f}%)\n"
                f"Trades: {total} ({wr:.1f}% WR)\n"
                f"Final: ${balances[-1]:.2f}"
            )
            ax.text(
                0.02, 0.98, stats_text,
                transform=ax.transAxes, fontsize=10,
                verticalalignment="top",
                color="#e6f1ff",
                fontfamily="monospace",
                bbox=dict(boxstyle="round,pad=0.5", facecolor="#16213e",
                          edgecolor="#2d2d44", alpha=0.9),
            )

            plt.tight_layout()
            plt.savefig(EQUITY_PNG, dpi=150, facecolor=fig.get_facecolor())
            plt.close()
            log.info(f"📈 Equity curve saved to {EQUITY_PNG}")

        except ImportError:
            log.warning("⚠️ matplotlib not installed, skipping equity curve plot")
        except Exception as e:
            log.error(f"💥 Plot error: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# FILTER COMPARISON MODE
# ═══════════════════════════════════════════════════════════════════════════════

def run_filter_comparison(start_ts: int, end_ts: int, trade_size: float):
    """
    Run multiple backtests with different filter combinations to compare
    their effectiveness.

    Runs 5 configurations:
      1. All filters ON (baseline)
      2. No streak filter
      3. No vol adjustment
      4. No oracle lag
      5. All filters OFF (raw drift-only)

    Outputs a comparison table.
    """
    configs = [
        ("All Filters ON", True, True, True),
        ("No Streak", False, True, True),
        ("No Vol Adjust", True, False, True),
        ("No Oracle Lag", True, True, False),
        ("All Filters OFF", False, False, False),
    ]

    results = []

    for name, streak, vol, lag in configs:
        log.info(f"\n{'─'*40}")
        log.info(f"🔬 Running: {name}")
        log.info(f"{'─'*40}")

        engine = BacktestEngine(
            start_ts=start_ts,
            end_ts=end_ts,
            trade_size=trade_size,
            use_streak=streak,
            use_vol_adjust=vol,
            use_oracle_lag=lag,
        )
        summary = engine.run()
        summary["config_name"] = name
        results.append(summary)

    # Print comparison table
    log.info(f"\n{'═'*80}")
    log.info(f"  🔬 FILTER COMPARISON")
    log.info(f"{'═'*80}")
    log.info(
        f"  {'Config':25s} {'Trades':>7} {'WR':>7} {'P&L':>10} {'PF':>6} "
        f"{'Sharpe':>7} {'MaxDD':>8}"
    )
    log.info(f"  {'─'*75}")

    for r in results:
        log.info(
            f"  {r['config_name']:25s} "
            f"{r['total_trades']:>7} "
            f"{r['win_rate']:>6.1f}% "
            f"${r['total_pnl']:>+9.2f} "
            f"{r['profit_factor']:>5.2f} "
            f"{r['sharpe']:>6.2f} "
            f"${r['max_drawdown']:>7.2f}"
        )

    log.info(f"{'═'*80}")

    # Write comparison to markdown
    comp_md = os.path.join(DATA_DIR, "backtest_filter_comparison.md")
    with open(comp_md, "w") as f:
        f.write("# 🔬 Filter Comparison Results\n\n")
        f.write(f"**Period:** {datetime.utcfromtimestamp(start_ts).strftime('%Y-%m-%d')} → "
                f"{datetime.utcfromtimestamp(end_ts).strftime('%Y-%m-%d')}\n\n")
        f.write("| Config | Trades | Win Rate | P&L | Profit Factor | Sharpe | Max DD |\n")
        f.write("|--------|--------|----------|-----|---------------|--------|--------|\n")
        for r in results:
            f.write(
                f"| {r['config_name']} | {r['total_trades']} | {r['win_rate']:.1f}% | "
                f"${r['total_pnl']:+.2f} | {r['profit_factor']:.2f} | "
                f"{r['sharpe']:.2f} | ${r['max_drawdown']:.2f} |\n"
            )
    log.info(f"📝 Comparison written to {comp_md}")

    return results


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="🎯 Late Drift Sniper v3 — Backtester",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python src/drift_backtester.py --days 30
  python src/drift_backtester.py --start-date 2026-02-01 --end-date 2026-03-01
  python src/drift_backtester.py --days 7 --no-streak --no-vol-adjust
  python src/drift_backtester.py --days 30 --compare-filters
  python src/drift_backtester.py --days 30 --download-poly-data --use-poly-data
  python src/drift_backtester.py --days 30 --verbose
        """,
    )

    # Time range
    time_group = parser.add_argument_group("Time Range")
    time_group.add_argument("--days", type=int, default=None,
                            help="Number of days to backtest (from today)")
    time_group.add_argument("--start-date", type=str, default=None,
                            help="Start date (YYYY-MM-DD)")
    time_group.add_argument("--end-date", type=str, default=None,
                            help="End date (YYYY-MM-DD)")

    # Strategy config
    config_group = parser.add_argument_group("Strategy Config")
    config_group.add_argument("--size", type=float, default=DEFAULT_TRADE_SIZE,
                              help=f"Trade size in USD (default: ${DEFAULT_TRADE_SIZE})")
    config_group.add_argument("--balance", type=float, default=1000.0,
                              help="Initial balance (default: $1000)")
    config_group.add_argument("--no-streak", action="store_true",
                              help="Disable streak filter")
    config_group.add_argument("--no-vol-adjust", action="store_true",
                              help="Disable volatility-adjusted drift")
    config_group.add_argument("--no-oracle-lag", action="store_true",
                              help="Disable oracle lag detection")

    # Data sources
    data_group = parser.add_argument_group("Data Sources")
    data_group.add_argument("--download-poly-data", action="store_true",
                            help="Download poly_data CSV (one-time, ~1-2 GB)")
    data_group.add_argument("--use-poly-data", action="store_true",
                            help="Use poly_data for real Polymarket fill prices")
    data_group.add_argument("--use-polybacktest", action="store_true",
                            help="Use PolyBackTest API (requires API key in .env)")

    # Output
    output_group = parser.add_argument_group("Output")
    output_group.add_argument("--verbose", "-v", action="store_true",
                              help="Print every trade to console")
    output_group.add_argument("--compare-filters", action="store_true",
                              help="Run comparison across filter combinations")

    args = parser.parse_args()

    # Resolve time range
    if args.days:
        end_ts = int(time.time())
        start_ts = end_ts - (args.days * 86400)
    elif args.start_date and args.end_date:
        start_ts = int(datetime.strptime(args.start_date, "%Y-%m-%d").replace(
            tzinfo=timezone.utc).timestamp())
        end_ts = int(datetime.strptime(args.end_date, "%Y-%m-%d").replace(
            tzinfo=timezone.utc).timestamp())
    else:
        parser.error("Specify either --days or both --start-date and --end-date")
        return

    # Download poly_data if requested
    if args.download_poly_data:
        download_poly_data()

    # Run backtest
    if args.compare_filters:
        run_filter_comparison(start_ts, end_ts, args.size)
    else:
        engine = BacktestEngine(
            start_ts=start_ts,
            end_ts=end_ts,
            initial_balance=args.balance,
            trade_size=args.size,
            use_streak=not args.no_streak,
            use_vol_adjust=not args.no_vol_adjust,
            use_oracle_lag=not args.no_oracle_lag,
            use_poly_data=args.use_poly_data,
            use_polybacktest=args.use_polybacktest,
            verbose=args.verbose,
        )
        engine.run()


if __name__ == "__main__":
    main()
