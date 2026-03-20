# 🎯 Late Drift Sniper v3

Post-open arbitrage on Polymarket BTC 5-minute up/down markets.

## Strategy

In the final 30-60 seconds of each 5-minute window, BTC has already moved from its open price. If the move is large enough, the outcome is partially known — but Polymarket books may not have fully priced it in. We buy the favored side if our continuation probability model shows sufficient edge over the implied probability.

### Entry Rules (ALL must pass)
1. **Drift ≥ vol-adjusted minimum** — BTC has moved enough from window open
2. **Book price ≤ $0.82** — Don't overpay for known information
3. **Model edge ≥ 7%** — Our P(win) exceeds implied by at least 7%
4. **Sufficient depth** — Enough shares available at acceptable prices
5. **Streak filter** — After 4+ same-direction outcomes, flip contrarian
6. **Oracle lag bonus** — +3% edge when Chainlink lag detected

### Exit
None. Hold to resolution. No early exit, no stop losses. The market resolves in 5-30 minutes.

---

## Setup

### Prerequisites
- Python 3.9+
- Polymarket account with USDC deposited (for live mode)
- No API keys needed for shadow/paper mode

### Install

```bash
cd polymarket-bot
python -m venv .venv
source .venv/bin/activate
pip install websockets requests python-dotenv ccxt py-clob-client streamlit plotly
```

### Configure (live mode only)

```bash
cp .env.example .env
# Edit .env with your Polygon wallet details
```

