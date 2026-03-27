"""
🎯 VWAP Sniper Strategy — Paper Trader v2

Mean-reversion for ranging/flat markets using VWAP deviation + RSI extremes.
Only trades when volatility is LOW (ATR filter), the inverse of Trend Rider.

Signal:
  - Price deviates significantly from VWAP
  - RSI at extreme (<30 oversold → buy UP, >70 overbought → buy DOWN)
  - Bollinger Band position confirms overextension
  - ATR% must be LOW (ranging market only)

Strengths: Captures reversions during flat periods other strategies skip
Weakness: Trend starts → gets wrecked (ATR filter protects)
"""

from dataclasses import dataclass, asdict


DEFAULTS = {
    "min_edge_pct": 4.0,
    "take_profit_pct": 50.0,
    "stop_loss_pct": 30.0,
    "rsi_period": 14,
    "rsi_oversold": 30,
    "rsi_overbought": 70,
    "bb_period": 20,
    "bb_std": 2.0,
    "max_atr_pct": 0.05,          # MAX ATR% — only trade in calm markets
    "min_vwap_dev_pct": 0.02,     # Min % deviation from VWAP
    "vwap_weight": 0.30,
    "rsi_weight": 0.35,
    "bb_weight": 0.25,
    "reversion_weight": 0.10,     # Are recent candles starting to revert?
}


@dataclass
class Signal:
    timestamp: int = 0
    direction: str = ""
    confidence: float = 0.0
    edge_pct: float = 0.0
    market_up_price: float = 0.0
    market_down_price: float = 0.0
    vwap: float = 0.0
    vwap_dev_pct: float = 0.0
    vwap_score: float = 0.0
    rsi: float = 50.0
    rsi_score: float = 0.0
    bb_pct: float = 0.5
    bb_score: float = 0.0
    reversion_score: float = 0.0
    atr_pct: float = 0.0
    candle_count: int = 0
    should_trade: bool = False
    reason: str = ""

    def to_dict(self):
        return asdict(self)


def _rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(0, d))
        losses.append(max(0, -d))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    return 100 - 100 / (1 + avg_gain / avg_loss)


def _atr(candles, period=14):
    if len(candles) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr_val = sum(trs[-period:]) / min(period, len(trs))
    return atr_val / candles[-1]["close"] * 100 if candles[-1]["close"] > 0 else 0.0


def _vwap(candles):
    cum_pv = sum(c.get("vwap", c["close"]) * c["volume"] for c in candles if c["volume"] > 0)
    cum_v = sum(c["volume"] for c in candles if c["volume"] > 0)
    return cum_pv / cum_v if cum_v > 0 else candles[-1]["close"]


def _bollinger(closes, period=20, std_mult=2.0):
    """Returns (bb_pct, upper, lower, sma). bb_pct: 0=lower band, 1=upper band."""
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


