"""
The Odds API v4 client.

Fetches live odds from sharp and soft sportsbooks.
Tracks API credit usage via response headers.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import requests

from .config import ODDS_API_KEY, ODDS_API_BASE, MONITORED_SPORTS, SHARP_BOOKS, SOFT_BOOKS

logger = logging.getLogger(__name__)


@dataclass
class OddsOutcome:
    """Single outcome line from a bookmaker."""
    name: str               # Team or Over/Under label
    price_american: int | None = None
    price_decimal: float | None = None
    point: float | None = None   # Spread or total line (e.g., -4.5 or 220.5)


@dataclass
class BookmakerMarket:
    """One market (h2h/spreads/totals) from one bookmaker."""
    bookmaker_key: str
    bookmaker_title: str
    market_key: str          # h2h, spreads, totals
    outcomes: list[OddsOutcome] = field(default_factory=list)
    last_update: datetime | None = None


@dataclass
class OddsEvent:
    """A single game/event with odds from multiple bookmakers."""
    event_id: str
    sport_key: str
    sport_title: str
    commence_time: datetime
    home_team: str
    away_team: str
    bookmaker_markets: list[BookmakerMarket] = field(default_factory=list)

    def get_sharp_markets(self, market_key: str = "h2h") -> list[BookmakerMarket]:
        """Return markets from sharp books only."""
        return [
            m for m in self.bookmaker_markets
            if m.bookmaker_key in SHARP_BOOKS and m.market_key == market_key
        ]

    def get_all_markets(self, market_key: str = "h2h") -> list[BookmakerMarket]:
        """Return all markets for a given market type."""
        return [m for m in self.bookmaker_markets if m.market_key == market_key]


class OddsAPIClient:
    """Client for The Odds API v4."""

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or ODDS_API_KEY
        self.base_url = ODDS_API_BASE
        self.session = requests.Session()
        # Credit tracking from response headers
        self.credits_remaining: int | None = None
        self.credits_used: int | None = None

        if not self.api_key:
            logger.warning("ODDS_API_KEY not set — API calls will fail")

    def _request(self, endpoint: str, params: dict[str, Any] | None = None) -> dict | list:
        """Make authenticated GET request and track credits."""
        url = f"{self.base_url}{endpoint}"
        params = params or {}
        params["apiKey"] = self.api_key

        try:
            resp = self.session.get(url, params=params, timeout=15)

            # Track credits from headers
            remaining = resp.headers.get("x-requests-remaining")
            used = resp.headers.get("x-requests-used")
            if remaining is not None:
                self.credits_remaining = int(remaining)
            if used is not None:
                self.credits_used = int(used)
                logger.info(
                    "Odds API credits — used: %s, remaining: %s",
                    self.credits_used, self.credits_remaining
                )

            resp.raise_for_status()
            return resp.json()

        except requests.exceptions.HTTPError as e:
            if resp.status_code == 401:
                logger.error("Odds API auth failed — check ODDS_API_KEY")
            elif resp.status_code == 429:
                logger.error("Odds API rate limited — credits exhausted")
            else:
                logger.error("Odds API HTTP error: %s", e)
            raise
        except requests.exceptions.RequestException as e:
            logger.error("Odds API request failed: %s", e)
            raise

    def list_sports(self, all_sports: bool = False) -> list[dict]:
        """
        GET /v4/sports — list available sports.

        Args:
            all_sports: If True, include out-of-season sports.

        Returns:
            List of sport dicts with key, group, title, active fields.
        """
        params = {}
        if all_sports:
            params["all"] = "true"

        sports = self._request("/v4/sports", params)
        logger.info("Fetched %d sports from Odds API", len(sports))
        return sports

    def get_odds(
        self,
        sport: str,
        regions: str = "us,eu",
        markets: str = "h2h,spreads,totals",
        odds_format: str = "american",
    ) -> list[OddsEvent]:
        """
        GET /v4/sports/{sport}/odds — fetch live odds for a sport.

        Uses both US and EU regions to get Pinnacle (EU) + US books.
        Each call costs 1 credit per region × market combo.

        Args:
            sport: Sport key (e.g., "basketball_nba").
            regions: Comma-separated regions (us, eu, uk, au).
            markets: Comma-separated market types (h2h, spreads, totals).
            odds_format: "american" or "decimal".

        Returns:
            List of OddsEvent with all bookmaker markets attached.
        """
        params = {
            "regions": regions,
            "markets": markets,
            "oddsFormat": odds_format,
        }

        raw_events = self._request(f"/v4/sports/{sport}/odds", params)
        events = []

        for raw in raw_events:
            event = OddsEvent(
                event_id=raw["id"],
                sport_key=raw["sport_key"],
                sport_title=raw["sport_title"],
                commence_time=datetime.fromisoformat(raw["commence_time"].replace("Z", "+00:00")),
                home_team=raw["home_team"],
                away_team=raw["away_team"],
            )

            for bm in raw.get("bookmakers", []):
                for mkt in bm.get("markets", []):
                    outcomes = []
                    for oc in mkt.get("outcomes", []):
                        outcomes.append(OddsOutcome(
                            name=oc["name"],
                            price_american=oc.get("price"),
                            point=oc.get("point"),
                        ))

                    event.bookmaker_markets.append(BookmakerMarket(
                        bookmaker_key=bm["key"],
                        bookmaker_title=bm["title"],
                        market_key=mkt["key"],
                        outcomes=outcomes,
                        last_update=datetime.fromisoformat(
                            bm["last_update"].replace("Z", "+00:00")
                        ) if bm.get("last_update") else None,
                    ))

            events.append(event)

        logger.info("Fetched %d events for %s", len(events), sport)
        return events

    def get_all_monitored_odds(
        self,
        sports: list[str] | None = None,
        markets: str = "h2h,spreads,totals",
    ) -> list[OddsEvent]:
        """
        Fetch odds for all monitored sports.

        Warning: each sport costs credits. With 3 markets × 2 regions = 6 credits per sport.
        7 sports × 6 = 42 credits per full scan. At 500 free, that's ~11 full scans.

        Args:
            sports: Override which sports to fetch. Defaults to MONITORED_SPORTS.
            markets: Market types to fetch.

        Returns:
            Aggregated list of OddsEvents across all sports.
        """
        sports = sports or MONITORED_SPORTS
        all_events: list[OddsEvent] = []

        for sport in sports:
            try:
                events = self.get_odds(sport, markets=markets)
                all_events.extend(events)
            except Exception as e:
                logger.error("Failed to fetch odds for %s: %s", sport, e)
                continue

        logger.info("Total: %d events across %d sports", len(all_events), len(sports))
        return all_events


def american_to_decimal(american: int) -> float:
    """Convert American odds to decimal odds."""
    if american > 0:
        return (american / 100) + 1
    else:
        return (100 / abs(american)) + 1


def american_to_implied_prob(american: int) -> float:
    """
    Convert American odds to raw implied probability (includes vig).

    Negative odds: prob = |odds| / (|odds| + 100)
    Positive odds: prob = 100 / (odds + 100)
    """
    if american < 0:
        return abs(american) / (abs(american) + 100)
    else:
        return 100 / (american + 100)
