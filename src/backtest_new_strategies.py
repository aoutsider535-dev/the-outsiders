"""
🏞 The Outsiders — Backtest: Streak Reversal & Orderbook Velocity

Uses 7 days of BTC 1-min candles (2015 five-minute windows) to test:
1. Streak Reversal — fade UP streaks of 3+
2. Orderbook Velocity — rate of change in orderbook depth predicts direction

Split: 70% in-sample / 30% out-of-sample (chronological)
"""
import json
import os
import sys
import requests
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta

DATA_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "historical_5min_outcomes.json")
TAKER_FEE_PCT = 0.02
SLIPPAGE_PCT = 0.005
TRADE_SIZE = 5.0
PST = timezone(timedelta(hours=-8))


def load_data():
    with open(DATA_PATH) as f:
        data = json.load(f)
    return data["candles"], data["outcomes"]


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


def print_results(label, stats):
    print(f"\n  {'Strategy':<35} {'Trades':>7} {'Wins':>6} {'WR':>7} {'P&L':>10} {'Avg':>8}")
    print(f"  {'-'*75}")
    
    for key in sorted(stats.keys(), key=lambda k: stats[k]["pnl"], reverse=True):
        s = stats[key]
        if s["trades"] == 0:
            continue
        wr = s["wins"] / s["trades"] * 100
        avg = s["pnl"] / s["trades"]
        emoji = "✅" if s["pnl"] > 0 else "❌"
        print(f"  {emoji} {key:<33} {s['trades']:>7} {s['wins']:>6} {wr:>6.1f}% ${s['pnl']:>+8.2f} ${avg:>+6.2f}")


# ═══════════════════════════════════════
#  STRATEGY 1: STREAK REVERSAL
# ═══════════════════════════════════════

