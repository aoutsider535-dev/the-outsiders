"""
🔁 Streak Reversal Strategy

Fades DOWN streaks of 3+ (bets UP after 3+ consecutive down outcomes).
Based on mean reversion of BTC 5-min outcomes — after sustained selling,
buyers step in.

OOS validated: 65.2% WR on 46 trades, +$63.91
Enhanced with HTF: 68.2% WR when 1hr trend is bullish

Signal:
  - Track consecutive UP/DOWN outcomes
  - After 3+ DOWN → bet UP (fade the streak)  
  - Optional HTF filter: only when 1hr trend agrees (bullish)
"""
from dataclasses import dataclass, asdict
from typing import Optional


DEFAULTS = {
    "min_streak": 3,           # Minimum consecutive downs before fading
    "min_edge_pct": 3.0,       # Lower threshold — the streak IS the edge
    "take_profit_pct": 55.0,
    "stop_loss_pct": 30.0,
    "use_htf_filter": True,    # Require 1hr bullish for UP bets
}


@dataclass
class Signal:
    timestamp: int = 0
    direction: str = ""
    confidence: float = 0.0
    edge_pct: float = 0.0
    market_up_price: float = 0.0
    market_down_price: float = 0.0
    streak_length: int = 0
    streak_direction: str = ""
    htf_agrees: bool = False
    should_trade: bool = False
    reason: str = ""

    def to_dict(self):
        return asdict(self)


# Module-level streak tracking (persists across calls within same process)
_streak_state = {"direction": None, "count": 0}


def update_outcome(outcome):
    """Call this after each market resolves to update the streak counter."""
    if outcome == _streak_state["direction"]:
        _streak_state["count"] += 1
    else:
        _streak_state["direction"] = outcome
        _streak_state["count"] = 1


def get_streak():
    """Get current streak state."""
    return _streak_state["direction"], _streak_state["count"]


def generate_signal(market_up_price=0.5, market_down_price=0.5,
                    htf_context=None, params=None, **kwargs):
    """Generate Streak Reversal signal."""
    params = params or DEFAULTS.copy()
    sig = Signal(market_up_price=market_up_price, market_down_price=market_down_price)
    
    streak_dir, streak_count = get_streak()
    sig.streak_length = streak_count
    sig.streak_direction = streak_dir or "none"
    
    # Only fade DOWN streaks (bet UP)
    if streak_dir != "down" or streak_count < params["min_streak"]:
        sig.reason = (f"⏸️ Streak: {streak_dir or 'none'} ×{streak_count} "
                      f"(need down ×{params['min_streak']}+)")
        return sig
    
    # Signal: bet UP (fade the down streak)
    sig.direction = "up"
    
    # Confidence scales with streak length
    base_conf = 0.55 + min(0.15, (streak_count - params["min_streak"]) * 0.05)
    sig.confidence = base_conf
    
    # HTF filter
    if params.get("use_htf_filter") and htf_context:
        sig.htf_agrees = htf_context.get("h1_bullish", False)
        if not sig.htf_agrees:
            sig.reason = (f"⏸️ Streak: DOWN ×{streak_count} but 1H bearish "
                          f"({htf_context.get('h1_trend', 0):+.2f})")
            return sig
        # Boost confidence when HTF agrees
        sig.confidence += 0.03
    
    # Edge
    sig.edge_pct = (sig.confidence - market_up_price) * 100
    
    if sig.edge_pct < params["min_edge_pct"]:
        sig.reason = (f"⏸️ Streak: DOWN ×{streak_count} but edge {sig.edge_pct:.1f}% "
                      f"< {params['min_edge_pct']}%")
        return sig
    
    sig.should_trade = True
    sig.reason = (f"🟢 STREAK FADE: DOWN ×{streak_count} → BUY UP @ "
                  f"${market_up_price:.3f} | Conf: {sig.confidence:.1%}")
    return sig


def format_signal_message(sig):
    if sig.should_trade:
        htf_tag = " [HTF✓]" if sig.htf_agrees else ""
        return f"{sig.reason}{htf_tag}"
    return (f"{sig.reason}\n"
            f"  Streak: {sig.streak_direction} ×{sig.streak_length}")
