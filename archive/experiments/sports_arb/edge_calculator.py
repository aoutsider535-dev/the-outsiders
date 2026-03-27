"""
Edge calculator: no-vig probability conversion and edge detection.

Converts sharp bookmaker odds to fair (no-vig) probabilities,
then compares against Polymarket prices to find edges.
"""

import logging
from dataclasses import dataclass
from typing import Any

from .config import MIN_EDGE_PCT, PM_FEE_PCT, EST_SLIPPAGE_PCT
from .odds_api import OddsEvent, BookmakerMarket, OddsOutcome, american_to_implied_prob

logger = logging.getLogger(__name__)


@dataclass
class EdgeOpportunity:
    """A detected edge between sharp odds and Polymarket price."""
    sport: str
    game: str                    # "Team A vs Team B"
    market_type: str             # h2h, spread, ou
    line: float | None           # Spread or total line
    pm_side: str                 # Which PM outcome has the edge
    pm_price: float              # Current PM price (0-1)
    sharp_no_vig_prob: float     # Fair probability from sharp books (0-1)
    edge_pct: float              # sharp_no_vig_prob - pm_price (positive = PM is cheap)
    edge_after_costs: float      # Edge minus fees and slippage
    pm_liquidity: float          # Estimated PM liquidity in USD
    pm_volume: float             # PM market volume
    books_used: list[str]        # Which sharp books contributed
    raw_odds: dict[str, int]     # Bookmaker → American odds for this side
    match_confidence: int        # From matcher (0-100)

    @property
    def is_actionable(self) -> bool:
        """Edge passes minimum threshold after costs."""
        return self.edge_after_costs >= MIN_EDGE_PCT


def remove_vig_basic(probs: list[float]) -> list[float]:
    """
    Remove vig by normalizing implied probabilities to sum to 1.0.

    This is the simplest method — divide each prob by the total.
    Works well for 2-way markets, less accurate for 3+ way.

    Args:
        probs: Raw implied probabilities (will sum to >1.0 due to vig).

    Returns:
        Fair probabilities summing to 1.0.
    """
    total = sum(probs)
    if total == 0:
        return probs
    return [p / total for p in probs]


def remove_vig_power(probs: list[float]) -> list[float]:
    """
    Remove vig using the power method (multiplicative).

    Better for sharp books like Pinnacle. Finds exponent k such that
    sum(p_i^k) = 1. More accurate than basic normalization because
    it accounts for the fact that vig is proportionally larger on
    the underdog side.

    For 2-way markets, the difference from basic normalization is small
    but it's more principled.
    """
    if len(probs) < 2:
        return probs

    total = sum(probs)
    if total <= 1.0:
        return probs  # No vig to remove

    # Binary search for exponent k
    lo, hi = 0.5, 2.0
    for _ in range(50):  # Converges fast
        k = (lo + hi) / 2
        adjusted_sum = sum(p ** k for p in probs)
        if adjusted_sum > 1.0:
            lo = k
        else:
            hi = k

    k = (lo + hi) / 2
    fair_probs = [p ** k for p in probs]
    # Final normalization for numerical precision
    total_fair = sum(fair_probs)
    return [p / total_fair for p in fair_probs]


def get_sharp_no_vig_probs(
    odds_event: OddsEvent,
    market_key: str = "h2h",
    use_power_method: bool = True,
) -> dict[str, float] | None:
    """
    Extract no-vig probabilities from sharp bookmakers for an event.

    Prioritizes Pinnacle. If multiple sharp books available, averages them.

    Args:
        odds_event: Event with bookmaker markets attached.
        market_key: Which market type (h2h, spreads, totals).
        use_power_method: Use power method for vig removal (better for Pinnacle).

    Returns:
        Dict mapping outcome name → fair probability, or None if no sharp data.
    """
    sharp_markets = odds_event.get_sharp_markets(market_key)

    if not sharp_markets:
        logger.debug(
            "No sharp book data for %s vs %s (%s)",
            odds_event.home_team, odds_event.away_team, market_key,
        )
        return None

    # Aggregate across sharp books (usually just Pinnacle)
    all_probs: dict[str, list[float]] = {}
    all_odds: dict[str, list[int]] = {}

    for market in sharp_markets:
        raw_probs = []
        for outcome in market.outcomes:
            if outcome.price_american is None:
                continue
            prob = american_to_implied_prob(outcome.price_american)
            raw_probs.append(prob)

        if len(raw_probs) < 2:
            continue

        # Remove vig
        remove_fn = remove_vig_power if use_power_method else remove_vig_basic
        fair_probs = remove_fn(raw_probs)

        for i, outcome in enumerate(market.outcomes):
            if i < len(fair_probs):
                all_probs.setdefault(outcome.name, []).append(fair_probs[i])
                if outcome.price_american is not None:
                    all_odds.setdefault(outcome.name, []).append(outcome.price_american)

    if not all_probs:
        return None

    # Average across sharp books
    result = {}
    for name, prob_list in all_probs.items():
        result[name] = sum(prob_list) / len(prob_list)

    return result


