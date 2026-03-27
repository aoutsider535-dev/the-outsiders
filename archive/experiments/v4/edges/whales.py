"""
Edge Source: Whale Copy Trading
=================================
Track profitable Polymarket wallets and copy their trades.

Strategy:
1. Identify wallets with consistent profitability (>55% WR, 100+ trades)
2. Monitor their recent activity via Polymarket data API
3. When a whale enters a position, evaluate if we should follow
4. Size our position based on whale conviction (position size relative to their portfolio)

Edge thesis: Some traders have genuine information edges or superior models.
By identifying and following them with a short delay, we borrow their edge
minus execution lag.
"""

import requests
import json
import time
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Tuple
from dataclasses import dataclass, field


@dataclass
class WhaleProfile:
    """Profile of a tracked profitable wallet."""
    address: str
    label: str                    # Human-readable name
    total_trades: int = 0
    win_rate: float = 0.0
    total_pnl: float = 0.0
    avg_position_size: float = 0.0
    last_seen: str = ""
    categories: List[str] = field(default_factory=list)  # What they trade
    trust_score: float = 0.0      # 0-1, based on track record


@dataclass
class WhaleSignal:
    """A detected whale trade to potentially copy."""
    whale: WhaleProfile
    market_slug: str
    question: str
    side: str                     # "YES" or "NO" / outcome name
    outcome_idx: int
    token_id: str
    price_at_entry: float
    position_size_usd: float
    timestamp: str
    
    # Our analysis
    conviction: float             # How much of their portfolio this represents
    delay_seconds: float          # How long since they traded


# ═══════════════════════════════════════════════════════════
# KNOWN PROFITABLE WALLETS
# ═══════════════════════════════════════════════════════════
# These can be discovered via:
# 1. Polymarket leaderboard API
# 2. On-chain analysis of high-volume traders
# 3. Manual research

TRACKED_WHALES = [
    WhaleProfile(
        address="0x751a2b86c6fd9a06fa1c0e5c2d710ba65a4bfac0",
        label="Sharky6999",
        trust_score=0.9,  # Proven $9,639 profit, ~100% WR on sniper trades
        categories=["crypto_5min"],
    ),
    # Add more whales as discovered
    # WhaleProfile(
    #     address="0x...",
    #     label="whale_2",
    #     trust_score=0.7,
    # ),
]


