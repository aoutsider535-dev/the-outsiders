"""
📈 Trend Rider Strategy — Paper Trader v2

Rides sustained BTC trends using EMA crossover + VWAP confirmation.
Uncorrelated from Momentum (which uses raw price change, not EMAs).

Signal:
  - EMA8 vs EMA21 crossover determines bull/bear bias
  - Price vs VWAP confirms institutional direction
  - RSI in trend zone (not overbought/oversold extremes)
  - ATR% must exceed threshold (needs movement, rejects flat markets)

Strengths: Catches sustained trends, filters noise
Weakness: Whipsaws in choppy/ranging markets
"""

from dataclasses import dataclass, asdict
from typing import Optional


DEFAULTS = {
    "min_edge_pct": 5.0,
    "take_profit_pct": 55.0,
    "stop_loss_pct": 30.0,
    "ema_fast": 8,
    "ema_slow": 21,
    "rsi_period": 14,
    "rsi_bull_range": (40, 75),   # RSI zone for bullish trades
    "rsi_bear_range": (25, 60),   # RSI zone for bearish trades
    "min_atr_pct": 0.03,          # Minimum ATR% to trade (reject flat)
    "vwap_confirm_weight": 0.25,
    "ema_cross_weight": 0.35,
    "rsi_weight": 0.20,
    "atr_weight": 0.20,
}


@dataclass
class Signal:
    timestamp: int = 0
    direction: str = ""
    confidence: float = 0.0
    edge_pct: float = 0.0
    market_up_price: float = 0.0
    market_down_price: float = 0.0
    ema_fast: float = 0.0
    ema_slow: float = 0.0
    ema_cross_score: float = 0.0
    vwap: float = 0.0
    vwap_score: float = 0.0
    rsi: float = 50.0
    rsi_score: float = 0.0
    atr_pct: float = 0.0
    atr_score: float = 0.0
    candle_count: int = 0
    should_trade: bool = False
    reason: str = ""

    def to_dict(self):
        return asdict(self)


def _ema(data, period):
    """Exponential moving average."""
    if not data:
        return 0.0
    k = 2 / (period + 1)
    e = data[0]
    for d in data[1:]:
        e = d * k + e * (1 - k)
    return e


def _rsi(closes, period=14):
    """Relative Strength Index."""
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
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def _atr(candles, period=14):
    """Average True Range as % of price."""
    if len(candles) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr_val = sum(trs[-period:]) / min(period, len(trs))
    return atr_val / candles[-1]["close"] * 100 if candles[-1]["close"] > 0 else 0.0


def _vwap(candles):
    """Volume-weighted average price."""
    cum_pv = sum(c.get("vwap", c["close"]) * c["volume"] for c in candles if c["volume"] > 0)
    cum_v = sum(c["volume"] for c in candles if c["volume"] > 0)
    return cum_pv / cum_v if cum_v > 0 else candles[-1]["close"]


def generate_signal(candles, market_up_price=0.5, market_down_price=0.5,
                    params=None, **kwargs):
    """Generate Trend Rider signal."""
    params = params or DEFAULTS.copy()
    sig = Signal(market_up_price=market_up_price, market_down_price=market_down_price)

    if not candles or len(candles) < params["ema_slow"] + 2:
        sig.reason = f"Need {params['ema_slow'] + 2} candles, have {len(candles) if candles else 0}"
        return sig

    sig.candle_count = len(candles)
    closes = [c["close"] for c in candles]

    # 1. EMA Cross
    sig.ema_fast = _ema(closes, params["ema_fast"])
    sig.ema_slow = _ema(closes, params["ema_slow"])
    ema_diff = (sig.ema_fast - sig.ema_slow) / sig.ema_slow * 100 if sig.ema_slow > 0 else 0
    sig.ema_cross_score = max(-1.0, min(1.0, ema_diff * 20))  # Normalize

    # 2. VWAP
    sig.vwap = _vwap(candles)
    vwap_diff = (closes[-1] - sig.vwap) / sig.vwap * 100 if sig.vwap > 0 else 0
    sig.vwap_score = max(-1.0, min(1.0, vwap_diff * 15))

    # 3. RSI
    sig.rsi = _rsi(closes, params["rsi_period"])
    # RSI score: centered around 50, positive = bullish momentum
    sig.rsi_score = (sig.rsi - 50) / 50  # -1 to +1

    # 4. ATR (volatility filter)
    sig.atr_pct = _atr(candles, 14)
    if sig.atr_pct < params["min_atr_pct"]:
        sig.reason = f"⏸️ TrendRider: ATR {sig.atr_pct:.3f}% < {params['min_atr_pct']}% (too flat)"
        return sig
    sig.atr_score = min(1.0, sig.atr_pct / 0.10)  # Normalize, cap at 0.10%

    # Composite score
    w = params
    score = (
        w["ema_cross_weight"] * sig.ema_cross_score +
        w["vwap_confirm_weight"] * sig.vwap_score +
        w["rsi_weight"] * sig.rsi_score +
        w["atr_weight"] * sig.atr_score * (1 if sig.ema_cross_score > 0 else -1)
    )

    # Direction
    sig.direction = "up" if score > 0 else "down"
    sig.confidence = 0.50 + abs(score) * 0.25  # Map to 0.50-0.75

    # RSI filter: must be in trend zone
    rsi_range = params["rsi_bull_range"] if sig.direction == "up" else params["rsi_bear_range"]
    if not (rsi_range[0] <= sig.rsi <= rsi_range[1]):
        sig.reason = (f"⏸️ TrendRider: RSI {sig.rsi:.0f} outside {sig.direction} zone "
                      f"({rsi_range[0]}-{rsi_range[1]}) | EMA: {sig.ema_cross_score:+.2f}")
        return sig

    # EMA + VWAP must agree
    if (sig.ema_cross_score > 0) != (sig.vwap_score > 0):
        sig.reason = (f"⏸️ TrendRider: EMA/VWAP disagree | EMA: {sig.ema_cross_score:+.2f} "
                      f"VWAP: {sig.vwap_score:+.2f}")
        return sig

    # Edge calculation
    market_price = market_up_price if sig.direction == "up" else market_down_price
    sig.edge_pct = (sig.confidence - market_price) * 100

    if sig.edge_pct < params["min_edge_pct"]:
        sig.reason = (f"⏸️ TrendRider: Edge {sig.edge_pct:.1f}% < {params['min_edge_pct']}% | "
                      f"Score: {score:+.3f} | EMA: {sig.ema_cross_score:+.2f} | "
                      f"VWAP: {sig.vwap_score:+.2f} | RSI: {sig.rsi:.0f}")
        return sig

    sig.should_trade = True
    sig.reason = (f"🟢 TREND RIDER: BUY {sig.direction.upper()} @ ${market_price:.3f}")
    return sig


def format_signal_message(sig):
    """Format signal for logging."""
    if sig.should_trade:
        return sig.reason
    return (f"{sig.reason}\n"
            f"  EMA: {sig.ema_cross_score:+.2f} | VWAP: {sig.vwap_score:+.2f} | "
            f"RSI: {sig.rsi:.0f} ({sig.rsi_score:+.2f}) | ATR: {sig.atr_pct:.3f}%")
