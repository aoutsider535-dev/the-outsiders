"""
Smart Money Strategy for BTC 5-Min Polymarket Markets.

Inspired by wallet analysis of profitable Polymarket traders.
Key findings from on-chain analysis:
1. Profitable traders buy the CHEAPER token (underpriced side)
2. They follow recent BTC trend but aren't rigid about direction
3. Equal sizing, high discipline
4. Edge comes from buying when odds are mispriced vs recent price action

This strategy combines:
1. Value signal — buy the cheaper side when it's "too cheap" relative to BTC action
2. Recent trend alignment — is BTC's recent direction consistent?
3. Market sentiment divergence — when Polymarket odds disagree with BTC price action
4. Crowd fade — when too many people pile on one side, fade them
5. Momentum confirmation — only enter when short-term price confirms the trend

Entry: When cheap side aligns with BTC trend AND market is mispricing
Exit: TP at +65%, SL at -22% (2.95:1 reward-to-risk)
"""
import numpy as np
from dataclasses import dataclass, asdict
from typing import Optional


DEFAULT_PARAMS = {
    "lookback_minutes": 15,
    "min_edge_pct": 10.0,
    "take_profit_pct": 65.0,
    "stop_loss_pct": 22.0,
    "risk_per_trade_pct": 5.0,
    "max_open_trades": 1,
    # Signal weights
    "w_value": 0.30,        # buy the cheap side
    "w_trend": 0.25,        # BTC trend direction
    "w_divergence": 0.20,   # market odds vs price action mismatch
    "w_crowd_fade": 0.15,   # fade lopsided odds
    "w_momentum_confirm": 0.10,  # short-term price confirmation
    # Thresholds
    "cheap_threshold": 0.48,    # price below this = "cheap" side
    "expensive_threshold": 0.52, # price above this = "expensive" side
    "crowd_threshold": 0.55,    # if one side > 55%, crowd is piling on
    "trend_lookback": 10,       # minutes for trend calculation
    "momentum_lookback": 3,     # minutes for short-term momentum
}


@dataclass 
class Signal:
    timestamp: int = 0
    direction: str = ""
    confidence: float = 0.0
    edge_pct: float = 0.0
    market_up_price: float = 0.0
    market_down_price: float = 0.0
    # Component scores (-1 to +1, positive = bullish/UP)
    value_score: float = 0.0
    trend_score: float = 0.0
    divergence_score: float = 0.0
    crowd_fade_score: float = 0.0
    momentum_confirm_score: float = 0.0
    # Raw data
    cheap_side: str = ""
    btc_trend_dir: str = ""
    btc_change_pct: float = 0.0
    candle_count: int = 0
    should_trade: bool = False
    reason: str = ""

    def to_dict(self):
        return asdict(self)


def compute_value_signal(market_up_price, market_down_price, params):
    """
    Smart money buys the cheaper side. The more mispriced, the stronger.
    Returns -1 to +1: positive = UP is cheap (buy UP), negative = DOWN is cheap (buy DOWN).
    """
    cheap = params["cheap_threshold"]
    expensive = params["expensive_threshold"]
    
    if market_up_price < cheap and market_down_price > expensive:
        # UP is cheap — bullish value signal
        intensity = (cheap - market_up_price) / cheap
        return min(1.0, intensity * 2), "up"
    elif market_down_price < cheap and market_up_price > expensive:
        # DOWN is cheap — bearish value signal
        intensity = (cheap - market_down_price) / cheap
        return max(-1.0, -intensity * 2), "down"
    else:
        # Both near 50/50 — weak value signal
        diff = market_down_price - market_up_price  # positive = DOWN more expensive = UP is value
        return max(-1.0, min(1.0, diff * 4)), "up" if diff > 0 else "down"


def compute_btc_trend(candles, lookback=10):
    """
    BTC trend over the lookback period.
    Returns -1 to +1: positive = BTC trending up.
    """
    if len(candles) < 3:
        return 0.0, "neutral", 0.0
    
    recent = candles[-lookback:] if len(candles) >= lookback else candles
    
    first_close = recent[0]["close"]
    last_close = recent[-1]["close"]
    
    if first_close == 0:
        return 0.0, "neutral", 0.0
    
    change_pct = ((last_close - first_close) / first_close) * 100
    
    # Count up vs down candles for consistency
    up_count = sum(1 for c in recent if c["close"] >= c["open"])
    consistency = abs(up_count / len(recent) - 0.5) * 2  # 0 = mixed, 1 = all same direction
    
    # Normalize change to -1 to +1 (0.2% move = max score)
    score = change_pct / 0.2
    score = max(-1.0, min(1.0, score))
    
    # Dampen if inconsistent
    score *= (0.5 + 0.5 * consistency)
    
    direction = "up" if score > 0.05 else "down" if score < -0.05 else "neutral"
    return score, direction, change_pct


