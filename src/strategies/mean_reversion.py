"""
Mean Reversion Strategy for BTC 5-Min Polymarket Markets.

Core idea: When BTC moves sharply in one direction, it often snaps back.
We bet AGAINST the recent move when conditions suggest overextension.

Edge sources:
1. Price deviation — how far price has moved from its recent mean
2. RSI (Relative Strength Index) — classic overbought/oversold indicator  
3. Wick rejection — long wicks signal reversal pressure
4. Volume climax — huge volume on a move = exhaustion
5. Bollinger Band position — price at band extremes tends to revert

Entry: Only when price is overextended AND showing reversal signals
Exit: TP at +60%, SL at -25% (2.4:1 reward-to-risk)
"""
import numpy as np
from dataclasses import dataclass, asdict
from typing import Optional


DEFAULT_PARAMS = {
    "lookback_minutes": 20,
    "min_edge_pct": 5.0,
    "take_profit_pct": 60.0,
    "stop_loss_pct": 25.0,
    "risk_per_trade_pct": 5.0,
    "max_open_trades": 1,
    # Signal weights
    "w_deviation": 0.30,
    "w_rsi": 0.25,
    "w_wick": 0.20,
    "w_volume_climax": 0.15,
    "w_bollinger": 0.10,
    # Thresholds
    "rsi_overbought": 70,
    "rsi_oversold": 30,
    "rsi_period": 14,
    "deviation_threshold": 0.15,  # 0.15% from mean = start considering reversion
    "bollinger_period": 20,
    "bollinger_std": 2.0,
    "volume_climax_multiplier": 2.0,  # volume > 2x average = climax
}


@dataclass
class Signal:
    timestamp: int = 0
    direction: str = ""
    confidence: float = 0.0
    edge_pct: float = 0.0
    market_up_price: float = 0.0
    market_down_price: float = 0.0
    # Component scores
    deviation_score: float = 0.0
    rsi_score: float = 0.0
    wick_score: float = 0.0
    volume_climax_score: float = 0.0
    bollinger_score: float = 0.0
    # Raw data
    rsi_value: float = 50.0
    price_deviation_pct: float = 0.0
    candle_count: int = 0
    should_trade: bool = False
    reason: str = ""

    def to_dict(self):
        return asdict(self)


def compute_rsi(candles, period=14):
    """Compute RSI. Returns 0-100, <30 oversold, >70 overbought."""
    if len(candles) < period + 1:
        return 50.0  # neutral
    
    closes = [c["close"] for c in candles]
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    
    gains = [d if d > 0 else 0 for d in deltas[-period:]]
    losses = [-d if d < 0 else 0 for d in deltas[-period:]]
    
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    
    if avg_loss == 0:
        return 100.0
    
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def compute_deviation(candles, lookback=20):
    """
    How far current price is from its moving average.
    Returns score from -1 (far below mean = oversold) to +1 (far above = overbought).
    Positive = price above mean (bet DOWN for reversion).
    """
    if len(candles) < 5:
        return 0.0, 0.0
    
    closes = np.array([c["close"] for c in candles[-lookback:]])
    mean_price = np.mean(closes)
    current = closes[-1]
    
    if mean_price == 0:
        return 0.0, 0.0
    
    deviation_pct = ((current - mean_price) / mean_price) * 100
    
    # Normalize: 0.3% deviation = max score
    std = np.std(closes) / mean_price * 100 if len(closes) > 2 else 0.1
    score = deviation_pct / max(std * 2, 0.05)
    score = max(-1.0, min(1.0, score))
    
    return score, deviation_pct


