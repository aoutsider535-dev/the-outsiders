"""
Strategy Optimizer — Grid search + analysis to find profitable parameter combinations.
"""
import itertools
import time
import json
import numpy as np
from copy import deepcopy
from datetime import datetime, timezone
from src.strategies.btc_5min import DEFAULT_PARAMS
from src.backtester import run_backtest, fetch_historical_candles, format_backtest_report, save_backtest_to_db
from src.database import init_db, get_connection


def optimize_grid(candles, param_grid, balance=1000.0, verbose=True):
    """
    Run backtests across all parameter combinations.
    
    param_grid: dict of param_name -> list of values to try
    Returns sorted list of (params, metrics) tuples.
    """
    # Generate all combinations
    keys = list(param_grid.keys())
    values = list(param_grid.values())
    combinations = list(itertools.product(*values))
    
    if verbose:
        print(f"🔍 Grid search: {len(combinations)} combinations across {len(keys)} parameters")
        print(f"   Parameters: {', '.join(keys)}")
    
    results = []
    best_pnl = -float('inf')
    
    for i, combo in enumerate(combinations):
        params = DEFAULT_PARAMS.copy()
        for k, v in zip(keys, combo):
            params[k] = v
        
        result = run_backtest(candles=candles, params=params, balance=balance, verbose=False)
        metrics = result["metrics"]
        
        if metrics.get("total_trades", 0) == 0:
            continue
        
        score = compute_score(metrics)
        entry = {
            "params": {k: v for k, v in zip(keys, combo)},
            "full_params": params,
            "metrics": metrics,
            "score": score,
            "trades": result["trades"],
        }
        results.append(entry)
        
        if metrics.get("total_pnl", -999) > best_pnl:
            best_pnl = metrics["total_pnl"]
            if verbose:
                combo_str = ", ".join(f"{k}={v}" for k, v in zip(keys, combo))
                print(f"   [{i+1}/{len(combinations)}] New best: P&L ${best_pnl:+.2f} | "
                      f"WR {metrics['win_rate']:.1f}% | {metrics['total_trades']} trades | {combo_str}")
        elif verbose and (i + 1) % 25 == 0:
            print(f"   [{i+1}/{len(combinations)}] Searching...")
    
    # Sort by composite score (higher = better)
    results.sort(key=lambda x: x["score"], reverse=True)
    
    if verbose and results:
        print(f"\n✅ Search complete. Top result:")
        top = results[0]
        m = top["metrics"]
        print(f"   Score: {top['score']:.2f} | P&L: ${m['total_pnl']:+.2f} ({m['total_pnl_pct']:+.1f}%) | "
              f"WR: {m['win_rate']:.1f}% | Trades: {m['total_trades']} | "
              f"MaxDD: {m['max_drawdown_pct']:.1f}%")
        print(f"   Params: {top['params']}")
    
    return results


def compute_score(metrics):
    """
    Composite score balancing profitability, consistency, and risk.
    Higher = better.
    """
    if metrics.get("total_trades", 0) < 5:
        return -100  # Not enough trades to evaluate
    
    pnl_pct = metrics.get("total_pnl_pct", 0)
    win_rate = metrics.get("win_rate", 0)
    sharpe = metrics.get("sharpe_ratio", 0)
    max_dd = metrics.get("max_drawdown_pct", 0)
    num_trades = metrics.get("total_trades", 0)
    
    # Reward profitability
    pnl_score = pnl_pct * 2
    
    # Reward win rate above breakeven (55.6% for our TP/SL)
    wr_score = (win_rate - 50) * 3
    
    # Reward good Sharpe, penalize bad
    sharpe_score = min(sharpe * 0.5, 20)  # cap contribution
    
    # Penalize large drawdowns
    dd_penalty = max(0, max_dd - 10) * -1.5
    
    # Slight bonus for having enough trades (statistical significance)
    trade_bonus = min(num_trades / 10, 5)
    
    return pnl_score + wr_score + sharpe_score + dd_penalty + trade_bonus