def compute_divergence(market_up_price, btc_trend_score, candles):
    """
    When Polymarket odds diverge from BTC price action, there's opportunity.
    If BTC is trending up but market prices DOWN higher → divergence → buy UP.
    Returns -1 to +1: positive = should buy UP (market underpricing UP move).
    """
    if len(candles) < 5:
        return 0.0
    
    # Market's implied probability for UP
    market_up_prob = market_up_price
    
    # BTC's "actual" direction (from price action)
    btc_up_prob = 0.5 + (btc_trend_score * 0.25)  # map trend to probability
    
    # Divergence: BTC says one thing, market says another
    divergence = btc_up_prob - market_up_prob
    
    # Scale: 5% divergence = moderate signal, 10%+ = strong
    score = divergence / 0.10  
    return max(-1.0, min(1.0, score))


def compute_crowd_fade(market_up_price, market_down_price, params):
    """
    When the crowd piles heavily on one side, fade them.
    In BTC 5-min markets, extreme odds (>55% one side) tend to be overreactions.
    Returns -1 to +1: positive = crowd is on DOWN (fade by buying UP).
    """
    threshold = params["crowd_threshold"]
    
    if market_down_price > threshold:
        # Crowd is bearish — consider fading (buy UP)
        intensity = (market_down_price - threshold) / (1 - threshold)
        return min(1.0, intensity * 2)
    elif market_up_price > threshold:
        # Crowd is bullish — consider fading (buy DOWN)
        intensity = (market_up_price - threshold) / (1 - threshold)
        return max(-1.0, -intensity * 2)
    else:
        return 0.0


def compute_momentum_confirmation(candles, lookback=3):
    """
    Short-term momentum as final confirmation.
    Only enters when the most recent candles agree with the signal.
    Returns -1 to +1: positive = recent candles are up.
    """
    if len(candles) < lookback:
        return 0.0
    
    recent = candles[-lookback:]
    up_count = sum(1 for c in recent if c["close"] > c["open"])
    down_count = lookback - up_count
    
    # Volume-weighted direction
    up_vol = sum(c["volume"] for c in recent if c["close"] > c["open"])
    down_vol = sum(c["volume"] for c in recent if c["close"] <= c["open"])
    total_vol = up_vol + down_vol
    
    if total_vol == 0:
        return 0.0
    
    score = (up_vol - down_vol) / total_vol
    
    # Amplify if all candles agree
    if up_count == lookback:
        score = min(1.0, score * 1.3)
    elif down_count == lookback:
        score = max(-1.0, score * 1.3)
    
    return max(-1.0, min(1.0, score))


