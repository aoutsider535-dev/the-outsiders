"""
🏞 The Outsiders — Backtest: Paper v2 & v3 strategies against historical data

Fetches all Kraken 1-min candles for the full time range in bulk,
then replays each market window through Trend Rider, VWAP Sniper, and Vol Spike.
Also tests their inverses (v3).
"""
import sqlite3
import time
import json
import os
import sys
import requests
from datetime import datetime, timezone, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.strategies.trend_rider import generate_signal as trend_rider_signal, DEFAULTS as TR_DEFAULTS
from src.strategies.vwap_sniper import generate_signal as vwap_sniper_signal, DEFAULTS as VS_DEFAULTS
from src.strategies.volatility_spike import generate_signal as vol_spike_signal, DEFAULTS as VSP_DEFAULTS

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "polymarket.db")
TAKER_FEE_PCT = 0.02
SLIPPAGE_PCT = 0.005
TRADE_SIZE = 5.0

PST = timezone(timedelta(hours=-8))


def fetch_all_candles(start_ts, end_ts):
    """Fetch ALL 1-min BTC candles via CryptoCompare (free, 2000/call, supports historical)."""
    all_candles = {}
    to_ts = end_ts + 300
    
    print(f"  Fetching CryptoCompare BTC/USD 1-min candles...")
    
    for batch in range(5):  # Max 5 batches = 10K candles = ~7 days
        try:
            resp = requests.get(
                "https://min-api.cryptocompare.com/data/v2/histominute",
                params={"fsym": "BTC", "tsym": "USD", "limit": 2000, "toTs": to_ts},
                timeout=15
            )
            data = resp.json()
            if data.get("Response") != "Success":
                print(f"  Batch {batch+1}: Error {data.get('Message', 'unknown')}")
                break
            
            candles = data["Data"]["Data"]
            for c in candles:
                all_candles[c["time"]] = {
                    "timestamp": c["time"],
                    "open": c["open"],
                    "high": c["high"],
                    "low": c["low"],
                    "close": c["close"],
                    "vwap": (c["high"] + c["low"] + c["close"]) / 3,  # Typical price as VWAP proxy
                    "volume": c["volumefrom"],  # BTC volume
                }
            
            print(f"  Batch {batch+1}: {len(candles)} candles | Total: {len(all_candles)} | Range: {candles[0]['time']}-{candles[-1]['time']}")
            
            if candles[0]["time"] <= start_ts - 1800:
                break
            to_ts = candles[0]["time"] - 1
            time.sleep(0.5)
            
        except Exception as e:
            print(f"  Batch {batch+1}: Exception {e}")
            time.sleep(2)
    
    print(f"  Total candles: {len(all_candles)}")
    return all_candles


def get_candles_for_timestamp(all_candles, ts, minutes=30):
    """Get candles for 'minutes' before timestamp from pre-fetched data."""
    start = ts - (minutes * 60)
    candles = []
    for t in sorted(all_candles.keys()):
        if start <= t <= ts:
            candles.append(all_candles[t])
    return candles


def get_market_outcomes():
    """Get all markets with known outcomes."""
    conn = sqlite3.connect(DB_PATH)
    
    # From real_trades (on-chain verified)
    real_markets = {}
    rows = conn.execute("""
        SELECT market_id, 
               MAX(CASE WHEN won=1 THEN direction END) as winner,
               MIN(timestamp) as ts
        FROM real_trades 
        GROUP BY market_id
        HAVING winner IS NOT NULL
    """).fetchall()
    for mid, winner, ts in rows:
        real_markets[mid] = {"winner": winner, "timestamp": ts, "source": "real_trades"}
    
    # From trades table (more coverage)
    all_markets = {}
    rows2 = conn.execute("""
        SELECT market_id,
               MAX(CASE WHEN pnl > 0 THEN direction END) as winner,
               MIN(timestamp) as ts
        FROM trades 
        WHERE status = 'closed' AND strategy LIKE '%_LIVE'
        GROUP BY market_id
        HAVING winner IS NOT NULL
    """).fetchall()
    for mid, winner, ts in rows2:
        all_markets[mid] = {"winner": winner, "timestamp": ts, "source": "trades"}
    
    combined = dict(all_markets)
    combined.update(real_markets)  # Real overrides
    
    conn.close()
    return combined, real_markets


