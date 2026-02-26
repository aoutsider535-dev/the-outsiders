"""
ML Meta-Learner: "The Brain"

Sits on top of existing strategies and learns WHEN to trust each signal.
Uses online learning — improves with every resolved trade.

Architecture:
  1. Strategies generate signals as before
  2. Feature extractor captures market regime + signal + strategy state
  3. Meta-learner predicts P(win) for the signal
  4. Only execute if P(win) > threshold (dynamic, adapts to fees)
  5. After resolution, update model with outcome

Model: Gradient Boosted Trees (LightGBM) with online retraining
- Lightweight, fast inference (<1ms)
- Handles mixed feature types well
- Retrains on rolling window every N trades
"""
import json
import os
import sqlite3
import time
import pickle
import numpy as np
from datetime import datetime, timezone, timedelta

PST = timezone(timedelta(hours=-8))

# Minimum trades before the model starts making decisions
# Below this, all signals pass through (exploration phase)
MIN_TRAINING_SAMPLES = 30

# Retrain every N new trades
RETRAIN_INTERVAL = 15

# Rolling window size for training (use most recent N trades)
TRAINING_WINDOW = 500

# Minimum predicted win probability to take a trade
# Must be > 0.52 to overcome ~2% fees + 0.5% slippage
MIN_WIN_PROB = 0.55

# Feature columns (ordered, must match training and inference)
FEATURE_COLS = [
    # Time (5)
    "hour_pst", "is_overnight", "is_market_hours", "day_of_week", "is_weekend",
    # Price momentum (4)
    "returns_5m", "returns_15m", "returns_30m", "returns_60m",
    # Volatility (3)
    "volatility_20", "volatility_5", "vol_regime",
    # Trend (3)
    "ema_spread", "ema_spread_abs", "trend_consistency",
    # RSI (3)
    "rsi", "rsi_extreme", "rsi_distance_50",
    # Volume (2)
    "volume_ratio", "volume_trend",
    # Signal (3)
    "signal_direction", "signal_confidence", "signal_edge",
    # Strategy state (5)
    "strategy_win_rate", "strategy_consecutive_losses", "strategy_trade_count", "strategy_kelly",
    "strategy_recent_pnl",
    # Market micro (2)
    "market_spread", "market_skew",
    # Streak (1)
    "direction_streak",
    # HTF context (7)
    "htf_h1_trend", "htf_h4_trend", "htf_h1_rsi", "htf_h1_rsi_extreme",
    "htf_aligned", "htf_confirms_signal", "htf_h4_confirms_signal",
    # Derivatives (5)
    "deriv_funding_rate", "deriv_oi_change", "deriv_combined_score",
    "deriv_funding_extreme", "deriv_confirms_signal",
    # Orderbook — Kraken (6)
    "ob_total_depth", "ob_imbalance", "ob_bid_depth", "ob_ask_depth",
    "ob_thin_market", "ob_confirms_signal",
    # Orderbook — Polymarket (2)
    "poly_ob_depth", "poly_ob_imbalance",
    # Strategy encoding (3)
    "is_trend_rider", "is_orderbook_imbalance", "is_smart_money",
]


