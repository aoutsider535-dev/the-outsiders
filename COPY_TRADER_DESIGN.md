# Copy Trader Bot — Design Notes

## Vision
Follow profitable Polymarket wallets across ALL market types. Disciplined, diversified, steady growth.

## Architecture Overview
```
Leader Wallets → Activity Monitor → Filter Pipeline → Execution Engine → Position Manager
                                                                              ↓
                                                              Dashboard / Alerts / P&L
```

---

## Filter Pipeline (all must pass for a trade to copy)

### Timing Filters
- **revertTrade** (bool, default: false) — when false, only copy BUYs (skip SELLs)
- **entryTradeSec** (int) — skip if leader's trade is older than N seconds (stale signal)
- **tradeSecFromResolve** (int) — skip if market endDate is within N seconds from now (too close to resolution)
- **minMarketAge** (int, seconds) — skip if market opened less than N seconds ago (no price discovery yet)

### Price Filters
- **maxEntryPrice** (float) — don't buy tokens above this price (e.g., $0.95 — not enough upside)
- **minEntryPrice** (float) — don't buy tokens below this price (e.g., $0.05 — too speculative)
- **maxSlippage** (float, %) — skip if our expected fill price is X% worse than leader's fill price
- **minLiquidity** (float, $) — skip if order book depth at our size is below this threshold

### Size Filters
- **minLeaderTradeSize** (float, $) — ignore dust trades from leader (e.g., skip if leader only bet $1)
- **maxLeaderTradeSize** (float, $) — skip if leader bet abnormally large (could be manipulation)
- **copyFraction** (float, 0-1) — what fraction of leader's trade size to copy (e.g., 0.10 = 10%)
- **fixedTradeSize** (float, $) — OR use a fixed $ amount per copy trade (overrides copyFraction)
- **minTradeSize** (float, $) — floor on our trade size
- **maxTradeSize** (float, $) — cap on our trade size

### Risk Filters
- **maxPositionsPerMarket** (int) — max open positions in a single market
- **maxPositionsTotal** (int) — max total open positions across all markets
- **maxExposurePerMarket** (float, $) — max $ at risk in one market
- **maxExposurePerLeader** (float, $) — max $ following a single leader
- **maxExposureTotal** (float, $) — max total $ deployed
- **dailyLossLimit** (float, $) — stop ALL copying if daily P&L hits -$X
- **maxDrawdown** (float, $) — kill switch — stop everything if total drawdown exceeds $X

### Market Filters
- **allowedCategories** (list) — e.g., ["politics", "sports", "crypto", "economics"] or ["all"]
- **blockedMarkets** (list) — specific market IDs to never trade
- **minMarketVolume** (float, $) — skip markets with less than $X total volume
- **minMarketLiquidity** (float, $) — skip markets with thin books

### Leader-Specific Filters
- **leaderMinWinRate** (float, %) — only copy leaders with historical WR above X% (tracked by us)
- **leaderMinTrades** (int) — don't copy a leader until we've observed N of their trades resolve
- **conflictResolution** (string) — what to do when two leaders bet opposite sides:
  - "skip" — don't copy either
  - "follow_best" — copy the leader with better track record
  - "follow_first" — copy whoever traded first
  - "both" — copy both (natural hedge)

### Anti-Gaming / Safety
- **recentSellCheck** (bool) — skip if leader SOLD this same token in last N minutes (pump & dump)
- **leaderPositionAge** (int, seconds) — skip if leader opened and is already closing (round-trip)
- **volumeProportionCheck** (float) — skip if leader's trade is >X% of market's daily volume (illiquid manipulation risk)

---

## Leader Management
- **Add/remove wallets** via config or command
- **Per-leader settings**: enabled/disabled, custom sizing multiplier, category restrictions
- **Performance tracking**: WR, P&L, avg hold time, Sharpe — all tracked per leader
- **Leader scoring**: Auto-weight leaders by recent performance (optional)

## Execution Engine
- **Polling interval**: Check leader activity every N seconds (1-5s for speed)
- **Order type**: Limit order at leader's price + slippage tolerance
- **Retry logic**: If order fails, retry up to N times with increasing price
- **Partial fills**: Accept partial fills, don't chase
- **Cancel stale orders**: Cancel unfilled orders after N seconds

## Position Manager
- **Track all open positions** with entry price, leader, market, timestamp
- **Auto-redeem** resolved winning positions
- **Burn** resolved losing positions (or batch burn)
- **P&L tracking**: Real-time, per-leader, per-market, per-category

## Dashboard
- Live positions with leader attribution
- Per-leader performance scorecards
- Trade log with filter pass/fail reasons
- Daily/weekly/monthly P&L
- Risk utilization (how close to limits)

---

## Open Questions
1. How do we discover good wallets to follow? (Leaderboard scraping? Manual research?)
2. Polling vs WebSocket for leader activity detection?
3. How do we handle leader exit signals if revertTrade is false? (Hold to resolution? Time-based exit?)
4. Should we paper-trade a leader for N days before going live?
5. What's the starting bankroll / max risk?

---

*Created: 2026-03-16 5:26 PM PST*
