"""
Configuration for the Sports Arbitrage Scanner.

All tuneable parameters live here. API keys loaded from .env.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_PROJECT_ROOT / ".env")

# ── API Keys ──────────────────────────────────────────────────────────────────
ODDS_API_KEY: str = os.getenv("ODDS_API_KEY", "")
ODDS_API_BASE: str = "https://api.the-odds-api.com"

GAMMA_API_BASE: str = "https://gamma-api.polymarket.com"
CLOB_API_BASE: str = "https://clob.polymarket.com"

# ── Sports to monitor (The Odds API sport keys) ──────────────────────────────
MONITORED_SPORTS: list[str] = [
    "basketball_nba",
    "basketball_ncaab",
    "icehockey_nhl",
    "americanfootball_nfl",
    "soccer_epl",
    "soccer_germany_bundesliga",
    "soccer_spain_la_liga",
]

# Maps Odds API sport keys → human-readable + Polymarket search terms
# SPORT_LABELS maps Odds API sport keys to PM tag matchers.
# The classifier checks these in order. More specific entries MUST come first
# (e.g., NCAA before generic "basketball") to prevent mis-classification.
SPORT_LABELS: dict[str, dict[str, str | list[str]]] = {
    "basketball_ncaab": {"label": "NCAAB", "pm_tags": ["NCAA", "NCAAB", "college basketball", "March Madness"]},
    "basketball_nba": {"label": "NBA", "pm_tags": ["NBA", "basketball"]},
    "icehockey_nhl": {"label": "NHL", "pm_tags": ["NHL", "hockey"]},
    "americanfootball_nfl": {"label": "NFL", "pm_tags": ["NFL", "football"]},
    "soccer_epl": {"label": "EPL", "pm_tags": ["Premier League", "EPL", "soccer"]},
    "soccer_germany_bundesliga": {"label": "Bundesliga", "pm_tags": ["Bundesliga"]},
    "soccer_spain_la_liga": {"label": "La Liga", "pm_tags": ["La Liga"]},
}

# ── Book classification ───────────────────────────────────────────────────────
SHARP_BOOKS: list[str] = ["pinnacle"]
SOFT_BOOKS: list[str] = ["draftkings", "fanduel", "betmgm"]

# ── Polling intervals (seconds) ──────────────────────────────────────────────
POLL_INTERVAL_ODDS: int = 300   # 5 min — conserve The Odds API credits
POLL_INTERVAL_PM: int = 60      # 1 min — Gamma API is free

# ── Edge thresholds ───────────────────────────────────────────────────────────
MIN_EDGE_PCT: float = 0.02          # 2% for detection/paper mode. BUMP TO 4% FOR LIVE TRADING.
MIN_PM_VOLUME: float = 100_000      # $100K minimum volume on PM market
MIN_PM_LIQUIDITY: float = 50_000    # $50K minimum orderbook depth
MIN_MATCH_CONFIDENCE: int = 80      # Out of 100

# ── Fee / slippage assumptions ────────────────────────────────────────────────
PM_FEE_PCT: float = 0.00            # Currently 0% on Polymarket
EST_SLIPPAGE_PCT: float = 0.005     # 0.5% estimated slippage

# ── Risk parameters (Phase 3 — not used yet) ─────────────────────────────────
BANKROLL: float = 1000
KELLY_FRACTION: float = 0.25        # Quarter Kelly
MAX_BET_PCT: float = 0.02           # 2% max per bet
MAX_DAILY_DRAWDOWN: float = 0.05    # 5% daily stop-loss

# ── Database ──────────────────────────────────────────────────────────────────
DB_PATH: Path = _PROJECT_ROOT / "data" / "sports_arb.db"

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL: str = os.getenv("SPORTS_ARB_LOG_LEVEL", "INFO")
