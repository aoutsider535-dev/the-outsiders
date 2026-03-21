"""
Main scanner loop: polls both APIs, detects edges, logs everything.

Run modes:
  --scan     Continuous polling (default)
  --once     Single pass, then exit
  --backfill Not yet implemented (Phase 2)
"""

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from typing import Any

from .config import (
    POLL_INTERVAL_ODDS,
    POLL_INTERVAL_PM,
    MIN_EDGE_PCT,
    MIN_PM_VOLUME,
    MIN_PM_LIQUIDITY,
    MIN_MATCH_CONFIDENCE,
    MONITORED_SPORTS,
    LOG_LEVEL,
    ODDS_API_KEY,
)
from .database import ArbDatabase
from .edge_calculator import EdgeOpportunity, calculate_edges
from .matcher import match_events, MatchResult
from .odds_api import OddsAPIClient, OddsEvent
from .polymarket_api import PolymarketAPIClient, PMEvent

logger = logging.getLogger(__name__)


def setup_logging() -> None:
    """Configure logging for the scanner."""
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Quiet down noisy libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)


def print_opportunities_table(edges: list[EdgeOpportunity]) -> None:
    """Print a formatted table of current edges to console."""
    if not edges:
        print("\n  No edges above threshold.\n")
        return

    # Sort by edge descending
    edges_sorted = sorted(edges, key=lambda e: e.edge_pct, reverse=True)

    header = (
        f"{'Sport':<12} {'Game':<35} {'Type':<6} {'Line':>6} "
        f"{'PM Side':<18} {'PM Price':>8} {'Sharp':>8} {'Edge':>7} "
        f"{'Net Edge':>9} {'Liq ($)':>10} {'Conf':>5}"
    )
    print("\n" + "=" * len(header))
    print("  LIVE EDGE OPPORTUNITIES  " + datetime.now(timezone.utc).strftime("%H:%M:%S UTC"))
    print("=" * len(header))
    print(header)
    print("-" * len(header))

    for e in edges_sorted:
        # Color coding via ANSI (green ≥5%, yellow 4-5%, red <4%)
        if e.edge_pct >= 0.05:
            color = "\033[92m"  # Green
        elif e.edge_pct >= 0.04:
            color = "\033[93m"  # Yellow
        else:
            color = "\033[91m"  # Red
        reset = "\033[0m"

        line_str = f"{e.line:+.1f}" if e.line is not None else "—"
        game_short = e.game[:33] + ".." if len(e.game) > 35 else e.game

        print(
            f"{e.sport:<12} {game_short:<35} {e.market_type:<6} {line_str:>6} "
            f"{e.pm_side:<18} {e.pm_price:>8.3f} {e.sharp_no_vig_prob:>8.3f} "
            f"{color}{e.edge_pct:>6.1%}{reset} "
            f"{e.edge_after_costs:>8.1%} {e.pm_liquidity:>10,.0f} {e.match_confidence:>4d}%"
        )

    print("-" * len(header))
    print(f"  {len(edges_sorted)} opportunities found\n")