def analyze_results(results, top_n=5):
    """Generate a detailed analysis of optimization results."""
    if not results:
        return "No valid results found."
    
    lines = [f"📊 **OPTIMIZATION RESULTS** — Top {min(top_n, len(results))} configurations\n"]
    lines.append("━" * 40)
    
    for i, r in enumerate(results[:top_n]):
        m = r["metrics"]
        lines.append(
            f"\n{'🥇' if i == 0 else '🥈' if i == 1 else '🥉' if i == 2 else f'#{i+1}'} "
            f"Score: {r['score']:.1f}\n"
            f"   P&L: ${m['total_pnl']:+.2f} ({m['total_pnl_pct']:+.1f}%) | "
            f"WR: {m['win_rate']:.1f}% | Trades: {m['total_trades']}\n"
            f"   MaxDD: {m['max_drawdown_pct']:.1f}% | Sharpe: {m['sharpe_ratio']:.2f} | "
            f"Avg Edge: {m['avg_edge']:.1f}%\n"
            f"   Params: {r['params']}"
        )
    
    # Analysis
    if len(results) >= 3:
        profitable = [r for r in results if r["metrics"]["total_pnl"] > 0]
        lines.append(f"\n━━━━━━━━━━━━━━━━━━")
        lines.append(f"📈 Profitable configs: {len(profitable)}/{len(results)} ({len(profitable)/len(results)*100:.0f}%)")
        
        if profitable:
            avg_wr = np.mean([r["metrics"]["win_rate"] for r in profitable])
            avg_edge = np.mean([r["metrics"]["avg_edge"] for r in profitable])
            lines.append(f"   Avg win rate (profitable): {avg_wr:.1f}%")
            lines.append(f"   Avg edge (profitable): {avg_edge:.1f}%")
        
        # Find which params matter most
        if results[0]["params"]:
            lines.append(f"\n🔑 Key findings:")
            for param in results[0]["params"]:
                top_vals = [r["params"].get(param) for r in results[:5] if param in r["params"]]
                if top_vals and len(set(top_vals)) < len(top_vals):
                    common_val = max(set(top_vals), key=top_vals.count)
                    lines.append(f"   {param}: top configs cluster around {common_val}")
    
    return "\n".join(lines)