def backtest_streak_reversal(outcomes, label="Full"):
    """
    Streak Reversal variants:
    - Fade UP streaks of 3+ (bet DOWN)
    - Fade UP streaks of 4+ (bet DOWN)
    - Fade DOWN streaks of 3+ (bet UP) 
    - Fade DOWN streaks of 4+ (bet UP)
    - Combined: fade UP 3+ AND ride DOWN 2+ (bet DOWN on both)
    - Inverse: do the opposite of each
    """
    print(f"\n{'='*70}")
    print(f"  STREAK REVERSAL — {label}")
    print(f"  Windows: {len(outcomes)}")
    print(f"{'='*70}")
    
    variants = {
        "fade_UP_3+": {},
        "fade_UP_4+": {},
        "fade_DOWN_3+": {},
        "fade_DOWN_4+": {},
        "fade_UP3_ride_DOWN2": {},
        "fade_UP_3+_INV": {},
        "fade_UP_4+_INV": {},
        "always_down": {},
        "always_up": {},
        "random_50/50": {},
    }
    for v in variants:
        variants[v] = {"wins": 0, "losses": 0, "trades": 0, "pnl": 0.0}
    
    import random
    random.seed(42)
    
    streak_up = 0
    streak_down = 0
    
    for i, outcome in enumerate(outcomes):
        winner = outcome["outcome"]
        
        # Baselines
        pnl_u, won_u = simulate_trade("up", winner)
        variants["always_up"]["trades"] += 1
        variants["always_up"]["pnl"] += pnl_u
        variants["always_up"]["wins" if won_u else "losses"] += 1
        
        pnl_d, won_d = simulate_trade("down", winner)
        variants["always_down"]["trades"] += 1
        variants["always_down"]["pnl"] += pnl_d
        variants["always_down"]["wins" if won_d else "losses"] += 1
        
        rand_dir = random.choice(["up", "down"])
        pnl_r, won_r = simulate_trade(rand_dir, winner)
        variants["random_50/50"]["trades"] += 1
        variants["random_50/50"]["pnl"] += pnl_r
        variants["random_50/50"]["wins" if won_r else "losses"] += 1
        
        # Streak strategies (need at least 1 prior outcome)
        if i > 0:
            # Fade UP streaks of 3+
            if streak_up >= 3:
                pnl, won = simulate_trade("down", winner)
                variants["fade_UP_3+"]["trades"] += 1
                variants["fade_UP_3+"]["pnl"] += pnl
                variants["fade_UP_3+"]["wins" if won else "losses"] += 1
                # Inverse: bet UP (ride the streak)
                pnl_inv, won_inv = simulate_trade("up", winner)
                variants["fade_UP_3+_INV"]["trades"] += 1
                variants["fade_UP_3+_INV"]["pnl"] += pnl_inv
                variants["fade_UP_3+_INV"]["wins" if won_inv else "losses"] += 1
            
            if streak_up >= 4:
                pnl, won = simulate_trade("down", winner)
                variants["fade_UP_4+"]["trades"] += 1
                variants["fade_UP_4+"]["pnl"] += pnl
                variants["fade_UP_4+"]["wins" if won else "losses"] += 1
                pnl_inv, won_inv = simulate_trade("up", winner)
                variants["fade_UP_4+_INV"]["trades"] += 1
                variants["fade_UP_4+_INV"]["pnl"] += pnl_inv
                variants["fade_UP_4+_INV"]["wins" if won_inv else "losses"] += 1
            
            # Fade DOWN streaks of 3+
            if streak_down >= 3:
                pnl, won = simulate_trade("up", winner)
                variants["fade_DOWN_3+"]["trades"] += 1
                variants["fade_DOWN_3+"]["pnl"] += pnl
                variants["fade_DOWN_3+"]["wins" if won else "losses"] += 1
            
            if streak_down >= 4:
                pnl, won = simulate_trade("up", winner)
                variants["fade_DOWN_4+"]["trades"] += 1
                variants["fade_DOWN_4+"]["pnl"] += pnl
                variants["fade_DOWN_4+"]["wins" if won else "losses"] += 1
            
            # Combined: fade UP 3+ OR ride DOWN 2+
            if streak_up >= 3:
                pnl, won = simulate_trade("down", winner)
                variants["fade_UP3_ride_DOWN2"]["trades"] += 1
                variants["fade_UP3_ride_DOWN2"]["pnl"] += pnl
                variants["fade_UP3_ride_DOWN2"]["wins" if won else "losses"] += 1
            elif streak_down >= 2:
                pnl, won = simulate_trade("down", winner)
                variants["fade_UP3_ride_DOWN2"]["trades"] += 1
                variants["fade_UP3_ride_DOWN2"]["pnl"] += pnl
                variants["fade_UP3_ride_DOWN2"]["wins" if won else "losses"] += 1
        
        # Update streaks
        if winner == "up":
            streak_up += 1
            streak_down = 0
        else:
            streak_down += 1
            streak_up = 0
    
    print_results(label, variants)
    
    # Streak frequency analysis
    print(f"\n  --- Streak Frequencies ---")
    streak_counts_up = defaultdict(int)
    streak_counts_down = defaultdict(int)
    current_streak = 0
    current_dir = None
    for o in outcomes:
        if o["outcome"] == current_dir:
            current_streak += 1
        else:
            if current_dir and current_streak >= 1:
                if current_dir == "up":
                    streak_counts_up[current_streak] += 1
                else:
                    streak_counts_down[current_streak] += 1
            current_streak = 1
            current_dir = o["outcome"]
    # Don't forget the last streak
    if current_dir:
        (streak_counts_up if current_dir == "up" else streak_counts_down)[current_streak] += 1
    
    for label_dir, counts in [("UP", streak_counts_up), ("DOWN", streak_counts_down)]:
        total = sum(counts.values())
        print(f"  {label_dir} streaks: ", end="")
        for length in sorted(counts.keys()):
            pct = counts[length] / total * 100
            print(f"{length}×:{counts[length]}({pct:.0f}%) ", end="")
        print()
    
    return variants


# ═══════════════════════════════════════
#  STRATEGY 2: ORDERBOOK VELOCITY
# ═══════════════════════════════════════