def run_single_scan(
    odds_client: OddsAPIClient,
    pm_client: PolymarketAPIClient,
    db: ArbDatabase,
    odds_cache: list[OddsEvent] | None = None,
) -> tuple[list[EdgeOpportunity], list[OddsEvent]]:
    """
    Run a single scan pass: fetch data, match, calculate edges, log.

    Args:
        odds_client: The Odds API client.
        pm_client: Polymarket API client.
        db: Database for logging.
        odds_cache: Reuse cached odds data (to conserve API credits).

    Returns:
        (list of actionable edges, odds events for caching)
    """
    scan_time = datetime.now(timezone.utc)
    all_edges: list[EdgeOpportunity] = []

    # ── Fetch Polymarket data ──
    logger.info("Fetching Polymarket sports events...")
    try:
        pm_events = pm_client.get_sports_events()
        logger.info("Got %d PM sports events", len(pm_events))
    except Exception as e:
        logger.error("Failed to fetch PM events: %s", e)
        pm_events = []

    # ── Fetch Odds API data (or use cache) ──
    odds_events = odds_cache or []
    if not odds_events:
        if not ODDS_API_KEY:
            logger.error(
                "ODDS_API_KEY not set. Add it to .env to fetch sportsbook odds. "
                "Get a free key at https://the-odds-api.com/"
            )
        else:
            logger.info("Fetching odds from The Odds API...")
            try:
                odds_events = odds_client.get_all_monitored_odds()
                logger.info("Got %d odds events", len(odds_events))
                if odds_client.credits_remaining is not None:
                    logger.info("API credits remaining: %d", odds_client.credits_remaining)
            except Exception as e:
                logger.error("Failed to fetch odds: %s", e)

    if not odds_events:
        logger.warning("No odds data available — skipping edge detection")
        # Still log PM events as snapshots
        for pm_event in pm_events:
            db.log_snapshot(
                sport=pm_event.sport,
                pm_event_id=pm_event.event_id,
                rejection_reason="no_odds_data",
            )
        return [], []

    if not pm_events:
        logger.warning("No PM events found — skipping edge detection")
        for odds_event in odds_events:
            db.log_snapshot(
                sport=odds_event.sport_key,
                odds_api_event_id=odds_event.event_id,
                rejection_reason="no_pm_data",
            )
        return [], odds_events

    # ── Match events across platforms ──
    logger.info("Matching %d odds events with %d PM events...", len(odds_events), len(pm_events))
    matches = match_events(odds_events, pm_events, min_confidence=0)  # Get all for logging

    logger.info("Found %d potential matches", len(matches))

    # ── Calculate edges for each match ──
    for match in matches:
        # Log the snapshot regardless of outcome
        rejection_reason = match.rejection_reason

        # Filter: skip games that are already in progress or resolved.
        # PM prices near 0 or 1 on a moneyline = game is over or nearly over.
        if match.market_type == "h2h":
            prices = [o.price for o in match.pm_market.outcomes if o.price > 0]
            if prices and (max(prices) >= 0.98 or min(prices) <= 0.02):
                db.log_snapshot(
                    sport=match.odds_event.sport_key,
                    pm_event_id=match.pm_event.event_id,
                    odds_api_event_id=match.odds_event.event_id,
                    match_confidence=match.confidence,
                    market_type=match.market_type,
                    rejection_reason="game_resolved_or_live (PM price near 0/1)",
                )
                continue

        # Filter: skip games where Odds API commence_time is in the past.
        # Pre-game odds become stale once the game starts.
        if match.odds_event.commence_time:
            if match.odds_event.commence_time < scan_time:
                db.log_snapshot(
                    sport=match.odds_event.sport_key,
                    pm_event_id=match.pm_event.event_id,
                    odds_api_event_id=match.odds_event.event_id,
                    match_confidence=match.confidence,
                    market_type=match.market_type,
                    rejection_reason="game_started (commence_time in past)",
                )
                continue

        if match.confidence < MIN_MATCH_CONFIDENCE:
            rejection_reason = rejection_reason or f"low_confidence ({match.confidence}%)"
            db.log_snapshot(
                sport=match.odds_event.sport_key,
                pm_event_id=match.pm_event.event_id,
                odds_api_event_id=match.odds_event.event_id,
                match_confidence=match.confidence,
                market_type=match.market_type,
                rejection_reason=rejection_reason,
            )
            continue

        # Check PM volume/liquidity filters
        if match.pm_market.volume < MIN_PM_VOLUME:
            rejection_reason = f"low_volume (${match.pm_market.volume:,.0f} < ${MIN_PM_VOLUME:,.0f})"
            db.log_snapshot(
                sport=match.odds_event.sport_key,
                pm_event_id=match.pm_event.event_id,
                odds_api_event_id=match.odds_event.event_id,
                match_confidence=match.confidence,
                market_type=match.market_type,
                rejection_reason=rejection_reason,
            )
            continue

        if match.pm_market.liquidity < MIN_PM_LIQUIDITY:
            rejection_reason = f"low_liquidity (${match.pm_market.liquidity:,.0f} < ${MIN_PM_LIQUIDITY:,.0f})"
            db.log_snapshot(
                sport=match.odds_event.sport_key,
                pm_event_id=match.pm_event.event_id,
                odds_api_event_id=match.odds_event.event_id,
                match_confidence=match.confidence,
                market_type=match.market_type,
                rejection_reason=rejection_reason,
            )
            continue

        # Build PM outcomes list for edge calculator
        pm_outcomes = [
            {"name": o.name, "price": o.price, "token_id": o.token_id}
            for o in match.pm_market.outcomes
        ]

        edges = calculate_edges(
            odds_event=match.odds_event,
            pm_outcomes=pm_outcomes,
            market_type=match.market_type,
            pm_volume=match.pm_market.volume,
            pm_liquidity=match.pm_market.liquidity,
            match_confidence=match.confidence,
        )

        for edge in edges:
            if edge.edge_pct >= MIN_EDGE_PCT:
                db.log_opportunity(edge)
                all_edges.append(edge)

                # Alert for big edges (placeholder for Telegram)
                if edge.edge_pct >= 0.05:
                    logger.warning(
                        "🚨 BIG EDGE: %s | %s | %.1f%% edge | PM: %.3f | Sharp: %.3f",
                        edge.game, edge.pm_side, edge.edge_pct * 100,
                        edge.pm_price, edge.sharp_no_vig_prob,
                    )
            else:
                db.log_snapshot(
                    sport=match.odds_event.sport_key,
                    pm_event_id=match.pm_event.event_id,
                    odds_api_event_id=match.odds_event.event_id,
                    match_confidence=match.confidence,
                    market_type=match.market_type,
                    rejection_reason=f"edge_too_small ({edge.edge_pct:.1%} < {MIN_EDGE_PCT:.0%})",
                    metadata={
                        "pm_price": edge.pm_price,
                        "sharp_prob": edge.sharp_no_vig_prob,
                        "edge_pct": edge.edge_pct,
                    },
                )

    return all_edges, odds_events