def simulate_trade(direction, winner, entry_price=0.5):
    fill_price = min(entry_price * (1 + SLIPPAGE_PCT), 0.99)
    quantity = TRADE_SIZE / fill_price
    cost = fill_price * quantity
    fee = cost * TAKER_FEE_PCT
    
    won = (direction == winner)
    if won:
        gross_pnl = quantity * (1.0 - fill_price)
        net_pnl = gross_pnl - fee
    else:
        net_pnl = -(cost + fee)
    
    return net_pnl, won


def backtest(markets, all_candles, label="Full Dataset"):
    print(f"\n{'='*70}")
    print(f"  BACKTEST: {label}")
    print(f"  Markets: {len(markets)} | Trade size: ${TRADE_SIZE} | Fees: {TAKER_FEE_PCT*100}% + {SLIPPAGE_PCT*100}% slippage")
    print(f"{'='*70}")
    
    sorted_markets = sorted(markets.items(), key=lambda x: x[1]["timestamp"])
    
    strategies = ["trend_rider", "vwap_sniper", "vol_spike"]
    stats = {}
    for s in strategies:
        for variant in ["v2", "v3_inv"]:
            key = f"{s}_{variant}"
            stats[key] = {"wins": 0, "losses": 0, "pnl": 0.0, "trades": 0}
    
    # Also track "always up" and "always down" baselines
    stats["baseline_up"] = {"wins": 0, "losses": 0, "pnl": 0.0, "trades": 0}
    stats["baseline_down"] = {"wins": 0, "losses": 0, "pnl": 0.0, "trades": 0}
    stats["baseline_random"] = {"wins": 0, "losses": 0, "pnl": 0.0, "trades": 0}
    
    processed = 0
    skipped = 0
    
    import random
    random.seed(42)
    
    for market_id, info in sorted_markets:
        ts = info["timestamp"]
        winner = info["winner"]
        
        candles = get_candles_for_timestamp(all_candles, ts, minutes=30)
        if not candles or len(candles) < 22:
            skipped += 1
            continue
        
        processed += 1
        
        # Baselines (always bet on every market)
        pnl_up, won_up = simulate_trade("up", winner)
        stats["baseline_up"]["trades"] += 1
        stats["baseline_up"]["pnl"] += pnl_up
        stats["baseline_up"]["wins" if won_up else "losses"] += 1
        
        pnl_dn, won_dn = simulate_trade("down", winner)
        stats["baseline_down"]["trades"] += 1
        stats["baseline_down"]["pnl"] += pnl_dn
        stats["baseline_down"]["wins" if won_dn else "losses"] += 1
        
        rand_dir = random.choice(["up", "down"])
        pnl_r, won_r = simulate_trade(rand_dir, winner)
        stats["baseline_random"]["trades"] += 1
        stats["baseline_random"]["pnl"] += pnl_r
        stats["baseline_random"]["wins" if won_r else "losses"] += 1
        
        # Run strategies
        sig_tr = trend_rider_signal(candles=candles, market_up_price=0.5, market_down_price=0.5, params=TR_DEFAULTS.copy())
        sig_vs = vwap_sniper_signal(candles=candles, market_up_price=0.5, market_down_price=0.5, params=VS_DEFAULTS.copy())
        sig_vsp = vol_spike_signal(candles=candles, market_up_price=0.5, market_down_price=0.5, kraken_orderbook=None, params=VSP_DEFAULTS.copy())
        
        for strat_name, sig in [("trend_rider", sig_tr), ("vwap_sniper", sig_vs), ("vol_spike", sig_vsp)]:
            if sig.should_trade:
                # V2
                v2k = f"{strat_name}_v2"
                pnl, won = simulate_trade(sig.direction, winner)
                stats[v2k]["trades"] += 1
                stats[v2k]["pnl"] += pnl
                stats[v2k]["wins" if won else "losses"] += 1
                
                # V3 inverse
                v3k = f"{strat_name}_v3_inv"
                inv_dir = "down" if sig.direction == "up" else "up"
                pnl_inv, won_inv = simulate_trade(inv_dir, winner)
                stats[v3k]["trades"] += 1
                stats[v3k]["pnl"] += pnl_inv
                stats[v3k]["wins" if won_inv else "losses"] += 1
    
    # Print results
    print(f"\n  Processed: {processed}/{len(sorted_markets)} (skipped {skipped} - insufficient candle data)")
    print(f"\n  {'Strategy':<30} {'Trades':>7} {'Wins':>6} {'Losses':>7} {'WR':>7} {'P&L':>10} {'Avg':>9}")
    print(f"  {'-'*77}")
    
    # Print baselines first
    for key in ["baseline_up", "baseline_down", "baseline_random"]:
        s = stats[key]
        if s["trades"] == 0: continue
        wr = s["wins"] / s["trades"] * 100
        avg = s["pnl"] / s["trades"]
        print(f"  📏 {key:<28} {s['trades']:>7} {s['wins']:>6} {s['losses']:>7} "
              f"{wr:>6.1f}% ${s['pnl']:>+8.2f} ${avg:>+7.2f}")
    
    print(f"  {'-'*77}")
    
    # Strategies sorted by P&L
    strat_keys = [k for k in stats if not k.startswith("baseline")]
    strat_keys.sort(key=lambda k: stats[k]["pnl"], reverse=True)
    
    for key in strat_keys:
        s = stats[key]
        if s["trades"] == 0:
            print(f"  ⬜ {key:<28} {'NO TRADES':>7}")
            continue
        wr = s["wins"] / s["trades"] * 100
        avg = s["pnl"] / s["trades"]
        emoji = "✅" if s["pnl"] > 0 else "❌"
        print(f"  {emoji} {key:<28} {s['trades']:>7} {s['wins']:>6} {s['losses']:>7} "
              f"{wr:>6.1f}% ${s['pnl']:>+8.2f} ${avg:>+7.2f}")
    
    # V2 vs V3 head-to-head
    print(f"\n  --- V2 vs V3 Head-to-Head ---")
    for strat in strategies:
        v2 = stats[f"{strat}_v2"]
        v3 = stats[f"{strat}_v3_inv"]
        if v2["trades"] == 0:
            print(f"  {strat}: No signals fired")
            continue
        v2_wr = v2["wins"]/v2["trades"]*100
        v3_wr = v3["wins"]/v3["trades"]*100
        better = "V2 ✅" if v2["pnl"] > v3["pnl"] else "V3 ✅" if v3["pnl"] > v2["pnl"] else "TIE"
        print(f"  {strat}: V2={v2_wr:.0f}%WR (${v2['pnl']:+.2f}) vs V3={v3_wr:.0f}%WR (${v3['pnl']:+.2f}) → {better}")
    
    return stats