def generate_signal(candles, orderbook_data=None, market_up_price=0.5,
                    market_down_price=0.5, params=DEFAULT_PARAMS, timestamp=0,
                    **kwargs):
    """
    Generate a Smart Money trading signal.
    
    Combines value buying (cheap side) with trend following and crowd fading.
    Only trades when multiple signals align.
    """
    signal = Signal(
        timestamp=timestamp,
        market_up_price=market_up_price,
        market_down_price=market_down_price,
        candle_count=len(candles) if candles else 0,
    )
    
    if not candles or len(candles) < 5:
        signal.reason = f"Insufficient candles ({len(candles) if candles else 0}, need 5+)"
        return signal
    
    # 1. Value: which side is cheap?
    signal.value_score, signal.cheap_side = compute_value_signal(
        market_up_price, market_down_price, params
    )
    
    # 2. BTC trend
    signal.trend_score, signal.btc_trend_dir, signal.btc_change_pct = compute_btc_trend(
        candles, params["trend_lookback"]
    )
    
    # 3. Divergence between market odds and BTC action
    signal.divergence_score = compute_divergence(
        market_up_price, signal.trend_score, candles
    )
    
    # 4. Crowd fade
    signal.crowd_fade_score = compute_crowd_fade(
        market_up_price, market_down_price, params
    )
    
    # 5. Short-term momentum confirmation
    signal.momentum_confirm_score = compute_momentum_confirmation(
        candles, params["momentum_lookback"]
    )
    
    # Weighted composite: positive = buy UP, negative = buy DOWN
    raw_score = (
        params["w_value"] * signal.value_score +
        params["w_trend"] * signal.trend_score +
        params["w_divergence"] * signal.divergence_score +
        params["w_crowd_fade"] * signal.crowd_fade_score +
        params["w_momentum_confirm"] * signal.momentum_confirm_score
    )
    
    # KEY SMART MONEY RULE: require alignment between value and trend
    # Don't buy cheap UP if BTC is clearly trending down (and vice versa)
    value_trend_aligned = (
        (signal.value_score > 0 and signal.trend_score > -0.2) or
        (signal.value_score < 0 and signal.trend_score < 0.2) or
        abs(signal.value_score) < 0.1  # near 50/50, trend alone is fine
    )
    
    if not value_trend_aligned:
        signal.reason = (f"Value-trend misalignment: value={signal.value_score:+.2f} "
                        f"({signal.cheap_side}) vs trend={signal.trend_score:+.2f} "
                        f"({signal.btc_trend_dir})")
        return signal
    
    # Convert to probability
    prob = 0.5 + (abs(raw_score) * 0.22)
    prob = max(0.05, min(0.95, prob))
    
    if raw_score > 0:
        signal.direction = "up"
        signal.confidence = prob
        signal.edge_pct = (prob - market_up_price) * 100
    else:
        signal.direction = "down"
        signal.confidence = prob
        signal.edge_pct = (prob - market_down_price) * 100
    
    min_edge = params["min_edge_pct"]
    min_confidence = params.get("min_confidence", 0.59)
    if signal.edge_pct >= min_edge and signal.confidence >= min_confidence:
        signal.should_trade = True
        signal.reason = (f"Smart Money: Edge {signal.edge_pct:.1f}% >= {min_edge}% | "
                        f"Score: {raw_score:+.3f} | "
                        f"Value: {signal.cheap_side} ({signal.value_score:+.2f}) | "
                        f"Trend: {signal.btc_trend_dir} ({signal.trend_score:+.2f}) | "
                        f"BTC: {signal.btc_change_pct:+.3f}%")
    elif signal.edge_pct >= min_edge and signal.confidence < min_confidence:
        signal.reason = (f"Edge OK but confidence {signal.confidence:.1%} < {min_confidence:.0%} | "
                        f"Score: {raw_score:+.3f}")
    else:
        signal.reason = (f"Edge {signal.edge_pct:.1f}% < {min_edge}% | "
                        f"Score: {raw_score:+.3f} | "
                        f"Value: {signal.cheap_side} | Trend: {signal.btc_trend_dir}")
    
    return signal


def format_signal_message(signal):
    """Format signal for logging/Telegram."""
    if not signal.should_trade:
        return (f"⏸️ SmartMoney: {signal.reason}\n"
                f"  Value: {signal.value_score:+.2f} ({signal.cheap_side}) | "
                f"Trend: {signal.trend_score:+.2f} ({signal.btc_trend_dir}) | "
                f"Diverg: {signal.divergence_score:+.2f} | "
                f"Crowd: {signal.crowd_fade_score:+.2f} | "
                f"MomConf: {signal.momentum_confirm_score:+.2f}")
    
    emoji = "🟢" if signal.direction == "up" else "🔴"
    return (
        f"{emoji} SMART MONEY: BUY {signal.direction.upper()} "
        f"@ ${signal.market_up_price if signal.direction == 'up' else signal.market_down_price:.3f}\n"
        f"  Edge: {signal.edge_pct:.1f}% | Confidence: {signal.confidence:.1%}\n"
        f"  Value: {signal.value_score:+.2f} ({signal.cheap_side}) | "
        f"Trend: {signal.trend_score:+.2f} ({signal.btc_trend_dir}) | "
        f"BTC Δ: {signal.btc_change_pct:+.3f}%\n"
        f"  Divergence: {signal.divergence_score:+.2f} | "
        f"Crowd fade: {signal.crowd_fade_score:+.2f} | "
        f"Mom confirm: {signal.momentum_confirm_score:+.2f}"
    )