def compute_wick_rejection(candles):
    """
    Check for wick rejection in recent candles.
    Long upper wick = rejection of higher prices (bearish reversal signal).
    Long lower wick = rejection of lower prices (bullish reversal signal).
    Returns -1 to +1: positive = upper wick rejection (bet DOWN), negative = lower wick (bet UP).
    """
    if len(candles) < 3:
        return 0.0
    
    scores = []
    for c in candles[-3:]:
        body = abs(c["close"] - c["open"])
        full_range = c["high"] - c["low"]
        
        if full_range == 0:
            scores.append(0.0)
            continue
        
        upper_wick = c["high"] - max(c["open"], c["close"])
        lower_wick = min(c["open"], c["close"]) - c["low"]
        
        # Wick dominance: which wick is larger relative to body?
        wick_ratio = (upper_wick - lower_wick) / full_range
        scores.append(wick_ratio)
    
    # Weight recent candles more
    weights = [0.2, 0.3, 0.5]
    score = sum(s * w for s, w in zip(scores, weights))
    return max(-1.0, min(1.0, score))


def compute_volume_climax(candles):
    """
    Detect volume climax — very high volume often marks exhaustion/reversal.
    Returns score: positive if recent volume spike on up move (bet DOWN),
    negative if spike on down move (bet UP).
    """
    if len(candles) < 10:
        return 0.0
    
    volumes = [c["volume"] for c in candles]
    avg_vol = np.mean(volumes[:-3])  # average excluding last 3
    
    if avg_vol == 0:
        return 0.0
    
    recent_vols = volumes[-3:]
    max_recent = max(recent_vols)
    vol_ratio = max_recent / avg_vol
    
    if vol_ratio < 1.5:
        return 0.0  # no climax
    
    # Direction of the high-volume candle
    high_vol_idx = recent_vols.index(max_recent)
    c = candles[-(3 - high_vol_idx)]
    direction = 1.0 if c["close"] > c["open"] else -1.0
    
    # Climax on up move = bearish reversal signal (positive score = bet DOWN)
    intensity = min((vol_ratio - 1.5) / 1.5, 1.0)  # normalize 1.5x-3x to 0-1
    return direction * intensity


def compute_bollinger_position(candles, period=20, num_std=2.0):
    """
    Where is price relative to Bollinger Bands?
    Returns -1 to +1: near upper band = +1 (overbought, bet DOWN),
    near lower band = -1 (oversold, bet UP).
    """
    if len(candles) < period:
        return 0.0
    
    closes = np.array([c["close"] for c in candles[-period:]])
    mean = np.mean(closes)
    std = np.std(closes)
    
    if std == 0:
        return 0.0
    
    upper = mean + num_std * std
    lower = mean - num_std * std
    current = closes[-1]
    
    band_width = upper - lower
    if band_width == 0:
        return 0.0
    
    # Position within bands: 0 = at mean, +1 = at upper, -1 = at lower
    position = (current - mean) / (band_width / 2)
    return max(-1.0, min(1.0, position))


