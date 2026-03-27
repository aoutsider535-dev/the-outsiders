"""
Backtesting engine — simulate the BTC 5-min strategy against historical Kraken data.

Approach:
- Fetch historical 1-min candles from Kraken
- For each 5-min window, generate a signal using the strategy
- Simulate Polymarket-like binary outcome: Up if close >= open of window, else Down
- Track P&L as if buying binary tokens at simulated market prices
"""
import time
import json
import numpy as np
from datetime import datetime, timezone
from src.strategies.btc_5min import generate_signal, DEFAULT_PARAMS, format_signal_message
from src.data.price_feed import fetch_kraken_candles
from src.database import init_db, insert_candles, insert_trade, close_trade, get_connection


def fetch_historical_candles(hours=24, pair="XXBTZUSD"):
    """Fetch historical candles from Kraken (up to 720 1-min candles = 12 hours)."""
    since = int(time.time()) - (hours * 3600)
    all_candles = []

    # Kraken returns max ~720 candles per request, so paginate if needed
    current_since = since
    while current_since < int(time.time()):
        candles = fetch_kraken_candles(pair=pair, interval=1, since=current_since)
        if not candles:
            break
        all_candles.extend(candles)
        # Move forward
        last_ts = candles[-1]["timestamp"]
        if last_ts <= current_since:
            break
        current_since = last_ts + 1
        time.sleep(1)  # Rate limit courtesy

    # Deduplicate by timestamp
    seen = set()
    unique = []
    for c in all_candles:
        if c["timestamp"] not in seen:
            seen.add(c["timestamp"])
            unique.append(c)
    unique.sort(key=lambda c: c["timestamp"])
    return unique


def simulate_market_price(candles_before_window, noise=0.05):
    """
    Simulate what Polymarket's market price would be.
    In reality this comes from the orderbook; here we add noise around 50%.
    We use a slight bias based on recent momentum to make it realistic.
    """
    if not candles_before_window:
        return 0.50, 0.50

    # Simple momentum-based market maker simulation
    up_count = sum(1 for c in candles_before_window[-5:] if c["close"] >= c["open"])
    ratio = up_count / min(len(candles_before_window), 5)

    # Market roughly tracks momentum with some noise
    base = 0.45 + (ratio * 0.10)  # range: 0.45 to 0.55
    noise_val = np.random.normal(0, noise * 0.1)
    up_price = max(0.15, min(0.85, base + noise_val))
    down_price = 1.0 - up_price  # Simplified; real markets have overround

    return round(up_price, 3), round(down_price, 3)


