"""
Feature engineering for the ML meta-learner.
Extracts market regime features at signal time for predicting trade outcomes.
"""
import numpy as np
from datetime import datetime, timezone, timedelta

PST = timezone(timedelta(hours=-8))


def extract_features(candles, signal, strategy_key, strategy_state, market_snapshot=None,
                     htf_context=None, derivatives=None, orderbook=None):
    """
    Extract feature vector from current market state + signal.
    Returns dict of named features (all numeric).
    """
    features = {}
    
    if not candles or len(candles) < 20:
        return None
    
    closes = [c["close"] for c in candles]
    volumes = [c.get("volume", 0) for c in candles]
    highs = [c.get("high", c["close"]) for c in candles]
    lows = [c.get("low", c["close"]) for c in candles]
    
    # ─── TIME FEATURES ───
    now_pst = datetime.now(PST)
    features["hour_pst"] = now_pst.hour + now_pst.minute / 60.0
    features["is_overnight"] = 1.0 if now_pst.hour < 6 or now_pst.hour >= 23 else 0.0
    features["is_market_hours"] = 1.0 if 6 <= now_pst.hour < 16 else 0.0  # US market hours
    features["day_of_week"] = now_pst.weekday()  # 0=Mon, 6=Sun
    features["is_weekend"] = 1.0 if now_pst.weekday() >= 5 else 0.0
    
    # ─── PRICE MOMENTUM (multiple timeframes) ───
    if len(closes) >= 5:
        features["returns_5m"] = (closes[-1] - closes[-2]) / closes[-2] * 100 if closes[-2] > 0 else 0
        features["returns_15m"] = (closes[-1] - closes[-4]) / closes[-4] * 100 if closes[-4] > 0 else 0
    if len(closes) >= 10:
        features["returns_30m"] = (closes[-1] - closes[-7]) / closes[-7] * 100 if closes[-7] > 0 else 0
    if len(closes) >= 20:
        features["returns_60m"] = (closes[-1] - closes[-13]) / closes[-13] * 100 if closes[-13] > 0 else 0
    
    # ─── VOLATILITY REGIME ───
    recent_returns = []
    for i in range(1, min(len(closes), 20)):
        if closes[i-1] > 0:
            recent_returns.append((closes[i] - closes[i-1]) / closes[i-1] * 100)
    
    if recent_returns:
        features["volatility_20"] = np.std(recent_returns)
        features["volatility_5"] = np.std(recent_returns[-5:]) if len(recent_returns) >= 5 else features["volatility_20"]
        features["vol_regime"] = features["volatility_5"] / features["volatility_20"] if features["volatility_20"] > 0 else 1.0
    
    # ─── TREND STRENGTH ───
    if len(closes) >= 20:
        ema8 = _ema(closes, 8)
        ema21 = _ema(closes, 21)
        features["ema_spread"] = (ema8 - ema21) / ema21 * 100 if ema21 > 0 else 0
        features["ema_spread_abs"] = abs(features["ema_spread"])
        
        # Trend consistency: how many of last 10 candles moved in same direction as EMA
        ema_direction = 1 if ema8 > ema21 else -1
        candle_directions = [1 if closes[i] > closes[i-1] else -1 for i in range(-10, 0) if i-1 >= -len(closes)]
        if candle_directions:
            features["trend_consistency"] = sum(1 for d in candle_directions if d == ema_direction) / len(candle_directions)
    
    # ─── RSI ───
    rsi = _rsi(closes, 14)
    features["rsi"] = rsi
    features["rsi_extreme"] = 1.0 if rsi < 30 or rsi > 70 else 0.0
    features["rsi_distance_50"] = abs(rsi - 50)
    
    # ─── VOLUME PROFILE ───
    if volumes and sum(volumes) > 0:
        avg_vol = np.mean(volumes[-20:]) if len(volumes) >= 20 else np.mean(volumes)
        features["volume_ratio"] = volumes[-1] / avg_vol if avg_vol > 0 else 1.0
        features["volume_trend"] = (np.mean(volumes[-5:]) / np.mean(volumes[-20:]) 
                                     if len(volumes) >= 20 and np.mean(volumes[-20:]) > 0 else 1.0)
    
    # ─── SIGNAL FEATURES ───
    features["signal_direction"] = 1.0 if signal.direction == "up" else -1.0
    features["signal_confidence"] = signal.confidence
    features["signal_edge"] = signal.edge_pct
    
    # ─── STRATEGY STATE FEATURES ───
    features["strategy_win_rate"] = (strategy_state.wins / max(strategy_state.trade_count, 1))
    features["strategy_consecutive_losses"] = float(strategy_state.consecutive_losses)
    features["strategy_trade_count"] = float(strategy_state.trade_count)
    features["strategy_kelly"] = strategy_state.kelly_fraction
    
    # Recent direction performance (did this direction win recently?)
    features["strategy_recent_pnl"] = strategy_state.total_pnl
    
    # ─── MARKET MICROSTRUCTURE ───
    if market_snapshot:
        up_price = market_snapshot.get("up_price", 0.5)
        down_price = market_snapshot.get("down_price", 0.5)
        features["market_spread"] = abs(up_price + down_price - 1.0)
        features["market_skew"] = up_price - down_price
    
    # ─── HTF CONTEXT (1hr / 4hr trends) ───
    if htf_context:
        features["htf_h1_trend"] = float(htf_context.get("h1_trend", 0))
        features["htf_h4_trend"] = float(htf_context.get("h4_trend", 0))
        features["htf_h1_rsi"] = float(htf_context.get("h1_rsi", 50))
        features["htf_h1_rsi_extreme"] = 1.0 if htf_context.get("h1_rsi", 50) < 30 or htf_context.get("h1_rsi", 50) > 70 else 0.0
        # Trend agreement: both timeframes same direction
        h1 = htf_context.get("h1_trend", 0)
        h4 = htf_context.get("h4_trend", 0)
        features["htf_aligned"] = 1.0 if (h1 > 0 and h4 > 0) or (h1 < 0 and h4 < 0) else 0.0
        # Does HTF agree with signal direction?
        sig_dir = 1.0 if signal.direction == "up" else -1.0
        features["htf_confirms_signal"] = 1.0 if (h1 * sig_dir > 0) else -1.0
        features["htf_h4_confirms_signal"] = 1.0 if (h4 * sig_dir > 0) else -1.0
    else:
        for k in ["htf_h1_trend", "htf_h4_trend", "htf_h1_rsi", "htf_h1_rsi_extreme",
                   "htf_aligned", "htf_confirms_signal", "htf_h4_confirms_signal"]:
            features[k] = 0.0
    
    # ─── DERIVATIVES (funding rate + open interest) ───
    if derivatives:
        features["deriv_funding_rate"] = float(derivatives.get("funding_rate", 0))
        features["deriv_oi_change"] = float(derivatives.get("oi_change_pct", 0))
        features["deriv_combined_score"] = float(derivatives.get("combined_score", 0))
        # Funding rate extremes
        fr = derivatives.get("funding_rate", 0)
        features["deriv_funding_extreme"] = 1.0 if abs(fr) > 0.01 else 0.0
        # Does derivatives agree with signal?
        deriv_dir = 1.0 if derivatives.get("combined_score", 0) > 0 else -1.0
        sig_dir = 1.0 if signal.direction == "up" else -1.0
        features["deriv_confirms_signal"] = 1.0 if deriv_dir * sig_dir > 0 else -1.0
    else:
        for k in ["deriv_funding_rate", "deriv_oi_change", "deriv_combined_score",
                   "deriv_funding_extreme", "deriv_confirms_signal"]:
            features[k] = 0.0
    
    # ─── ORDERBOOK DEPTH / IMBALANCE ───
    if orderbook:
        bid_depth = float(orderbook.get("bid_depth", 0))
        ask_depth = float(orderbook.get("ask_depth", 0))
        total_depth = bid_depth + ask_depth
        features["ob_total_depth"] = total_depth
        features["ob_imbalance"] = (bid_depth - ask_depth) / total_depth if total_depth > 0 else 0.0
        features["ob_bid_depth"] = bid_depth
        features["ob_ask_depth"] = ask_depth
        # Thin market flag
        features["ob_thin_market"] = 1.0 if total_depth < 100 else 0.0
        # Does OB imbalance agree with signal? (more bids = bullish)
        ob_dir = 1.0 if bid_depth > ask_depth else -1.0
        sig_dir = 1.0 if signal.direction == "up" else -1.0
        features["ob_confirms_signal"] = 1.0 if ob_dir * sig_dir > 0 else -1.0
    else:
        for k in ["ob_total_depth", "ob_imbalance", "ob_bid_depth", "ob_ask_depth",
                   "ob_thin_market", "ob_confirms_signal"]:
            features[k] = 0.0
    
    # Also capture Polymarket-specific orderbook if in market_snapshot
    if market_snapshot:
        up_ob = market_snapshot.get("up_orderbook")
        down_ob = market_snapshot.get("down_orderbook")
        if up_ob and down_ob:
            poly_bid = up_ob.get("bid_depth", 0) + down_ob.get("bid_depth", 0)
            poly_ask = up_ob.get("ask_depth", 0) + down_ob.get("ask_depth", 0)
            poly_total = poly_bid + poly_ask
            features["poly_ob_depth"] = poly_total
            features["poly_ob_imbalance"] = (poly_bid - poly_ask) / poly_total if poly_total > 0 else 0.0
        else:
            features["poly_ob_depth"] = 0.0
            features["poly_ob_imbalance"] = 0.0
    else:
        features["poly_ob_depth"] = 0.0
        features["poly_ob_imbalance"] = 0.0
    
    # ─── OUTCOME STREAK ───
    # Recent market outcomes (if available from strategy state)
    features["direction_streak"] = _get_direction_streak(strategy_state)
    
    # ─── STRATEGY ENCODING ───
    # One-hot encode strategy
    for s in ["trend_rider", "orderbook_imbalance", "smart_money"]:
        features[f"is_{s}"] = 1.0 if strategy_key == s else 0.0
    
    return features


