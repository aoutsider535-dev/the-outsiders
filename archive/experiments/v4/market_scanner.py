"""
Component 1 — Market Selection Engine
======================================
Scans Polymarket for tradeable markets that meet our criteria:
- Sufficient liquidity (book depth, spread, volume)
- Appropriate time horizon (1 hour to 30 days)
- Categories where AI can have information edge
- Not dominated by sophisticated quant firms (avoid 5-min crypto)

Usage:
    scanner = MarketScanner()
    markets = scanner.scan()  # Returns list of MarketOpportunity
"""

import requests
import json
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import List, Optional, Dict
from .config import *


@dataclass
class MarketOpportunity:
    """A market that passes our selection filters."""
    # Identity
    condition_id: str
    question: str
    slug: str
    category: str
    
    # Pricing
    outcomes: List[str]           # e.g., ["Yes", "No"]
    prices: List[float]           # e.g., [0.65, 0.35]
    tokens: List[str]             # CLOB token IDs
    
    # Liquidity
    bid_depth_usd: float          # Total bid-side liquidity
    ask_depth_usd: float          # Total ask-side liquidity
    spread: float                 # Best ask - best bid
    spread_pct: float             # Spread as % of mid price
    volume_24h: float             # 24h trading volume
    
    # Timing
    end_date: Optional[str]       # Resolution date/time
    hours_to_resolution: float    # Hours until market resolves
    
    # Metadata
    tick_size: str = "0.01"
    neg_risk: bool = False
    market_id: str = ""
    
    # Scoring
    liquidity_score: float = 0.0  # 0-1, higher = more liquid
    edge_potential: float = 0.0   # Estimated information asymmetry potential


