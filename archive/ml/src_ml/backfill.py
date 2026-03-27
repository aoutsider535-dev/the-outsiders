"""
Backfill ML training data from historical trades.

We can't reconstruct all 54 features (no HTF/orderbook snapshots saved),
but we CAN rebuild ~25 features from signal_data + trade metadata.
Missing features default to 0.0 — the model learns to weight what's available.
"""
import json
import sqlite3
import numpy as np
from datetime import datetime, timezone, timedelta

PST = timezone(timedelta(hours=-8))

# Map old strategy names to keys
STRATEGY_MAP = {
    "btc_5min_momentum_LIVE": "momentum",
    "btc_5min_trend_rider_LIVE": "trend_rider",
    "btc_5min_ob_imbalance_LIVE": "orderbook_imbalance",
    "btc_5min_smart_money_LIVE": "smart_money",
    "btc_5min_meanrev_LIVE": "mean_reversion",
}


def backfill(db_path, verbose=True):
    """
    Backfill ml_features table from historical closed LIVE trades.
    Only adds trades that aren't already in ml_features.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    # Get existing trade_ids in ml_features
    existing = set(r[0] for r in conn.execute("SELECT trade_id FROM ml_features WHERE trade_id IS NOT NULL").fetchall())
    
    # Get closed LIVE trades with signal_data
    trades = conn.execute("""
        SELECT id, strategy, direction, entry_price, quantity, pnl, exit_reason,
               signal_data, created_at, closed_at
        FROM trades 
        WHERE strategy LIKE '%_LIVE' 
          AND status = 'closed' 
          AND signal_data IS NOT NULL 
          AND signal_data != ''
        ORDER BY id
    """).fetchall()
    
    inserted = 0
    skipped = 0
    
    for trade in trades:
        trade_id = trade["id"]
        if trade_id in existing:
            skipped += 1
            continue
        
        try:
            sig_data = json.loads(trade["signal_data"])
        except (json.JSONDecodeError, TypeError):
            skipped += 1
            continue
        
        # Determine outcome
        exit_reason = trade["exit_reason"] or ""
        if "win" in exit_reason:
            outcome = 1
        elif "loss" in exit_reason:
            outcome = 0
        else:
            outcome = None  # stuck/unknown
        
        pnl = trade["pnl"] or 0.0
        strategy = trade["strategy"]
        strategy_key = STRATEGY_MAP.get(strategy, strategy)
        
        # Reconstruct features from signal_data
        features = _reconstruct_features(sig_data, trade, strategy_key)
        
        conn.execute(
            """INSERT INTO ml_features (trade_id, strategy, features, prediction, decision, outcome, pnl, created_at, resolved_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (trade_id, strategy_key, json.dumps(features), None, "take",
             outcome, pnl, trade["created_at"], trade["closed_at"])
        )
        inserted += 1
    
    conn.commit()
    
    # Count resolved
    resolved = conn.execute("SELECT COUNT(*) FROM ml_features WHERE outcome IS NOT NULL").fetchone()[0]
    conn.close()
    
    if verbose:
        print(f"✅ Backfill complete: {inserted} inserted, {skipped} skipped")
        print(f"   Total resolved training samples: {resolved}")
    
    return {"inserted": inserted, "skipped": skipped, "total_resolved": resolved}


def _reconstruct_features(sig_data, trade, strategy_key):
    """Reconstruct as many features as possible from stored signal_data."""
    features = {}
    
    # Time features from created_at
    try:
        if trade["created_at"]:
            ts = datetime.fromisoformat(trade["created_at"])
            ts_pst = ts.astimezone(PST)
            features["hour_pst"] = ts_pst.hour + ts_pst.minute / 60.0
            features["is_overnight"] = 1.0 if ts_pst.hour < 6 or ts_pst.hour >= 23 else 0.0
            features["is_market_hours"] = 1.0 if 6 <= ts_pst.hour < 16 else 0.0
            features["day_of_week"] = ts_pst.weekday()
            features["is_weekend"] = 1.0 if ts_pst.weekday() >= 5 else 0.0
    except Exception:
        pass
    
    # Signal features
    features["signal_direction"] = 1.0 if trade["direction"] == "up" else -1.0
    features["signal_confidence"] = float(sig_data.get("confidence", 0.5))
    features["signal_edge"] = float(sig_data.get("edge_pct", 0))
    
    # Market prices
    up_price = sig_data.get("market_up_price", 0.5)
    down_price = sig_data.get("market_down_price", 0.5)
    features["market_spread"] = abs(up_price + down_price - 1.0)
    features["market_skew"] = up_price - down_price
    
    # Strategy-specific signal features that map to our ML features
    # Momentum/Trend signals
    if "momentum_score" in sig_data:
        features["ema_spread"] = float(sig_data.get("momentum_score", 0)) * 0.1  # Approximate
    if "trend_score" in sig_data:
        features["trend_consistency"] = max(0, min(1, (float(sig_data.get("trend_score", 0)) + 1) / 2))
    if "volatility_regime" in sig_data:
        vr = sig_data.get("volatility_regime", 1.0)
        if isinstance(vr, str):
            features["vol_regime"] = {"low": 0.5, "normal": 1.0, "high": 1.5, "extreme": 2.0}.get(vr, 1.0)
        else:
            features["vol_regime"] = float(vr)
    if "price_change_pct" in sig_data:
        features["returns_5m"] = float(sig_data.get("price_change_pct", 0))
    
    # Trend Rider signals
    if "ema_fast" in sig_data and "ema_slow" in sig_data:
        ema_f = sig_data["ema_fast"]
        ema_s = sig_data["ema_slow"]
        if ema_s > 0:
            features["ema_spread"] = (ema_f - ema_s) / ema_s * 100
            features["ema_spread_abs"] = abs(features["ema_spread"])
    if "rsi" in sig_data:
        rsi = float(sig_data["rsi"])
        features["rsi"] = rsi
        features["rsi_extreme"] = 1.0 if rsi < 30 or rsi > 70 else 0.0
        features["rsi_distance_50"] = abs(rsi - 50)
    if "atr_pct" in sig_data:
        features["volatility_5"] = float(sig_data["atr_pct"])
    if "vwap_score" in sig_data:
        features["volume_trend"] = (float(sig_data["vwap_score"]) + 1) / 2  # Normalize -1..1 to 0..1
    
    # OB Imbalance signals
    if "poly_imbalance" in sig_data:
        features["poly_ob_imbalance"] = float(sig_data["poly_imbalance"])
    if "kraken_imbalance" in sig_data:
        features["ob_imbalance"] = float(sig_data["kraken_imbalance"])
    if "depth_concentration" in sig_data:
        features["ob_total_depth"] = float(sig_data.get("total_depth", 0))
    
    # Smart Money signals
    if "divergence_score" in sig_data:
        features["deriv_combined_score"] = float(sig_data.get("divergence_score", 0))
    if "btc_change_pct" in sig_data:
        features["returns_5m"] = float(sig_data["btc_change_pct"])
    
    # Strategy encoding
    for s in ["trend_rider", "orderbook_imbalance", "smart_money"]:
        features[f"is_{s}"] = 1.0 if strategy_key == s else 0.0
    
    return features


if __name__ == "__main__":
    import os
    db_path = os.path.join(os.path.dirname(__file__), "..", "..", "data", "polymarket.db")
    backfill(db_path)
