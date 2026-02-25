"""
⚡ Volatility Spike Strategy — Paper Trader v2

Catches breakouts when volume surges and price pierces Bollinger Bands.
High-conviction, low-frequency — only fires on genuine spikes.

Signal:
  - Volume ratio > 2x recent average (FRESH spike)
  - Price breaks above upper or below lower Bollinger Band
  - Kraken orderbook aligned with breakout direction
  - Recent volume (5-10 min ago) must have been normal (confirms freshness)

Strengths: Catches sudden moves other strategies miss
Weakness: Fake breakouts, but volume + orderbook confirmation filters those
"""

from dataclasses import dataclass, asdict


DEFAULTS = {
    "min_edge_pct": 5.0,
    "take_profit_pct": 60.0,
    "stop_loss_pct": 25.0,
    "bb_period": 20,
    "bb_std": 2.0,
    "min_vol_ratio": 2.0,         # Current volume must be 2x average
    "max_prior_vol_ratio": 1.3,   # Volume 5-10 min ago must be normal (freshness)
    "min_bb_breach": 0.0,         # Price must be outside BB (bb_pct > 1.0 or < 0.0)
    "vol_spike_weight": 0.30,
    "bb_breach_weight": 0.30,
    "orderbook_weight": 0.20,
    "candle_strength_weight": 0.20,
}


@dataclass
class Signal:
    timestamp: int = 0
    direction: str = ""
    confidence: float = 0.0
    edge_pct: float = 0.0
    market_up_price: float = 0.0
    market_down_price: float = 0.0
    vol_ratio: float = 0.0
    prior_vol_ratio: float = 0.0
    vol_spike_score: float = 0.0
    bb_pct: float = 0.5
    bb_breach_score: float = 0.0
    ob_score: float = 0.0
    candle_strength: float = 0.0
    atr_pct: float = 0.0
    candle_count: int = 0
    should_trade: bool = False
    reason: str = ""

    def to_dict(self):
        return asdict(self)


def _bollinger(closes, period=20, std_mult=2.0):
    if len(closes) < period:
        return 0.5, 0, 0, 0
    window = closes[-period:]
    sma = sum(window) / period
    std = (sum((c - sma) ** 2 for c in window) / period) ** 0.5
    upper = sma + std_mult * std
    lower = sma - std_mult * std
    bb_range = upper - lower
    if bb_range <= 0:
        return 0.5, upper, lower, sma
    bb_pct = (closes[-1] - lower) / bb_range
    return bb_pct, upper, lower, sma


def _atr(candles, period=14):
    if len(candles) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr_val = sum(trs[-period:]) / min(period, len(trs))
    return atr_val / candles[-1]["close"] * 100 if candles[-1]["close"] > 0 else 0.0