**Getting your Polymarket API credentials:**
1. Go to [polymarket.com](https://polymarket.com) and connect your wallet
2. Your wallet address is your `POLYGON_WALLET_ADDRESS`
3. Your private key is exported from MetaMask/Rabby: Account → Export Private Key
4. Deposit USDC on Polygon to your Polymarket account

---

## Usage

### Shadow Mode (recommended first)
Observe every 5-minute window, log all data, never trade. Use this to validate the strategy on your setup before risking money.

```bash
python src/sniper_v3.py --shadow
```

### Paper Mode
Simulate trades at real book prices with a $1,000 virtual balance.

```bash
python src/sniper_v3.py --paper --size 10
```

### Live Mode
Real money orders. Start small ($5-10), scale up after confirming profitability.

```bash
python src/sniper_v3.py --live --size 10
```

### Dashboard
Real-time monitoring of all modes.

```bash
streamlit run src/dashboard_sniper.py --server.port 8502
```
Open http://localhost:8502

---

## Tuning Guide

### Parameters to adjust (in `sniper_v3.py` constants section)

| Parameter | Default | What it does | When to change |
|-----------|---------|-------------|----------------|
| `BASE_MIN_DRIFT_PCT` | 0.15% | Minimum BTC drift to consider | Lower → more trades, lower WR. Higher → fewer trades, higher WR |
| `MAX_BUY_PRICE` | $0.82 | Max price for favored side | Lower → higher profit per win, but miss more opportunities |
| `MIN_EDGE` | 7% | Required edge over implied prob | Lower → more trades. Below 5% gets unprofitable after fees |
| `STREAK_THRESHOLD` | 4 | When to flip contrarian | Higher → fewer reversals. Set to 999 to disable |
| `LAG_EDGE_BONUS` | 3% | Bonus when oracle lag detected | Set to 0 to disable |
| `RISK_PER_TRADE` | 1% | Base bet as fraction of bankroll | Higher = more aggressive sizing |
| `MAX_TRADE_SIZE` | $100 | Hard cap per trade | Scale with confidence and bankroll |

### Recommended tuning process
1. Run shadow mode for 24-48 hours
2. Analyze with dashboard: how often do signals fire? What's the distribution of drift sizes?
3. Adjust thresholds based on observed data
4. Run paper mode for 50+ trades
5. Only go live after paper shows 60%+ WR

### Time-of-day notes
- **Active US/Asia hours** (9 AM - 11 PM PST): Higher vol, more signals, better fills
- **Overnight** (11 PM - 6 AM PST): Lower vol, fewer signals, wider spreads
- **Weekends**: Lower volume but can still work

---

## Realistic Expectations

### Win Rate
- **Filtered trades** (all rules pass): 68-82% expected WR
- **Unfiltered** (drift only, no vol/streak/edge filters): ~55-60%
- The filters are doing the heavy lifting — they reject the marginal trades

### Profit per trade
- At $0.70-0.75 book price: $0.25-0.30 profit per $1 share on wins
- At $0.80 book price: $0.20 profit per $1 share on wins
- Losses: full cost of shares purchased

### Expected daily volume
- **12-20 signals/day** in active markets (not all will pass all filters)
- **3-8 trades/day** after all filters applied
- Depends heavily on BTC volatility — flat days = few/no trades

### Scaling
| Trade Size | Expected Daily P&L | Monthly (est.) |
|-----------|-------------------|----------------|
| $5 | $3-8 | $90-240 |
| $10 | $6-16 | $180-480 |
| $20 | $12-32 | $360-960 |
| $50 | $30-80 | $900-2400 |

These are ESTIMATES based on backtested data. Actual results will vary. Do not risk money you can't afford to lose.

### Fees
- Polymarket takes ~2% on winning trades (conditional fee)
- No fee on losing trades
- Net effect: ~1-2% drag on total returns

---

## ⚠️ Risk Warning

**This is experimental trading software. You can and will lose money.**

- Past performance (backtested or paper) does not guarantee future results
- Polymarket markets can have low liquidity, wide spreads, or fail to resolve
- Smart contract risk: Polymarket, Polygon, and Chainlink can have bugs
- Execution risk: FOK orders may not fill, leaving you with no position
- Model risk: The continuation probability model is empirical, not proven
- This is NOT financial advice. This is a research tool.

**Start with shadow mode. Then paper. Then live with minimum size. Scale slowly.**

---

## Architecture

```
sniper_v3.py
├── PriceFeed          # WebSocket price aggregator (Binance + Bybit)
│   ├── _run_binance_ws()   # Persistent WS connection, <50ms latency
│   └── _run_bybit_ws()     # Persistent WS connection, <50ms latency
│
├── LateDriftSniperV3  # Core trading engine
│   ├── Volatility     # Rolling vol estimation + vol-adjusted thresholds
│   ├── Streak Filter  # Mean-reversion on retail over-betting
│   ├── Oracle Lag     # Chainlink latency detection
│   ├── Probability    # Continuation model (calibrated from shadow data)
│   ├── Sizing         # Kelly-lite with edge scaling
│   ├── Execution      # FOK orders via py-clob-client
│   └── Resolution     # Track outcomes, update streaks, calculate P&L
│
├── SQLite DB          # data/drift_sniper.db
│   ├── observations   # Every window evaluation (shadow data)
│   ├── trades         # Actual/simulated trades
│   └── resolutions    # Window outcomes
│
└── dashboard_sniper.py  # Streamlit real-time monitoring
```

## Files
- `src/sniper_v3.py` — Main bot script
- `src/dashboard_sniper.py` — Streamlit dashboard
- `data/drift_sniper.db` — SQLite database
- `data/drift_sniper.log` — Detailed log file
- `data/drift_sniper_state.json` — Persisted state (streaks, vol)
- `data/drift_sniper_vol.json` — Volatility buffer
- `.env` — Wallet credentials (DO NOT COMMIT)

---

## Math Reference

### Volatility-Adjusted Drift
```
σ_current = mean(|return_i|) for last 12 five-minute periods
σ_avg     = EMA(σ_current, α=0.01)
vol_factor = σ_current / σ_avg
min_drift  = 0.15% × max(vol_factor, 0.5)
```

### Continuation Probability Model
```
P(continuation) = 0.50 + |drift| × 180 - (time_left/300) × 0.05
Bounded to [0.50, 0.98]
```

### Oracle Lag Detection
```
spread = (max(exchange_prices) - min(exchange_prices)) / mean(exchange_prices)
lag_detected = spread < 0.01%  (tight consensus = oracle hasn't caught up)
```

### Kelly-Lite Sizing
```
base_bet = 1% × bankroll
edge_mult = actual_edge / min_edge
size = base_bet × edge_mult × streak_boost
Capped at min($100, 5% × bankroll)
```