def run_backtest(candles=None, hours=12, params=None, balance=1000.0,
                 verbose=False, pair="XXBTZUSD"):
    """
    Run a full backtest simulation.

    Returns dict with:
    - trades: list of simulated trades
    - metrics: performance summary
    - equity_curve: balance over time
    """
    if params is None:
        params = DEFAULT_PARAMS.copy()

    if candles is None:
        print(f"Fetching {hours}h of historical data...")
        candles = fetch_historical_candles(hours=hours, pair=pair)

    if len(candles) < 20:
        return {"error": f"Insufficient data: {len(candles)} candles", "trades": [], "metrics": {}}

    print(f"Backtesting with {len(candles)} candles "
          f"({datetime.fromtimestamp(candles[0]['timestamp'], tz=timezone.utc)} to "
          f"{datetime.fromtimestamp(candles[-1]['timestamp'], tz=timezone.utc)})")

    trades = []
    equity_curve = [{"timestamp": candles[0]["timestamp"], "balance": balance}]
    current_balance = balance
    lookback = params["lookback_minutes"]

    # Group candles into 5-min windows
    window_size = 5
    i = lookback  # Start after enough lookback data

    while i + window_size < len(candles):
        window_start_idx = i
        window_end_idx = i + window_size

        # Lookback candles for signal generation
        lookback_candles = candles[max(0, i - lookback):i]

        # Window candles (what we're predicting)
        window_candles = candles[window_start_idx:window_end_idx]

        # Actual outcome
        window_open = window_candles[0]["open"]
        window_close = window_candles[-1]["close"]
        actual_direction = "up" if window_close >= window_open else "down"

        # Simulate market prices
        up_price, down_price = simulate_market_price(lookback_candles)

        # Generate signal
        signal = generate_signal(
            candles=lookback_candles,
            orderbook_data=None,  # No historical orderbook data
            market_up_price=up_price,
            market_down_price=down_price,
            params=params,
            timestamp=window_candles[0]["timestamp"]
        )

        if signal.should_trade:
            # Calculate position size
            risk_amount = current_balance * (params["risk_per_trade_pct"] / 100)
            entry_price = up_price if signal.direction == "up" else down_price
            quantity = risk_amount / entry_price if entry_price > 0 else 0

            # Determine outcome
            won = (signal.direction == actual_direction)
            if won:
                # Binary pays $1, we bought at entry_price
                pnl = quantity * (1.0 - entry_price)
                pnl_pct = ((1.0 - entry_price) / entry_price) * 100
                # Apply TP cap
                if pnl_pct > params["take_profit_pct"]:
                    pnl_pct = params["take_profit_pct"]
                    pnl = risk_amount * (pnl_pct / 100)
                exit_price = 1.0
                exit_reason = "win"
            else:
                # Token goes to $0
                pnl = -risk_amount
                pnl_pct = -100.0
                # Apply SL (in practice you'd exit early)
                if abs(pnl_pct) > params["stop_loss_pct"]:
                    pnl_pct = -params["stop_loss_pct"]
                    pnl = -risk_amount * (params["stop_loss_pct"] / 100)
                exit_price = 0.0
                exit_reason = "loss"

            current_balance += pnl

            trade = {
                "timestamp": window_candles[0]["timestamp"],
                "window_start": datetime.fromtimestamp(window_candles[0]["timestamp"], tz=timezone.utc).isoformat(),
                "direction": signal.direction,
                "actual": actual_direction,
                "won": won,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "quantity": round(quantity, 4),
                "pnl": round(pnl, 4),
                "pnl_pct": round(pnl_pct, 2),
                "edge_pct": round(signal.edge_pct, 2),
                "confidence": round(signal.confidence, 4),
                "momentum": round(signal.momentum_score, 3),
                "trend": round(signal.trend_score, 3),
                "volatility_regime": signal.volatility_regime,
                "balance_after": round(current_balance, 2),
            }
            trades.append(trade)

            if verbose:
                icon = "✅" if won else "❌"
                print(f"  {icon} {trade['window_start'][:19]} | "
                      f"{signal.direction.upper()} (actual: {actual_direction}) | "
                      f"Edge: {signal.edge_pct:.1f}% | PnL: ${pnl:+.2f} ({pnl_pct:+.1f}%) | "
                      f"Balance: ${current_balance:.2f}")

        equity_curve.append({
            "timestamp": window_candles[0]["timestamp"],
            "balance": round(current_balance, 2)
        })

        i += window_size  # Move to next window

    # Calculate metrics
    if trades:
        wins = [t for t in trades if t["won"]]
        losses = [t for t in trades if not t["won"]]
        total_pnl = sum(t["pnl"] for t in trades)
        metrics = {
            "total_trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(trades) * 100,
            "total_pnl": round(total_pnl, 2),
            "total_pnl_pct": round((total_pnl / balance) * 100, 2),
            "avg_pnl": round(total_pnl / len(trades), 2),
            "avg_win": round(np.mean([t["pnl"] for t in wins]), 2) if wins else 0,
            "avg_loss": round(np.mean([t["pnl"] for t in losses]), 2) if losses else 0,
            "best_trade": round(max(t["pnl"] for t in trades), 2),
            "worst_trade": round(min(t["pnl"] for t in trades), 2),
            "max_drawdown_pct": round(calculate_max_drawdown(equity_curve), 2),
            "sharpe_ratio": round(calculate_sharpe(trades), 2),
            "avg_edge": round(np.mean([t["edge_pct"] for t in trades]), 2),
            "final_balance": round(current_balance, 2),
            "data_points": len(candles),
            "windows_analyzed": (len(candles) - lookback) // window_size,
        }
    else:
        metrics = {
            "total_trades": 0,
            "note": "No trades triggered — edge threshold may be too high",
            "windows_analyzed": (len(candles) - lookback) // window_size,
        }

    return {
        "trades": trades,
        "metrics": metrics,
        "equity_curve": equity_curve,
        "params": params,
    }