def generate_signal(candles, market_up_price=0.5, market_down_price=0.5,
                    kraken_orderbook=None, params=None, **kwargs):
    """Generate Volatility Spike signal."""
    params = params or DEFAULTS.copy()
    sig = Signal(market_up_price=market_up_price, market_down_price=market_down_price)

    if not candles or len(candles) < params["bb_period"] + 5:
        sig.reason = f"Need {params['bb_period'] + 5} candles, have {len(candles) if candles else 0}"
        return sig

    sig.candle_count = len(candles)
    closes = [c["close"] for c in candles]
    volumes = [c["volume"] for c in candles]

    # 1. Volume spike detection
    # Recent volume = last 3 candles avg
    recent_vol = sum(volumes[-3:]) / 3 if len(volumes) >= 3 else volumes[-1]
    # Baseline = candles 5-15 ago
    baseline_vols = volumes[-15:-5] if len(volumes) >= 15 else volumes[:-3]
    avg_vol = sum(baseline_vols) / len(baseline_vols) if baseline_vols else 1.0

    sig.vol_ratio = recent_vol / avg_vol if avg_vol > 0 else 1.0

    # Prior volume (5-10 min ago) — must be calm for "fresh spike"
    prior_vols = volumes[-10:-5] if len(volumes) >= 10 else volumes[:3]
    prior_avg = sum(prior_vols) / len(prior_vols) if prior_vols else avg_vol
    sig.prior_vol_ratio = prior_avg / avg_vol if avg_vol > 0 else 1.0

    if sig.vol_ratio < params["min_vol_ratio"]:
        sig.reason = (f"⏸️ VolSpike: Vol ratio {sig.vol_ratio:.2f}x < {params['min_vol_ratio']}x "
                      f"(no spike)")
        return sig

    if sig.prior_vol_ratio > params["max_prior_vol_ratio"]:
        sig.reason = (f"⏸️ VolSpike: Prior vol {sig.prior_vol_ratio:.2f}x > "
                      f"{params['max_prior_vol_ratio']}x (not fresh, spike already started)")
        return sig

    sig.vol_spike_score = min(1.0, (sig.vol_ratio - 1.0) / 3.0)

    # 2. Bollinger Band breach
    sig.bb_pct, bb_upper, bb_lower, bb_sma = _bollinger(
        closes, params["bb_period"], params["bb_std"]
    )
    # Must be outside bands
    if 0.0 <= sig.bb_pct <= 1.0:
        sig.reason = (f"⏸️ VolSpike: Price inside BB ({sig.bb_pct:.2f}) | "
                      f"Vol: {sig.vol_ratio:.2f}x (spike detected but no breakout)")
        return sig

    # How far outside the band?
    if sig.bb_pct > 1.0:
        sig.bb_breach_score = min(1.0, (sig.bb_pct - 1.0) * 2)  # Upside breach
    else:
        sig.bb_breach_score = -min(1.0, (0.0 - sig.bb_pct) * 2)  # Downside breach

    # 3. Candle strength — is the current candle strong?
    last = candles[-1]
    candle_range = last["high"] - last["low"]
    candle_body = last["close"] - last["open"]
    if candle_range > 0:
        sig.candle_strength = candle_body / candle_range  # -1 to +1
    else:
        sig.candle_strength = 0.0

    # Candle must agree with BB breach direction
    if (sig.bb_breach_score > 0 and sig.candle_strength < 0) or \
       (sig.bb_breach_score < 0 and sig.candle_strength > 0):
        sig.reason = (f"⏸️ VolSpike: Candle opposes breakout | BB: {sig.bb_pct:.2f} | "
                      f"Candle: {sig.candle_strength:+.2f} | Vol: {sig.vol_ratio:.2f}x")
        return sig

    # 4. Orderbook confirmation
    sig.ob_score = 0.0
    if kraken_orderbook:
        imb = kraken_orderbook.get("imbalance", 0)
        sig.ob_score = max(-1.0, min(1.0, imb))

    # Composite score — follow the breakout
    w = params
    score = (
        w["vol_spike_weight"] * sig.vol_spike_score * (1 if sig.bb_breach_score > 0 else -1) +
        w["bb_breach_weight"] * sig.bb_breach_score +
        w["orderbook_weight"] * sig.ob_score +
        w["candle_strength_weight"] * sig.candle_strength
    )

    sig.direction = "up" if score > 0 else "down"
    sig.confidence = 0.50 + abs(score) * 0.30  # Map to 0.50-0.80 (high conviction)

    sig.atr_pct = _atr(candles, 14)

    # Edge
    market_price = market_up_price if sig.direction == "up" else market_down_price
    sig.edge_pct = (sig.confidence - market_price) * 100

    if sig.edge_pct < params["min_edge_pct"]:
        sig.reason = (f"⏸️ VolSpike: Edge {sig.edge_pct:.1f}% < {params['min_edge_pct']}% | "
                      f"Vol: {sig.vol_ratio:.2f}x | BB: {sig.bb_pct:.2f} | "
                      f"Candle: {sig.candle_strength:+.2f}")
        return sig

    sig.should_trade = True
    sig.reason = f"🟢 VOL SPIKE: BUY {sig.direction.upper()} @ ${market_price:.3f}"
    return sig


def format_signal_message(sig):
    if sig.should_trade:
        return sig.reason
    return (f"{sig.reason}\n"
            f"  Vol: {sig.vol_ratio:.2f}x | BB%: {sig.bb_pct:.2f} | "
            f"Candle: {sig.candle_strength:+.2f} | OB: {sig.ob_score:+.2f}")
