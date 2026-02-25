"""
BTC 5-Min Up/Down Strategy for Polymarket.

Edge sources:
1. Momentum — 15-min Kraken candle bias (volume-weighted direction)
2. Trend — Linear regression slope on closes
3. Last Candle — Strength/direction of most recent candle
4. Orderbook Imbalance — bid vs ask depth (Kraken as proxy)
5. Volatility Regime — low vol = fade, high vol = trend-follow

Entry: Only if edge > MIN_EDGE over market price
Exit: TP at +40%, SL at -50%
Risk: 5% of balance per trade, max 1 open position
"""
import numpy as np
from scipy import stats
from dataclasses import dataclass, field, asdict
from typing import Optional


# Default strategy parameters
DEFAULT_PARAMS = {
    "lookback_minutes": 15,
    "min_edge_pct": 8.0,
    "max_edge_pct": 12.0,
    "take_profit_pct": 40.0,
    "stop_loss_pct": 50.0,
    "risk_per_trade_pct": 5.0,
    "max_open_trades": 1,
    # Signal weights
    "w_momentum": 0.30,
    "w_trend": 0.25,
    "w_last_candle": 0.20,
    "w_orderbook": 0.15,
    "w_volatility": 0.10,
    # Volatility thresholds (as fraction of price)
    "vol_low_threshold": 0.0005,   # 0.05%
    "vol_high_threshold": 0.002,   # 0.2%
    # Momentum
    "momentum_strong_threshold": 0.6,  # 60% of candles in one direction = strong
}


@dataclass
class Signal:
    timestamp: int = 0
    direction: str = ""  # "up" or "down"
    confidence: float = 0.0  # 0-1, our estimated probability
    edge_pct: float = 0.0  # confidence - market_price
    market_up_price: float = 0.0
    market_down_price: float = 0.0
    # Component scores (-1 to +1, positive = up bias)
    momentum_score: float = 0.0
    trend_score: float = 0.0
    last_candle_score: float = 0.0
    orderbook_score: float = 0.0
    volatility_regime: str = "normal"  # low, normal, high
    volatility_multiplier: float = 1.0
    # Raw data
    candle_count: int = 0
    avg_volume: float = 0.0
    price_change_pct: float = 0.0
    should_trade: bool = False
    shadow_trade: bool = False  # Track capped trades for validation
    reason: str = ""

    def to_dict(self):
        return asdict(self)


def compute_momentum(candles, params=DEFAULT_PARAMS):
    """
    Volume-weighted directional bias from recent candles.
    Returns score from -1 (strong down) to +1 (strong up).
    """
    if len(candles) < 3:
        return 0.0

    up_volume = 0.0
    down_volume = 0.0
    for c in candles:
        change = c["close"] - c["open"]
        if change >= 0:
            up_volume += c["volume"]
        else:
            down_volume += c["volume"]

    total = up_volume + down_volume
    if total == 0:
        return 0.0

    # Bias from -1 to +1
    bias = (up_volume - down_volume) / total

    # Count direction
    up_count = sum(1 for c in candles if c["close"] >= c["open"])
    up_ratio = up_count / len(candles)

    # Amplify if strong consensus
    threshold = params["momentum_strong_threshold"]
    if up_ratio >= threshold:
        bias = bias * (1 + (up_ratio - threshold))
    elif (1 - up_ratio) >= threshold:
        bias = bias * (1 + ((1 - up_ratio) - threshold))

    return max(-1.0, min(1.0, bias))


def compute_trend(candles):
    """
    Linear regression slope on close prices.
    Returns normalized score from -1 to +1.
    """
    if len(candles) < 5:
        return 0.0

    closes = np.array([c["close"] for c in candles])
    x = np.arange(len(closes))

    slope, _, r_value, _, _ = stats.linregress(x, closes)

    # Normalize: slope as fraction of mean price, scaled by R²
    mean_price = np.mean(closes)
    if mean_price == 0:
        return 0.0

    normalized = (slope / mean_price) * 1000  # scale up small fractions
    # Weight by R² (confidence in the trend)
    score = normalized * (r_value ** 2)

    return max(-1.0, min(1.0, score))


def compute_last_candle(candles):
    """
    Strength and direction of the most recent candle.
    Returns score from -1 to +1.
    """
    if not candles:
        return 0.0

    last = candles[-1]
    body = last["close"] - last["open"]
    full_range = last["high"] - last["low"]

    if full_range == 0:
        return 0.0

    # Body-to-range ratio: strong candle = large body relative to wicks
    body_ratio = abs(body) / full_range
    direction = 1.0 if body >= 0 else -1.0

    # Compare to average candle size
    if len(candles) >= 3:
        avg_range = np.mean([c["high"] - c["low"] for c in candles[-5:]])
        size_factor = full_range / avg_range if avg_range > 0 else 1.0
        size_factor = min(size_factor, 2.0)  # cap amplification
    else:
        size_factor = 1.0

    score = direction * body_ratio * size_factor
    return max(-1.0, min(1.0, score))


def compute_orderbook_signal(orderbook_data):
    """
    Orderbook imbalance signal.
    Returns score from -1 to +1 (positive = more bids = up bias).
    """
    if not orderbook_data:
        return 0.0
    return max(-1.0, min(1.0, orderbook_data["imbalance"]))


