# Archive

Retired code from The Outsiders project. Kept for reference, not actively used.

## What's Here

### strategies/
BTC 5-min up/down strategies. **Proven broken** — TA is noise on BTC at all timeframes (5m, 15m, 1hr, 4hr all 47-52% WR, coin flip). See MEMORY.md.

### ml/
ML Meta-Learner ("The Brain"). 59-feature GBM classifier for signal filtering. Real WR was 47.9% — couldn't beat fees. Abandoned.

### sniper/
Post-close sniper bots (v3-v8). Exploited 21-50s gap between BTC 5-min window end and trading close. Worked briefly but we discovered we were Sharky's counterparty. Also: late drift sniper (parked Mar 19).

### paper_traders/
Paper trading engines v1-v4. Various iterations of the BTC strategy paper trader.

### live_traders/
Live BTC trading engines v1-v3. Used real money on BTC 5-min markets.

### experiments/
- `sports_arb/` — Sports arbitrage scanner (Polymarket vs odds APIs)
- `v4/` — Edge detection engine (news, whales, sports edges)
- `wallet_analyzer.py` — Polymarket wallet profiling
- `binance_trader.py`, `straddle_trader.py`, `chainlink.py` — misc tools
- `backtester.py`, `backtest_*.py`, `optimizer.py` — backtesting infrastructure

## Why Archived (Not Deleted)
Some of this code has useful patterns (executor architecture, Polymarket API integration, Polygon tx handling). Kept as reference for future work.