def calculate_max_drawdown(equity_curve):
    """Calculate maximum drawdown percentage."""
    if len(equity_curve) < 2:
        return 0.0
    balances = [e["balance"] for e in equity_curve]
    peak = balances[0]
    max_dd = 0.0
    for b in balances:
        if b > peak:
            peak = b
        dd = (peak - b) / peak * 100 if peak > 0 else 0
        max_dd = max(max_dd, dd)
    return max_dd


def calculate_sharpe(trades, risk_free_rate=0):
    """Calculate Sharpe ratio from trade returns."""
    if len(trades) < 2:
        return 0.0
    returns = [t["pnl_pct"] / 100 for t in trades]
    mean_return = np.mean(returns)
    std_return = np.std(returns)
    if std_return == 0:
        return 0.0
    # Annualize: ~288 5-min windows per day, 365 days
    periods_per_year = 288 * 365
    return (mean_return - risk_free_rate) / std_return * np.sqrt(periods_per_year)


def format_backtest_report(result):
    """Format backtest results for Telegram."""
    m = result["metrics"]
    if m.get("total_trades", 0) == 0:
        return f"📊 Backtest complete — no trades triggered.\n{m.get('note', '')}\nWindows analyzed: {m.get('windows_analyzed', 0)}"

    return (
        f"📊 **BACKTEST RESULTS**\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📈 Trades: {m['total_trades']} ({m['wins']}W / {m['losses']}L)\n"
        f"🎯 Win Rate: {m['win_rate']:.1f}%\n"
        f"💰 Total P&L: ${m['total_pnl']:+.2f} ({m['total_pnl_pct']:+.1f}%)\n"
        f"📊 Avg P&L: ${m['avg_pnl']:+.2f}\n"
        f"✅ Avg Win: ${m['avg_win']:+.2f}\n"
        f"❌ Avg Loss: ${m['avg_loss']:+.2f}\n"
        f"🏆 Best: ${m['best_trade']:+.2f} | Worst: ${m['worst_trade']:+.2f}\n"
        f"📉 Max Drawdown: {m['max_drawdown_pct']:.1f}%\n"
        f"📐 Sharpe: {m['sharpe_ratio']:.2f}\n"
        f"🎯 Avg Edge: {m['avg_edge']:.1f}%\n"
        f"💼 Final Balance: ${m['final_balance']:,.2f}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⚙️ Windows: {m['windows_analyzed']} | Data: {m['data_points']} candles"
    )


def save_backtest_to_db(result, strategy_name="btc_5min"):
    """Save backtest trades and params to database."""
    init_db()
    conn = get_connection()

    for t in result["trades"]:
        trade_data = {
            "timestamp": t["timestamp"],
            "market_id": f"backtest_{t['timestamp']}",
            "strategy": strategy_name,
            "side": "buy",
            "direction": t["direction"],
            "entry_price": t["entry_price"],
            "quantity": t["quantity"],
            "edge_pct": t["edge_pct"],
            "confidence": t["confidence"],
            "signal_data": {
                "momentum": t["momentum"],
                "trend": t["trend"],
                "volatility_regime": t["volatility_regime"],
                "actual_direction": t["actual"],
            },
            "is_simulated": 1,
        }
        trade_id = insert_trade(trade_data)
        close_trade(trade_id, t["exit_price"], t["pnl"], t["pnl_pct"],
                    "backtest_win" if t["won"] else "backtest_loss")

    # Save params
    m = result["metrics"]
    conn.execute(
        """INSERT INTO strategy_params (strategy, params, performance_score, backtest_trades,
           backtest_pnl, backtest_win_rate, created_at, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (strategy_name, json.dumps(result["params"]),
         m.get("sharpe_ratio", 0), m.get("total_trades", 0),
         m.get("total_pnl", 0), m.get("win_rate", 0),
         datetime.now(timezone.utc).isoformat(),
         f"Backtest: {m.get('data_points', 0)} candles, {m.get('windows_analyzed', 0)} windows")
    )
    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    print("🚀 Running BTC 5-Min backtest...")
    result = run_backtest(hours=12, verbose=True, balance=1000.0)
    print("\n" + format_backtest_report(result))

    if result["trades"]:
        save_backtest_to_db(result)
        print("\n💾 Results saved to database.")