def run_full_optimization(hours=12, verbose=True):
    """Run the complete optimization pipeline."""
    init_db()
    
    print("🚀 The Outsiders — Strategy Optimizer")
    print("=" * 40)
    
    # Fetch data once
    print(f"\n📥 Fetching {hours}h of historical data...")
    candles = fetch_historical_candles(hours=hours)
    if len(candles) < 50:
        print(f"❌ Not enough data: {len(candles)} candles")
        return None
    print(f"   Got {len(candles)} candles")
    
    # Phase 1: Edge threshold & TP/SL optimization
    print(f"\n{'='*40}")
    print("Phase 1: Edge Threshold & TP/SL Tuning")
    print(f"{'='*40}")
    
    grid_1 = {
        "min_edge_pct": [3.0, 4.0, 5.0, 6.0, 8.0, 10.0],
        "take_profit_pct": [30.0, 40.0, 50.0, 60.0],
        "stop_loss_pct": [30.0, 40.0, 50.0, 60.0],
    }
    results_1 = optimize_grid(candles, grid_1, verbose=verbose)
    
    if not results_1:
        print("❌ No valid results in Phase 1")
        return None
    
    best_1 = results_1[0]
    
    # Phase 2: Signal weight optimization (using best TP/SL from phase 1)
    print(f"\n{'='*40}")
    print("Phase 2: Signal Weight Tuning")
    print(f"{'='*40}")
    
    grid_2 = {
        "w_momentum": [0.20, 0.30, 0.40, 0.50],
        "w_trend": [0.15, 0.25, 0.35],
        "w_last_candle": [0.10, 0.20, 0.30],
        "w_orderbook": [0.05, 0.10, 0.15],
    }
    
    # Apply best phase 1 params as base
    base_params = DEFAULT_PARAMS.copy()
    for k, v in best_1["params"].items():
        base_params[k] = v
    
    # Override DEFAULT_PARAMS temporarily for phase 2
    phase2_results = []
    keys = list(grid_2.keys())
    values = list(grid_2.values())
    combinations = list(itertools.product(*values))
    
    print(f"🔍 Grid search: {len(combinations)} weight combinations")
    best_pnl = -float('inf')
    
    for i, combo in enumerate(combinations):
        params = base_params.copy()
        for k, v in zip(keys, combo):
            params[k] = v
        # Normalize weights to sum to ~0.9 (leaving 0.1 for volatility)
        w_sum = params["w_momentum"] + params["w_trend"] + params["w_last_candle"] + params["w_orderbook"]
        if w_sum > 0:
            scale = 0.90 / w_sum
            params["w_momentum"] *= scale
            params["w_trend"] *= scale
            params["w_last_candle"] *= scale
            params["w_orderbook"] *= scale
            params["w_volatility"] = 0.10
        
        result = run_backtest(candles=candles, params=params, balance=1000.0, verbose=False)
        metrics = result["metrics"]
        
        if metrics.get("total_trades", 0) == 0:
            continue
        
        score = compute_score(metrics)
        entry = {
            "params": {k: round(v, 3) for k, v in zip(keys, combo)},
            "full_params": params,
            "metrics": metrics,
            "score": score,
            "trades": result["trades"],
        }
        phase2_results.append(entry)
        
        if metrics.get("total_pnl", -999) > best_pnl:
            best_pnl = metrics["total_pnl"]
            if verbose:
                print(f"   [{i+1}/{len(combinations)}] New best: P&L ${best_pnl:+.2f} | "
                      f"WR {metrics['win_rate']:.1f}%")
        elif verbose and (i + 1) % 50 == 0:
            print(f"   [{i+1}/{len(combinations)}] Searching...")
    
    phase2_results.sort(key=lambda x: x["score"], reverse=True)
    
    # Phase 3: Confidence filter — require minimum signal agreement
    print(f"\n{'='*40}")
    print("Phase 3: Confidence Filter")
    print(f"{'='*40}")
    
    best_full = phase2_results[0]["full_params"] if phase2_results else base_params
    
    grid_3 = {
        "min_edge_pct": [best_full["min_edge_pct"]],
        "momentum_strong_threshold": [0.5, 0.6, 0.7, 0.8],
        "lookback_minutes": [10, 15, 20, 30],
    }
    results_3 = optimize_grid(candles, grid_3, verbose=verbose)
    
    # Compile final results
    all_results = results_1 + phase2_results + results_3
    all_results.sort(key=lambda x: x["score"], reverse=True)
    
    # Print final report
    print(f"\n{'='*40}")
    print("🏆 FINAL OPTIMIZATION REPORT")
    print(f"{'='*40}")
    print(analyze_results(all_results, top_n=5))
    
    # Save best to DB
    if all_results:
        best = all_results[0]
        save_backtest_to_db({
            "trades": best["trades"],
            "metrics": best["metrics"],
            "params": best["full_params"],
        }, strategy_name="btc_5min_optimized")
        print(f"\n💾 Best configuration saved to database.")
        
        # Save best params to file
        best_config = {
            "strategy": "btc_5min_optimized",
            "params": best["full_params"],
            "metrics": best["metrics"],
            "optimized_at": datetime.now(timezone.utc).isoformat(),
            "data_points": len(candles),
        }
        config_path = "data/best_params.json"
        import os
        os.makedirs("data", exist_ok=True)
        with open(config_path, "w") as f:
            json.dump(best_config, f, indent=2)
        print(f"📄 Best params saved to {config_path}")
    
    return all_results


if __name__ == "__main__":
    results = run_full_optimization(hours=12, verbose=True)