def backtest_orderbook_velocity(candles, outcomes, label="Full"):
    """
    Orderbook Velocity proxy using volume + price action:
    Since we can't get historical orderbook data, we approximate with:
    - Volume delta: sharp increase in volume = aggressive orders hitting the book
    - Price-volume alignment: high volume + directional candle = informed flow
    - Volume imbalance: compare buy-volume vs sell-volume (close > open = buy vol)
    
    These are proxies for what real-time orderbook velocity would capture.
    """
    print(f"\n{'='*70}")
    print(f"  ORDERBOOK VELOCITY (Volume-Flow Proxy) — {label}")
    print(f"  Windows: {len(outcomes)}")
    print(f"{'='*70}")
    
    # Build price map
    price_map = {c["time"]: c for c in candles}
    
    variants = {
        "vol_flow_basic": {},           # Volume surge + direction
        "vol_flow_strict": {},          # Higher thresholds
        "vol_flow_reversal": {},        # Volume surge AGAINST recent trend (reversal)
        "vol_acceleration": {},         # Volume accelerating (2nd derivative)
        "buy_vol_imbalance": {},        # Buy volume > sell volume ratio
        "vol_flow_basic_INV": {},       # Inverse
        "baseline_random": {},
    }
    for v in variants:
        variants[v] = {"wins": 0, "losses": 0, "trades": 0, "pnl": 0.0}
    
    import random
    random.seed(42)
    
    for outcome in outcomes:
        t = outcome["time"]
        winner = outcome["outcome"]
        
        # Baseline
        rand_dir = random.choice(["up", "down"])
        pnl_r, won_r = simulate_trade(rand_dir, winner)
        variants["baseline_random"]["trades"] += 1
        variants["baseline_random"]["pnl"] += pnl_r
        variants["baseline_random"]["wins" if won_r else "losses"] += 1
        
        # Get candles for the lookback period (15 min before this window)
        lookback_candles = []
        for offset in range(15, 0, -1):
            ct = t - offset * 60
            if ct in price_map:
                lookback_candles.append(price_map[ct])
        
        if len(lookback_candles) < 10:
            continue
        
        # === Volume Flow Basic ===
        # Compare last 3 candles' volume to previous 7 candles
        recent_vols = [c["volumefrom"] if "volumefrom" in c else c.get("volume", 0) for c in lookback_candles[-3:]]
        baseline_vols = [c["volumefrom"] if "volumefrom" in c else c.get("volume", 0) for c in lookback_candles[-10:-3]]
        
        avg_recent = sum(recent_vols) / len(recent_vols) if recent_vols else 0
        avg_baseline = sum(baseline_vols) / len(baseline_vols) if baseline_vols else 1
        vol_ratio = avg_recent / avg_baseline if avg_baseline > 0 else 1
        
        # Direction of recent candles (price-weighted)
        recent_direction = 0
        for c in lookback_candles[-3:]:
            body = c["close"] - c["open"]
            c_range = c["high"] - c["low"]
            if c_range > 0:
                strength = body / c_range  # -1 to +1
                vol_weight = (c.get("volumefrom", c.get("volume", 0))) / avg_baseline if avg_baseline > 0 else 1
                recent_direction += strength * vol_weight
        
        # Buy vs Sell volume estimation
        buy_vol = sum(c.get("volumefrom", c.get("volume", 0)) for c in lookback_candles[-5:] if c["close"] >= c["open"])
        sell_vol = sum(c.get("volumefrom", c.get("volume", 0)) for c in lookback_candles[-5:] if c["close"] < c["open"])
        total_vol = buy_vol + sell_vol
        buy_ratio = buy_vol / total_vol if total_vol > 0 else 0.5
        
        # Volume acceleration (2nd derivative)
        if len(lookback_candles) >= 9:
            vol_period1 = sum(c.get("volumefrom", c.get("volume", 0)) for c in lookback_candles[-3:]) / 3
            vol_period2 = sum(c.get("volumefrom", c.get("volume", 0)) for c in lookback_candles[-6:-3]) / 3
            vol_period3 = sum(c.get("volumefrom", c.get("volume", 0)) for c in lookback_candles[-9:-6]) / 3
            vol_accel = (vol_period1 - vol_period2) - (vol_period2 - vol_period3)
        else:
            vol_accel = 0
        
        # Recent trend (for reversal detection)
        trend_5 = lookback_candles[-1]["close"] - lookback_candles[-5]["close"] if len(lookback_candles) >= 5 else 0
        trend_dir = "up" if trend_5 > 0 else "down"
        
        # --- Vol Flow Basic: volume surge + follow the direction ---
        if vol_ratio > 1.5 and abs(recent_direction) > 0.5:
            direction = "up" if recent_direction > 0 else "down"
            pnl, won = simulate_trade(direction, winner)
            variants["vol_flow_basic"]["trades"] += 1
            variants["vol_flow_basic"]["pnl"] += pnl
            variants["vol_flow_basic"]["wins" if won else "losses"] += 1
            # Inverse
            inv_dir = "down" if direction == "up" else "up"
            pnl_inv, won_inv = simulate_trade(inv_dir, winner)
            variants["vol_flow_basic_INV"]["trades"] += 1
            variants["vol_flow_basic_INV"]["pnl"] += pnl_inv
            variants["vol_flow_basic_INV"]["wins" if won_inv else "losses"] += 1
        
        # --- Vol Flow Strict: higher thresholds ---
        if vol_ratio > 2.0 and abs(recent_direction) > 1.0:
            direction = "up" if recent_direction > 0 else "down"
            pnl, won = simulate_trade(direction, winner)
            variants["vol_flow_strict"]["trades"] += 1
            variants["vol_flow_strict"]["pnl"] += pnl
            variants["vol_flow_strict"]["wins" if won else "losses"] += 1
        
        # --- Vol Flow Reversal: volume surge against recent trend ---
        if vol_ratio > 1.5 and abs(recent_direction) > 0.5:
            flow_dir = "up" if recent_direction > 0 else "down"
            if flow_dir != trend_dir:  # Flow opposes trend = potential reversal
                pnl, won = simulate_trade(flow_dir, winner)
                variants["vol_flow_reversal"]["trades"] += 1
                variants["vol_flow_reversal"]["pnl"] += pnl
                variants["vol_flow_reversal"]["wins" if won else "losses"] += 1
        
        # --- Volume Acceleration ---
        if abs(vol_accel) > avg_baseline * 0.5:  # Significant acceleration
            # Accelerating volume in direction of recent flow
            direction = "up" if (vol_accel > 0 and recent_direction > 0) or (vol_accel < 0 and recent_direction < 0) else \
                        "down" if (vol_accel > 0 and recent_direction < 0) or (vol_accel < 0 and recent_direction > 0) else None
            if vol_accel > 0:  # Volume is accelerating
                flow_dir = "up" if recent_direction > 0 else "down"
                pnl, won = simulate_trade(flow_dir, winner)
                variants["vol_acceleration"]["trades"] += 1
                variants["vol_acceleration"]["pnl"] += pnl
                variants["vol_acceleration"]["wins" if won else "losses"] += 1
        
        # --- Buy Volume Imbalance ---
        if buy_ratio > 0.65:  # Strong buy volume
            pnl, won = simulate_trade("up", winner)
            variants["buy_vol_imbalance"]["trades"] += 1
            variants["buy_vol_imbalance"]["pnl"] += pnl
            variants["buy_vol_imbalance"]["wins" if won else "losses"] += 1
        elif buy_ratio < 0.35:  # Strong sell volume
            pnl, won = simulate_trade("down", winner)
            variants["buy_vol_imbalance"]["trades"] += 1
            variants["buy_vol_imbalance"]["pnl"] += pnl
            variants["buy_vol_imbalance"]["wins" if won else "losses"] += 1
    
    print_results(label, variants)
    return variants