class MarketScanner:
    """Scans Polymarket for tradeable opportunities."""
    
    def __init__(self):
        self.cache = {}           # slug -> MarketOpportunity (TTL 5 min)
        self.cache_ts = {}        # slug -> timestamp
        self.scan_history = []    # Track what we've scanned
    
    def scan(self, limit: int = 100, category: str = None, max_book_checks: int = 20) -> List[MarketOpportunity]:
        """
        Scan Polymarket for markets meeting our criteria.
        
        Returns markets sorted by edge_potential (best first).
        max_book_checks limits expensive CLOB API calls.
        """
        raw_markets = self._fetch_active_markets(limit=limit)
        
        # Pre-filter before expensive book checks
        pre_filtered = []
        for market in raw_markets:
            slug = market.get("_event_slug", "")
            if any(p in slug for p in AVOID_PATTERNS):
                continue
            volume_24h = float(market.get("volume24hr", 0) or 0)
            if volume_24h < MIN_VOLUME_24H:
                continue
            pre_filtered.append(market)
        
        opportunities = []
        book_checks = 0
        for market in pre_filtered:
            if book_checks >= max_book_checks:
                break
            opp = self._evaluate_market(market)
            book_checks += 1  # _evaluate_market calls _check_book_depth
            time.sleep(0.5)   # Rate limit: max 2 book checks/sec
            if opp:
                opportunities.append(opp)
        
        # Sort by liquidity score × edge potential
        opportunities.sort(key=lambda o: o.liquidity_score * o.edge_potential, reverse=True)
        
        self.scan_history.append({
            "timestamp": datetime.now(PST).isoformat(),
            "scanned": len(raw_markets),
            "passed": len(opportunities),
        })
        
        return opportunities
    
    def _fetch_active_markets(self, limit: int = 100) -> List[dict]:
        """Fetch active markets from Gamma API."""
        markets = []
        try:
            # Fetch active events
            r = requests.get(
                f"{GAMMA_API}/events",
                params={
                    "active": True,
                    "closed": False,
                    "limit": limit,
                    "order": "volume24hr",
                    "ascending": False,
                },
                timeout=10
            )
            events = r.json()
            
            for event in events:
                for market in event.get("markets", []):
                    market["_event_slug"] = event.get("slug", "")
                    market["_event_title"] = event.get("title", "")
                    market["_category"] = event.get("category", "").lower()
                    markets.append(market)
        except Exception as e:
            print(f"Error fetching markets: {e}")
        
        return markets
    
    def _evaluate_market(self, market: dict) -> Optional[MarketOpportunity]:
        """
        Evaluate a single market against all selection criteria.
        Returns MarketOpportunity if it passes, None if filtered out.
        """
        slug = market.get("_event_slug", "")
        question = market.get("question", "")
        category = market.get("_category", "")
        
        # ── FILTER 1: Avoid patterns ──
        for pattern in AVOID_PATTERNS:
            if pattern in slug:
                return None
        
        # ── FILTER 2: Category check ──
        if category and category not in TARGET_CATEGORIES:
            # Allow uncategorized markets through (many aren't tagged)
            if category not in ("", "other", "unknown"):
                pass  # Don't filter strictly — Polymarket categories are inconsistent
        
        # ── FILTER 3: Time horizon ──
        end_date = market.get("endDate") or market.get("end_date_iso")
        hours_to_resolution = self._calc_hours_to_resolution(end_date)
        
        if hours_to_resolution is not None:
            if hours_to_resolution < RESOLUTION_HORIZON_MIN_HOURS:
                return None
            if hours_to_resolution > RESOLUTION_HORIZON_MAX_DAYS * 24:
                return None
        
        # ── FILTER 4: Parse market data ──
        try:
            outcomes = json.loads(market["outcomes"]) if isinstance(market["outcomes"], str) else market.get("outcomes", [])
            prices = json.loads(market["outcomePrices"]) if isinstance(market.get("outcomePrices", ""), str) else market.get("outcomePrices", [])
            tokens = json.loads(market["clobTokenIds"]) if isinstance(market.get("clobTokenIds", ""), str) else market.get("clobTokenIds", [])
        except:
            return None
        
        if not outcomes or not prices or not tokens:
            return None
        
        prices = [float(p) for p in prices]
        
        # ── FILTER 5: Liquidity check (requires book query) ──
        book_data = self._check_book_depth(tokens[0])
        if not book_data:
            return None
        
        if book_data["bid_depth"] < MIN_BOOK_DEPTH_USD:
            return None
        if book_data["ask_depth"] < MIN_BOOK_DEPTH_USD:
            return None
        if book_data["spread_pct"] > MAX_SPREAD_PCT:
            return None
        
        # ── FILTER 6: Volume ──
        volume = float(market.get("volume", 0) or 0)
        volume_24h = float(market.get("volume24hr", 0) or 0)
        if volume_24h < MIN_VOLUME_24H:
            return None
        
        # ── SCORING ──
        liquidity_score = self._calc_liquidity_score(book_data, volume_24h)
        edge_potential = self._calc_edge_potential(question, category, hours_to_resolution, prices)
        
        return MarketOpportunity(
            condition_id=market.get("conditionId", ""),
            question=question,
            slug=slug,
            category=category,
            outcomes=outcomes,
            prices=prices,
            tokens=tokens,
            bid_depth_usd=book_data["bid_depth"],
            ask_depth_usd=book_data["ask_depth"],
            spread=book_data["spread"],
            spread_pct=book_data["spread_pct"],
            volume_24h=volume_24h,
            end_date=end_date,
            hours_to_resolution=hours_to_resolution or 0,
            tick_size=market.get("minimumTickSize", "0.01"),
            neg_risk=market.get("negRisk", False),
            market_id=market.get("id", ""),
            liquidity_score=liquidity_score,
            edge_potential=edge_potential,
        )
    
    def _check_book_depth(self, token_id: str) -> Optional[dict]:
        """Check order book depth for a token. Returns liquidity metrics."""
        try:
            r = requests.get(f"{CLOB_API}/book?token_id={token_id}", timeout=3)
            book = r.json()
            
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            
            if not bids or not asks:
                return None
            
            best_bid = max(float(b["price"]) for b in bids)
            best_ask = min(float(a["price"]) for a in asks)
            mid = (best_bid + best_ask) / 2
            spread = best_ask - best_bid
            
            bid_depth = sum(float(b["price"]) * float(b["size"]) for b in bids)
            ask_depth = sum(float(a["price"]) * float(a["size"]) for a in asks)
            
            return {
                "best_bid": best_bid,
                "best_ask": best_ask,
                "mid": mid,
                "spread": spread,
                "spread_pct": spread / mid if mid > 0 else 1.0,
                "bid_depth": bid_depth,
                "ask_depth": ask_depth,
            }
        except:
            return None
    
    def _calc_hours_to_resolution(self, end_date: Optional[str]) -> Optional[float]:
        """Calculate hours until market resolves."""
        if not end_date:
            return None
        try:
            if end_date.endswith("Z"):
                end = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            else:
                end = datetime.fromisoformat(end_date)
            
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            
            now = datetime.now(timezone.utc)
            delta = (end - now).total_seconds() / 3600
            return max(delta, 0)
        except:
            return None
    
    def _calc_liquidity_score(self, book_data: dict, volume_24h: float) -> float:
        """Score 0-1 based on how liquid/tradeable this market is."""
        score = 0.0
        
        # Spread score (tighter = better)
        if book_data["spread_pct"] < 0.01:
            score += 0.4
        elif book_data["spread_pct"] < 0.03:
            score += 0.3
        elif book_data["spread_pct"] < 0.05:
            score += 0.2
        
        # Depth score
        min_depth = min(book_data["bid_depth"], book_data["ask_depth"])
        if min_depth > 500:
            score += 0.3
        elif min_depth > 200:
            score += 0.2
        elif min_depth > 50:
            score += 0.1
        
        # Volume score
        if volume_24h > 10000:
            score += 0.3
        elif volume_24h > 2000:
            score += 0.2
        elif volume_24h > 500:
            score += 0.1
        
        return min(score, 1.0)
    
    def _calc_edge_potential(self, question: str, category: str,
                            hours_to_resolution: Optional[float],
                            prices: List[float]) -> float:
        """
        Estimate information asymmetry potential.
        Higher score = more likely we can find an edge.
        """
        score = 0.0
        q_lower = question.lower()
        
        # ── News-processable events ──
        news_keywords = ["announce", "sign", "approve", "pass", "vote", "elect",
                        "launch", "release", "report", "decision", "ruling"]
        if any(kw in q_lower for kw in news_keywords):
            score += 0.3
        
        # ── Data-driven markets (scheduled events) ──
        data_keywords = ["jobs", "gdp", "inflation", "cpi", "fed", "rate",
                        "earnings", "poll", "survey", "forecast"]
        if any(kw in q_lower for kw in data_keywords):
            score += 0.4  # Best edge — public data we can model
        
        # ── Political markets ──
        if category == "politics" or any(kw in q_lower for kw in ["trump", "biden", "congress", "senate", "election"]):
            score += 0.2  # Polling data available
        
        # ── Price near 50/50 = more room for edge ──
        if prices:
            max_p = max(prices)
            min_p = min(prices)
            if 0.35 < max_p < 0.65:
                score += 0.2  # Market is uncertain = opportunity
        
        # ── Time horizon sweet spot (1-14 days) ──
        if hours_to_resolution:
            if 24 < hours_to_resolution < 336:  # 1-14 days
                score += 0.2  # Best horizon for information edge
            elif hours_to_resolution < 24:
                score += 0.0  # Too fast, informed traders dominate
        
        # ── Crypto price markets = zero edge for us ──
        crypto_keywords = ["bitcoin", "btc", "ethereum", "eth", "solana", "sol",
                          "price above", "price below", "close above", "close below"]
        if any(kw in q_lower for kw in crypto_keywords):
            score -= 0.3  # Quant firms dominate these
        
        return max(score, 0.0)