def add_cross_strategy_features(features, all_signals):
    """
    Enrich feature dict with cross-strategy context.
    
    Args:
        features: existing feature dict for one strategy
        all_signals: list of dicts with {strategy_key, direction, confidence, edge_pct, should_trade}
    """
    if not all_signals:
        features["n_strategies_firing"] = 0.0
        features["strategy_agreement"] = 0.0
        features["avg_confidence_all"] = 0.0
        features["max_edge_all"] = 0.0
        features["direction_consensus"] = 0.0
        return features
    
    # How many strategies want to trade this window?
    firing = [s for s in all_signals if s.get("should_trade")]
    features["n_strategies_firing"] = float(len(firing))
    
    if not firing:
        features["strategy_agreement"] = 0.0
        features["avg_confidence_all"] = 0.0
        features["max_edge_all"] = 0.0
        features["direction_consensus"] = 0.0
        return features
    
    # Direction consensus: +1 = all agree UP, -1 = all agree DOWN, 0 = split
    up_count = sum(1 for s in firing if s["direction"] == "up")
    down_count = len(firing) - up_count
    features["direction_consensus"] = (up_count - down_count) / len(firing) if firing else 0.0
    
    # Agreement: do all firing strategies agree on direction?
    features["strategy_agreement"] = 1.0 if (up_count == len(firing) or down_count == len(firing)) else 0.0
    
    # Aggregate signal quality
    features["avg_confidence_all"] = np.mean([s["confidence"] for s in firing])
    features["max_edge_all"] = max(s["edge_pct"] for s in firing)
    
    return features


def _get_direction_streak(state):
    """Count consecutive same-direction outcomes from recent trades."""
    if not hasattr(state, 'recent_outcomes') or not state.recent_outcomes:
        return 0
    
    streak = 0
    last_dir = state.recent_outcomes[-1] if state.recent_outcomes else None
    for outcome in reversed(state.recent_outcomes):
        if outcome == last_dir:
            streak += 1
        else:
            break
    return streak if last_dir == "up" else -streak


def _ema(data, period):
    """Exponential moving average."""
    if len(data) < period:
        return data[-1] if data else 0
    k = 2 / (period + 1)
    ema = sum(data[:period]) / period
    for val in data[period:]:
        ema = val * k + ema * (1 - k)
    return ema


def _rsi(data, period=14):
    """Relative Strength Index."""
    if len(data) < period + 1:
        return 50
    deltas = [data[i] - data[i-1] for i in range(1, len(data))]
    gains = [d if d > 0 else 0 for d in deltas[-period:]]
    losses = [-d if d < 0 else 0 for d in deltas[-period:]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))
