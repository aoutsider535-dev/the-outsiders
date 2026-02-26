"""
🎯 Combined Streak + Volume Flow Strategy

High-conviction, low-frequency strategy combining two signals:
1. DOWN streak of 3+ (mean reversion pressure)
2. Volume flow fading (recent volume surge was bearish → exhaustion)

Only fires when BOTH conditions align. The sniper.

OOS validated: 81.2% WR on 16 trades, +$47.75
With HTF: 83.3% WR on 6 trades (too few, use as boost not filter)
"""
from dataclasses import dataclass, asdict


DEFAULTS = {
    "min_edge_pct": 3.0,
    "take_profit_pct": 55.0,
    "stop_loss_pct": 30.0,
    "min_streak": 3,
    "min_vol_ratio": 1.2,      # Lower than pure vol flow (1.5) — streak provides conviction
    "vol_lookback_recent": 3,   # Recent candles for volume
    "vol_lookback_baseline": 7, # Baseline candles
    "use_htf_boost": True,      # Boost confidence (not filter) when HTF agrees
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
    vol_ratio: float = 0.0
    vol_flow_direction: float = 0.0
    htf_agrees: bool = False
    should_trade: bool = False
    reason: str = ""

    def to_dict(self):
        return asdict(self)


def generate_signal(candles, market_up_price=0.5, market_down_price=0.5,
                    streak_state=None, htf_context=None, params=None, **kwargs):
    """Generate Combined Streak + Vol Flow signal."""
    params = params or DEFAULTS.copy()
    sig = Signal(market_up_price=market_up_price, market_down_price=market_down_price)
    
    # Get streak state from streak_reversal module
    if streak_state is None:
        from src.strategies.streak_reversal import get_streak
        streak_dir, streak_count = get_streak()
    else:
        streak_dir, streak_count = streak_state
    
    sig.streak_length = streak_count
    
    # Condition 1: DOWN streak of 3+
    if streak_dir != "down" or streak_count < params["min_streak"]:
        sig.reason = (f"⏸️ Combo: Streak {streak_dir or 'none'} ×{streak_count} "
                      f"(need down ×{params['min_streak']}+)")
        return sig
    
    # Condition 2: Volume flow is bearish (which we'll fade)
    if not candles or len(candles) < 10:
        sig.reason = f"⏸️ Combo: Need 10+ candles, have {len(candles) if candles else 0}"
        return sig
    
    recent = candles[-params["vol_lookback_recent"]:]
    baseline = candles[-(params["vol_lookback_recent"] + params["vol_lookback_baseline"]):-params["vol_lookback_recent"]]
    
    if not baseline:
        sig.reason = "⏸️ Combo: Insufficient candle history for volume baseline"
        return sig
    
    avg_recent = sum(c.get("volume", 0) for c in recent) / len(recent) if recent else 0
    avg_baseline = sum(c.get("volume", 0) for c in baseline) / len(baseline) if baseline else 1
    
    sig.vol_ratio = avg_recent / avg_baseline if avg_baseline > 0 else 1
    
    if sig.vol_ratio < params["min_vol_ratio"]:
        sig.reason = (f"⏸️ Combo: DOWN ×{streak_count} but vol ratio {sig.vol_ratio:.2f}x "
                      f"< {params['min_vol_ratio']}x")
        return sig
    
    # Check volume flow direction (must be bearish — we're fading it)
    flow = 0
    for c in recent:
        body = c.get("close", 0) - c.get("open", 0)
        c_range = c.get("high", 0) - c.get("low", 0)
        if c_range > 0:
            strength = body / c_range
            vol_w = c.get("volume", 0) / avg_baseline if avg_baseline > 0 else 1
            flow += strength * vol_w
    
    sig.vol_flow_direction = flow
    
    if flow >= 0:
        sig.reason = (f"⏸️ Combo: DOWN ×{streak_count} but vol flow bullish ({flow:+.2f})")
        return sig
    
    # Both conditions met: DOWN streak + bearish volume exhaustion → BUY UP
    sig.direction = "up"
    sig.confidence = 0.58 + min(0.12, (streak_count - params["min_streak"]) * 0.03)
    
    # HTF boost (not filter — too few trades to filter)
    if params.get("use_htf_boost") and htf_context:
        sig.htf_agrees = htf_context.get("h1_bullish", False)
        if sig.htf_agrees:
            sig.confidence += 0.03
    
    # Edge
    sig.edge_pct = (sig.confidence - market_up_price) * 100
    
    if sig.edge_pct < params["min_edge_pct"]:
        sig.reason = (f"⏸️ Combo: DOWN ×{streak_count} + vol fade but edge {sig.edge_pct:.1f}%")
        return sig
    
    sig.should_trade = True
    htf_tag = " [HTF✓]" if sig.htf_agrees else ""
    sig.reason = (f"🟢 COMBO: DOWN ×{streak_count} + Vol Fade → BUY UP @ "
                  f"${market_up_price:.3f}{htf_tag}")
    return sig


def format_signal_message(sig):
    if sig.should_trade:
        return sig.reason
    return (f"{sig.reason}\n"
            f"  Streak: down ×{sig.streak_length} | Vol: {sig.vol_ratio:.2f}x | "
            f"Flow: {sig.vol_flow_direction:+.2f}")
