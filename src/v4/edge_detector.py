"""
Component 2 — Probabilistic Edge Detection
=============================================
Estimates "fair probability" for market outcomes and identifies
when market price diverges enough to trade profitably.

Edge sources:
1. Base rate analysis (historical frequency of similar events)
2. News sentiment aggregation
3. Statistical models (polls, data releases)
4. Market structure (YES+NO > $1.00 overpricing)

Only trades when: |our_estimate - market_price| > MIN_EDGE_PCT
"""

import requests
import json
import math
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Optional, List, Dict, Tuple
from .config import *
from .market_scanner import MarketOpportunity


@dataclass
class EdgeSignal:
    """A detected trading opportunity with quantified edge."""
    # Market
    market: MarketOpportunity
    
    # Our estimate
    our_probability: float        # Our estimate of true probability (0-1)
    confidence: float             # How confident we are in our estimate (0-1)
    
    # Edge
    market_price: float           # Current market price
    edge_pct: float              # our_probability - market_price (positive = underpriced)
    expected_value: float         # EV per dollar risked
    
    # Trade direction
    side: str                     # "YES" or "NO" (which outcome to buy)
    outcome_idx: int              # Index into market.outcomes
    token_id: str                 # CLOB token to trade
    
    # Thesis
    thesis: str                   # Human-readable explanation of edge
    edge_sources: List[str]       # What data supports this edge
    
    # Sizing
    kelly_fraction: float = 0.0   # Optimal Kelly bet size (fraction of bankroll)
    recommended_size: float = 0.0 # Actual recommended position in USD