class WhaleEdge:
    """Track and copy profitable Polymarket whales."""
    
    def __init__(self, whales: List[WhaleProfile] = None):
        self.whales = whales or TRACKED_WHALES
        self.recent_signals: List[WhaleSignal] = []
        self.last_check: Dict[str, float] = {}  # address -> timestamp
        self.check_interval = 60  # Check each whale every 60s
        self.seen_trades: set = set()  # (address, market_slug, side) to avoid duplicates
    
    def scan_whale_activity(self) -> List[WhaleSignal]:
        """
        Check all tracked whales for recent trades.
        Returns new signals since last check.
        """
        new_signals = []
        
        for whale in self.whales:
            # Rate limit
            last = self.last_check.get(whale.address, 0)
            if time.time() - last < self.check_interval:
                continue
            
            trades = self._fetch_recent_trades(whale.address)
            self.last_check[whale.address] = time.time()
            
            for trade in trades:
                signal = self._evaluate_whale_trade(whale, trade)
                if signal:
                    # Dedup
                    key = (whale.address, signal.market_slug, signal.side)
                    if key not in self.seen_trades:
                        self.seen_trades.add(key)
                        new_signals.append(signal)
        
        self.recent_signals.extend(new_signals)
        
        # Trim old signals
        cutoff = time.time() - 3600  # Keep last hour
        self.recent_signals = [
            s for s in self.recent_signals 
            if not s.timestamp or datetime.fromisoformat(s.timestamp).timestamp() > cutoff
        ]
        
        return new_signals
    
    def analyze_market(self, question: str, outcomes: List[str],
                       prices: List[float], slug: str = "",
                       condition_id: str = "") -> Optional[dict]:
        """
        Check if any tracked whale has recently traded this market.
        Returns edge estimate if a trusted whale has a position.
        """
        # Check recent signals for this market
        matching = [s for s in self.recent_signals if s.market_slug == slug]
        
        if not matching:
            # Do a fresh scan for this specific market
            for whale in self.whales:
                trades = self._fetch_market_trades(condition_id, whale.address)
                for trade in trades:
                    signal = self._evaluate_whale_trade(whale, trade)
                    if signal:
                        matching.append(signal)
        
        if not matching:
            return None
        
        # Use highest trust whale signal
        best = max(matching, key=lambda s: s.whale.trust_score * s.conviction)
        
        # Calculate edge based on whale trust + conviction
        # A trusted whale (0.9) with high conviction (0.5) → 0.45 raw edge
        # We cap and adjust for our uncertainty
        raw_edge = best.whale.trust_score * best.conviction * 0.15
        
        # Decay edge based on delay (fresher = better)
        delay_decay = max(0, 1 - (best.delay_seconds / 300))  # Full decay after 5 min
        edge = raw_edge * delay_decay
        
        if edge < 0.03:
            return None
        
        # Estimate probability
        # If whale buys YES at $0.60 with high conviction, we estimate YES > 60%
        our_prob = best.price_at_entry + edge
        our_prob = max(0.05, min(0.95, our_prob))
        
        return {
            "probability": our_prob,
            "confidence": best.whale.trust_score * delay_decay,
            "edge": edge,
            "ev": edge - 0.02,
            "outcome_idx": best.outcome_idx,
            "thesis": f"Whale '{best.whale.label}' (trust {best.whale.trust_score:.1f}) "
                     f"bought {best.side} at ${best.price_at_entry:.3f} "
                     f"(${best.position_size_usd:.0f}, {best.delay_seconds:.0f}s ago). "
                     f"Copying with {edge*100:.1f}% estimated edge.",
            "sources": ["whale_copy", f"whale_{best.whale.label}"],
        }
    
    def _fetch_recent_trades(self, address: str, limit: int = 20) -> List[dict]:
        """Fetch recent trades for a wallet from Polymarket data API."""
        try:
            r = requests.get(
                f"https://data-api.polymarket.com/activity",
                params={
                    "user": address,
                    "limit": limit,
                    "type": "TRADE",
                },
                timeout=5
            )
            return r.json() if r.status_code == 200 else []
        except:
            return []
    
    def _fetch_market_trades(self, condition_id: str, address: str) -> List[dict]:
        """Fetch trades for a specific market by a specific wallet."""
        if not condition_id:
            return []
        try:
            r = requests.get(
                f"https://data-api.polymarket.com/activity",
                params={
                    "user": address,
                    "limit": 10,
                    "type": "TRADE",
                },
                timeout=5
            )
            trades = r.json() if r.status_code == 200 else []
            # Filter to matching market
            return [t for t in trades if t.get("conditionId") == condition_id]
        except:
            return []
    
    def _evaluate_whale_trade(self, whale: WhaleProfile, trade: dict) -> Optional[WhaleSignal]:
        """Evaluate a single whale trade for copy potential."""
        try:
            slug = trade.get("eventSlug", "")
            if not slug:
                return None
            
            # Parse trade details
            side = trade.get("side", "")  # BUY or SELL
            if side != "BUY":
                return None  # Only copy entries, not exits
            
            outcome = trade.get("outcome", "")
            price = float(trade.get("price", 0))
            size = float(trade.get("size", 0))
            usd_value = price * size
            
            # Skip tiny trades
            if usd_value < 10:
                return None
            
            # Calculate delay
            trade_ts = trade.get("timestamp", "")
            delay = 0
            if trade_ts:
                try:
                    if isinstance(trade_ts, (int, float)):
                        trade_time = datetime.fromtimestamp(trade_ts / 1000, tz=timezone.utc)
                    else:
                        trade_time = datetime.fromisoformat(str(trade_ts).replace("Z", "+00:00"))
                    delay = (datetime.now(timezone.utc) - trade_time).total_seconds()
                except:
                    delay = 300  # Default 5 min if can't parse
            
            # Skip stale signals (>10 min old)
            if delay > 600:
                return None
            
            # Determine outcome index
            outcome_idx = 0 if outcome.lower() in ("yes", "up", "over") else 1
            
            # Conviction = position size relative to typical
            conviction = min(usd_value / max(whale.avg_position_size, 50), 1.0)
            
            return WhaleSignal(
                whale=whale,
                market_slug=slug,
                question=trade.get("eventTitle", trade.get("title", slug)),
                side=outcome,
                outcome_idx=outcome_idx,
                token_id=trade.get("tokenId", ""),
                price_at_entry=price,
                position_size_usd=usd_value,
                timestamp=str(trade_ts),
                conviction=conviction,
                delay_seconds=delay,
            )
        except:
            return None
    
    def discover_whales(self, min_trades: int = 50, min_wr: float = 0.55) -> List[WhaleProfile]:
        """
        Discover new profitable wallets by scanning recent market activity.
        
        Approach: Look at recent resolved markets, find wallets that were 
        on the winning side consistently.
        
        This is expensive (many API calls) — run periodically, not every cycle.
        """
        # TODO: Implement whale discovery
        # 1. Fetch recent resolved markets from Gamma API
        # 2. For each, fetch trades from data-api
        # 3. Aggregate by wallet: count wins/losses
        # 4. Filter by min_trades and min_wr
        # 5. Return new WhaleProfile objects
        
        print("Whale discovery not yet implemented — add wallets manually to TRACKED_WHALES")
        return []
