# 🎯 Late Drift Sniper v3 — Backtester

Standalone backtesting module that replays the **exact** v3 drift sniper strategy against historical data. Reuses all strategy functions from `sniper_v3.py` — guaranteed parity with live.

## Quick Start

```bash
cd polymarket-bot
source .venv/bin/activate

# Install deps (if not already)
pip install pandas numpy matplotlib requests websockets

# Run a 7-day backtest (uses free Binance data, no API key needed)
python src/drift_backtester.py --days 7

# Run with verbose output (every trade printed)
python src/drift_backtester.py --days 7 --verbose

# Specific date range
python src/drift_backtester.py --start-date 2026-02-01 --end-date 2026-03-01

# Compare filter combinations
python src/drift_backtester.py --days 30 --compare-filters
```

No API keys needed for basic usage. Binance 1m candles are free and auto-cached.

---

## Data Sources

### Tier 1: Binance 1-minute candles (default, free)
- **Always used** — BTC spot prices at any historical timestamp
- Auto-fetched from Binance REST API (no key needed)
- Cached per-day as parquet files in `data/backtest_cache/binance_1m/`
- First run downloads data; subsequent runs load from cache
- Polymarket book prices are **estimated** from drift magnitude

### Tier 2: warproxxx/poly_data CSV (optional, free)
- **Real** Polymarket order fill data — actual prices people traded at
- ~1-2 GB download (one-time), cached after extraction
- Enables realistic fill price simulation

```bash
# Download poly_data (one-time, ~1-2 GB)
python src/drift_backtester.py --days 30 --download-poly-data --use-poly-data
```

### Tier 3: PolyBackTest.com API (optional, premium)
- Sub-second orderbook snapshots with exact spread/depth
- Chainlink on-chain reference prices
- Most accurate simulation possible

```bash
# Add to .env:
# POLYBACKTEST_API_KEY=your_key_here

python src/drift_backtester.py --days 30 --use-polybacktest
```

---

## Commands

### Basic Backtest
```bash
# Last 30 days with all filters
python src/drift_backtester.py --days 30

# Custom date range
python src/drift_backtester.py --start-date 2026-02-01 --end-date 2026-03-01

# Custom starting balance and trade size
python src/drift_backtester.py --days 30 --balance 5000 --size 50
```

### Toggle Filters
```bash
# Disable streak filter
python src/drift_backtester.py --days 30 --no-streak

# Disable vol adjustment
python src/drift_backtester.py --days 30 --no-vol-adjust

# Disable oracle lag detection
python src/drift_backtester.py --days 30 --no-oracle-lag

# Raw drift-only (no filters)
python src/drift_backtester.py --days 30 --no-streak --no-vol-adjust --no-oracle-lag
```

### Filter Comparison
```bash
# Runs 5 backtests with different filter combos and outputs comparison table
python src/drift_backtester.py --days 30 --compare-filters
```

This runs:
1. All filters ON (baseline)
2. No streak filter
3. No vol adjustment
4. No oracle lag
5. All filters OFF (raw drift)

---

## Output Files

| File | Contents |
|------|----------|
| `data/backtest_results.md` | Full markdown report with stats, tables, analysis |
| `data/backtest_equity_curve.png` | Equity curve chart (dark theme, annotated) |
| `data/backtest_trades.csv` | Every signal evaluation with all fields |
| `data/backtest_filter_comparison.md` | Filter comparison table (if `--compare-filters`) |
| `data/backtest_cache/` | Cached Binance data + processed poly_data |

### Console Output Includes
- Win rate, total trades, profit factor, Sharpe ratio
- Expectancy per trade, max drawdown, ROI
- Edge bucket analysis (WR by edge size)
- Filter effectiveness breakdown
- Rejection reason distribution

---

## Expected Results

