"""
Polymarket Gamma API + CLOB client for sports markets.

Fetches active sports events, parses market types, and checks orderbook depth.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import requests

from .config import GAMMA_API_BASE, CLOB_API_BASE, SPORT_LABELS

logger = logging.getLogger(__name__)

# Patterns for classifying Polymarket sports markets
_SPREAD_PATTERN = re.compile(
    r"(?:spread|line)[:\s]*(.+?)\s*\(([+-]?\d+\.?\d*)\)",
    re.IGNORECASE,
)
_OU_PATTERN = re.compile(
    r"(?:over|under|O/U|total)[:\s]*(\d+\.?\d*)",
    re.IGNORECASE,
)


@dataclass
class PMOutcome:
    """Single outcome on a Polymarket market."""
    name: str
    price: float            # 0-1, acts as implied probability
    token_id: str = ""


@dataclass
class PMMarket:
    """
    A single Polymarket market (one question).

    Polymarket structures things as events containing markets.
    A moneyline game might be one market with 2 outcomes.
    Spreads and O/U are typically separate markets.
    """
    market_id: str
    question: str
    outcomes: list[PMOutcome] = field(default_factory=list)
    market_type: str = "unknown"  # h2h, spread, ou, other
    spread_line: float | None = None
    total_line: float | None = None
    volume: float = 0.0
    liquidity: float = 0.0
    end_date: datetime | None = None
    active: bool = True
    closed: bool = False


@dataclass
class PMEvent:
    """A Polymarket event (e.g., a single game) containing one or more markets."""
    event_id: str
    title: str
    slug: str
    sport: str              # Detected sport category
    markets: list[PMMarket] = field(default_factory=list)
    start_date: datetime | None = None
    end_date: datetime | None = None


class PolymarketAPIClient:
    """Client for Polymarket Gamma API and CLOB."""

    def __init__(self):
        self.gamma_base = GAMMA_API_BASE
        self.clob_base = CLOB_API_BASE
        self.session = requests.Session()

    def _gamma_request(
        self, endpoint: str, params: dict[str, Any] | None = None
    ) -> list[dict] | dict:
        """GET request to Gamma API."""
        url = f"{self.gamma_base}{endpoint}"
        try:
            resp = self.session.get(url, params=params or {}, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            logger.error("Gamma API request failed (%s): %s", endpoint, e)
            raise

    def get_sports_events(
        self,
        limit: int = 100,
        offset: int = 0,
        active: bool = True,
        closed: bool = False,
    ) -> list[PMEvent]:
        """
        Fetch active sports events from Gamma API.

        Strategy: pull top events by 24h volume, then filter for actual game
        matchups (have spread/OU markets, "vs" in title, etc.). This catches
        individual NBA/NCAA/NHL/soccer games that have $1-7M daily volume.

        Also fetches sport-tagged events for broader coverage.

        Returns:
            List of PMEvent objects with markets attached.
        """
        all_events: list[PMEvent] = []
        seen_event_ids: set[str] = set()

        # --- Phase 1: Fetch top events by volume (catches individual games) ---
        # Individual game matchups are the highest volume events on PM.
        # They have "vs" in the title and spread/OU sub-markets.
        try:
            for page_offset in range(0, 200, 50):
                params = {
                    "limit": 50,
                    "offset": page_offset,
                    "active": str(active).lower(),
                    "closed": str(closed).lower(),
                    "order": "volume24hr",
                    "ascending": "false",
                }
                raw_events = self._gamma_request("/events", params)
                if not isinstance(raw_events, list) or len(raw_events) == 0:
                    break

                for raw in raw_events:
                    event_id = str(raw.get("id", ""))
                    if event_id in seen_event_ids:
                        continue
                    seen_event_ids.add(event_id)

                    # Quick check: is this a sports game?
                    title = raw.get("title", "")
                    tags = raw.get("tags", [])
                    tag_labels = [
                        (t.get("label", "") if isinstance(t, dict) else str(t)).lower()
                        for t in (tags or [])
                    ]
                    markets = raw.get("markets", [])

                    is_sports = any(
                        t in tag_labels
                        for t in ["sports", "nba", "ncaa", "nhl", "nfl", "soccer",
                                  "basketball", "football", "baseball", "hockey",
                                  "esports", "mma", "ufc"]
                    )
                    has_spread = any(
                        "spread" in m.get("question", "").lower() for m in markets
                    )
                    has_ou = any(
                        "o/u" in m.get("question", "").lower() for m in markets
                    )
                    has_vs = "vs" in title.lower()

                    if is_sports or has_spread or has_ou or has_vs:
                        event = self._parse_event(raw)
                        if event and event.sport != "unknown":
                            all_events.append(event)

        except Exception as e:
            logger.warning("Failed to fetch top volume PM events: %s", e)

        # --- Phase 2: Tag-based search for any missed sport events ---
        sport_keywords = set()
        for sport_info in SPORT_LABELS.values():
            tags = sport_info.get("pm_tags", [])
            if isinstance(tags, list):
                sport_keywords.update(tags)

        for keyword in list(sport_keywords)[:6]:
            try:
                params = {
                    "limit": limit,
                    "offset": offset,
                    "active": str(active).lower(),
                    "closed": str(closed).lower(),
                    "tag": keyword,
                }
                raw_events = self._gamma_request("/events", params)
                if not isinstance(raw_events, list):
                    continue

                for raw in raw_events:
                    event_id = str(raw.get("id", ""))
                    if event_id in seen_event_ids:
                        continue
                    seen_event_ids.add(event_id)

                    event = self._parse_event(raw)
                    if event and event.sport != "unknown":
                        all_events.append(event)

            except Exception as e:
                logger.warning("Failed to search PM events for '%s': %s", keyword, e)

        logger.info("Found %d sports events on Polymarket", len(all_events))
        return all_events

    def _parse_event(self, raw: dict) -> PMEvent | None:
        """Parse a raw Gamma API event into a PMEvent."""
        try:
            event_id = str(raw.get("id", ""))
            title = raw.get("title", "")
            slug = raw.get("slug", "")

            # Detect sport from title and tags
            sport = self._classify_sport(title, raw.get("tags", []))

            event = PMEvent(
                event_id=event_id,
                title=title,
                slug=slug,
                sport=sport,
            )

            # Parse start/end dates
            if raw.get("startDate"):
                try:
                    event.start_date = datetime.fromisoformat(
                        raw["startDate"].replace("Z", "+00:00")
                    )
                except (ValueError, TypeError):
                    pass

            if raw.get("endDate"):
                try:
                    event.end_date = datetime.fromisoformat(
                        raw["endDate"].replace("Z", "+00:00")
                    )
                except (ValueError, TypeError):
                    pass

            # Parse markets within the event
            for mkt_raw in raw.get("markets", []):
                market = self._parse_market(mkt_raw)
                if market:
                    event.markets.append(market)

            return event

        except Exception as e:
            logger.warning("Failed to parse PM event: %s", e)
            return None

    def _parse_market(self, raw: dict) -> PMMarket | None:
        """Parse a raw market dict into a PMMarket."""
        try:
            question = raw.get("question", "")
            market_id = str(raw.get("id", ""))

            # Determine market type from question text
            market_type, spread_line, total_line = self._classify_market(question)

            # Parse outcomes
            # Gamma API returns these as JSON-encoded strings: '["Over", "Under"]'
            outcomes = []
            outcome_names = raw.get("outcomes", "")
            outcome_prices = raw.get("outcomePrices", "")
            clob_token_ids = raw.get("clobTokenIds", "")

            names = self._parse_json_or_csv(outcome_names)
            price_strs = self._parse_json_or_csv(outcome_prices)
            token_ids = self._parse_json_or_csv(clob_token_ids)

            try:
                prices = [float(p) for p in price_strs]
            except (ValueError, TypeError):
                prices = []

            for i, name in enumerate(names):
                price = prices[i] if i < len(prices) else 0.0
                token_id = token_ids[i] if i < len(token_ids) else ""
                outcomes.append(PMOutcome(name=name, price=price, token_id=token_id))

            # Volume and liquidity
            volume = float(raw.get("volume", 0) or 0)
            liquidity = float(raw.get("liquidityClob", 0) or raw.get("liquidity", 0) or 0)

            # Active/closed status
            active = raw.get("active", True)
            closed = raw.get("closed", False)

            end_date = None
            if raw.get("endDate"):
                try:
                    end_date = datetime.fromisoformat(raw["endDate"].replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    pass

            return PMMarket(
                market_id=market_id,
                question=question,
                outcomes=outcomes,
                market_type=market_type,
                spread_line=spread_line,
                total_line=total_line,
                volume=volume,
                liquidity=liquidity,
                end_date=end_date,
                active=active,
                closed=closed,
            )

        except Exception as e:
            logger.warning("Failed to parse PM market: %s", e)
            return None

    @staticmethod
    def _parse_json_or_csv(value: Any) -> list[str]:
        """Parse a Gamma API field that may be a JSON string, CSV, or list."""
        if isinstance(value, list):
            return [str(v) for v in value]
        if isinstance(value, str):
            value = value.strip()
            if value.startswith("["):
                try:
                    import json
                    parsed = json.loads(value)
                    if isinstance(parsed, list):
                        return [str(v) for v in parsed]
                except (json.JSONDecodeError, ValueError):
                    pass
            # Fallback: comma-separated
            return [v.strip() for v in value.split(",") if v.strip()]
        return []

    def _classify_sport(self, title: str, tags: list | Any) -> str:
        """
        Detect which sport a PM event belongs to.

        Checks title and tags against known sport keywords.
        Handles Gamma API tag format (list of dicts with 'label' key).
        Returns the Odds API sport key or 'unknown'.
        """
        title_lower = title.lower()
        tags_lower = []
        if isinstance(tags, list):
            for t in tags:
                if isinstance(t, dict):
                    tags_lower.append(t.get("label", "").lower())
                else:
                    tags_lower.append(str(t).lower())

        combined = title_lower + " " + " ".join(tags_lower)

        for sport_key, info in SPORT_LABELS.items():
            pm_tags = info.get("pm_tags", [])
            if isinstance(pm_tags, list):
                for tag in pm_tags:
                    if tag.lower() in combined:
                        return sport_key

        return "unknown"

    def _classify_market(
        self, question: str
    ) -> tuple[str, float | None, float | None]:
        """
        Classify a market question into type: h2h, spread, ou, or other.

        Returns:
            (market_type, spread_line, total_line)
        """
        q_lower = question.lower()

        # Check for spread
        spread_match = _SPREAD_PATTERN.search(question)
        if spread_match:
            try:
                line = float(spread_match.group(2))
                return ("spread", line, None)
            except ValueError:
                pass

        # Look for spread indicators even without exact pattern
        if any(kw in q_lower for kw in ["spread", "cover", "points"]):
            # Try to extract a number
            nums = re.findall(r"[+-]?\d+\.5", question)
            if nums:
                return ("spread", float(nums[0]), None)

        # Check for over/under
        ou_match = _OU_PATTERN.search(question)
        if ou_match:
            try:
                total = float(ou_match.group(1))
                return ("ou", None, total)
            except ValueError:
                pass

        if any(kw in q_lower for kw in ["over", "under", "total", "o/u"]):
            nums = re.findall(r"\d+\.5", question)
            if nums:
                return ("ou", None, float(nums[0]))

        # Check for moneyline / head-to-head indicators
        # Most PM sports markets with two team outcomes are moneyline
        if any(kw in q_lower for kw in ["win", "beat", "vs", "v.", "defeat"]):
            return ("h2h", None, None)

        # Default: if it has exactly 2 outcomes that look like teams, treat as h2h
        return ("h2h", None, None)

    def get_orderbook(self, token_id: str) -> dict[str, Any]:
        """
        Fetch CLOB orderbook for a specific outcome token.

        Returns dict with bids and asks, each as list of {price, size}.
        Used to estimate liquidity and slippage.
        """
        if not token_id:
            return {"bids": [], "asks": []}

        try:
            url = f"{self.clob_base}/book"
            params = {"token_id": token_id}
            resp = self.session.get(url, params=params, timeout=10)
            resp.raise_for_status()
            book = resp.json()

            # Summarize depth
            bids = book.get("bids", [])
            asks = book.get("asks", [])

            total_bid_size = sum(float(b.get("size", 0)) for b in bids)
            total_ask_size = sum(float(a.get("size", 0)) for a in asks)

            logger.debug(
                "Orderbook for %s: %d bids ($%.0f), %d asks ($%.0f)",
                token_id[:12], len(bids), total_bid_size, len(asks), total_ask_size,
            )

            return {
                "bids": bids,
                "asks": asks,
                "total_bid_size": total_bid_size,
                "total_ask_size": total_ask_size,
            }

        except requests.exceptions.RequestException as e:
            logger.warning("Failed to fetch orderbook for %s: %s", token_id[:12], e)
            return {"bids": [], "asks": [], "total_bid_size": 0, "total_ask_size": 0}

    def estimate_liquidity(self, token_id: str) -> float:
        """
        Estimate available liquidity in USD for an outcome.

        Sums ask-side depth (what we'd buy into).
        """
        book = self.get_orderbook(token_id)
        return book.get("total_ask_size", 0.0)
