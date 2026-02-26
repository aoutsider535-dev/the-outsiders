"""
🏞 The Outsiders — Enhanced Backtest with Higher Timeframe (HTF) Analysis

Tests Streak Reversal and Volume Flow Inverse enhanced with:
- 1hr candle trend (EMA, RSI, VWAP)
- 4hr candle trend (macro direction)
- Time-of-day bias

Splits data into 3 sets:
- Training (first 50%): develop the signals
- Validation (next 20%): tune parameters  
- Out-of-Sample (last 30%): final test — NO peeking
"""
import json
import os
import sys
import math
from collections import defaultdict
from datetime import datetime, timezone, timedelta

DATA_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "historical_extended.json")
TAKER_FEE_PCT = 0.02
SLIPPAGE_PCT = 0.005
TRADE_SIZE = 5.0
PST = timezone(timedelta(hours=-8))


def load_data():
    with open(DATA_PATH) as f:
        data = json.load(f)
    return data["candles_1m"], data["candles_1h"], data["candles_4h"], data["outcomes"]


def simulate_trade(direction, winner, entry_price=0.5):
    fill_price = min(entry_price * (1 + SLIPPAGE_PCT), 0.99)
    quantity = TRADE_SIZE / fill_price
    cost = fill_price * quantity
    fee = cost * TAKER_FEE_PCT
    won = (direction == winner)
    if won:
        net_pnl = quantity * (1.0 - fill_price) - fee
    else:
        net_pnl = -(cost + fee)
    return net_pnl, won


def ema(values, period):
    if not values:
        return 0
    k = 2 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(0, d))
        losses.append(max(0, -d))
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def get_htf_context(t, candles_1h_map, candles_4h_map, candles_1h_sorted):
    """Get higher timeframe context for a given timestamp."""
    ctx = {
        "h1_trend": 0,       # -1 to +1: bearish to bullish
        "h1_rsi": 50,
        "h1_ema_cross": 0,   # EMA8 vs EMA21 on hourly
        "h1_momentum": 0,    # Recent hourly price change
        "h4_trend": 0,       # -1 to +1: 4hr direction
        "h4_momentum": 0,
        "hour_of_day": 0,    # UTC hour
    }
    
    # Get relevant hourly candles (last 24 hours)
    h1_window = []
    for c in candles_1h_sorted:
        if c["time"] <= t and c["time"] > t - 86400:
            h1_window.append(c)
    
    if len(h1_window) >= 21:
        closes = [c["close"] for c in h1_window]
        ema8 = ema(closes, 8)
        ema21 = ema(closes, 21)
        
        # EMA cross direction
        if ema21 > 0:
            ctx["h1_ema_cross"] = max(-1, min(1, (ema8 - ema21) / ema21 * 100))
        
        # RSI
        ctx["h1_rsi"] = rsi(closes)
        
        # Recent momentum (last 3 hours)
        if len(closes) >= 3:
            ctx["h1_momentum"] = (closes[-1] - closes[-3]) / closes[-3] * 100 if closes[-3] > 0 else 0
        
        # Overall trend score
        ctx["h1_trend"] = max(-1, min(1, 
            0.4 * ctx["h1_ema_cross"] + 
            0.3 * ((ctx["h1_rsi"] - 50) / 50) +
            0.3 * max(-1, min(1, ctx["h1_momentum"] * 5))
        ))
    
    # 4hr context
    h4_window = []
    for ts in sorted(candles_4h_map.keys()):
        if ts <= t and ts > t - 172800:  # Last 48 hours
            h4_window.append(candles_4h_map[ts])
    
    if len(h4_window) >= 3:
        closes_4h = [c["close"] for c in h4_window]
        ctx["h4_momentum"] = (closes_4h[-1] - closes_4h[-3]) / closes_4h[-3] * 100 if closes_4h[-3] > 0 else 0
        ctx["h4_trend"] = max(-1, min(1, ctx["h4_momentum"] * 3))
    
    # Hour of day (UTC)
    ctx["hour_of_day"] = datetime.fromtimestamp(t, tz=timezone.utc).hour
    
    return ctx


