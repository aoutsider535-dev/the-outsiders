#!/usr/bin/env python3
"""
═══════════════════════════════════════════════════════════════════════════════
📦 THE OUTSIDERS — Backtest Data Utilities
═══════════════════════════════════════════════════════════════════════════════

Data acquisition, caching, and preprocessing for the drift sniper backtester.

DATA SOURCES (priority order):
  1. Binance 1-minute klines (free, no key) — BTC spot prices
  2. warproxxx/poly_data CSV — Polymarket fill prices & outcomes
  3. PolyBackTest.com API (premium) — Sub-second orderbook snapshots

All data is cached locally after first download to avoid re-fetching.

═══════════════════════════════════════════════════════════════════════════════
"""

import os
import sys
import json
import time
import lzma
import logging
import hashlib
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Tuple

import requests
import pandas as pd
import numpy as np

# ─── Paths ───────────────────────────────────────────────────────────────────
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT_DIR, "data")
CACHE_DIR = os.path.join(DATA_DIR, "backtest_cache")

# Poly data URLs
POLY_DATA_URL = "https://polydata-archive.s3.us-east-1.amazonaws.com/orderFilled_complete.csv.xz"
POLY_DATA_FILE = os.path.join(DATA_DIR, "orderFilled_complete.csv")
POLY_DATA_XZ = os.path.join(DATA_DIR, "orderFilled_complete.csv.xz")
POLY_BTC_CACHE = os.path.join(CACHE_DIR, "btc_5m_fills.parquet")

# Binance cache
BINANCE_CACHE_DIR = os.path.join(CACHE_DIR, "binance_1m")

log = logging.getLogger("drift_sniper.backtest_data")


# ═══════════════════════════════════════════════════════════════════════════════
# BINANCE HISTORICAL KLINES
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_binance_klines(
    start_ts: int,
    end_ts: int,
    interval: str = "1m",
    symbol: str = "BTCUSDT",
) -> pd.DataFrame:
    """
    Fetch historical Binance klines (candles) for a time range.

    Uses REST API (no key needed). Paginates at 1000 candles per request.
    For 1-minute candles, 30 days = ~43,200 candles = ~44 requests.

    Caches each day to disk as parquet to avoid re-fetching.

    Args:
        start_ts: Start unix timestamp (seconds)
        end_ts: End unix timestamp (seconds)
        interval: Candle interval ('1m', '5m', etc.)
        symbol: Trading pair

    Returns:
        DataFrame with columns: [timestamp, open, high, low, close, volume]
        timestamp is unix seconds (int), prices are float.
    """
    os.makedirs(BINANCE_CACHE_DIR, exist_ok=True)

    # Check cache first — split by day for incremental caching
    all_frames = []
    current_day = start_ts - (start_ts % 86400)  # Floor to day

    while current_day < end_ts:
        day_end = current_day + 86400
        cache_file = os.path.join(
            BINANCE_CACHE_DIR,
            f"{symbol}_{interval}_{current_day}.parquet"
        )

        if os.path.exists(cache_file):
            df = pd.read_parquet(cache_file)
            all_frames.append(df)
            log.debug(f"📂 Cache hit: {cache_file}")
        else:
            # Fetch from Binance
            day_df = _fetch_binance_day(current_day, day_end, interval, symbol)
            if not day_df.empty:
                day_df.to_parquet(cache_file, index=False)
                all_frames.append(day_df)
                log.debug(f"📥 Fetched + cached: {datetime.utcfromtimestamp(current_day).strftime('%Y-%m-%d')}")

        current_day = day_end

    if not all_frames:
        return pd.DataFrame()

    result = pd.concat(all_frames, ignore_index=True)
    # Filter to exact range
    result = result[(result["timestamp"] >= start_ts) & (result["timestamp"] <= end_ts)]
    result = result.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)

    log.info(f"📊 Loaded {len(result):,} Binance {interval} candles ({symbol})")
    return result