def compute_volatility_regime(candles, params=DEFAULT_PARAMS):
    """
    Classify volatility regime and return multiplier.
    Low vol → fade (reduce confidence), High vol → trend-follow (amplify).
    """
    if len(candles) < 5:
        return "normal", 1.0

    returns = []
    for i in range(1, len(candles)):
        if candles[i - 1]["close"] > 0:
            r = (candles[i]["close"] - candles[i - 1]["close"]) / candles[i - 1]["close"]
            returns.append(r)

    if not returns:
        return "normal", 1.0

    vol = np.std(returns)

    if vol < params["vol_low_threshold"]:
        return "low", 0.7  # reduce confidence in low vol (mean-revert)
    elif vol > params["vol_high_threshold"]:
        return "high", 1.3  # amplify in high vol (trend-follow)
    else:
        return "normal", 1.0


def generate_signal(candles, orderbook_data=None, market_up_price=0.5,
                    market_down_price=0.5, params=DEFAULT_PARAMS, timestamp=0):
    """
    Generate a trading signal from market data.

    Args:
        candles: List of 1-min OHLCV dicts (most recent last)
        orderbook_data: Dict with imbalance info (optional)
        market_up_price: Current Polymarket price for "Up" token (0-1)
        market_down_price: Current Polymarket price for "Down" token (0-1)
        params: Strategy parameters
        timestamp: Current timestamp

    Returns:
        Signal object with direction, confidence, and edge
    """
    signal = Signal(
        timestamp=timestamp,
        market_up_price=market_up_price,
        market_down_price=market_down_price,
        candle_count=len(candles),
    )

    if len(candles) < 5:
        signal.reason = f"Insufficient candles ({len(candles)}, need 5+)"
        return signal

    # Compute component scores
    signal.momentum_score = compute_momentum(candles, params)
    signal.trend_score = compute_trend(candles)
    signal.last_candle_score = compute_last_candle(candles)
    signal.orderbook_score = compute_orderbook_signal(orderbook_data)
    signal.volatility_regime, signal.volatility_multiplier = compute_volatility_regime(candles, params)

    # Weighted composite score (-1 to +1)
    raw_score = (
        params["w_momentum"] * signal.momentum_score +
        params["w_trend"] * signal.trend_score +
        params["w_last_candle"] * signal.last_candle_score +
        params["w_orderbook"] * signal.orderbook_score
    )

    # Apply volatility multiplier
    adjusted_score = raw_score * signal.volatility_multiplier

    # Convert to probability (sigmoid-like mapping)
    # score of 0 = 50%, score of ±1 = ~75%/25%
    up_probability = 0.5 + (adjusted_score * 0.25)
    up_probability = max(0.05, min(0.95, up_probability))
    down_probability = 1.0 - up_probability

    # Determine direction and edge
    up_edge = up_probability - market_up_price
    down_edge = down_probability - market_down_price

    if up_edge > down_edge:
        signal.direction = "up"
        signal.confidence = up_probability
        signal.edge_pct = up_edge * 100
    else:
        signal.direction = "down"
        signal.confidence = down_probability
        signal.edge_pct = down_edge * 100

    # Price change info
    if candles:
        first_close = candles[0]["close"]
        last_close = candles[-1]["close"]
        if first_close > 0:
            signal.price_change_pct = ((last_close - first_close) / first_close) * 100
        signal.avg_volume = np.mean([c["volume"] for c in candles])

    # Should we trade?
    min_edge = params["min_edge_pct"]
    min_confidence = params.get("min_confidence", 0.57)
    max_edge = params.get("max_edge_pct", 12.0)
    
    if signal.edge_pct > max_edge:
        signal.shadow_trade = True  # Track what would have happened
        signal.reason = (f"👻 Momentum SHADOW: Edge {signal.edge_pct:.1f}% > {max_edge}% cap — tracking only | "
                        f"Direction: {signal.direction} @ {signal.confidence:.1%}")
    elif signal.edge_pct >= min_edge and signal.confidence >= min_confidence:
        signal.should_trade = True
        signal.reason = (f"Edge {signal.edge_pct:.1f}% >= {min_edge}% threshold | "
                        f"Direction: {signal.direction} @ {signal.confidence:.1%}")
    elif signal.edge_pct >= min_edge and signal.confidence < min_confidence:
        signal.reason = (f"Edge {signal.edge_pct:.1f}% OK but confidence {signal.confidence:.1%} < {min_confidence:.0%} | "
                        f"Best direction: {signal.direction}")
    else:
        signal.reason = (f"Edge {signal.edge_pct:.1f}% < {min_edge}% threshold | "
                        f"Best direction: {signal.direction}")

    return signal


def format_signal_message(signal):
    """Format signal for Telegram reporting."""
    if not signal.should_trade:
        return (f"⏸️ No trade | {signal.reason}\n"
                f"  Mom: {signal.momentum_score:+.2f} | Trend: {signal.trend_score:+.2f} | "
                f"Candle: {signal.last_candle_score:+.2f} | OB: {signal.orderbook_score:+.2f} | "
                f"Vol: {signal.volatility_regime}")

    emoji = "🟢" if signal.direction == "up" else "🔴"
    return (
        f"{emoji} TRADE SIGNAL: BUY {signal.direction.upper()} "
        f"@ ${signal.market_up_price if signal.direction == 'up' else signal.market_down_price:.3f}\n"
        f"  Edge: {signal.edge_pct:.1f}% | Confidence: {signal.confidence:.1%} vs "
        f"Market: {signal.market_up_price if signal.direction == 'up' else signal.market_down_price:.1%}\n"
        f"  Momentum: {signal.momentum_score:+.2f} | Trend: {signal.trend_score:+.2f}\n"
        f"  Last candle: {signal.last_candle_score:+.2f} | OB: {signal.orderbook_score:+.2f}\n"
        f"  Vol regime: {signal.volatility_regime} (x{signal.volatility_multiplier:.1f}) | "
        f"BTC Δ: {signal.price_change_pct:+.2f}%"
    )
