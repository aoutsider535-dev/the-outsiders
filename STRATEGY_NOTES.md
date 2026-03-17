# Strategy Notes — March 16, 2026

## Momentum Strategy (Validated)

### The Insight
If BTC has moved in one direction by minute 3 of a 5-minute Polymarket window, it almost always stays in that direction at minute 5.

### Raw Data (no model, just BTC prices from Binance)
| BTC move at minute 3 | Windows tested | Direction holds? |
|---|---|---|
| ≥ 0.04% | 8,268 | 86.5% |
| ≥ 0.06% | 6,450 | 88.9% |
| ≥ 0.08% | 5,054 | 91.0% |
| ≥ 0.10% | 4,037 | 92.5% |
| ≥ 0.15% | 2,368 | 95.1% |
| ≥ 0.20% | 1,512 | 96.9% |

### Tested On
- **22,000 windows** from 110K real Binance 1-min candles
- **Three separate datasets:**
  - Fresh OOS: Dec 28 - Jan 31 (10,000 windows) → 95.5% WR ✅
  - Previous OOS: Feb 1 - Mar 7 (9,800 windows) → 90.3% WR ✅
  - In-sample: Mar 7-14 (2,000 windows) → 93.3% WR ✅
- **12/12 weeks profitable** — no losing weeks across entire test period

### Strategy Rules
1. **Entry:** At minute 3 of each 5-min window, check BTC price vs window open
2. **Signal:** If BTC has moved ≥ 0.04% in either direction → buy the Polymarket token matching that direction
3. **Exit:** Hold to resolution (minute 5). No TP/SL — tested every combination, holding to resolution wins.
4. **Size:** $5 flat per trade
5. **Hours:** 7AM - 9PM PST (daytime only, optional filter)

### Why It Works
- By minute 3, BTC has established a direction. 2 more minutes isn't enough time for a full reversal most of the time.
- The bigger the move by minute 3, the more likely it holds.
- We're not predicting direction — we're observing it and betting it continues.

### P&L Estimates (these DO use a price model)
- Token entry prices estimated via logistic function (sensitivity 0.08)
- Model has ~$0.14 median error vs real Polymarket book prices
- At 90%+ WR, strategy is robust to this error
- Estimated: ~$48-156/day on $5 bets depending on filters

### What Killed Our First Run (Feb 23-25)
- Hit +$119 peak, gave back to +$7
- **Root causes:** OB Imbalance sizing up to $29/trade, strategies contradicting each other, overnight trading
- **Fix:** Single strategy (momentum), $5 flat bets, daytime hours

### Caveats / Still Need to Validate
- [ ] Real Polymarket token prices vs our model (paper trade 1 day)
- [ ] Actual fill rates at minute 3 (liquidity/slippage)
- [ ] Whether the edge persists as more bots trade it

### Files
- `momentum_dashboard.py` — Streamlit dashboard at localhost:8502, shows all raw data
- `data/momentum_backtest_raw.csv` — 22K windows with BTC prices, no model
- `data/btc_1m_fresh_oos.json` — 50K candles Dec 28 - Jan 31
- `data/btc_1m_oos.json` — 49K candles Feb 1 - Mar 7
- `data/btc_1m_candles.json` — 10K candles Mar 7 - Mar 14
- `src/strategies/simple_momentum.py` — Strategy code

---

## Wallet Analysis: 0xd0d6053c3c37e727402d84c14069780d360993aa

- 82 recent trades, $267 volume
- Average trade: $3.25, average price: $0.228
- They're trading political/event markets (Biden, not crypto up/down)
- Buying cheap tokens ($0.17-$0.58) across a single market
- Looks like a speculative play on "Will Biden get Coronavirus" — not a systematic bot

---

## Next Steps
1. Paper trade the momentum strategy for 1 day to validate real fills
2. If fills match model → go live with $5/trade
3. Scale size once live WR confirmed