def _fetch_binance_day(
    start_ts: int, end_ts: int, interval: str, symbol: str
) -> pd.DataFrame:
    """
    Fetch one day of Binance klines via REST API.
    Paginates through 1000-candle pages.
    Rate limit: ~1200 req/min on Binance, we use ~2/day.
    """
    all_rows = []
    current_ms = start_ts * 1000

    while current_ms < end_ts * 1000:
        try:
            r = requests.get(
                "https://api.binance.com/api/v3/klines",
                params={
                    "symbol": symbol,
                    "interval": interval,
                    "startTime": int(current_ms),
                    "endTime": int(end_ts * 1000),
                    "limit": 1000,
                },
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()

            if not data:
                break

            for candle in data:
                all_rows.append({
                    "timestamp": int(candle[0]) // 1000,  # ms → s
                    "open": float(candle[1]),
                    "high": float(candle[2]),
                    "low": float(candle[3]),
                    "close": float(candle[4]),
                    "volume": float(candle[5]),
                })

            # Move past last candle
            last_ms = data[-1][0]
            if last_ms <= current_ms:
                break
            current_ms = last_ms + 1

            # Courtesy delay to avoid rate limits
            time.sleep(0.1)

        except requests.exceptions.RequestException as e:
            log.warning(f"⚠️ Binance API error: {e}. Retrying in 2s...")
            time.sleep(2)
            continue
        except Exception as e:
            log.error(f"💥 Binance fetch error: {e}")
            break

    if not all_rows:
        return pd.DataFrame()

    return pd.DataFrame(all_rows)


def get_btc_price_at(
    candles_df: pd.DataFrame, target_ts: int
) -> Optional[float]:
    """
    Get the BTC price at a specific timestamp from 1-minute candle data.

    Uses the close price of the candle that contains the target timestamp.
    For T-45s before window end, this gives us the price with ~30s precision.

    Args:
        candles_df: DataFrame with 'timestamp' and 'close' columns
        target_ts: Unix timestamp (seconds) to look up

    Returns:
        BTC price (float) or None if no data available
    """
    if candles_df.empty:
        return None

    # Find the 1m candle that contains this timestamp
    candle_ts = target_ts - (target_ts % 60)  # Floor to minute

    match = candles_df[candles_df["timestamp"] == candle_ts]
    if not match.empty:
        return float(match.iloc[0]["close"])

    # Nearest candle within 60s
    diffs = (candles_df["timestamp"] - target_ts).abs()
    nearest_idx = diffs.idxmin()
    if diffs[nearest_idx] <= 60:
        return float(candles_df.loc[nearest_idx, "close"])

    return None


def get_btc_candle_open(
    candles_df: pd.DataFrame, window_ts: int
) -> Optional[float]:
    """
    Get the 5-minute candle open price for a window.

    Args:
        candles_df: 1-minute candle DataFrame
        window_ts: Window start timestamp (5-min aligned)

    Returns:
        Open price of the first 1m candle in this window
    """
    if candles_df.empty:
        return None

    match = candles_df[candles_df["timestamp"] == window_ts]
    if not match.empty:
        return float(match.iloc[0]["open"])

    # Try within first minute of window
    close = candles_df[
        (candles_df["timestamp"] >= window_ts) &
        (candles_df["timestamp"] < window_ts + 60)
    ]
    if not close.empty:
        return float(close.iloc[0]["open"])

    return None


def get_btc_candle_close(
    candles_df: pd.DataFrame, window_ts: int
) -> Optional[float]:
    """
    Get the 5-minute candle close price (last 1m candle's close in window).
    Used to determine window outcome: close > open = UP, close < open = DOWN.
    """
    if candles_df.empty:
        return None

    # The 5m candle close is the close of the last 1m candle in the window
    # Window: [window_ts, window_ts + 300)
    # Last 1m candle starts at window_ts + 240
    last_minute_ts = window_ts + 240
    match = candles_df[candles_df["timestamp"] == last_minute_ts]
    if not match.empty:
        return float(match.iloc[0]["close"])

    # Fallback: closest candle to window end
    window_end = window_ts + 300
    close_candles = candles_df[
        (candles_df["timestamp"] >= window_ts + 180) &
        (candles_df["timestamp"] < window_end)
    ]
    if not close_candles.empty:
        return float(close_candles.iloc[-1]["close"])

    return None


# ═══════════════════════════════════════════════════════════════════════════════
# WARPROXXX/POLY_DATA — Polymarket Historical Fill Data
# ═══════════════════════════════════════════════════════════════════════════════

def download_poly_data(force: bool = False) -> bool:
    """
    Download the warproxxx/poly_data orderFilled CSV if not present.

    File: ~1-2 GB compressed (.xz), ~5-10 GB uncompressed.
    Contains ALL Polymarket order fills ever — we filter to BTC 5m later.

    Downloads with progress indicator. Extracts .xz to .csv.
    This is a one-time operation; subsequent runs use the cached file.

    Returns True if file is available (downloaded or already existed).
    """
    if os.path.exists(POLY_DATA_FILE) and not force:
        size_gb = os.path.getsize(POLY_DATA_FILE) / 1e9
        log.info(f"📂 poly_data CSV exists ({size_gb:.1f} GB)")
        return True

    if os.path.exists(POLY_DATA_XZ) and not force:
        log.info("📦 Found compressed file, extracting...")
        return _extract_xz(POLY_DATA_XZ, POLY_DATA_FILE)

    log.info(f"📥 Downloading poly_data CSV from S3...")
    log.info(f"   URL: {POLY_DATA_URL}")
    log.info(f"   This is a large file (~1-2 GB). One-time download.")

    os.makedirs(DATA_DIR, exist_ok=True)

    try:
        r = requests.get(POLY_DATA_URL, stream=True, timeout=30)
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))

        downloaded = 0
        last_log = 0
        with open(POLY_DATA_XZ, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):  # 1MB chunks
                f.write(chunk)
                downloaded += len(chunk)
                # Log progress every 50MB
                if downloaded - last_log > 50 * 1024 * 1024:
                    pct = downloaded / total * 100 if total else 0
                    log.info(f"   📥 {downloaded / 1e6:.0f} MB / {total / 1e6:.0f} MB ({pct:.1f}%)")
                    last_log = downloaded

        log.info(f"✅ Download complete: {downloaded / 1e6:.0f} MB")

        # Extract
        return _extract_xz(POLY_DATA_XZ, POLY_DATA_FILE)

    except requests.exceptions.RequestException as e:
        log.error(f"❌ Download failed: {e}")
        return False
    except Exception as e:
        log.error(f"💥 Download error: {e}")
        return False


