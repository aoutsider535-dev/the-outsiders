"""
Backtesting module for the Sports Arbitrage Scanner.

Phase 1: Forward-looking dataset from scanner logs.
Simulates buying every edge above a threshold and tracks PnL.

Historical Odds API data requires a paid plan, so we build our
dataset from scanner logs going forward.
"""

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from .config import DB_PATH, MIN_EDGE_PCT, BANKROLL, KELLY_FRACTION, MAX_BET_PCT
from .database import ArbDatabase

logger = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    """Summary of a backtest run."""
    edge_threshold: float
    total_bets: int
    wins: int
    losses: int
    pending: int
    win_rate: float
    total_pnl: float
    avg_edge: float
    max_drawdown: float
    sharpe_approx: float
    equity_curve: list[float]


def kelly_bet_size(
    edge: float,
    probability: float,
    bankroll: float,
    kelly_fraction: float = KELLY_FRACTION,
    max_pct: float = MAX_BET_PCT,
) -> float:
    """
    Calculate bet size using fractional Kelly criterion.

    Kelly formula: f* = (bp - q) / b
    where b = odds - 1, p = probability, q = 1 - p

    For Polymarket (binary): b = (1/price) - 1

    Args:
        edge: Detected edge (sharp_prob - pm_price).
        probability: Sharp no-vig probability (our estimate of true prob).
        bankroll: Current bankroll.
        kelly_fraction: Fraction of full Kelly to use (0.25 = quarter Kelly).
        max_pct: Maximum bet as fraction of bankroll.

    Returns:
        Dollar amount to bet.
    """
    if edge <= 0 or probability <= 0 or probability >= 1:
        return 0.0

    # Binary market: payout is 1/price - 1 per dollar risked
    pm_price = probability - edge  # Reconstruct PM price
    if pm_price <= 0 or pm_price >= 1:
        return 0.0

    b = (1 / pm_price) - 1  # Decimal odds minus 1
    p = probability
    q = 1 - p

    full_kelly = (b * p - q) / b
    if full_kelly <= 0:
        return 0.0

    fractional = full_kelly * kelly_fraction
    bet_pct = min(fractional, max_pct)

    return bankroll * bet_pct


def run_backtest(
    edge_threshold: float = MIN_EDGE_PCT,
    initial_bankroll: float = BANKROLL,
) -> BacktestResult:
    """
    Backtest using logged opportunities.

    For opportunities with outcomes: calculate actual PnL.
    For opportunities without outcomes: mark as pending.

    Uses fractional Kelly sizing.

    Args:
        edge_threshold: Minimum edge to simulate betting on.
        initial_bankroll: Starting bankroll.

    Returns:
        BacktestResult with stats and equity curve.
    """
    db = ArbDatabase()
    opps = db.get_recent_opportunities(limit=10000, min_edge=edge_threshold)

    if not opps:
        logger.warning("No opportunities found for backtest")
        return BacktestResult(
            edge_threshold=edge_threshold,
            total_bets=0, wins=0, losses=0, pending=0,
            win_rate=0.0, total_pnl=0.0, avg_edge=0.0,
            max_drawdown=0.0, sharpe_approx=0.0,
            equity_curve=[initial_bankroll],
        )

    df = pd.DataFrame(opps)

    bankroll = initial_bankroll
    equity_curve = [bankroll]
    wins = 0
    losses = 0
    pending = 0
    returns: list[float] = []

    for _, row in df.iterrows():
        edge = row["edge_pct"]
        sharp_prob = row["sharp_no_vig_prob"]
        pm_price = row["pm_price"]

        # Calculate bet size
        bet = kelly_bet_size(
            edge=edge,
            probability=sharp_prob,
            bankroll=bankroll,
        )

        if bet <= 0:
            continue

        # Check if we have an outcome
        # Phase 1: most will be pending since we haven't tracked resolutions yet
        status = row.get("status", "detected")

        if status in ("won", "resolved_win"):
            payout = bet * (1 / pm_price - 1)
            bankroll += payout
            wins += 1
            returns.append(payout / bet if bet > 0 else 0)
        elif status in ("lost", "resolved_loss"):
            bankroll -= bet
            losses += 1
            returns.append(-1.0)
        else:
            pending += 1
            # For pending: simulate using expected value
            expected_pnl = bet * (sharp_prob * (1 / pm_price - 1) - (1 - sharp_prob))
            bankroll += expected_pnl
            returns.append(expected_pnl / bet if bet > 0 else 0)

        equity_curve.append(bankroll)

    total_bets = wins + losses + pending
    win_rate = wins / (wins + losses) if (wins + losses) > 0 else 0.0
    total_pnl = bankroll - initial_bankroll
    avg_edge = df["edge_pct"].mean() if not df.empty else 0.0

    # Max drawdown
    peak = initial_bankroll
    max_dd = 0.0
    for eq in equity_curve:
        peak = max(peak, eq)
        dd = (peak - eq) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)

    # Approximate Sharpe (annualized assuming daily bets)
    import numpy as np
    if returns:
        mean_ret = np.mean(returns)
        std_ret = np.std(returns) if len(returns) > 1 else 1.0
        sharpe = (mean_ret / std_ret) * (365 ** 0.5) if std_ret > 0 else 0.0
    else:
        sharpe = 0.0

    return BacktestResult(
        edge_threshold=edge_threshold,
        total_bets=total_bets,
        wins=wins,
        losses=losses,
        pending=pending,
        win_rate=win_rate,
        total_pnl=total_pnl,
        avg_edge=avg_edge,
        max_drawdown=max_dd,
        sharpe_approx=sharpe,
        equity_curve=equity_curve,
    )


def print_backtest_report(result: BacktestResult) -> None:
    """Print a formatted backtest report to console."""
    print("\n" + "=" * 50)
    print("  BACKTEST REPORT")
    print("=" * 50)
    print(f"  Edge threshold:   {result.edge_threshold:.1%}")
    print(f"  Total bets:       {result.total_bets}")
    print(f"    Wins:           {result.wins}")
    print(f"    Losses:         {result.losses}")
    print(f"    Pending:        {result.pending}")
    print(f"  Win rate:         {result.win_rate:.1%}")
    print(f"  Avg edge:         {result.avg_edge:.1%}")
    print(f"  Total PnL:        ${result.total_pnl:,.2f}")
    print(f"  Max drawdown:     {result.max_drawdown:.1%}")
    print(f"  Sharpe (approx):  {result.sharpe_approx:.2f}")
    print(f"  Final equity:     ${result.equity_curve[-1]:,.2f}")
    print("=" * 50)

    if result.pending > 0:
        print(f"\n  ⚠️  {result.pending} bets are pending (simulated with EV)")
        print("  Track outcomes in Phase 2 for real PnL.\n")


def main() -> None:
    """CLI entry point for backtesting."""
    parser = argparse.ArgumentParser(
        description="Backtest sports arbitrage opportunities"
    )
    parser.add_argument(
        "--threshold", type=float, default=MIN_EDGE_PCT,
        help=f"Minimum edge threshold (default: {MIN_EDGE_PCT})",
    )
    parser.add_argument(
        "--bankroll", type=float, default=BANKROLL,
        help=f"Starting bankroll (default: ${BANKROLL})",
    )

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    result = run_backtest(
        edge_threshold=args.threshold,
        initial_bankroll=args.bankroll,
    )
    print_backtest_report(result)


if __name__ == "__main__":
    main()