def main():
    print("🏞 The Outsiders — Streak Reversal & Orderbook Velocity Backtest")
    print("=" * 70)
    
    candles, outcomes = load_data()
    print(f"Loaded {len(candles)} candles, {len(outcomes)} 5-min windows")
    
    ups = sum(1 for o in outcomes if o["outcome"] == "up")
    downs = len(outcomes) - ups
    print(f"Base rate: UP {ups} ({ups/len(outcomes)*100:.1f}%) | DOWN {downs} ({downs/len(outcomes)*100:.1f}%)")
    
    # Split 70/30 chronologically
    split = int(len(outcomes) * 0.70)
    in_sample = outcomes[:split]
    out_of_sample = outcomes[split:]
    
    print(f"Split: {len(in_sample)} in-sample / {len(out_of_sample)} out-of-sample")
    
    # ── STREAK REVERSAL ──
    print("\n" + "█" * 70)
    print("  STRATEGY 1: STREAK REVERSAL")
    print("█" * 70)
    
    backtest_streak_reversal(outcomes, "ALL DATA (2015 windows)")
    backtest_streak_reversal(in_sample, f"IN-SAMPLE (first 70%, {len(in_sample)} windows)")
    backtest_streak_reversal(out_of_sample, f"OUT-OF-SAMPLE (last 30%, {len(out_of_sample)} windows)")
    
    # ── ORDERBOOK VELOCITY ──
    print("\n" + "█" * 70)
    print("  STRATEGY 2: ORDERBOOK VELOCITY (Volume-Flow Proxy)")
    print("█" * 70)
    
    backtest_orderbook_velocity(candles, outcomes, "ALL DATA (2015 windows)")
    backtest_orderbook_velocity(candles, in_sample, f"IN-SAMPLE (first 70%, {len(in_sample)} windows)")
    backtest_orderbook_velocity(candles, out_of_sample, f"OUT-OF-SAMPLE (last 30%, {len(out_of_sample)} windows)")
    
    print(f"\n{'='*70}")
    print(f"  ✅ BACKTEST COMPLETE")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()