def print_results(stats):
    print(f"\n  {'Strategy':<40} {'Trades':>6} {'Wins':>5} {'WR':>7} {'P&L':>10} {'Avg':>8}")
    print(f"  {'-'*78}")
    for key in sorted(stats.keys(), key=lambda k: stats[k]["pnl"], reverse=True):
        s = stats[key]
        if s["trades"] == 0:
            continue
        wr = s["wins"] / s["trades"] * 100
        avg = s["pnl"] / s["trades"]
        emoji = "✅" if s["pnl"] > 0 else "❌"
        print(f"  {emoji} {key:<38} {s['trades']:>6} {s['wins']:>5} {wr:>6.1f}% ${s['pnl']:>+8.2f} ${avg:>+6.2f}")


def backtest(outcomes, candles_1m, candles_1h_sorted, candles_1h_map, candles_4h_map, label):
    print(f"\n{'='*70}")
    print(f"  {label}")
    print(f"  Windows: {len(outcomes)}")
    print(f"{'='*70}")
    
    price_map_1m = {c["time"]: c for c in candles_1m}
    
    stats = {}
    strategies = [
        # Streak Reversal variants
        "streak_fade_DOWN3",
        "streak_fade_DOWN3+HTF",         # Only when 1hr is bullish
        "streak_fade_DOWN3+HTF_strict",   # Only when 1hr AND 4hr bullish
        "streak_fade_DOWN4",
        "streak_fade_DOWN4+HTF",
        
        # Volume Flow Inverse variants
        "volflow_inv",
        "volflow_inv+HTF",               # Only when HTF agrees with inverse
        "volflow_inv+HTF_strict",
        
        # Combined: streak + volume
        "combined_streak_vol",
        "combined_streak_vol+HTF",
        
        # HTF-only strategies
        "htf_trend_follow",              # Just follow the 1hr trend
        "htf_h1_rsi_extreme",            # Trade RSI extremes on 1hr
        "htf_h1h4_aligned",              # Only trade when 1hr and 4hr agree
        
        # Baselines
        "baseline_random",
        "baseline_always_up",
    ]
    for s in strategies:
        stats[s] = {"wins": 0, "losses": 0, "trades": 0, "pnl": 0.0}
    
    import random
    random.seed(42)
    
    streak_up = 0
    streak_down = 0
    
    for i, outcome in enumerate(outcomes):
        t = outcome["time"]
        winner = outcome["outcome"]
        
        # Baselines
        pnl_r, won_r = simulate_trade(random.choice(["up", "down"]), winner)
        stats["baseline_random"]["trades"] += 1
        stats["baseline_random"]["pnl"] += pnl_r
        stats["baseline_random"]["wins" if won_r else "losses"] += 1
        
        pnl_u, won_u = simulate_trade("up", winner)
        stats["baseline_always_up"]["trades"] += 1
        stats["baseline_always_up"]["pnl"] += pnl_u
        stats["baseline_always_up"]["wins" if won_u else "losses"] += 1
        
        # Get HTF context
        htf = get_htf_context(t, candles_1h_map, candles_4h_map, candles_1h_sorted)
        h1_bullish = htf["h1_trend"] > 0.1
        h1_bearish = htf["h1_trend"] < -0.1
        h4_bullish = htf["h4_trend"] > 0.05
        h4_bearish = htf["h4_trend"] < -0.05
        h1_rsi = htf["h1_rsi"]
        
        # === STREAK REVERSAL ===
        if i > 0:
            # Fade DOWN 3+ (bet UP)
            if streak_down >= 3:
                pnl, won = simulate_trade("up", winner)
                stats["streak_fade_DOWN3"]["trades"] += 1
                stats["streak_fade_DOWN3"]["pnl"] += pnl
                stats["streak_fade_DOWN3"]["wins" if won else "losses"] += 1
                
                # HTF enhanced: only fade DOWN when 1hr is bullish
                if h1_bullish:
                    pnl2, won2 = simulate_trade("up", winner)
                    stats["streak_fade_DOWN3+HTF"]["trades"] += 1
                    stats["streak_fade_DOWN3+HTF"]["pnl"] += pnl2
                    stats["streak_fade_DOWN3+HTF"]["wins" if won2 else "losses"] += 1
                
                # Strict: 1hr AND 4hr both bullish
                if h1_bullish and h4_bullish:
                    pnl3, won3 = simulate_trade("up", winner)
                    stats["streak_fade_DOWN3+HTF_strict"]["trades"] += 1
                    stats["streak_fade_DOWN3+HTF_strict"]["pnl"] += pnl3
                    stats["streak_fade_DOWN3+HTF_strict"]["wins" if won3 else "losses"] += 1
            
            if streak_down >= 4:
                pnl, won = simulate_trade("up", winner)
                stats["streak_fade_DOWN4"]["trades"] += 1
                stats["streak_fade_DOWN4"]["pnl"] += pnl
                stats["streak_fade_DOWN4"]["wins" if won else "losses"] += 1
                
                if h1_bullish:
                    pnl2, won2 = simulate_trade("up", winner)
                    stats["streak_fade_DOWN4+HTF"]["trades"] += 1
                    stats["streak_fade_DOWN4+HTF"]["pnl"] += pnl2
                    stats["streak_fade_DOWN4+HTF"]["wins" if won2 else "losses"] += 1
        
        # === VOLUME FLOW INVERSE ===
        lookback = []
        for offset in range(15, 0, -1):
            ct = t - offset * 60
            if ct in price_map_1m:
                lookback.append(price_map_1m[ct])
        
        if len(lookback) >= 10:
            recent_vols = [c.get("volumefrom", c.get("volume", 0)) for c in lookback[-3:]]
            baseline_vols = [c.get("volumefrom", c.get("volume", 0)) for c in lookback[-10:-3]]
            avg_recent = sum(recent_vols) / len(recent_vols) if recent_vols else 0
            avg_baseline = sum(baseline_vols) / len(baseline_vols) if baseline_vols else 1
            vol_ratio = avg_recent / avg_baseline if avg_baseline > 0 else 1
            
            recent_direction = 0
            for c in lookback[-3:]:
                body = c["close"] - c["open"]
                c_range = c["high"] - c["low"]
                if c_range > 0:
                    strength = body / c_range
                    vol_weight = c.get("volumefrom", c.get("volume", 0)) / avg_baseline if avg_baseline > 0 else 1
                    recent_direction += strength * vol_weight
            
            if vol_ratio > 1.5 and abs(recent_direction) > 0.5:
                # Inverse: bet AGAINST the flow
                inv_dir = "down" if recent_direction > 0 else "up"
                
                pnl, won = simulate_trade(inv_dir, winner)
                stats["volflow_inv"]["trades"] += 1
                stats["volflow_inv"]["pnl"] += pnl
                stats["volflow_inv"]["wins" if won else "losses"] += 1
                
                # HTF: only take inverse when HTF agrees with our inverse direction
                htf_agrees = (inv_dir == "up" and h1_bullish) or (inv_dir == "down" and h1_bearish)
                if htf_agrees:
                    pnl2, won2 = simulate_trade(inv_dir, winner)
                    stats["volflow_inv+HTF"]["trades"] += 1
                    stats["volflow_inv+HTF"]["pnl"] += pnl2
                    stats["volflow_inv+HTF"]["wins" if won2 else "losses"] += 1
                
                htf_strict = htf_agrees and ((inv_dir == "up" and h4_bullish) or (inv_dir == "down" and h4_bearish))
                if htf_strict:
                    pnl3, won3 = simulate_trade(inv_dir, winner)
                    stats["volflow_inv+HTF_strict"]["trades"] += 1
                    stats["volflow_inv+HTF_strict"]["pnl"] += pnl3
                    stats["volflow_inv+HTF_strict"]["wins" if won3 else "losses"] += 1
        
        # === COMBINED: Streak + Volume ===
        if i > 0 and streak_down >= 3 and len(lookback) >= 10:
            # Volume flow also pointing up (inverse of down flow)
            if vol_ratio > 1.2 and recent_direction < 0:  # Recent flow is down, we fade it
                pnl, won = simulate_trade("up", winner)
                stats["combined_streak_vol"]["trades"] += 1
                stats["combined_streak_vol"]["pnl"] += pnl
                stats["combined_streak_vol"]["wins" if won else "losses"] += 1
                
                if h1_bullish:
                    pnl2, won2 = simulate_trade("up", winner)
                    stats["combined_streak_vol+HTF"]["trades"] += 1
                    stats["combined_streak_vol+HTF"]["pnl"] += pnl2
                    stats["combined_streak_vol+HTF"]["wins" if won2 else "losses"] += 1
        
        # === HTF-ONLY STRATEGIES ===
        # Simple 1hr trend follow
        if abs(htf["h1_trend"]) > 0.2:
            direction = "up" if htf["h1_trend"] > 0 else "down"
            pnl, won = simulate_trade(direction, winner)
            stats["htf_trend_follow"]["trades"] += 1
            stats["htf_trend_follow"]["pnl"] += pnl
            stats["htf_trend_follow"]["wins" if won else "losses"] += 1
        
        # 1hr RSI extremes (contrarian)
        if h1_rsi < 30:
            pnl, won = simulate_trade("up", winner)
            stats["htf_h1_rsi_extreme"]["trades"] += 1
            stats["htf_h1_rsi_extreme"]["pnl"] += pnl
            stats["htf_h1_rsi_extreme"]["wins" if won else "losses"] += 1
        elif h1_rsi > 70:
            pnl, won = simulate_trade("down", winner)
            stats["htf_h1_rsi_extreme"]["trades"] += 1
            stats["htf_h1_rsi_extreme"]["pnl"] += pnl
            stats["htf_h1_rsi_extreme"]["wins" if won else "losses"] += 1
        
        # 1hr + 4hr aligned
        if h1_bullish and h4_bullish:
            pnl, won = simulate_trade("up", winner)
            stats["htf_h1h4_aligned"]["trades"] += 1
            stats["htf_h1h4_aligned"]["pnl"] += pnl
            stats["htf_h1h4_aligned"]["wins" if won else "losses"] += 1
        elif h1_bearish and h4_bearish:
            pnl, won = simulate_trade("down", winner)
            stats["htf_h1h4_aligned"]["trades"] += 1
            stats["htf_h1h4_aligned"]["pnl"] += pnl
            stats["htf_h1h4_aligned"]["wins" if won else "losses"] += 1
        
        # Update streaks
        if winner == "up":
            streak_up += 1
            streak_down = 0
        else:
            streak_down += 1
            streak_up = 0
    
    print_results(stats)
    return stats