class MetaLearner:
    """Online ML meta-learner that decides whether to take a trade signal."""
    
    def __init__(self, db_path, model_dir="data/ml"):
        self.db_path = db_path
        self.model_dir = model_dir
        self.model = None
        self.model_version = 0
        self.trades_since_retrain = 0
        self.total_predictions = 0
        self.total_trades_taken = 0
        self.total_trades_skipped = 0
        self.correct_takes = 0  # Took trade and it won
        self.correct_skips = 0  # Skipped trade and it would have lost
        self.is_exploring = True  # True until MIN_TRAINING_SAMPLES reached
        
        os.makedirs(model_dir, exist_ok=True)
        self._init_db()
        self._load_model()
    
    def _init_db(self):
        """Create ML tables if they don't exist."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ml_features (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id INTEGER,
                strategy TEXT,
                features TEXT,  -- JSON blob
                prediction REAL,  -- P(win) at decision time
                decision TEXT,  -- 'take' or 'skip'
                outcome INTEGER,  -- 1=win, 0=loss, NULL=pending
                pnl REAL,
                created_at TEXT,
                resolved_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ml_model_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                version INTEGER,
                training_samples INTEGER,
                accuracy REAL,
                precision_take REAL,
                recall_take REAL,
                win_rate_taken REAL,
                win_rate_skipped REAL,
                feature_importance TEXT,  -- JSON
                created_at TEXT
            )
        """)
        conn.commit()
        conn.close()
    
    def _load_model(self):
        """Load the latest trained model from disk."""
        model_path = os.path.join(self.model_dir, "meta_model.pkl")
        if os.path.exists(model_path):
            try:
                with open(model_path, "rb") as f:
                    data = pickle.load(f)
                self.model = data["model"]
                self.model_version = data.get("version", 0)
                self.is_exploring = False
                return True
            except Exception:
                pass
        return False
    
    def _save_model(self):
        """Save model to disk."""
        if self.model is None:
            return
        model_path = os.path.join(self.model_dir, "meta_model.pkl")
        with open(model_path, "wb") as f:
            pickle.dump({
                "model": self.model,
                "version": self.model_version,
                "saved_at": time.time(),
            }, f)
    
    def should_trade(self, features, strategy_key, trade_id=None):
        """
        Decide whether to take a trade signal.
        
        Args:
            features: dict from extract_features()
            strategy_key: strategy name
            trade_id: optional DB trade ID for tracking
            
        Returns:
            (should_trade: bool, prediction: float, reason: str)
        """
        if features is None:
            return True, 0.5, "no features — defaulting to trade"
        
        self.total_predictions += 1
        
        # Exploration phase: take all trades to collect training data
        sample_count = self._get_training_count()
        if sample_count < MIN_TRAINING_SAMPLES or self.model is None:
            self.is_exploring = True
            self._record_decision(trade_id, strategy_key, features, 0.5, "take")
            self.total_trades_taken += 1
            remaining = MIN_TRAINING_SAMPLES - sample_count
            return True, 0.5, f"exploring ({remaining} trades until ML active)"
        
        self.is_exploring = False
        
        # Extract feature vector in correct order
        X = self._features_to_array(features)
        if X is None:
            self._record_decision(trade_id, strategy_key, features, 0.5, "take")
            self.total_trades_taken += 1
            return True, 0.5, "incomplete features — defaulting to trade"
        
        # Predict
        try:
            prob = self.model.predict_proba(X.reshape(1, -1))[0][1]  # P(win)
        except Exception as e:
            self._record_decision(trade_id, strategy_key, features, 0.5, "take")
            self.total_trades_taken += 1
            return True, 0.5, f"prediction error: {e}"
        
        # Decision
        if prob >= MIN_WIN_PROB:
            decision = "take"
            self.total_trades_taken += 1
            reason = f"🧠 ML: P(win)={prob:.1%} ≥ {MIN_WIN_PROB:.0%} — TRADE"
        else:
            decision = "skip"
            self.total_trades_skipped += 1
            reason = f"🧠 ML: P(win)={prob:.1%} < {MIN_WIN_PROB:.0%} — SKIP"
        
        self._record_decision(trade_id, strategy_key, features, prob, decision)
        
        return decision == "take", prob, reason
    
    def record_outcome(self, trade_id, won, pnl):
        """Record the outcome of a trade for learning."""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE ml_features SET outcome=?, pnl=?, resolved_at=? WHERE trade_id=?",
            (1 if won else 0, pnl, datetime.now(timezone.utc).isoformat(), trade_id)
        )
        conn.commit()
        conn.close()
        
        self.trades_since_retrain += 1
        
        # Check if we should retrain
        if self.trades_since_retrain >= RETRAIN_INTERVAL:
            self.retrain()
    
    def retrain(self):
        """Retrain the model on recent trade data."""
        try:
            from sklearn.ensemble import GradientBoostingClassifier
            from sklearn.model_selection import cross_val_score
        except ImportError:
            # Fallback if sklearn not available
            try:
                import subprocess
                import sys
                subprocess.check_call([sys.executable, "-m", "pip", "install", "scikit-learn", "-q"])
                from sklearn.ensemble import GradientBoostingClassifier
                from sklearn.model_selection import cross_val_score
            except Exception:
                return False
        
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            """SELECT features, outcome FROM ml_features 
               WHERE outcome IS NOT NULL 
               ORDER BY id DESC LIMIT ?""",
            (TRAINING_WINDOW,)
        ).fetchall()
        conn.close()
        
        if len(rows) < MIN_TRAINING_SAMPLES:
            return False
        
        # Build training data
        X_list = []
        y_list = []
        for feat_json, outcome in rows:
            features = json.loads(feat_json)
            X = self._features_to_array(features)
            if X is not None:
                X_list.append(X)
                y_list.append(outcome)
        
        if len(X_list) < MIN_TRAINING_SAMPLES:
            return False
        
        X_train = np.array(X_list)
        y_train = np.array(y_list)
        
        # Train with conservative hyperparameters (avoid overfitting on small data)
        model = GradientBoostingClassifier(
            n_estimators=50,       # Small forest — we don't have much data
            max_depth=3,           # Shallow trees — prevent overfitting
            learning_rate=0.1,
            min_samples_leaf=5,    # Need at least 5 samples per leaf
            subsample=0.8,         # Stochastic gradient boosting
            random_state=42,
        )
        
        model.fit(X_train, y_train)
        
        # Cross-validation score
        if len(X_list) >= 20:
            cv_folds = min(5, len(X_list) // 10)
            if cv_folds >= 2:
                cv_scores = cross_val_score(model, X_train, y_train, cv=cv_folds, scoring="accuracy")
                accuracy = cv_scores.mean()
            else:
                accuracy = model.score(X_train, y_train)
        else:
            accuracy = model.score(X_train, y_train)
        
        # Feature importance
        importances = dict(zip(FEATURE_COLS, model.feature_importances_))
        top_features = dict(sorted(importances.items(), key=lambda x: x[1], reverse=True)[:10])
        
        # Evaluate: what's the win rate of trades we WOULD have taken vs skipped?
        probs = model.predict_proba(X_train)[:, 1]
        taken_mask = probs >= MIN_WIN_PROB
        skipped_mask = ~taken_mask
        
        wr_taken = y_train[taken_mask].mean() if taken_mask.sum() > 0 else 0
        wr_skipped = y_train[skipped_mask].mean() if skipped_mask.sum() > 0 else 0
        
        # Only deploy if model shows edge: taken WR > skipped WR
        if wr_taken > wr_skipped or self.model is None:
            self.model = model
            self.model_version += 1
            self.trades_since_retrain = 0
            self._save_model()
            
            # Log model performance
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                """INSERT INTO ml_model_log 
                   (version, training_samples, accuracy, win_rate_taken, win_rate_skipped, 
                    feature_importance, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (self.model_version, len(X_list), accuracy, float(wr_taken), float(wr_skipped),
                 json.dumps(top_features), datetime.now(timezone.utc).isoformat())
            )
            conn.commit()
            conn.close()
            
            return True
        
        self.trades_since_retrain = 0
        return False
    
    def get_stats(self):
        """Return current ML stats for logging."""
        conn = sqlite3.connect(self.db_path)
        
        # Overall stats
        total = conn.execute("SELECT COUNT(*) FROM ml_features WHERE outcome IS NOT NULL").fetchone()[0]
        taken_wins = conn.execute(
            "SELECT COUNT(*) FROM ml_features WHERE decision='take' AND outcome=1"
        ).fetchone()[0]
        taken_total = conn.execute(
            "SELECT COUNT(*) FROM ml_features WHERE decision='take' AND outcome IS NOT NULL"
        ).fetchone()[0]
        skipped_wins = conn.execute(
            "SELECT COUNT(*) FROM ml_features WHERE decision='skip' AND outcome=1"
        ).fetchone()[0]
        skipped_total = conn.execute(
            "SELECT COUNT(*) FROM ml_features WHERE decision='skip' AND outcome IS NOT NULL"
        ).fetchone()[0]
        
        conn.close()
        
        return {
            "model_version": self.model_version,
            "is_exploring": self.is_exploring,
            "total_samples": total,
            "taken": {"wins": taken_wins, "total": taken_total, 
                      "wr": taken_wins/taken_total if taken_total > 0 else 0},
            "skipped": {"wins": skipped_wins, "total": skipped_total,
                        "wr": skipped_wins/skipped_total if skipped_total > 0 else 0},
            "trades_until_retrain": max(0, RETRAIN_INTERVAL - self.trades_since_retrain),
        }
    
    def _features_to_array(self, features):
        """Convert feature dict to numpy array in correct column order."""
        try:
            return np.array([features.get(col, 0.0) for col in FEATURE_COLS], dtype=np.float64)
        except (ValueError, TypeError):
            return None
    
    def _record_decision(self, trade_id, strategy_key, features, prediction, decision):
        """Record a prediction for later learning."""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                """INSERT INTO ml_features (trade_id, strategy, features, prediction, decision, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (trade_id, strategy_key, json.dumps(features), prediction, decision,
                 datetime.now(timezone.utc).isoformat())
            )
            conn.commit()
            conn.close()
        except Exception:
            pass
    
    def _get_training_count(self):
        """Get number of resolved training samples."""
        try:
            conn = sqlite3.connect(self.db_path)
            count = conn.execute("SELECT COUNT(*) FROM ml_features WHERE outcome IS NOT NULL").fetchone()[0]
            conn.close()
            return count
        except Exception:
            return 0
