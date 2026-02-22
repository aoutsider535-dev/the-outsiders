"""
Out-of-sample validation — the real test of whether our strategy works.

Approach:
1. Fetch maximum historical data from Kraken (up to 720 candles per request, paginate)
2. Split into train (first 70%) and test (last 30%)
3. Optimize on train set only
4. Run best params on test set (never seen during optimization)
5. Compare in-sample vs out-of-sample performance
"""
import time
import json
import os
import numpy as np
from datetime import datetime, timezone
from src.data.price_feed import fetch_kraken_candles
from src.backtester import run_backtest, format_backtest_report, save_backtest_to_db, fetch_historical_candles
from src.optimizer import optimize_grid, compute_score, analyze_results
from src.strategies.btc_5min import DEFAULT_PARAMS
from src.database import init_db


def fetch_max_historical_data(pair="XXBTZUSD", days=7):
    """
    Fetch as much historical 1-min data as possible from Kraken.
    Kraken gives ~720 candles per request. We paginate to get more.
    In practice, Kraken's free API gives about 10-12 hours of 1-min data.
    For more, we'll also try 5-min candles and interpolate.
    """
    print(f"📥 Fetching maximum historical data...")

    # Strategy 1: Get 1-min candles (most recent ~12h)
    one_min = fetch_historical_candles(hours=24, pair=pair)
    print(f"   1-min candles: {len(one_min)}")

    # Strategy 2: Get 5-min candles for longer history
    since_5m = int(time.time()) - (days * 24 * 3600)
    five_min_raw = []
    current_since = since_5m

    for attempt in range(10):  # max 10 pagination requests
        candles = fetch_kraken_candles(pair=pair, interval=5, since=current_since)
        if not candles:
            break
        five_min_raw.extend(candles)
        last_ts = candles[-1]["timestamp"]
        if last_ts <= current_since or last_ts >= int(time.time()) - 300:
            break
        current_since = last_ts + 1
        time.sleep(1.5)  # Rate limit

    # Deduplicate
    seen = set()
    five_min = []
    for c in five_min_raw:
        if c["timestamp"] not in seen:
            seen.add(c["timestamp"])
            five_min.append(c)
    five_min.sort(key=lambda c: c["timestamp"])
    print(f"   5-min candles: {len(five_min)}")

    # Strategy 3: Get 15-min candles for even longer history
    since_15m = int(time.time()) - (days * 24 * 3600)
    fifteen_min_raw = []
    current_since = since_15m

    for attempt in range(10):
        candles = fetch_kraken_candles(pair=pair, interval=15, since=current_since)
        if not candles:
            break
        fifteen_min_raw.extend(candles)
        last_ts = candles[-1]["timestamp"]
        if last_ts <= current_since or last_ts >= int(time.time()) - 900:
            break
        current_since = last_ts + 1
        time.sleep(1.5)

    seen = set()
    fifteen_min = []
    for c in fifteen_min_raw:
        if c["timestamp"] not in seen:
            seen.add(c["timestamp"])
            fifteen_min.append(c)
    fifteen_min.sort(key=lambda c: c["timestamp"])
    print(f"   15-min candles: {len(fifteen_min)}")

    return {
        "1m": one_min,
        "5m": five_min,
        "15m": fifteen_min,
    }


