"""
📈 HTF Trend Follow Strategy

Trades in the direction of the 1hr AND 4hr BTC trend.
High volume (fires on ~84% of windows), consistent 53.8% WR OOS.
The "tide" strategy — aligns 5-min bets with the macro direction.

OOS validated: 53.8% WR on 509 trades, +$130.47

Signal:
  - 1hr EMA8/EMA21 cross + RSI + momentum → composite trend score
  - 4hr trend (12hr price change)
  - Both must agree on direction (both bullish or both bearish)
"""
from dataclasses import dataclass, asdict


DEFAULTS = {
    "min_edge_pct": 2.0,       # Low threshold — HTF alignment IS the edge
    "take_profit_pct": 50.0,
    "stop_loss_pct": 30.0,
    "h1_threshold": 0.1,       # Min |h1_trend| to consider directional
    "h4_threshold": 0.05,      # Min |h4_trend| to consider directional
    "require_h4": True,        # Require 4hr agreement (53.8% WR) vs 1hr only (54.3%)
}


@dataclass
class Signal:
    timestamp: int = 0
    direction: str = ""
    confidence: float = 0.0
    edge_pct: float = 0.0
    market_up_price: float = 0.0
    market_down_price: float = 0.0
    h1_trend: float = 0.0
    h4_trend: float = 0.0
    h1_rsi: float = 50.0
    should_trade: bool = False
    reason: str = ""

    def to_dict(self):
        return asdict(self)


def generate_signal(market_up_price=0.5, market_down_price=0.5,
                    htf_context=None, params=None, **kwargs):
    """Generate HTF Trend Follow signal."""
    params = params or DEFAULTS.copy()
    sig = Signal(market_up_price=market_up_price, market_down_price=market_down_price)
    
    if not htf_context:
        sig.reason = "⏸️ HTF Trend: No HTF context available"
        return sig
    
    sig.h1_trend = htf_context.get("h1_trend", 0)
    sig.h4_trend = htf_context.get("h4_trend", 0)
    sig.h1_rsi = htf_context.get("h1_rsi", 50)
    
    h1_bull = sig.h1_trend > params["h1_threshold"]
    h1_bear = sig.h1_trend < -params["h1_threshold"]
    h4_bull = sig.h4_trend > params["h4_threshold"]
    h4_bear = sig.h4_trend < -params["h4_threshold"]
    
    # Need 1hr to be directional
    if not h1_bull and not h1_bear:
        sig.reason = (f"⏸️ HTF Trend: 1H neutral ({sig.h1_trend:+.2f}), "
                      f"need >{params['h1_threshold']} or <-{params['h1_threshold']}")
        return sig
    
    # Optionally require 4hr agreement
    if params["require_h4"]:
        if h1_bull and not h4_bull:
            sig.reason = (f"⏸️ HTF Trend: 1H bullish but 4H not ({sig.h4_trend:+.2f})")
            return sig
        if h1_bear and not h4_bear:
            sig.reason = (f"⏸️ HTF Trend: 1H bearish but 4H not ({sig.h4_trend:+.2f})")
            return sig
    
    # Direction follows HTF
    sig.direction = "up" if h1_bull else "down"
    
    # Confidence based on trend strength
    trend_strength = abs(sig.h1_trend) * 0.6 + abs(sig.h4_trend) * 0.4
    sig.confidence = 0.52 + min(0.10, trend_strength * 0.08)
    
    # Edge
    market_price = market_up_price if sig.direction == "up" else market_down_price
    sig.edge_pct = (sig.confidence - market_price) * 100
    
    if sig.edge_pct < params["min_edge_pct"]:
        sig.reason = (f"⏸️ HTF Trend: Edge {sig.edge_pct:.1f}% < {params['min_edge_pct']}% | "
                      f"1H: {sig.h1_trend:+.2f} | 4H: {sig.h4_trend:+.2f}")
        return sig
    
    sig.should_trade = True
    emoji = "🟢" if sig.direction == "up" else "🔴"
    sig.reason = (f"{emoji} HTF TREND: BUY {sig.direction.upper()} @ "
                  f"${market_price:.3f} | 1H: {sig.h1_trend:+.2f} | 4H: {sig.h4_trend:+.2f}")
    return sig


def format_signal_message(sig):
    if sig.should_trade:
        return sig.reason
    return (f"{sig.reason}\n"
            f"  1H: {sig.h1_trend:+.2f} | 4H: {sig.h4_trend:+.2f} | RSI: {sig.h1_rsi:.0f}")