### With All Filters (realistic)
- **Win Rate:** 68-82% on filtered trades
- **Signal Rate:** 1-3% of windows generate signals
- **Trades/Day:** 3-8 (depends on BTC volatility)
- **Profit Factor:** 1.5-3.0
- **Expectancy:** $0.50-2.00 per trade at $10 size

### Without Filters (raw drift only)
- **Win Rate:** 55-60% (significantly worse)
- **Signal Rate:** 10-20% (too many marginal trades)
- **Profit Factor:** 0.8-1.2 (barely profitable or losing)

The filters are the edge. Raw drift-only is close to a coin flip.

### Important Caveats
- **Estimated PM prices** (Tier 1) are approximate — real results may differ by ±5-10%
- **No slippage model** without poly_data or PolyBackTest data
- **Fees:** 2% on winning trades is applied (matches Polymarket)
- **Oracle lag detection** in backtest uses a proxy (rapid 1m candle moves) — real oracle lag detection is more precise
- **Streaks** are tracked in-sample — the streak filter's edge comes from crowd behavior that may change

---

## Integration with sniper_v3.py

The backtester imports strategy functions directly from `sniper_v3.py`:

```python
from src.sniper_v3 import LateDriftSniperV3

# All these methods are reused exactly:
# - _estimate_continuation_prob()
# - _get_vol_adjusted_min_drift()
# - _get_streak()
# - _record_outcome()
# - _detect_oracle_lag()
# - _calc_trade_size()
# - _simulate_fill()
# - _calc_current_vol()
# - _update_avg_vol()
```

**Any change to the strategy in sniper_v3.py is automatically reflected in backtests.** No need to update two files.

### Programmatic Usage

```python
from src.drift_backtester import BacktestEngine
import time

engine = BacktestEngine(
    start_ts=int(time.time()) - 30 * 86400,
    end_ts=int(time.time()),
    initial_balance=1000,
    trade_size=10,
    use_streak=True,
    use_vol_adjust=True,
    use_oracle_lag=True,
)
results = engine.run()

print(f"Win rate: {results['win_rate']:.1f}%")
print(f"P&L: ${results['total_pnl']:+.2f}")
```

---

## Architecture

```
drift_backtester.py
├── BacktestEngine           # Main simulation loop
│   ├── load_data()          # Fetch Binance + optional poly_data
│   ├── _evaluate_window()   # Reuses sniper_v3 strategy methods
│   ├── _get_book_price()    # Multi-tier price lookup
│   ├── _compute_stats()     # Full statistical analysis
│   └── run()                # Process all windows chronologically
│
├── run_filter_comparison()  # Compare filter combinations
│
├── backtest_data_utils.py   # Data acquisition layer
│   ├── fetch_binance_klines()      # Binance REST → cached parquet
│   ├── load_btc_5m_fills()         # poly_data CSV → filtered cache
│   ├── estimate_pm_book_price()    # Drift-based price estimation
│   ├── PolyBackTestClient          # Premium API wrapper
│   └── generate_windows()          # Window timestamp generator
│
└── sniper_v3.py (imported)  # Strategy functions (single source of truth)
```

## Performance

- **30 days** (8,640 windows): ~60-90 seconds on first run (data download), ~10-20 seconds cached
- **7 days** (2,016 windows): ~15-30 seconds first run, ~5 seconds cached
- Binance data cached per-day as parquet — only new days are fetched
- poly_data filtered and cached as parquet on first load

---

## Troubleshooting

### "No Binance data available"
- Check internet connection
- Binance may be geo-restricted — try a VPN
- Check if `data/backtest_cache/binance_1m/` has any parquet files

### "poly_data CSV not found"
- Run with `--download-poly-data` first (one-time, ~1-2 GB)
- Or just use default mode (estimated PM prices)

### "matplotlib not installed"
- `pip install matplotlib`
- Equity curve is optional — all other outputs still generate

### Import errors
- Make sure you're running from the `polymarket-bot/` directory
- Activate the venv: `source .venv/bin/activate`