def generate_signal(candles, market_up_price=0.5, market_down_price=0.5,
                    params=None, **kwargs):
    """Generate VWAP Sniper signal."""
    params = params or DEFAULTS.copy()
    sig = Signal(market_up_price=market_up_price, market_down_price=market_down_price)

    if not candles or len(candles) < params["bb_period"] + 2:
        sig.reason = f"Need {params['bb_period'] + 2} candles, have {len(candles) if candles else 0}"
        return sig

    sig.candle_count = len(candles)
    closes = [c["close"] for c in candles]

    # 1. ATR filter — MUST be calm market
    sig.atr_pct = _atr(candles, 14)
    if sig.atr_pct > params["max_atr_pct"]:
        sig.reason = (f"⏸️ VWAPSniper: ATR {sig.atr_pct:.3f}% > {params['max_atr_pct']}% "
                      f"(too volatile, Trend Rider territory)")
        return sig

    # 2. VWAP deviation
    sig.vwap = _vwap(candles)
    sig.vwap_dev_pct = (closes[-1] - sig.vwap) / sig.vwap * 100 if sig.vwap > 0 else 0
    if abs(sig.vwap_dev_pct) < params["min_vwap_dev_pct"]:
        sig.reason = (f"⏸️ VWAPSniper: VWAP dev {sig.vwap_dev_pct:+.3f}% too small "
                      f"(need ±{params['min_vwap_dev_pct']}%)")
        return sig
    # Negative dev = price below VWAP = buy UP (reversion), positive = buy DOWN
    sig.vwap_score = max(-1.0, min(1.0, -sig.vwap_dev_pct * 20))

    # 3. RSI — need extremes
    sig.rsi = _rsi(closes, params["rsi_period"])
    if params["rsi_oversold"] < sig.rsi < params["rsi_overbought"]:
        sig.reason = (f"⏸️ VWAPSniper: RSI {sig.rsi:.0f} not extreme "
                      f"(need <{params['rsi_oversold']} or >{params['rsi_overbought']}) | "
                      f"VWAP dev: {sig.vwap_dev_pct:+.3f}%")
        return sig
    # Oversold (low RSI) → buy UP, Overbought (high RSI) → buy DOWN
    sig.rsi_score = -(sig.rsi - 50) / 50  # Inverted: low RSI = positive (buy up)

    # 4. Bollinger Bands
    sig.bb_pct, _, _, _ = _bollinger(closes, params["bb_period"], params["bb_std"])
    # Below lower band → buy UP, above upper → buy DOWN
    sig.bb_score = -(sig.bb_pct - 0.5) * 2  # Inverted: low bb_pct = positive

    # 5. Reversion detection — are last 2-3 candles starting to reverse?
    if len(closes) >= 3:
        recent_move = closes[-1] - closes[-3]
        prior_move = closes[-3] - closes[-6] if len(closes) >= 6 else 0
        # Reversion = recent move opposite to prior move
        if prior_move != 0:
            sig.reversion_score = max(-1.0, min(1.0, -recent_move / abs(prior_move)))
        else:
            sig.reversion_score = 0.0

    # Composite score
    w = params
    score = (
        w["vwap_weight"] * sig.vwap_score +
        w["rsi_weight"] * sig.rsi_score +
        w["bb_weight"] * sig.bb_score +
        w["reversion_weight"] * sig.reversion_score
    )

    # Direction (contrarian — fade the move)
    sig.direction = "up" if score > 0 else "down"
    sig.confidence = 0.50 + abs(score) * 0.25

    # All signals must agree on direction
    if sig.direction == "up" and (sig.rsi > 50 or sig.vwap_dev_pct > 0):
        sig.reason = (f"⏸️ VWAPSniper: Mixed signals for UP | RSI: {sig.rsi:.0f} | "
                      f"VWAP dev: {sig.vwap_dev_pct:+.3f}%")
        return sig
    if sig.direction == "down" and (sig.rsi < 50 or sig.vwap_dev_pct < 0):
        sig.reason = (f"⏸️ VWAPSniper: Mixed signals for DOWN | RSI: {sig.rsi:.0f} | "
                      f"VWAP dev: {sig.vwap_dev_pct:+.3f}%")
        return sig

    # Edge
    market_price = market_up_price if sig.direction == "up" else market_down_price
    sig.edge_pct = (sig.confidence - market_price) * 100

    if sig.edge_pct < params["min_edge_pct"]:
        sig.reason = (f"⏸️ VWAPSniper: Edge {sig.edge_pct:.1f}% < {params['min_edge_pct']}% | "
                      f"RSI: {sig.rsi:.0f} | VWAP: {sig.vwap_dev_pct:+.3f}% | "
                      f"BB: {sig.bb_pct:.2f}")
        return sig

    sig.should_trade = True
    sig.reason = f"🟢 VWAP SNIPER: BUY {sig.direction.upper()} @ ${market_price:.3f}"
    return sig


def format_signal_message(sig):
    if sig.should_trade:
        return sig.reason
    return (f"{sig.reason}\n"
            f"  VWAP: {sig.vwap_dev_pct:+.3f}% | RSI: {sig.rsi:.0f} | "
            f"BB%: {sig.bb_pct:.2f} | ATR: {sig.atr_pct:.3f}%")