def calculate_edges(
    odds_event: OddsEvent,
    pm_outcomes: list[dict[str, Any]],
    market_type: str = "h2h",
    pm_volume: float = 0.0,
    pm_liquidity: float = 0.0,
    match_confidence: int = 0,
) -> list[EdgeOpportunity]:
    """
    Calculate edges between sharp odds and Polymarket prices.

    For each PM outcome, checks if the sharp no-vig probability
    exceeds the PM price (meaning PM is underpricing this outcome).

    Args:
        odds_event: Event from The Odds API with bookmaker odds.
        pm_outcomes: List of dicts with 'name' and 'price' from PM.
        market_type: h2h, spread, or ou.
        pm_volume: Market volume on PM.
        pm_liquidity: Estimated PM liquidity.
        match_confidence: From matcher (0-100).

    Returns:
        List of EdgeOpportunity (may be empty).
    """
    sharp_probs = get_sharp_no_vig_probs(odds_event, market_type)
    if not sharp_probs:
        return []

    edges: list[EdgeOpportunity] = []
    game_name = f"{odds_event.away_team} vs {odds_event.home_team}"

    for pm_out in pm_outcomes:
        pm_name = pm_out["name"]
        pm_price = pm_out["price"]

        # Find matching sharp probability
        # Try exact match first, then fuzzy
        sharp_prob = _find_matching_prob(pm_name, sharp_probs, odds_event)

        if sharp_prob is None:
            continue

        # Edge = sharp fair prob - PM price
        # Positive edge means PM is offering a better price than fair value
        raw_edge = sharp_prob - pm_price

        # Adjust for costs
        edge_after_costs = raw_edge - PM_FEE_PCT - EST_SLIPPAGE_PCT

        # Collect which books contributed
        books_used = [
            m.bookmaker_key
            for m in odds_event.get_sharp_markets(market_type)
        ]

        # Raw odds for reference
        raw_odds: dict[str, int] = {}
        for m in odds_event.get_sharp_markets(market_type):
            for oc in m.outcomes:
                if oc.name.lower() == pm_name.lower() and oc.price_american is not None:
                    raw_odds[m.bookmaker_key] = oc.price_american

        # Determine line for spread/ou
        line: float | None = None
        if market_type == "spread":
            for m in odds_event.get_sharp_markets("spreads"):
                for oc in m.outcomes:
                    if oc.point is not None:
                        line = oc.point
                        break
        elif market_type == "ou":
            for m in odds_event.get_sharp_markets("totals"):
                for oc in m.outcomes:
                    if oc.point is not None:
                        line = oc.point
                        break

        edge = EdgeOpportunity(
            sport=odds_event.sport_key,
            game=game_name,
            market_type=market_type,
            line=line,
            pm_side=pm_name,
            pm_price=pm_price,
            sharp_no_vig_prob=sharp_prob,
            edge_pct=raw_edge,
            edge_after_costs=edge_after_costs,
            pm_liquidity=pm_liquidity,
            pm_volume=pm_volume,
            books_used=books_used,
            raw_odds=raw_odds,
            match_confidence=match_confidence,
        )

        if raw_edge > 0:
            logger.info(
                "Edge found: %s | %s %.1f%% (after costs: %.1f%%) | PM: %.2f, Sharp: %.2f",
                game_name, pm_name, raw_edge * 100, edge_after_costs * 100,
                pm_price, sharp_prob,
            )

        edges.append(edge)

    return edges


def _find_matching_prob(
    pm_name: str,
    sharp_probs: dict[str, float],
    odds_event: OddsEvent,
) -> float | None:
    """
    Find the sharp probability that corresponds to a PM outcome name.

    Handles cases where PM uses "Yes"/"No" for moneyline markets by
    mapping to the home/away team, and fuzzy matches team names.
    """
    pm_lower = pm_name.lower().strip()

    # Direct match
    for sharp_name, prob in sharp_probs.items():
        if sharp_name.lower().strip() == pm_lower:
            return prob

    # PM sometimes uses team names that are slightly different
    from .matcher import fuzzy_score  # Local import to avoid circular

    best_score = 0.0
    best_prob = None

    for sharp_name, prob in sharp_probs.items():
        score = fuzzy_score(pm_name, sharp_name)
        if score > best_score and score > 0.6:
            best_score = score
            best_prob = prob

    # Handle Yes/No outcomes — map to home team win / away team win
    if best_prob is None and pm_lower in ("yes", "no"):
        # Need context from the market question to determine which side "Yes" maps to
        # For now, skip these — they need the full market context
        logger.debug("Skipping Yes/No outcome '%s' — needs market context mapping", pm_name)
        return None

    # Handle Over/Under
    if best_prob is None and pm_lower in ("over", "under"):
        for sharp_name, prob in sharp_probs.items():
            if sharp_name.lower().strip() == pm_lower:
                return prob

    return best_prob