def generate_signal(candles, orderbook_data=None, market_up_price=0.5,
                    market_down_price=0.5, params=DEFAULT_PARAMS, timestamp=0):
    """
    Generate a mean reversion trading signal.
    
    Key insight: All component scores are oriented so that POSITIVE = overbought (bet DOWN)
    and NEGATIVE = oversold (bet UP). This is the OPPOSITE of the momentum strategy.
    """
    signal = Signal(
        timestamp=timestamp,
        market_up_price=market_up_price,
        market_down_price=market_down_price,
        candle_count=len(candles),
    )
    
    if len(candles) < 10:
        signal.reason = f"Insufficient candles ({len(candles)}, need 10+)"
        return signal
    
    # Compute components (all oriented: positive = overbought = bet DOWN for reversion)
    signal.deviation_score, signal.price_deviation_pct = compute_deviation(
        candles, params["lookback_minutes"]
    )
    
    rsi = compute_rsi(candles, params.get("rsi_period", 14))
    signal.rsi_value = rsi
    # Map RSI: >70 → positive (overbought), <30 → negative (oversold)
    signal.rsi_score = (rsi - 50) / 50  # -1 to +1
    
    signal.wick_score = compute_wick_rejection(candles)
    signal.volume_climax_score = compute_volume_climax(candles)
    signal.bollinger_score = compute_bollinger_position(
        candles, params.get("bollinger_period", 20), params.get("bollinger_std", 2.0)
    )
    
    # Weighted composite: positive = overbought = bet DOWN
    raw_score = (
        params["w_deviation"] * signal.deviation_score +
        params["w_rsi"] * signal.rsi_score +
        params["w_wick"] * signal.wick_score +
        params["w_volume_climax"] * signal.volume_climax_score +
        params["w_bollinger"] * signal.bollinger_score
    )
    
    # For mean reversion, we BET AGAINST the score direction
    # Positive score = overbought = bet DOWN
    # Negative score = oversold = bet UP
    
    # Only trade if there's enough overextension (key filter)
    abs_score = abs(raw_score)
    if abs_score < 0.15:
        signal.reason = f"Not overextended enough (score={raw_score:+.3f}, need ±0.15)"
        return signal
    
    # Convert to probability
    # Mean reversion: we're betting on the SNAP BACK
    reversion_probability = 0.5 + (abs_score * 0.20)  # more conservative than momentum
    reversion_probability = max(0.05, min(0.95, reversion_probability))
    
    if raw_score > 0:
        # Overbought → bet DOWN (price should revert down)
        signal.direction = "down"
        signal.confidence = reversion_probability
        signal.edge_pct = (reversion_probability - market_down_price) * 100
    else:
        # Oversold → bet UP (price should revert up)
        signal.direction = "up"
        signal.confidence = reversion_probability
        signal.edge_pct = (reversion_probability - market_up_price) * 100
    
    # Should we trade?
    min_edge = params["min_edge_pct"]
    min_confidence = params.get("min_confidence", 0.55)
    # Mean reversion: data shows higher edge = LOWER win rate, so cap max edge too
    max_edge = params.get("max_edge_pct", 12.0)
    if signal.edge_pct >= min_edge and signal.confidence >= min_confidence and signal.edge_pct <= max_edge:
        signal.should_trade = True
        signal.reason = (f"Mean reversion: Edge {signal.edge_pct:.1f}% >= {min_edge}% | "
                        f"Score: {raw_score:+.3f} | RSI: {rsi:.0f} | "
                        f"Dev: {signal.price_deviation_pct:+.3f}%")
    elif signal.edge_pct > max_edge:
        signal.reason = (f"Edge {signal.edge_pct:.1f}% > {max_edge}% max (overextended) | "
                        f"Score: {raw_score:+.3f} | RSI: {rsi:.0f}")
    elif signal.confidence < min_confidence:
        signal.reason = (f"Confidence {signal.confidence:.1%} < {min_confidence:.0%} | "
                        f"Score: {raw_score:+.3f} | RSI: {rsi:.0f}")
    else:
        signal.reason = (f"Edge {signal.edge_pct:.1f}% < {min_edge}% | "
                        f"Score: {raw_score:+.3f} | RSI: {rsi:.0f}")
    
    return signal


def format_signal_message(signal):
    """Format signal for logging/Telegram."""
    if not signal.should_trade:
        return (f"⏸️ MeanRev: {signal.reason}\n"
                f"  Dev: {signal.deviation_score:+.2f} | RSI: {signal.rsi_value:.0f} ({signal.rsi_score:+.2f}) | "
                f"Wick: {signal.wick_score:+.2f} | VolClx: {signal.volume_climax_score:+.2f} | "
                f"BB: {signal.bollinger_score:+.2f}")
    
    emoji = "🟢" if signal.direction == "up" else "🔴"
    return (
        f"{emoji} MEAN REVERSION: BUY {signal.direction.upper()} "
        f"@ ${signal.market_up_price if signal.direction == 'up' else signal.market_down_price:.3f}\n"
        f"  Edge: {signal.edge_pct:.1f}% | Confidence: {signal.confidence:.1%}\n"
        f"  RSI: {signal.rsi_value:.0f} | Dev: {signal.price_deviation_pct:+.3f}% | "
        f"BB: {signal.bollinger_score:+.2f}\n"
        f"  Wick: {signal.wick_score:+.2f} | Vol Climax: {signal.volume_climax_score:+.2f}"
    )
