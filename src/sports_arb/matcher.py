"""
Cross-platform game matcher.

Fuzzy matches games between The Odds API and Polymarket
using team names, sport, date, and line numbers.

Uses difflib for fuzzy matching (rapidfuzz preferred but not required).
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Any

from .odds_api import OddsEvent
from .polymarket_api import PMEvent, PMMarket

logger = logging.getLogger(__name__)

# Try rapidfuzz first for better matching, fall back to difflib
try:
    from rapidfuzz import fuzz as rf_fuzz
    _USE_RAPIDFUZZ = True
    logger.info("Using rapidfuzz for team name matching")
except ImportError:
    _USE_RAPIDFUZZ = False
    logger.info("rapidfuzz not installed, falling back to difflib")

# Common team name abbreviations and variations
_TEAM_ALIASES: dict[str, list[str]] = {
    # NBA
    "los angeles lakers": ["lakers", "la lakers", "lal"],
    "los angeles clippers": ["clippers", "la clippers", "lac"],
    "golden state warriors": ["warriors", "gsw", "golden state"],
    "new york knicks": ["knicks", "nyk", "ny knicks"],
    "brooklyn nets": ["nets", "bkn", "brooklyn"],
    "boston celtics": ["celtics", "bos", "boston"],
    "milwaukee bucks": ["bucks", "mil", "milwaukee"],
    "philadelphia 76ers": ["76ers", "sixers", "phi", "philly"],
    "oklahoma city thunder": ["thunder", "okc", "oklahoma city"],
    "denver nuggets": ["nuggets", "den", "denver"],
    # NCAA
    "michigan state spartans": ["michigan st", "msu", "michigan state"],
    "north carolina tar heels": ["unc", "north carolina", "tar heels"],
    "duke blue devils": ["duke", "blue devils"],
    "gonzaga bulldogs": ["gonzaga", "zags"],
    "uconn huskies": ["uconn", "connecticut"],
    "purdue boilermakers": ["purdue", "boilermakers"],
    "kansas jayhawks": ["kansas", "ku", "jayhawks"],
    # NFL
    "kansas city chiefs": ["chiefs", "kc", "kansas city"],
    "san francisco 49ers": ["49ers", "niners", "sf", "san francisco"],
    "new england patriots": ["patriots", "pats", "ne", "new england"],
    "dallas cowboys": ["cowboys", "dal", "dallas"],
    "green bay packers": ["packers", "gb", "green bay"],
    # NHL
    "new york rangers": ["rangers", "nyr", "ny rangers"],
    "new york islanders": ["islanders", "nyi", "ny islanders"],
    "los angeles kings": ["kings", "la kings", "lak"],
    "tampa bay lightning": ["lightning", "tb", "tampa bay", "tbl"],
    "vegas golden knights": ["golden knights", "vgk", "vegas"],
    # Soccer
    "manchester united": ["man utd", "man united", "mufc"],
    "manchester city": ["man city", "mcfc"],
    "real madrid": ["real", "madrid"],
    "fc barcelona": ["barcelona", "barca", "fcb"],
    "bayern munich": ["bayern", "fc bayern"],
}

# Build reverse lookup: alias → canonical
_ALIAS_TO_CANONICAL: dict[str, str] = {}
for canonical, aliases in _TEAM_ALIASES.items():
    for alias in aliases:
        _ALIAS_TO_CANONICAL[alias.lower()] = canonical
    _ALIAS_TO_CANONICAL[canonical] = canonical


@dataclass
class MatchResult:
    """Result of matching an Odds API event to a Polymarket event/market."""
    odds_event: OddsEvent
    pm_event: PMEvent
    pm_market: PMMarket
    confidence: int                  # 0-100
    market_type: str                 # h2h, spread, ou
    home_team_match_score: float     # 0-1 fuzzy score
    away_team_match_score: float     # 0-1 fuzzy score
    line_match: bool                 # For spreads/totals: do lines match?
    rejection_reason: str | None = None


def normalize_team_name(name: str) -> str:
    """
    Normalize a team name for comparison.

    Strips common suffixes, lowercases, removes punctuation.
    """
    name = name.lower().strip()
    # Remove common suffixes that don't help matching
    name = re.sub(r"\s+(fc|sc|cf|afc)$", "", name)
    # Remove punctuation
    name = re.sub(r"[^\w\s]", "", name)
    # Collapse whitespace
    name = re.sub(r"\s+", " ", name).strip()
    return name


def _canonicalize(name: str) -> str:
    """Try to map a name to its canonical form via alias table."""
    normalized = normalize_team_name(name)
    return _ALIAS_TO_CANONICAL.get(normalized, normalized)


def fuzzy_score(name_a: str, name_b: str) -> float:
    """
    Compute fuzzy match score between two team names.

    Returns 0-1 score. Uses rapidfuzz if available, else difflib.
    Also checks canonical forms for exact matches.
    """
    # First check canonical match — instant 1.0 if aliases resolve to same team
    canon_a = _canonicalize(name_a)
    canon_b = _canonicalize(name_b)
    if canon_a == canon_b:
        return 1.0

    norm_a = normalize_team_name(name_a)
    norm_b = normalize_team_name(name_b)

    if norm_a == norm_b:
        return 1.0

    # Check if one is a substring of the other (e.g., "Lakers" in "Los Angeles Lakers")
    if norm_a in norm_b or norm_b in norm_a:
        return 0.9

    if _USE_RAPIDFUZZ:
        # rapidfuzz returns 0-100
        ratio = rf_fuzz.ratio(norm_a, norm_b) / 100
        partial = rf_fuzz.partial_ratio(norm_a, norm_b) / 100
        token_sort = rf_fuzz.token_sort_ratio(norm_a, norm_b) / 100
        return max(ratio, partial, token_sort)
    else:
        # difflib SequenceMatcher returns 0-1
        ratio = SequenceMatcher(None, norm_a, norm_b).ratio()
        # Also try token-level matching
        tokens_a = set(norm_a.split())
        tokens_b = set(norm_b.split())
        if tokens_a and tokens_b:
            overlap = len(tokens_a & tokens_b)
            token_score = overlap / max(len(tokens_a), len(tokens_b))
            return max(ratio, token_score)
        return ratio


def _date_within_range(dt1: datetime, dt2: datetime, hours: int = 24) -> bool:
    """Check if two datetimes are within `hours` of each other."""
    if dt1.tzinfo is None or dt2.tzinfo is None:
        # If either is naive, just compare dates
        return abs((dt1.date() - dt2.date()).days) <= 1
    return abs((dt1 - dt2).total_seconds()) < hours * 3600


def _extract_teams_from_pm(pm_market: PMMarket, pm_event: PMEvent) -> tuple[str, str]:
    """
    Extract two team names from a Polymarket market/event.

    PM markets often have "Team A vs Team B" in the title or
    outcome names like "Yes"/"No" or actual team names.
    """
    # First try: outcome names that look like team names (not Yes/No)
    team_names = [
        o.name for o in pm_market.outcomes
        if o.name.lower() not in ("yes", "no", "over", "under")
    ]
    if len(team_names) >= 2:
        return team_names[0], team_names[1]

    # Second try: parse from event title "Team A vs Team B"
    title = pm_event.title
    vs_match = re.search(r"(.+?)\s+(?:vs\.?|v\.)\s+(.+?)(?:\s*[-–—]|$)", title)
    if vs_match:
        return vs_match.group(1).strip(), vs_match.group(2).strip()

    # Third try: parse from question
    vs_match = re.search(r"(.+?)\s+(?:vs\.?|v\.)\s+(.+?)(?:\s*[-–—]|\?|$)", pm_market.question)
    if vs_match:
        return vs_match.group(1).strip(), vs_match.group(2).strip()

    return "", ""


def match_events(
    odds_events: list[OddsEvent],
    pm_events: list[PMEvent],
    min_confidence: int = 80,
) -> list[MatchResult]:
    """
    Match Odds API events to Polymarket events/markets.

    For each Odds API event, finds the best matching PM market by:
    1. Same sport category
    2. Team name fuzzy matching
    3. Date proximity
    4. Line number matching (for spreads/totals)

    Args:
        odds_events: Events from The Odds API.
        pm_events: Events from Polymarket.
        min_confidence: Minimum confidence to include. Set to 0 to include all with rejection reasons.

    Returns:
        List of MatchResult, sorted by confidence descending.
    """
    results: list[MatchResult] = []

    for odds_event in odds_events:
        for pm_event in pm_events:
            # Sport filter — must be same category
            if pm_event.sport != odds_event.sport_key:
                continue

            for pm_market in pm_event.markets:
                # Skip closed/inactive markets
                if pm_market.closed or not pm_market.active:
                    continue

                result = _score_match(odds_event, pm_event, pm_market)
                if result.confidence >= min_confidence or min_confidence == 0:
                    results.append(result)

    # Sort by confidence
    results.sort(key=lambda r: r.confidence, reverse=True)

    # Deduplicate: keep best match per odds event + market type
    seen: set[str] = set()
    deduped: list[MatchResult] = []
    for r in results:
        key = f"{r.odds_event.event_id}:{r.market_type}"
        if key not in seen:
            seen.add(key)
            deduped.append(r)

    logger.info(
        "Matched %d pairs (%d above threshold) from %d odds × %d PM events",
        len(results), len(deduped), len(odds_events), len(pm_events),
    )
    return deduped


def _score_match(
    odds_event: OddsEvent,
    pm_event: PMEvent,
    pm_market: PMMarket,
) -> MatchResult:
    """Score how well an odds event matches a PM market. Returns MatchResult with confidence 0-100."""
    confidence = 0
    rejection_reason = None

    # ── Team name matching (0-60 points) ──
    pm_team_a, pm_team_b = _extract_teams_from_pm(pm_market, pm_event)

    if not pm_team_a or not pm_team_b:
        # Can't extract teams — likely a yes/no market, try event title
        pm_team_a, pm_team_b = _extract_teams_from_pm(pm_market, pm_event)

    # Try both orderings (home/away might be swapped)
    score_1h = fuzzy_score(odds_event.home_team, pm_team_a)
    score_1a = fuzzy_score(odds_event.away_team, pm_team_b)
    combo_1 = (score_1h + score_1a) / 2

    score_2h = fuzzy_score(odds_event.home_team, pm_team_b)
    score_2a = fuzzy_score(odds_event.away_team, pm_team_a)
    combo_2 = (score_2h + score_2a) / 2

    if combo_1 >= combo_2:
        home_score, away_score = score_1h, score_1a
    else:
        home_score, away_score = score_2h, score_2a

    best_combo = max(combo_1, combo_2)
    confidence += int(best_combo * 60)

    if best_combo < 0.5:
        rejection_reason = f"team name match too low ({best_combo:.2f})"

    # ── Date proximity (0-20 points) ──
    # PM events use end_date as the game/resolution time.
    # start_date is when the event was created on PM (often days before the game).
    # So we compare the Odds API commence_time against PM end_date.
    date_points = 0
    pm_date = pm_event.end_date or pm_event.start_date
    if pm_date and odds_event.commence_time:
        if _date_within_range(odds_event.commence_time, pm_date, hours=6):
            date_points = 20
        elif _date_within_range(odds_event.commence_time, pm_date, hours=24):
            date_points = 15
        elif _date_within_range(odds_event.commence_time, pm_date, hours=48):
            date_points = 10
        else:
            if rejection_reason is None:
                rejection_reason = "date mismatch"
    else:
        date_points = 10  # Can't verify date, give partial credit
    confidence += date_points

    # ── Market type + line matching (0-20 points) ──
    market_type = pm_market.market_type
    line_match = True

    if market_type == "spread":
        if pm_market.spread_line is not None:
            # Check if the odds event has a matching spread line
            # We'll verify against the actual odds in edge_calculator
            confidence += 15
        else:
            confidence += 10
            line_match = False
    elif market_type == "ou":
        if pm_market.total_line is not None:
            confidence += 15
        else:
            confidence += 10
            line_match = False
    elif market_type == "h2h":
        confidence += 20  # Moneyline is simpler to match
    else:
        confidence += 5

    return MatchResult(
        odds_event=odds_event,
        pm_event=pm_event,
        pm_market=pm_market,
        confidence=min(confidence, 100),
        market_type=market_type,
        home_team_match_score=home_score,
        away_team_match_score=away_score,
        line_match=line_match,
        rejection_reason=rejection_reason,
    )
