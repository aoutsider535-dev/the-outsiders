"""
📊 Higher Timeframe (HTF) Context Module

Provides 1hr and 4hr trend analysis for any strategy to use as an overlay.
Fetches hourly candles from CryptoCompare (free, no key needed).
Caches results to avoid redundant API calls within the same cycle.
"""
import time
import requests
from datetime import datetime, timezone

_cache = {"candles_1h": [], "last_fetch": 0, "ttl": 300}  # 5 min cache


def fetch_hourly_candles(limit=48):
    """Fetch recent 1hr BTC/USD candles. Cached for 5 minutes."""
    now = time.time()
    if _cache["candles_1h"] and (now - _cache["last_fetch"]) < _cache["ttl"]:
        return _cache["candles_1h"]
    
    try:
        resp = requests.get(
            "https://min-api.cryptocompare.com/data/v2/histohour",
            params={"fsym": "BTC", "tsym": "USD", "limit": limit},
            timeout=10
        )
        if resp.status_code == 200 and resp.json().get("Response") == "Success":
            candles = resp.json()["Data"]["Data"]
            _cache["candles_1h"] = candles
            _cache["last_fetch"] = now
            return candles
    except Exception as e:
        print(f"⚠️ HTF fetch error: {e}")
    
    return _cache["candles_1h"]  # Return stale cache on error


def _ema(values, period):
    if not values:
        return 0
    k = 2 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def _rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    gains = [max(0, closes[i] - closes[i-1]) for i in range(1, len(closes))]
    losses = [max(0, closes[i-1] - closes[i]) for i in range(1, len(closes))]
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    return 100 - 100 / (1 + ag / al) if al > 0 else 100.0


def get_htf_context():
    """
    Get current higher timeframe context.
    
    Returns dict:
        h1_trend: float (-1 to +1, bearish to bullish)
        h1_rsi: float (0-100)
        h1_ema_cross: float (-1 to +1)
        h1_momentum: float (recent % change)
        h4_trend: float (-1 to +1)
        h1_bullish: bool (trend > 0.1)
        h1_bearish: bool (trend < -0.1)
        h4_bullish: bool (trend > 0.05)
        h4_bearish: bool (trend < -0.05)
        description: str (human-readable summary)
    """
    candles = fetch_hourly_candles(48)
    
    ctx = {
        "h1_trend": 0.0,
        "h1_rsi": 50.0,
        "h1_ema_cross": 0.0,
        "h1_momentum": 0.0,
        "h4_trend": 0.0,
        "h1_bullish": False,
        "h1_bearish": False,
        "h4_bullish": False,
        "h4_bearish": False,
        "description": "No data",
    }
    
    if not candles or len(candles) < 24:
        return ctx
    
    closes = [c["close"] for c in candles]
    
    # 1hr EMA cross
    ema8 = _ema(closes, 8)
    ema21 = _ema(closes, 21)
    if ema21 > 0:
        ctx["h1_ema_cross"] = max(-1, min(1, (ema8 - ema21) / ema21 * 100))
    
    # 1hr RSI
    ctx["h1_rsi"] = _rsi(closes)
    
    # 1hr momentum (last 3 hours)
    if len(closes) >= 3 and closes[-3] > 0:
        ctx["h1_momentum"] = (closes[-1] - closes[-3]) / closes[-3] * 100
    
    # Composite 1hr trend
    ctx["h1_trend"] = max(-1, min(1,
        0.4 * ctx["h1_ema_cross"] +
        0.3 * ((ctx["h1_rsi"] - 50) / 50) +
        0.3 * max(-1, min(1, ctx["h1_momentum"] * 5))
    ))
    
    # 4hr trend (derived from hourly — last 12 hours in 4hr chunks)
    if len(closes) >= 12:
        c_now = closes[-1]
        c_12h = closes[-12]
        if c_12h > 0:
            ctx["h4_trend"] = max(-1, min(1, (c_now - c_12h) / c_12h * 100 * 3))
    
    # Boolean flags
    ctx["h1_bullish"] = ctx["h1_trend"] > 0.1
    ctx["h1_bearish"] = ctx["h1_trend"] < -0.1
    ctx["h4_bullish"] = ctx["h4_trend"] > 0.05
    ctx["h4_bearish"] = ctx["h4_trend"] < -0.05
    
    # Description
    h1_dir = "bullish" if ctx["h1_bullish"] else "bearish" if ctx["h1_bearish"] else "neutral"
    h4_dir = "bullish" if ctx["h4_bullish"] else "bearish" if ctx["h4_bearish"] else "neutral"
    ctx["description"] = f"1H: {h1_dir} ({ctx['h1_trend']:+.2f}) | 4H: {h4_dir} ({ctx['h4_trend']:+.2f}) | RSI: {ctx['h1_rsi']:.0f}"
    
    return ctx


def htf_agrees_with(direction, ctx=None):
    """Check if HTF context agrees with a given trade direction."""
    if ctx is None:
        ctx = get_htf_context()
    
    if direction == "up":
        return ctx["h1_bullish"]
    else:
        return ctx["h1_bearish"]


def htf_strongly_agrees(direction, ctx=None):
    """Check if BOTH 1hr and 4hr agree with direction."""
    if ctx is None:
        ctx = get_htf_context()
    
    if direction == "up":
        return ctx["h1_bullish"] and ctx["h4_bullish"]
    else:
        return ctx["h1_bearish"] and ctx["h4_bearish"]
