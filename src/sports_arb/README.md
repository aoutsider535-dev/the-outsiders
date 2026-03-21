# Sports Arbitrage Scanner — Phase 1

Detects pricing edges between sharp sportsbook odds (via [The Odds API](https://the-odds-api.com/)) and [Polymarket](https://polymarket.com/) prediction market prices.

Part of **The Outsiders** — a Polymarket trading system.

## How It Works

1. **Fetch sharp odds** from Pinnacle and other books via The Odds API
2. **Fetch Polymarket** sports markets via the Gamma API
3. **Fuzzy match** games across platforms (team names, sport, date)
4. **Remove vig** from sharp odds to get fair probabilities (power method)
5. **Detect edges** where Polymarket price < fair probability
6. **Log everything** to SQLite — every scan, not just edges
7. **Dashboard** shows live and historical opportunities

## Setup

### 1. API Key

Get a free API key from [The Odds API](https://the-odds-api.com/) (500 credits/month).

Add to `.env` in the project root:
```
ODDS_API_KEY=your_key_here
```

### 2. Dependencies

Uses the existing project venv. Required packages (already installed):
- `requests`
- `streamlit`
- `pandas`
- `python-dotenv`

Optional (better fuzzy matching):
```bash
pip install rapidfuzz
```

### 3. Run the Scanner

```bash
# Single scan pass
python -m src.sports_arb.scanner --once

# Continuous scanning
python -m src.sports_arb.scanner --scan

# Just test Polymarket connection (no Odds API key needed)
python -m src.sports_arb.scanner --once
```

### 4. Dashboard

```bash
streamlit run src/sports_arb/dashboard.py
```

### 5. Backtest

```bash
# Run with default settings
python -m src.sports_arb.backtest

# Custom threshold
python -m src.sports_arb.backtest --threshold 0.05 --bankroll 5000
```

## Architecture

```
src/sports_arb/
├── config.py           # All settings, API keys from .env
├── odds_api.py         # The Odds API v4 client
├── polymarket_api.py   # Gamma API + CLOB orderbook
├── matcher.py          # Fuzzy match games across platforms
├── edge_calculator.py  # No-vig conversion, edge detection
├── database.py         # SQLite logging
├── scanner.py          # Main loop
├── dashboard.py        # Streamlit dashboard
└── backtest.py         # Historical analysis
```

## Configuration

All settings in `config.py`:

| Setting | Default | Description |
|---------|---------|-------------|
| `MIN_EDGE_PCT` | 4% | Minimum edge to flag |
| `MIN_PM_VOLUME` | $500K | Minimum PM market volume |
| `MIN_PM_LIQUIDITY` | $50K | Minimum orderbook depth |
| `MIN_MATCH_CONFIDENCE` | 80% | Minimum match confidence |
| `POLL_INTERVAL_ODDS` | 300s | Odds API poll interval |
| `POLL_INTERVAL_PM` | 60s | Polymarket poll interval |

## API Credit Usage

The Odds API free tier: 500 credits/month.

Each sport costs ~6 credits (2 regions × 3 markets). With 7 sports, a full scan costs ~42 credits. At 5-minute intervals, that's ~12 scans/hour.

**Tip:** Start with 1-2 sports to conserve credits while testing.

## Phase Roadmap

- [x] **Phase 1** — Detection (this module)
- [ ] **Phase 2** — Outcome tracking, real PnL measurement
- [ ] **Phase 3** — Auto-execution via Polymarket CLOB
- [ ] **Phase 4** — Multi-strategy portfolio integration

## ⚠️ Legal Disclaimer

**User must confirm WA state compliance before live trading.**

This tool is for informational and research purposes only. It does not constitute financial advice. Use at your own risk. Check local regulations regarding prediction market participation.