def _extract_xz(xz_path: str, out_path: str) -> bool:
    """Extract an .xz compressed file."""
    try:
        log.info(f"📦 Extracting {xz_path}...")
        with lzma.open(xz_path, "rb") as xz_in:
            with open(out_path, "wb") as f_out:
                while True:
                    chunk = xz_in.read(1024 * 1024 * 10)  # 10MB
                    if not chunk:
                        break
                    f_out.write(chunk)
        size_gb = os.path.getsize(out_path) / 1e9
        log.info(f"✅ Extracted: {size_gb:.1f} GB")
        return True
    except Exception as e:
        log.error(f"❌ Extraction failed: {e}")
        return False


def load_btc_5m_fills(
    start_ts: int,
    end_ts: int,
    force_rebuild: bool = False,
) -> Optional[pd.DataFrame]:
    """
    Load Polymarket fill data for BTC 5-minute markets from poly_data CSV.

    First call filters the massive CSV to just BTC 5m fills and caches as
    parquet. Subsequent calls load from cache.

    Columns in output:
      - timestamp (unix seconds)
      - window_ts (5-min window this fill belongs to)
      - side (BUY/SELL)
      - price (fill price, 0-1)
      - size (shares filled)
      - outcome_token (UP or DOWN token)

    Returns None if poly_data not available.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)

    # Check parquet cache
    cache_key = f"btc_5m_fills_{start_ts}_{end_ts}.parquet"
    cache_path = os.path.join(CACHE_DIR, cache_key)

    if os.path.exists(cache_path) and not force_rebuild:
        df = pd.read_parquet(cache_path)
        log.info(f"📂 Loaded {len(df):,} BTC 5m fills from cache")
        return df

    # Need to process the raw CSV
    if not os.path.exists(POLY_DATA_FILE):
        log.warning("⚠️ poly_data CSV not found. Run with --download-poly-data first.")
        return None

    log.info("🔍 Filtering poly_data CSV for BTC 5m markets (may take a minute)...")

    # Read in chunks to handle the massive file
    # The CSV has columns like: id, market, asset, side, price, size, timestamp, ...
    # We need to identify BTC 5m markets by their slug/condition_id pattern
    chunks = []
    chunk_size = 500_000

    try:
        for chunk in pd.read_csv(POLY_DATA_FILE, chunksize=chunk_size, low_memory=False):
            # Normalize column names
            chunk.columns = [c.strip().lower() for c in chunk.columns]

            # Look for timestamp column
            ts_col = None
            for candidate in ["timestamp", "blocktimestamp", "block_timestamp", "time"]:
                if candidate in chunk.columns:
                    ts_col = candidate
                    break

            if ts_col is None:
                log.error(f"❌ Cannot find timestamp column. Columns: {list(chunk.columns)}")
                return None

            # Look for market identifier
            market_col = None
            for candidate in ["market", "market_slug", "slug", "condition_id", "conditionid"]:
                if candidate in chunk.columns:
                    market_col = candidate
                    break

            if market_col is None:
                # Try filtering by asset/token patterns instead
                log.debug("No market column found, will try alternative filtering")
                continue

            # Filter for BTC 5m markets
            # Slugs look like: btc-updown-5m-1710000000
            btc_mask = chunk[market_col].astype(str).str.contains(
                r"btc.*5m|btc.*updown", case=False, na=False, regex=True
            )

            # Time filter
            if ts_col in chunk.columns:
                # Handle various timestamp formats
                ts_vals = pd.to_numeric(chunk[ts_col], errors="coerce")
                # If timestamps are in milliseconds, convert
                if ts_vals.median() > 1e12:
                    ts_vals = ts_vals / 1000
                time_mask = (ts_vals >= start_ts) & (ts_vals <= end_ts)
                chunk["_unix_ts"] = ts_vals
            else:
                time_mask = pd.Series(True, index=chunk.index)
                chunk["_unix_ts"] = 0

            filtered = chunk[btc_mask & time_mask].copy()
            if not filtered.empty:
                chunks.append(filtered)
                log.debug(f"  Found {len(filtered)} BTC 5m fills in chunk")

    except Exception as e:
        log.error(f"💥 Error reading poly_data: {e}")
        return None

    if not chunks:
        log.warning("⚠️ No BTC 5m fills found in poly_data for this time range")
        return None

    result = pd.concat(chunks, ignore_index=True)

    # Standardize columns
    if "_unix_ts" in result.columns:
        result["timestamp"] = result["_unix_ts"].astype(int)
    result["window_ts"] = (result["timestamp"] // 300) * 300

    # Normalize price and size columns
    for price_col in ["price", "fill_price", "fillprice"]:
        if price_col in result.columns:
            result["price"] = pd.to_numeric(result[price_col], errors="coerce")
            break

    for size_col in ["size", "fill_size", "fillsize", "amount"]:
        if size_col in result.columns:
            result["size"] = pd.to_numeric(result[size_col], errors="coerce")
            break

    # Cache
    result.to_parquet(cache_path, index=False)
    log.info(f"✅ Cached {len(result):,} BTC 5m fills to {cache_path}")

    return result


def estimate_pm_book_price(drift_pct: float) -> Tuple[float, float]:
    """
    Estimate Polymarket best ask price for favored side based on BTC drift.

    When no real orderbook data is available, we estimate what the book
    would look like based on observed relationships between drift and
    Polymarket prices.

    IMPORTANT: Polymarket markets are efficient — when BTC drifts, makers
    reprice quickly. The book mostly tracks the "true" continuation
    probability. Our edge comes from the 3-7% gap between model and
    market, NOT from massive mispricings.

    Empirical model (calibrated from live observations + theoretical):
      - At 0% drift: both sides ~$0.50 (fair coin)
      - At 0.10% drift: favored ~$0.56 (market barely moved)
      - At 0.15% drift: favored ~$0.62 (our min threshold)
      - At 0.20% drift: favored ~$0.67
      - At 0.30% drift: favored ~$0.74
      - At 0.40% drift: favored ~$0.78
      - At 0.50%+ drift: favored ~$0.82+ (near our max price cap)

    The key insight: the market is efficient enough that our edge is
    SMALL (7-15%), not huge. If estimated prices show 30%+ edge,
    the estimate is too low.

    Returns: (favored_ask, estimated_depth_shares)
    """
    abs_drift = abs(drift_pct)

    # Price estimation using logistic-like curve
    # Maps drift → implied probability, then adds a small spread
    #
    # Base continuation probability (from our model at T-45s):
    #   P = 0.50 + drift * 180 - 0.15 * 0.05
    # Market ask ≈ P - small_edge (market is ~3-5% behind our model)
    #
    # Use a sigmoid-like mapping for realistic book behavior:
    #   ask = 0.50 + 0.40 * tanh(abs_drift * 350)
    # This gives:
    #   0.10% → 0.63, 0.15% → 0.70, 0.20% → 0.74
    #   0.30% → 0.80, 0.40% → 0.83, 0.50% → 0.85
    import math
    # The market is HIGHLY efficient at T-45s. Makers see the same drift
    # we do and reprice accordingly. The ask tracks continuation prob
    # closely, leaving only a small edge for us.
    #
    # Calibration: steeper curve so that at our minimum drift (0.15%),
    # the ask is already ~$0.72 (our model says ~$0.77 = 5% edge).
    # At 0.25%+ drift, asks are $0.80+ and most trades get rejected
    # by the MAX_BUY_PRICE filter.
    estimated_ask = 0.50 + 0.42 * math.tanh(abs_drift * 450)
    estimated_ask = max(0.50, min(estimated_ask, 0.95))

    # Add noise to simulate real market microstructure
    # ±2% random walk around the estimate (seeded by drift for reproducibility)
    noise_seed = int(abs_drift * 1e8) % 100
    noise = (noise_seed - 50) / 50 * 0.04  # ±4% noise
    estimated_ask = max(0.50, min(estimated_ask + noise, 0.95))

    # Depth estimation: larger drifts have thinner books
    # At low drift: ~200 shares available. At high drift: ~30-50 shares.
    estimated_depth = max(30, 200 * math.exp(-abs_drift * 500))

    return estimated_ask, estimated_depth


# ═══════════════════════════════════════════════════════════════════════════════
# POLYBACKTEST.COM API (Premium Data Source)
# ═══════════════════════════════════════════════════════════════════════════════

class PolyBackTestClient:
    """
    Client for PolyBackTest.com API — sub-second orderbook snapshots.

    This is the highest-fidelity data source: actual Polymarket orderbook
    state at any historical timestamp, plus Chainlink reference prices.

    API endpoints:
      GET /v1/markets                        — List available markets
      GET /v1/snapshot-at/{timestamp}        — Book snapshot at exact time
      GET /v1/markets/{id}/snapshots         — All snapshots for a market

    Rate limits: Depends on plan (typically 100-1000 req/min).

    Usage:
      client = PolyBackTestClient(api_key="...")
      snapshot = client.get_snapshot_at(window_ts + 255)  # T-45s
    """

    BASE_URL = "https://api.polybacktest.com"

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {api_key}"
        self.session.headers["Accept"] = "application/json"
        self._rate_limit_remaining = 100
        self._rate_limit_reset = 0

    def _request(self, method: str, path: str, params: dict = None) -> Optional[dict]:
        """Make an authenticated API request with rate limit handling."""
        # Check rate limit
        if self._rate_limit_remaining <= 1 and time.time() < self._rate_limit_reset:
            wait = self._rate_limit_reset - time.time() + 0.5
            log.debug(f"⏳ Rate limit, waiting {wait:.1f}s")
            time.sleep(wait)

        url = f"{self.BASE_URL}{path}"
        try:
            r = self.session.request(method, url, params=params, timeout=10)

            # Update rate limit tracking
            self._rate_limit_remaining = int(r.headers.get("X-RateLimit-Remaining", 100))
            reset = r.headers.get("X-RateLimit-Reset")
            if reset:
                self._rate_limit_reset = float(reset)

            if r.status_code == 429:
                retry_after = float(r.headers.get("Retry-After", 5))
                log.warning(f"⚠️ Rate limited, retrying in {retry_after}s")
                time.sleep(retry_after)
                return self._request(method, path, params)

            r.raise_for_status()
            return r.json()

        except requests.exceptions.RequestException as e:
            log.warning(f"⚠️ PolyBackTest API error: {e}")
            return None

    def get_markets(self, search: str = "btc-updown-5m") -> list:
        """List markets matching a search term."""
        data = self._request("GET", "/v1/markets", {"search": search})
        return data if data else []

    def get_snapshot_at(
        self, market_id: str, timestamp: int
    ) -> Optional[dict]:
        """
        Get orderbook snapshot at a specific timestamp.

        Returns dict with:
          - asks: [(price, size), ...]
          - bids: [(price, size), ...]
          - chainlink_price: float (on-chain BTC/USD at that moment)
          - timestamp: actual snapshot timestamp
        """
        data = self._request(
            "GET",
            f"/v1/snapshot-at/{timestamp}",
            {"market_id": market_id},
        )
        return data

    def get_market_snapshots(
        self, market_id: str, start_ts: int = None, end_ts: int = None
    ) -> list:
        """Get all snapshots for a market in a time range."""
        params = {"market_id": market_id}
        if start_ts:
            params["start"] = start_ts
        if end_ts:
            params["end"] = end_ts
        data = self._request("GET", f"/v1/markets/{market_id}/snapshots", params)
        return data if data else []


def get_polybacktest_client() -> Optional[PolyBackTestClient]:
    """Create a PolyBackTest client from .env credentials, or None if not configured."""
    try:
        from dotenv import dotenv_values
        config = dotenv_values(os.path.join(ROOT_DIR, ".env"))
        api_key = config.get("POLYBACKTEST_API_KEY", "")
        if api_key:
            return PolyBackTestClient(api_key)
    except Exception:
        pass
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# WINDOW GENERATION
# ═══════════════════════════════════════════════════════════════════════════════

def generate_windows(start_ts: int, end_ts: int) -> List[int]:
    """
    Generate all 5-minute window timestamps in a range.

    Windows are aligned to 5-minute boundaries (unix_ts % 300 == 0).
    BTC 5-min markets run 24/7 on Polymarket.

    Args:
        start_ts: Start timestamp (floored to 5-min boundary)
        end_ts: End timestamp

    Returns:
        List of window start timestamps
    """
    start_ts = start_ts - (start_ts % 300)  # Floor to 5-min
    windows = []
    current = start_ts
    while current < end_ts:
        windows.append(current)
        current += 300
    return windows


def determine_outcome(
    candles_df: pd.DataFrame, window_ts: int
) -> Optional[str]:
    """
    Determine the BTC 5-min window outcome from candle data.

    Logic: If BTC close price > open price → 'up', else → 'down'.
    Exactly matches how Polymarket resolves these markets.

    Note: A close exactly equal to open technically resolves as 'down'
    on Polymarket (price did not go UP). We match this behavior.
    """
    btc_open = get_btc_candle_open(candles_df, window_ts)
    btc_close = get_btc_candle_close(candles_df, window_ts)

    if btc_open is None or btc_close is None:
        return None

    return "up" if btc_close > btc_open else "down"
