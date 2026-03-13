# Changelog — The Outsiders 🏞

## 2026-03-12 (Evening)

### TP/SL Overhaul
- **TP now checks FIRST** before any time-based exit — fixed bug where breakeven/fire sale was stealing +30-47% gains from TP
- **Percentage-based TP/SL**: TP +20% / SL -10% (was fixed dollar amounts)
- **Breakeven exit moved to T-90s** (was T-2min) — gives TP more time to trigger
- Exit priority: TP → SL → Breakeven (60-90s) → Fire sale (15-60s) → Emergency (<15s)

### Auto-Redeemer Fix
- Scans last 15 windows (~75 min) instead of only 10 recent trades
- Tracks already-redeemed CIDs to avoid wasting gas
- Cleared $27.89 backlog of stuck unredeemed positions

### Critical Bug Fixes
- **Complement detection**: `asset_type="CONDITIONAL"` not integer `1` — was silently returning 0 for all token balances, disabling TP/SL on every trade (~$40-50 in preventable losses)
- **Entry pricing**: Uses actual CLOB best ask, not Gamma API probability (~$0.50). Fill rate went from ~17% to ~100%
- **Fill price tracking**: `takingAmount`/`makingAmount` from CLOB response with `float(x or 0)` pattern for empty strings
- **CLOB balance**: Raw values need `/1e6` conversion
- **Settlement timing**: 3s wait + retry (was 2s, often returned 0)

### Trader v3 "The Scalper"
- New `src/live_trader_v3.py` — BTC+ETH with graduated exit strategy
- 5-second check interval, never lets positions expire
- True portfolio tracking: USDC + unredeemed winners + open position value
- V2 continues running SOL+XRP

## 2026-03-01 — Auto-Claimer

### Auto-Claimer
- EIP-1559 gas pricing for Polygon
- Same-RPC receipt wait with 180s timeout
- Routes through ProxyWalletFactory (Solidity 0.5 ABIEncoderV2 quirk)

## 2026-02-28 — Mission Control

### Dashboard
- Mission Control tab: system health, Kanban task board, learnings, git status
- Sniper tab: gold-themed hero, per-asset cards, cumulative profit chart

## 2026-02-27 — ML & Paper Trading

### ML Meta-Learner
- 59-feature GBM classifier, EV-optimized trade selection
- Online learning: retrains every 15 resolved trades on rolling 500 window
- Shadow tracking for skipped trades

### Paper Traders (v1-v3.3)
- Original paper traders with simulated resolution — showed unrealistic results
- v3.2/v3.3 mirror tests validated against real Chainlink resolution
- Key finding: simulated resolution gave ~76% WR, real resolution gave ~33% WR

## 2026-02-24 — Strategy Tuning

### Multi-Strategy System
- Momentum v3, Mean Reversion v3 "Combo C", OB Imbalance v3, Smart Money v3
- Dynamic trade sizing: 4% of balance, min $5, max $50
- Per-strategy version filters on dashboard

## 2026-02-23 — Initial Build

### Core System
- Polymarket CLOB integration (py-clob-client)
- BTC/ETH/SOL/XRP 5-min up/down markets
- SQLite trade logging, Streamlit dashboard
- Binance price feeds, TA indicators (EMA, MACD, RSI, VWAP, ATR)

### Key Discovery
- **TA is noise on BTC at ALL timeframes** — backtested 2,011 windows: EMA crossover = 49.9% WR
- Also confirmed at 15m (50.0%), 1hr (45.6%), 4hr (45.8%)
- Only proven edge: post-close sniper arbitrage