def main():
    print("🏞 The Outsiders — Paper v2/v3 Historical Backtest")
    print("=" * 70)
    
    combined_markets, real_markets = get_market_outcomes()
    print(f"Total markets with outcomes: {len(combined_markets)}")
    print(f"Real (on-chain verified) markets: {len(real_markets)}")
    
    # Get time range
    all_ts = [m["timestamp"] for m in combined_markets.values()]
    min_ts, max_ts = min(all_ts), max(all_ts)
    
    print(f"Time range: {min_ts} to {max_ts}")
    print(f"Span: {(max_ts - min_ts) / 3600:.1f} hours")
    
    # Fetch ALL candles in bulk
    print(f"\n📡 Fetching Kraken historical candles (bulk)...")
    all_candles = fetch_all_candles(min_ts, max_ts)
    
    if len(all_candles) < 100:
        print("❌ Not enough candle data. Aborting.")
        return
    
    # OOS split on real_trades only
    real_sorted = sorted(real_markets.items(), key=lambda x: x[1]["timestamp"])
    split_idx = int(len(real_sorted) * 0.70)
    in_sample = dict(real_sorted[:split_idx])
    out_of_sample = dict(real_sorted[split_idx:])
    
    print(f"\nReal data split: {len(in_sample)} in-sample / {len(out_of_sample)} out-of-sample")
    
    # 1. Full dataset
    backtest(combined_markets, all_candles, f"ALL Markets ({len(combined_markets)} windows)")
    
    # 2. In-sample  
    backtest(in_sample, all_candles, f"IN-SAMPLE (first 70% real trades, {len(in_sample)} windows)")
    
    # 3. Out-of-sample
    backtest(out_of_sample, all_candles, f"OUT-OF-SAMPLE (last 30% real trades, {len(out_of_sample)} windows)")
    
    print(f"\n{'='*70}")
    print(f"  ✅ BACKTEST COMPLETE")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()
