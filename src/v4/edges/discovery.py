"""
Edge Source: Market Discovery & Filter Tuning
================================================
Loosens filters to find tradeable markets we're missing.
Scans broader, scores everything, and surfaces the best opportunities
for manual review or automated trading.

This module is the "wide net" — scan everything, score it, let the 
human or the edge detector decide what to trade.
"""

import requests
import json
import time
from datetime import datetime, timezone
from typing import List, Dict, Optional
from dataclasses import dataclass


GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"


@dataclass 
class DiscoveredMarket:
    """A market found during broad scanning."""
    slug: str
    question: str
    category: str
    
    # Pricing
    outcomes: List[str]
    prices: List[float]
    tokens: List[str]
    
    # Metrics
    volume_24h: float
    volume_total: float
    liquidity_score: float
    
    # Time
    hours_to_resolution: float
    end_date: str
    
    # Why it's interesting
    interest_reasons: List[str]
    
    # Market structure
    price_sum: float             # YES + NO (>1.0 = overpriced)
    spread_pct: float
    
    condition_id: str = ""
    neg_risk: bool = False
    tick_size: str = "0.01"


class MarketDiscovery:
    """Broad market scanner with loose filters for opportunity discovery."""
    
    def __init__(self):
        self.all_markets: List[DiscoveredMarket] = []
        self.last_full_scan = 0
        self.scan_interval = 1800  # Full scan every 30 min
    
    def full_scan(self, limit: int = 500) -> List[DiscoveredMarket]:
        """
        Scan ALL active markets on Polymarket with minimal filtering.
        Score and rank everything.
        """
        print(f"🔍 Full market discovery scan (limit {limit})...")
        
        markets = []
        offset = 0
        page_size = 100
        
        while offset < limit:
            try:
                r = requests.get(
                    f"{GAMMA_API}/events",
                    params={
                        "active": True,
                        "closed": False,
                        "limit": page_size,
                        "offset": offset,
                        "order": "volume24hr",
                        "ascending": False,
                    },
                    timeout=10
                )
                events = r.json()
                if not events:
                    break
                
                for event in events:
                    for market in event.get("markets", []):
                        discovered = self._process_market(event, market)
                        if discovered:
                            markets.append(discovered)
                
                offset += page_size
                time.sleep(0.5)  # Rate limit
                
            except Exception as e:
                print(f"  Error at offset {offset}: {e}")
                break
        
        # Sort by interest score
        markets.sort(key=lambda m: self._interest_score(m), reverse=True)
        
        self.all_markets = markets
        self.last_full_scan = time.time()
        
        print(f"  Found {len(markets)} markets")
        return markets
    
    def _process_market(self, event: dict, market: dict) -> Optional[DiscoveredMarket]:
        """Process a single market from the API response."""
        try:
            slug = event.get("slug", "")
            question = market.get("question", "")
            category = event.get("category", "").lower()
            
            # Skip 5-min binary crypto
            if "updown-5m" in slug or "updown-1m" in slug:
                return None
            
            outcomes = json.loads(market["outcomes"]) if isinstance(market.get("outcomes", ""), str) else market.get("outcomes", [])
            prices = json.loads(market["outcomePrices"]) if isinstance(market.get("outcomePrices", ""), str) else market.get("outcomePrices", [])
            tokens = json.loads(market["clobTokenIds"]) if isinstance(market.get("clobTokenIds", ""), str) else market.get("clobTokenIds", [])
            
            if not outcomes or not prices or not tokens:
                return None
            
            prices = [float(p) for p in prices]
            price_sum = sum(prices)
            
            volume_24h = float(market.get("volume24hr", 0) or 0)
            volume_total = float(market.get("volume", 0) or 0)
            
            # Time to resolution
            end_date = market.get("endDate") or market.get("end_date_iso") or ""
            hours = self._hours_to_resolution(end_date)
            
            # Interest reasons
            reasons = []
            if price_sum > 1.03:
                reasons.append(f"overpriced: YES+NO=${price_sum:.3f}")
            if volume_24h > 100000:
                reasons.append(f"high_volume: ${volume_24h:,.0f}")
            if any(0.4 < p < 0.6 for p in prices):
                reasons.append("uncertain: price near 50/50")
            if hours and 24 < hours < 168:
                reasons.append(f"sweet_spot: {hours:.0f}h to resolution")
            if category in ("politics", "economics"):
                reasons.append(f"data_driven: {category}")
            
            return DiscoveredMarket(
                slug=slug,
                question=question,
                category=category,
                outcomes=outcomes,
                prices=prices,
                tokens=tokens,
                volume_24h=volume_24h,
                volume_total=volume_total,
                liquidity_score=0,  # Filled later if we check book
                hours_to_resolution=hours or 0,
                end_date=end_date,
                interest_reasons=reasons,
                price_sum=price_sum,
                spread_pct=0,
                condition_id=market.get("conditionId", ""),
                neg_risk=market.get("negRisk", False),
                tick_size=market.get("minimumTickSize", "0.01"),
            )
        except:
            return None
    
    def _hours_to_resolution(self, end_date: str) -> Optional[float]:
        """Calculate hours until resolution."""
        if not end_date:
            return None
        try:
            if end_date.endswith("Z"):
                end = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            else:
                end = datetime.fromisoformat(end_date)
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            delta = (end - datetime.now(timezone.utc)).total_seconds() / 3600
            return max(delta, 0)
        except:
            return None
    
    def _interest_score(self, market: DiscoveredMarket) -> float:
        """Score how interesting/tradeable a market is."""
        score = 0
        
        # Volume
        if market.volume_24h > 100000: score += 3
        elif market.volume_24h > 10000: score += 2
        elif market.volume_24h > 1000: score += 1
        
        # Price uncertainty (near 50/50 = more opportunity)
        for p in market.prices:
            if 0.35 < p < 0.65:
                score += 2
                break
        
        # Overpricing
        if market.price_sum > 1.04:
            score += 2
        
        # Time horizon sweet spot
        if 24 < market.hours_to_resolution < 336:
            score += 1
        
        # Data-driven categories
        if market.category in ("politics", "economics"):
            score += 2
        elif market.category in ("sports",):
            score += 1
        
        # Interest reasons count
        score += len(market.interest_reasons) * 0.5
        
        return score
    
    def get_top_opportunities(self, n: int = 20) -> List[DiscoveredMarket]:
        """Get top N most interesting markets."""
        if not self.all_markets or time.time() - self.last_full_scan > self.scan_interval:
            self.full_scan()
        
        return self.all_markets[:n]
    
    def print_report(self, n: int = 20):
        """Print a human-readable report of top opportunities."""
        markets = self.get_top_opportunities(n)
        
        print(f"\n{'='*80}")
        print(f"📊 MARKET DISCOVERY REPORT — Top {len(markets)} Opportunities")
        print(f"{'='*80}\n")
        
        for i, m in enumerate(markets):
            score = self._interest_score(m)
            print(f"{i+1}. [{score:.1f}] {m.question[:70]}")
            print(f"   {m.category or 'uncategorized'} | "
                  f"Vol 24h: ${m.volume_24h:,.0f} | "
                  f"Resolves: {m.hours_to_resolution:.0f}h")
            print(f"   Prices: {' / '.join(f'{o}={p:.2f}' for o, p in zip(m.outcomes, m.prices))} "
                  f"(sum={m.price_sum:.3f})")
            if m.interest_reasons:
                print(f"   Reasons: {', '.join(m.interest_reasons)}")
            print()
        
        # Summary stats
        categories = {}
        for m in self.all_markets:
            cat = m.category or "uncategorized"
            if cat not in categories:
                categories[cat] = 0
            categories[cat] += 1
        
        print(f"{'='*80}")
        print(f"Total markets scanned: {len(self.all_markets)}")
        print(f"Categories: {json.dumps(dict(sorted(categories.items(), key=lambda x: -x[1])[:10]), indent=2)}")
        overpriced = len([m for m in self.all_markets if m.price_sum > 1.03])
        uncertain = len([m for m in self.all_markets if any(0.4 < p < 0.6 for p in m.prices)])
        print(f"Overpriced (>3%): {overpriced}")
        print(f"Uncertain (near 50/50): {uncertain}")
        print(f"{'='*80}")