class EdgeDetector:
    """Detects probabilistic edges in Polymarket markets."""
    
    def __init__(self):
        self.bankroll = INITIAL_BANKROLL
        
        # Initialize edge source plugins
        self.sports_edge = None
        self.news_edge = None
        self.whale_edge = None
        
        try:
            from .edges.sports import SportsEdge
            self.sports_edge = SportsEdge()
        except Exception as e:
            print(f"Sports edge not loaded: {e}")
        
        try:
            from .edges.news import NewsEdge
            self.news_edge = NewsEdge()
        except Exception as e:
            print(f"News edge not loaded: {e}")
        
        try:
            from .edges.whales import WhaleEdge
            self.whale_edge = WhaleEdge()
        except Exception as e:
            print(f"Whale edge not loaded: {e}")
    
    def evaluate(self, market: MarketOpportunity) -> Optional[EdgeSignal]:
        """
        Evaluate a market for tradeable edge.
        Returns EdgeSignal if edge > MIN_EDGE_PCT, None otherwise.
        """
        # Run all edge detection methods
        estimates = []
        
        # Method 1: Market structure (YES+NO overpricing)
        structure_edge = self._check_market_structure(market)
        if structure_edge:
            estimates.append(structure_edge)
        
        # Method 2: Base rate analysis
        base_rate = self._estimate_base_rate(market)
        if base_rate:
            estimates.append(base_rate)
        
        # Method 3: Sports statistical model
        if self.sports_edge:
            try:
                sports = self.sports_edge.analyze_market(
                    market.question, market.outcomes, market.prices)
                if sports and sports.get("probability"):
                    estimates.append(sports)
            except:
                pass
        
        # Method 4: News sentiment
        if self.news_edge:
            try:
                news = self.news_edge.analyze_market(
                    market.question, market.outcomes, market.prices, market.slug)
                if news and news.get("probability"):
                    estimates.append(news)
            except:
                pass
        
        # Method 5: Whale copy trading
        if self.whale_edge:
            try:
                whale = self.whale_edge.analyze_market(
                    market.question, market.outcomes, market.prices,
                    market.slug, market.condition_id)
                if whale and whale.get("probability"):
                    estimates.append(whale)
            except:
                pass
        
        if not estimates:
            return None
        
        # Combine estimates (weighted by confidence)
        best = max(estimates, key=lambda e: abs(e["edge"]) * e["confidence"])
        
        if abs(best["edge"]) < MIN_EDGE_PCT:
            return None
        
        if best["confidence"] < CONFIDENCE_FLOOR:
            return None
        
        # Calculate Kelly sizing
        kelly = self._calc_kelly(best["probability"], market.prices[best["outcome_idx"]])
        recommended = self._calc_position_size(kelly)
        
        return EdgeSignal(
            market=market,
            our_probability=best["probability"],
            confidence=best["confidence"],
            market_price=market.prices[best["outcome_idx"]],
            edge_pct=best["edge"],
            expected_value=best["ev"],
            side=market.outcomes[best["outcome_idx"]],
            outcome_idx=best["outcome_idx"],
            token_id=market.tokens[best["outcome_idx"]],
            thesis=best["thesis"],
            edge_sources=best["sources"],
            kelly_fraction=kelly,
            recommended_size=recommended,
        )
    
    def _check_market_structure(self, market: MarketOpportunity) -> Optional[dict]:
        """
        Check for YES+NO overpricing.
        On Polymarket, YES + NO often sums to >$1.00 due to fees/spread.
        If sum > $1.04, there's a structural edge in selling the overpriced side.
        """
        if len(market.prices) != 2:
            return None
        
        price_sum = sum(market.prices)
        overpricing = price_sum - 1.0
        
        if overpricing < 0.03:  # Need at least 3% overpricing
            return None
        
        # The overpriced side is the one we'd SHORT (buy NO against it)
        # Find which side is more overpriced relative to fair value
        # If YES = 0.55 and NO = 0.50, sum = 1.05
        # Fair values would be ~0.524 and 0.476
        # YES is overpriced by 0.026, NO by 0.024
        
        fair_yes = market.prices[0] / price_sum
        fair_no = market.prices[1] / price_sum
        
        yes_overpriced = market.prices[0] - fair_yes
        no_overpriced = market.prices[1] - fair_no
        
        if yes_overpriced > no_overpriced:
            # Buy NO (bet against YES being overpriced)
            outcome_idx = 1
            edge = no_overpriced  # We're getting NO cheap relative to fair
            probability = fair_no
        else:
            outcome_idx = 0
            edge = yes_overpriced
            probability = fair_yes
        
        # This edge is small and structural — low confidence
        return {
            "probability": probability,
            "confidence": 0.5,  # Low — this is mechanical, not informational
            "edge": overpricing / 2,  # Each side benefits by half the overpricing
            "ev": (overpricing / 2) - 0.02,  # Minus resolution fee
            "outcome_idx": outcome_idx,
            "thesis": f"YES+NO sum to ${price_sum:.3f} (overpriced by {overpricing*100:.1f}%). "
                      f"Buy {market.outcomes[outcome_idx]} at ${market.prices[outcome_idx]:.3f} "
                      f"(fair value ~${probability:.3f})",
            "sources": ["market_structure"],
        }
    
    def _estimate_base_rate(self, market: MarketOpportunity) -> Optional[dict]:
        """
        Estimate probability from historical base rates.
        This is where the real alpha lives.
        
        For v4.0: Returns a conservative estimate based on market question analysis.
        Future: Plug in real data sources (polling APIs, government data feeds, etc.)
        """
        question = market.question.lower()
        
        # ── Federal Reserve / Economic Data ──
        if any(kw in question for kw in ["fed", "federal reserve", "rate cut", "rate hike"]):
            # Could integrate CME FedWatch probabilities
            # For now, flag as high-potential but don't estimate
            return {
                "probability": None,  # Need external data
                "confidence": 0.0,
                "edge": 0,
                "ev": 0,
                "outcome_idx": 0,
                "thesis": "Fed market — needs CME FedWatch data integration",
                "sources": ["base_rate_placeholder"],
            }
        
        # ── Political with polling data ──
        if any(kw in question for kw in ["election", "win", "nominee", "poll"]):
            return {
                "probability": None,
                "confidence": 0.0,
                "edge": 0,
                "ev": 0,
                "outcome_idx": 0,
                "thesis": "Political market — needs polling data integration",
                "sources": ["base_rate_placeholder"],
            }
        
        # ── Scheduled events with clear base rates ──
        # e.g., "Will SpaceX launch before X date?" — historical launch success rate
        # e.g., "Will jobs report beat expectations?" — historical beat rate is ~60%
        if any(kw in question for kw in ["jobs report", "nonfarm", "unemployment"]):
            # Historical: jobs report beats expectations ~60% of the time
            base_prob = 0.60
            market_price_yes = market.prices[0]
            edge = base_prob - market_price_yes
            
            if abs(edge) < MIN_EDGE_PCT:
                return None
            
            outcome_idx = 0 if edge > 0 else 1
            our_prob = base_prob if edge > 0 else (1 - base_prob)
            
            return {
                "probability": our_prob,
                "confidence": 0.65,  # Moderate — base rate is real but conditions vary
                "edge": abs(edge),
                "ev": abs(edge) - 0.02,  # Minus fees
                "outcome_idx": outcome_idx,
                "thesis": f"Historical base rate: jobs report beats expectations ~60% of the time. "
                         f"Market prices YES at ${market_price_yes:.2f}. "
                         f"Edge: {abs(edge)*100:.1f}%",
                "sources": ["historical_base_rate"],
            }
        
        return None
    
    def _calc_kelly(self, our_probability: float, market_price: float) -> float:
        """
        Kelly Criterion: f* = (bp - q) / b
        where:
          b = net odds (payout per dollar risked) = (1/market_price) - 1
          p = our estimated probability of winning
          q = 1 - p
          
        Returns fraction of bankroll to bet (before applying KELLY_FRACTION).
        """
        if not our_probability or our_probability <= 0 or our_probability >= 1:
            return 0.0
        if market_price <= 0 or market_price >= 1:
            return 0.0
        
        b = (1.0 / market_price) - 1.0  # Net odds
        p = our_probability
        q = 1.0 - p
        
        # Resolution fee reduces effective payout
        fee = 0.02  # 2% on winnings
        b_after_fees = b * (1 - fee)
        
        kelly = (b_after_fees * p - q) / b_after_fees
        kelly = max(kelly, 0.0)  # Never bet negative
        
        # Apply fractional Kelly
        kelly *= KELLY_FRACTION
        
        return kelly
    
    def _calc_position_size(self, kelly_fraction: float) -> float:
        """Convert Kelly fraction to actual USD position size."""
        if kelly_fraction <= 0:
            return 0.0
        
        # Kelly says bet this fraction of bankroll
        raw_size = self.bankroll * kelly_fraction
        
        # Apply caps
        max_by_pct = self.bankroll * MAX_POSITION_PCT
        size = min(raw_size, max_by_pct, MAX_POSITION_USD)
        size = max(size, MIN_POSITION_USD) if size >= MIN_POSITION_USD else 0.0
        
        return round(size, 2)
    
    def update_bankroll(self, new_balance: float):
        """Update bankroll for Kelly calculations."""
        self.bankroll = new_balance