def run_validation(verbose=True):
    """Full out-of-sample validation pipeline."""
    init_db()

    print("🕶️ The Outsiders — Out-of-Sample Validation")
    print("=" * 50)

    # Fetch all data
    data = fetch_max_historical_data(days=7)

    # Use 5-min candles for longer history validation since we have more of them
    # We'll treat each 5-min candle as a "market window" for backtesting
    # But first, let's use our 1-min data for the proper split

    candles_1m = data["1m"]
    candles_5m = data["5m"]

    print(f"\n📊 Data Summary:")
    if candles_1m:
        span_1m = (candles_1m[-1]["timestamp"] - candles_1m[0]["timestamp"]) / 3600
        print(f"   1-min: {len(candles_1m)} candles spanning {span_1m:.1f} hours")
        print(f"   Range: {datetime.fromtimestamp(candles_1m[0]['timestamp'], tz=timezone.utc)} → "
              f"{datetime.fromtimestamp(candles_1m[-1]['timestamp'], tz=timezone.utc)}")
    if candles_5m:
        span_5m = (candles_5m[-1]["timestamp"] - candles_5m[0]["timestamp"]) / 3600
        print(f"   5-min: {len(candles_5m)} candles spanning {span_5m:.1f} hours")
        print(f"   Range: {datetime.fromtimestamp(candles_5m[0]['timestamp'], tz=timezone.utc)} → "
              f"{datetime.fromtimestamp(candles_5m[-1]['timestamp'], tz=timezone.utc)}")

    # === VALIDATION ON 1-MIN DATA (split 70/30) ===
    print(f"\n{'=' * 50}")
    print("📋 VALIDATION 1: 1-Min Data (70/30 Split)")
    print(f"{'=' * 50}")

    if len(candles_1m) >= 100:
        split_idx = int(len(candles_1m) * 0.7)
        train_1m = candles_1m[:split_idx]
        test_1m = candles_1m[split_idx:]

        print(f"   Train: {len(train_1m)} candles | Test: {len(test_1m)} candles")

        # Optimize on training set
        print(f"\n🔧 Optimizing on training data...")
        grid = {
            "min_edge_pct": [3.0, 4.0, 5.0, 6.0, 8.0],
            "take_profit_pct": [40.0, 50.0, 60.0, 70.0],
            "stop_loss_pct": [20.0, 30.0, 40.0],
            "w_momentum": [0.30, 0.40, 0.50],
            "w_trend": [0.20, 0.30, 0.40],
        }
        train_results = optimize_grid(train_1m, grid, verbose=verbose)

        if train_results:
            best_train = train_results[0]
            best_params = best_train["full_params"]

            # In-sample result
            print(f"\n📈 IN-SAMPLE (Training) — Best Config:")
            m = best_train["metrics"]
            print(f"   P&L: ${m['total_pnl']:+.2f} ({m['total_pnl_pct']:+.1f}%) | "
                  f"WR: {m['win_rate']:.1f}% | Trades: {m['total_trades']} | "
                  f"MaxDD: {m['max_drawdown_pct']:.1f}%")
            print(f"   Params: {best_train['params']}")

            # Out-of-sample result
            print(f"\n🧪 OUT-OF-SAMPLE (Test) — Same Config:")
            test_result = run_backtest(candles=test_1m, params=best_params,
                                       balance=1000.0, verbose=False)
            tm = test_result["metrics"]

            if tm.get("total_trades", 0) > 0:
                print(f"   P&L: ${tm['total_pnl']:+.2f} ({tm['total_pnl_pct']:+.1f}%) | "
                      f"WR: {tm['win_rate']:.1f}% | Trades: {tm['total_trades']} | "
                      f"MaxDD: {tm['max_drawdown_pct']:.1f}%")

                # Degradation analysis
                train_wr = m["win_rate"]
                test_wr = tm["win_rate"]
                wr_delta = test_wr - train_wr

                print(f"\n📊 DEGRADATION ANALYSIS:")
                print(f"   Win Rate: {train_wr:.1f}% → {test_wr:.1f}% ({wr_delta:+.1f}%)")
                print(f"   P&L/trade: ${m['avg_pnl']:+.2f} → ${tm['avg_pnl']:+.2f}")

                if test_wr >= train_wr * 0.90:
                    print(f"   ✅ Strategy holds up! (<10% degradation)")
                elif test_wr >= train_wr * 0.80:
                    print(f"   ⚠️ Moderate degradation (10-20%). Strategy may be slightly overfit.")
                else:
                    print(f"   ❌ Significant degradation (>20%). Likely overfit to training data.")

                # Save out-of-sample results
                save_backtest_to_db(test_result, strategy_name="btc_5min_v2_oos")
            else:
                print(f"   No trades triggered in test set")

    # === VALIDATION ON 5-MIN DATA (longer history) ===
    print(f"\n{'=' * 50}")
    print("📋 VALIDATION 2: 5-Min Candle Data (Longer History)")
    print(f"{'=' * 50}")

    if len(candles_5m) >= 50:
        # Load our best params
        best_params_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "best_params.json")
        if os.path.exists(best_params_path):
            with open(best_params_path) as f:
                saved = json.load(f)
            params_5m = saved["params"]
        else:
            params_5m = DEFAULT_PARAMS.copy()
            params_5m.update({
                "take_profit_pct": 60.0,
                "stop_loss_pct": 30.0,
                "w_momentum": 0.45,
                "w_trend": 0.315,
            })

        # For 5-min candles, adjust lookback (each candle = 5 min, so lookback of 3 = 15 min)
        params_5m_adjusted = params_5m.copy()
        params_5m_adjusted["lookback_minutes"] = 3  # 3 five-min candles = 15 min

        # Split
        split_idx = int(len(candles_5m) * 0.7)
        train_5m = candles_5m[:split_idx]
        test_5m = candles_5m[split_idx:]

        print(f"   Train: {len(train_5m)} candles | Test: {len(test_5m)} candles")

        # Run on both
        print(f"\n📈 Training set:")
        train_result_5m = run_backtest(candles=train_5m, params=params_5m_adjusted,
                                        balance=1000.0, verbose=False)
        tm5_train = train_result_5m["metrics"]
        if tm5_train.get("total_trades", 0) > 0:
            print(f"   P&L: ${tm5_train['total_pnl']:+.2f} ({tm5_train['total_pnl_pct']:+.1f}%) | "
                  f"WR: {tm5_train['win_rate']:.1f}% | Trades: {tm5_train['total_trades']}")
        else:
            print(f"   No trades triggered (edge threshold too high for 5m data)")

        print(f"\n🧪 Test set:")
        test_result_5m = run_backtest(candles=test_5m, params=params_5m_adjusted,
                                       balance=1000.0, verbose=False)
        tm5_test = test_result_5m["metrics"]
        if tm5_test.get("total_trades", 0) > 0:
            print(f"   P&L: ${tm5_test['total_pnl']:+.2f} ({tm5_test['total_pnl_pct']:+.1f}%) | "
                  f"WR: {tm5_test['win_rate']:.1f}% | Trades: {tm5_test['total_trades']}")
            save_backtest_to_db(test_result_5m, strategy_name="btc_5min_v2_5m_oos")
        else:
            print(f"   No trades triggered")

    # === DEFAULT PARAMS BASELINE ===
    print(f"\n{'=' * 50}")
    print("📋 BASELINE: Default Params on Full 1-Min Data")
    print(f"{'=' * 50}")

    if len(candles_1m) >= 50:
        baseline = run_backtest(candles=candles_1m, params=DEFAULT_PARAMS,
                                balance=1000.0, verbose=False)
        bm = baseline["metrics"]
        if bm.get("total_trades", 0) > 0:
            print(f"   P&L: ${bm['total_pnl']:+.2f} ({bm['total_pnl_pct']:+.1f}%) | "
                  f"WR: {bm['win_rate']:.1f}% | Trades: {bm['total_trades']}")

    print(f"\n{'=' * 50}")
    print("🏁 Validation complete!")
    print(f"{'=' * 50}")


if __name__ == "__main__":
    run_validation(verbose=True)