def main():
    print("🏞 The Outsiders — HTF-Enhanced Strategy Backtest")
    print("=" * 70)
    
    candles_1m, candles_1h, candles_4h, outcomes = load_data()
    
    candles_1h_map = {c["time"]: c for c in candles_1h}
    candles_4h_map = {c["time"]: c for c in candles_4h}
    
    print(f"1-min candles: {len(candles_1m)}")
    print(f"1-hr candles: {len(candles_1h)}")
    print(f"4-hr candles: {len(candles_4h)}")
    print(f"5-min windows: {len(outcomes)}")
    
    ups = sum(1 for o in outcomes if o["outcome"] == "up")
    print(f"Base rate: UP {ups}/{len(outcomes)} ({ups/len(outcomes)*100:.1f}%)")
    
    # 3-way split: 50% train / 20% validate / 30% OOS
    n = len(outcomes)
    split1 = int(n * 0.50)
    split2 = int(n * 0.70)
    
    train = outcomes[:split1]
    validate = outcomes[split1:split2]
    oos = outcomes[split2:]
    
    print(f"Split: {len(train)} train / {len(validate)} validate / {len(oos)} OOS")
    
    backtest(outcomes, candles_1m, candles_1h, candles_1h_map, candles_4h_map, 
             f"ALL DATA ({len(outcomes)} windows, 7 days)")
    
    backtest(train, candles_1m, candles_1h, candles_1h_map, candles_4h_map,
             f"TRAINING (first 50%, {len(train)} windows)")
    
    backtest(validate, candles_1m, candles_1h, candles_1h_map, candles_4h_map,
             f"VALIDATION (next 20%, {len(validate)} windows)")
    
    backtest(oos, candles_1m, candles_1h, candles_1h_map, candles_4h_map,
             f"OUT-OF-SAMPLE (last 30%, {len(oos)} windows)")
    
    print(f"\n{'='*70}")
    print(f"  ✅ BACKTEST COMPLETE")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()