def run_scanner(mode: str = "scan") -> None:
    """
    Main entry point for the scanner.

    Args:
        mode: "scan" (continuous), "once" (single pass), or "backfill" (not implemented).
    """
    setup_logging()
    logger.info("=" * 60)
    logger.info("Sports Arbitrage Scanner — Phase 1 (Detection Only)")
    logger.info("Mode: %s", mode)
    logger.info("=" * 60)

    odds_client = OddsAPIClient()
    pm_client = PolymarketAPIClient()
    db = ArbDatabase()

    if mode == "once":
        edges, _ = run_single_scan(odds_client, pm_client, db)
        print_opportunities_table(edges)
        stats = db.get_stats()
        logger.info(
            "Scan complete. %d opportunities found. %d total logged.",
            len(edges), stats["total_opportunities"],
        )
        return

    if mode == "backfill":
        logger.error("Backfill mode not yet implemented. Use --once or --scan.")
        return

    # Continuous scanning mode
    logger.info(
        "Starting continuous scan. Odds API poll: %ds, PM poll: %ds",
        POLL_INTERVAL_ODDS, POLL_INTERVAL_PM,
    )

    odds_cache: list[OddsEvent] = []
    last_odds_fetch = 0.0
    scan_count = 0

    try:
        while True:
            scan_count += 1
            now = time.time()

            # Refresh odds data if interval elapsed
            use_cache = odds_cache if (now - last_odds_fetch) < POLL_INTERVAL_ODDS else None
            if use_cache is None:
                last_odds_fetch = now

            logger.info("─── Scan #%d ───", scan_count)
            edges, new_odds = run_single_scan(
                odds_client, pm_client, db,
                odds_cache=use_cache,
            )

            if new_odds:
                odds_cache = new_odds

            print_opportunities_table(edges)

            # Show credits status
            if odds_client.credits_remaining is not None:
                logger.info("Odds API credits remaining: %d", odds_client.credits_remaining)

            # Sleep until next PM poll
            logger.info("Next scan in %d seconds...", POLL_INTERVAL_PM)
            time.sleep(POLL_INTERVAL_PM)

    except KeyboardInterrupt:
        logger.info("\nScanner stopped by user.")
        stats = db.get_stats()
        logger.info(
            "Session stats: %d scans, %d opportunities logged",
            stats["total_scans"], stats["total_opportunities"],
        )


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Polymarket Sports Arbitrage Scanner (Phase 1 — Detection Only)"
    )
    parser.add_argument(
        "--scan", action="store_true", default=True,
        help="Continuous scanning mode (default)",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Single scan pass, then exit",
    )
    parser.add_argument(
        "--backfill", action="store_true",
        help="Backfill historical data (not yet implemented)",
    )

    args = parser.parse_args()

    if args.once:
        mode = "once"
    elif args.backfill:
        mode = "backfill"
    else:
        mode = "scan"

    run_scanner(mode)


if __name__ == "__main__":
    main()
