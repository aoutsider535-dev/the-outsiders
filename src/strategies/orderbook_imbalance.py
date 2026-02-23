"""
Orderbook Imbalance Strategy for BTC 5-Min Polymarket Markets.

Core idea: Follow the smart money. When there's significantly more buying 
pressure on one side of the Polymarket orderbook, that side is more likely to win.

Edge sources:
1. Polymarket OB imbalance — bid/ask depth ratio on Up vs Down tokens
2. Polymarket spread analysis — tight spread on one side = confident market makers
3. Kraken OB imbalance — BTC spot market pressure (proxy for direction)
4. Price momentum alignment — does the OB pressure agree with recent price action?
5. Depth concentration — is the depth near the top of book (informed) or deep (passive)?

Entry: When orderbook imbalance strongly favors one direction
Exit: TP at +55%, SL at -25% (2.2:1 reward-to-risk)
"""
import numpy as np
from dataclasses import dataclass, asdict


DEFAULT_PARAMS = {
    "lookback_minutes": 10,
    "min_edge_pct": 4.0,
    "take_profit_pct": 55.0,
    "stop_loss_pct": 25.0,
    "risk_per_trade_pct": 5.0,
    "max_open_trades": 1,
    # Signal weights
    "w_poly_imbalance": 0.35,
    "w_spread_signal": 0.20,
    "w_kraken_ob": 0.20,
    "w_momentum_align": 0.15,
    "w_depth_concentration": 0.10,
    # Thresholds
    "min_imbalance": 0.10,  # at least 10% imbalance to consider
    "depth_near_pct": 0.30,  # top 30% of book = "near" depth
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
    poly_imbalance_score: float = 0.0
    spread_score: float = 0.0
    kraken_ob_score: float = 0.0
    momentum_align_score: float = 0.0
    depth_concentration_score: float = 0.0
    # Raw data
    up_bid_depth: float = 0.0
    up_ask_depth: float = 0.0
    down_bid_depth: float = 0.0
    down_ask_depth: float = 0.0
    up_spread: float = 0.0
    down_spread: float = 0.0
    candle_count: int = 0
    should_trade: bool = False
    reason: str = ""

    def to_dict(self):
        return asdict(self)


def compute_poly_imbalance(up_orderbook, down_orderbook):
    """
    Compare Polymarket Up vs Down token orderbooks.
    More bids on Up = bullish. More bids on Down = bearish.
    Returns -1 to +1: positive = bullish (bet UP).
    """
    if not up_orderbook or not down_orderbook:
        return 0.0
    
    up_bid = up_orderbook.get("bid_depth", 0)
    up_ask = up_orderbook.get("ask_depth", 0)
    down_bid = down_orderbook.get("bid_depth", 0)
    down_ask = down_orderbook.get("ask_depth", 0)
    
    # Net buying pressure: bids on Up + asks on Down (both bullish)
    bullish_pressure = up_bid + down_ask
    bearish_pressure = up_ask + down_bid
    total = bullish_pressure + bearish_pressure
    
    if total == 0:
        return 0.0
    
    imbalance = (bullish_pressure - bearish_pressure) / total
    return max(-1.0, min(1.0, imbalance))


def compute_spread_signal(up_orderbook, down_orderbook):
    """
    Tighter spread on one side = more confident market makers.
    If Up has tighter spread, market makers are more confident in Up.
    Returns -1 to +1: positive = Up has tighter spread (bullish).
    """
    if not up_orderbook or not down_orderbook:
        return 0.0
    
    up_spread = up_orderbook.get("spread", 0.1)
    down_spread = down_orderbook.get("spread", 0.1)
    
    if up_spread == 0 and down_spread == 0:
        return 0.0
    
    # Tighter spread = more confidence
    avg_spread = (up_spread + down_spread) / 2
    if avg_spread == 0:
        return 0.0
    
    # Positive if Up spread is tighter (bullish)
    score = (down_spread - up_spread) / avg_spread
    return max(-1.0, min(1.0, score))


def compute_kraken_ob(kraken_orderbook):
    """
    Kraken BTC/USD orderbook imbalance as directional proxy.
    More bids = buying pressure = bullish.
    Returns -1 to +1: positive = bullish.
    """
    if not kraken_orderbook:
        return 0.0
    
    return max(-1.0, min(1.0, kraken_orderbook.get("imbalance", 0)))


def compute_momentum_alignment(candles):
    """
    Check if recent price momentum aligns with orderbook signals.
    Returns -1 to +1: positive = recent price going up.
    
    Used as confirmation — OB signal + price agreement = stronger.
    """
    if len(candles) < 5:
        return 0.0
    
    recent = candles[-5:]
    up_count = sum(1 for c in recent if c["close"] >= c["open"])
    
    # Simple directional bias
    score = (up_count / len(recent) - 0.5) * 2
    return max(-1.0, min(1.0, score))


def compute_depth_concentration(up_orderbook, down_orderbook, near_pct=0.30):
    """
    Is the depth concentrated near the top of book (informed traders)
    or spread deep (passive/market makers)?
    
    Near-book concentration = more actionable signal.
    Returns -1 to +1: positive = informed buying on Up side.
    """
    if not up_orderbook or not down_orderbook:
        return 0.0
    
    def near_depth_ratio(book):
        bids = book.get("bids", [])
        if not bids or len(bids) < 2:
            return 0.5
        total_depth = sum(size for _, size in bids)
        if total_depth == 0:
            return 0.5
        # Top N% of levels
        n_near = max(1, int(len(bids) * near_pct))
        near_depth = sum(size for _, size in bids[:n_near])
        return near_depth / total_depth
    
    up_concentration = near_depth_ratio(up_orderbook)
    down_concentration = near_depth_ratio(down_orderbook)
    
    # Higher near-book concentration on Up = informed bullish interest
    score = (up_concentration - down_concentration) * 2
    return max(-1.0, min(1.0, score))


def generate_signal(candles, orderbook_data=None, market_up_price=0.5,
                    market_down_price=0.5, params=DEFAULT_PARAMS, timestamp=0,
                    up_orderbook=None, down_orderbook=None, kraken_orderbook=None):
    """
    Generate an orderbook imbalance trading signal.
    
    This strategy primarily uses Polymarket orderbook data, with Kraken as confirmation.
    """
    signal = Signal(
        timestamp=timestamp,
        market_up_price=market_up_price,
        market_down_price=market_down_price,
        candle_count=len(candles) if candles else 0,
    )
    
    # Need at least Polymarket orderbook data
    if not up_orderbook and not down_orderbook:
        # Fall back to basic orderbook_data if available
        if orderbook_data:
            signal.kraken_ob_score = compute_kraken_ob(orderbook_data)
        signal.reason = "No Polymarket orderbook data available"
        return signal
    
    # Store raw data
    if up_orderbook:
        signal.up_bid_depth = up_orderbook.get("bid_depth", 0)
        signal.up_ask_depth = up_orderbook.get("ask_depth", 0)
        signal.up_spread = up_orderbook.get("spread", 0)
    if down_orderbook:
        signal.down_bid_depth = down_orderbook.get("bid_depth", 0)
        signal.down_ask_depth = down_orderbook.get("ask_depth", 0)
        signal.down_spread = down_orderbook.get("spread", 0)
    
    # Compute components (all positive = bullish = bet UP)
    signal.poly_imbalance_score = compute_poly_imbalance(up_orderbook, down_orderbook)
    signal.spread_score = compute_spread_signal(up_orderbook, down_orderbook)
    signal.kraken_ob_score = compute_kraken_ob(kraken_orderbook or orderbook_data)
    signal.momentum_align_score = compute_momentum_alignment(candles) if candles else 0.0
    signal.depth_concentration_score = compute_depth_concentration(
        up_orderbook, down_orderbook, params.get("depth_near_pct", 0.30)
    )
    
    # Weighted composite: positive = bullish = bet UP
    raw_score = (
        params["w_poly_imbalance"] * signal.poly_imbalance_score +
        params["w_spread_signal"] * signal.spread_score +
        params["w_kraken_ob"] * signal.kraken_ob_score +
        params["w_momentum_align"] * signal.momentum_align_score +
        params["w_depth_concentration"] * signal.depth_concentration_score
    )
    
    # Need minimum imbalance to trade
    min_imbalance = params.get("min_imbalance", 0.10)
    if abs(raw_score) < min_imbalance:
        signal.reason = (f"Imbalance too weak ({raw_score:+.3f}, need ±{min_imbalance}) | "
                        f"Poly: {signal.poly_imbalance_score:+.2f} | "
                        f"Kraken: {signal.kraken_ob_score:+.2f}")
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
    if signal.edge_pct >= min_edge:
        signal.should_trade = True
        signal.reason = (f"OB Imbalance: Edge {signal.edge_pct:.1f}% >= {min_edge}% | "
                        f"Score: {raw_score:+.3f} | "
                        f"Poly: {signal.poly_imbalance_score:+.2f} | "
                        f"Spread: {signal.spread_score:+.2f}")
    else:
        signal.reason = (f"Edge {signal.edge_pct:.1f}% < {min_edge}% | "
                        f"Score: {raw_score:+.3f}")
    
    return signal


def format_signal_message(signal):
    """Format signal for logging/Telegram."""
    if not signal.should_trade:
        return (f"⏸️ OBImb: {signal.reason}\n"
                f"  PolyOB: {signal.poly_imbalance_score:+.2f} | "
                f"Spread: {signal.spread_score:+.2f} | "
                f"Kraken: {signal.kraken_ob_score:+.2f} | "
                f"MomAlign: {signal.momentum_align_score:+.2f} | "
                f"DepthConc: {signal.depth_concentration_score:+.2f}")
    
    emoji = "🟢" if signal.direction == "up" else "🔴"
    return (
        f"{emoji} OB IMBALANCE: BUY {signal.direction.upper()} "
        f"@ ${signal.market_up_price if signal.direction == 'up' else signal.market_down_price:.3f}\n"
        f"  Edge: {signal.edge_pct:.1f}% | Confidence: {signal.confidence:.1%}\n"
        f"  PolyOB: {signal.poly_imbalance_score:+.2f} | Spread: {signal.spread_score:+.2f} | "
        f"Kraken: {signal.kraken_ob_score:+.2f}\n"
        f"  Up depth: {signal.up_bid_depth:.0f}b/{signal.up_ask_depth:.0f}a | "
        f"Down depth: {signal.down_bid_depth:.0f}b/{signal.down_ask_depth:.0f}a"
    )
