"""
ML Meta-Learner: "The Brain" v2

Optimizes for EXPECTED P&L, not just win rate.
- Predicts P(win) per signal
- Calculates E[PnL] = P(win) * profit - (1-P(win)) * cost - fees
- Only takes trades with positive expected value
- Ranks competing signals by E[PnL] when multiple strategies fire
- Learns which strategies work in which market regimes

Model: Gradient Boosted Trees with online retraining
"""
import json
import os
import sqlite3
import time
import pickle
import numpy as np
from datetime import datetime, timezone, timedelta

PST = timezone(timedelta(hours=-8))

# ─── CONFIG ───
MIN_TRAINING_SAMPLES = 30     # Explore first, then filter
RETRAIN_INTERVAL = 15         # Retrain every N resolved trades
TRAINING_WINDOW = 500         # Rolling window for training

# Fee model for EV calculation
TAKER_FEE = 0.02              # 2% Polymarket taker fee
SLIPPAGE = 0.005              # 0.5% average slippage

# Minimum expected value to take a trade (in dollars, per $1 risked)
# E[PnL] > 0 means positive EV, but we add a small buffer for model uncertainty
MIN_EV_PER_DOLLAR = 0.02      # Need at least $0.02 EV per dollar risked

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
    """Online ML meta-learner that maximizes expected P&L."""
    
    def __init__(self, db_path, model_dir="data/ml"):
        self.db_path = db_path
        self.model_dir = model_dir
        self.model = None
        self.model_version = 0
        self.trades_since_retrain = 0
        self.total_predictions = 0
        self.total_trades_taken = 0
        self.total_trades_skipped = 0
        self.is_exploring = True
        
        # Pending signals for ranking within a window
        self._pending_signals = []
        
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
                features TEXT,
                prediction REAL,
                expected_value REAL,
                decision TEXT,
                outcome INTEGER,
                pnl REAL,
                created_at TEXT,
                resolved_at TEXT
            )
        """)
        # Add expected_value column if missing (upgrade from v1)
        try:
            conn.execute("ALTER TABLE ml_features ADD COLUMN expected_value REAL")
        except sqlite3.OperationalError:
            pass  # Column already exists
        
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
                ev_taken REAL,
                ev_skipped REAL,
                feature_importance TEXT,
                created_at TEXT
            )
        """)
        # Add EV columns if missing (upgrade from v1)
        for col in ["ev_taken", "ev_skipped"]:
            try:
                conn.execute(f"ALTER TABLE ml_model_log ADD COLUMN {col} REAL")
            except sqlite3.OperationalError:
                pass
        
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
    
    @staticmethod
    def calc_expected_value(p_win, entry_price, trade_size=5.0):
        """
        Calculate expected P&L for a binary option trade.
        
        Win:  tokens resolve at $1.00 → profit = qty * (1 - entry) - fees
        Loss: tokens resolve at $0.00 → loss = cost + fees
        
        Returns EV per dollar risked.
        """
        fees = trade_size * TAKER_FEE + trade_size * SLIPPAGE
        
        # Quantity of tokens bought
        qty = trade_size / entry_price if entry_price > 0 else 0
        
        # Win: tokens worth $1 each
        profit_if_win = qty * (1.0 - entry_price) - fees
        # Loss: tokens worth $0
        loss_if_loss = trade_size + fees
        
        ev = p_win * profit_if_win - (1 - p_win) * loss_if_loss
        ev_per_dollar = ev / trade_size if trade_size > 0 else 0
        
        return ev, ev_per_dollar
    
    def should_trade(self, features, strategy_key, entry_price=0.5, trade_size=5.0, trade_id=None):
        """
        Decide whether to take a trade signal based on expected value.
        
        Returns:
            (should_trade: bool, prediction: float, reason: str)
        """
        if features is None:
            return True, 0.5, "no features — defaulting to trade"
        
        self.total_predictions += 1
        
        # Exploration phase
        sample_count = self._get_training_count()
        if sample_count < MIN_TRAINING_SAMPLES or self.model is None:
            self.is_exploring = True
            self._record_decision(trade_id, strategy_key, features, 0.5, 0.0, "take")
            self.total_trades_taken += 1
            remaining = MIN_TRAINING_SAMPLES - sample_count
            return True, 0.5, f"exploring ({remaining} trades until ML active)"
        
        self.is_exploring = False
        
        # Extract feature vector
        X = self._features_to_array(features)
        if X is None:
            self._record_decision(trade_id, strategy_key, features, 0.5, 0.0, "take")
            self.total_trades_taken += 1
            return True, 0.5, "incomplete features — defaulting to trade"
        
        # Predict P(win)
        try:
            prob = self.model.predict_proba(X.reshape(1, -1))[0][1]
        except Exception as e:
            self._record_decision(trade_id, strategy_key, features, 0.5, 0.0, "take")
            self.total_trades_taken += 1
            return True, 0.5, f"prediction error: {e}"
        
        # Calculate expected value
        ev, ev_per_dollar = self.calc_expected_value(prob, entry_price, trade_size)
        
        # Decision based on EV, not just win probability
        if ev_per_dollar >= MIN_EV_PER_DOLLAR:
            decision = "take"
            self.total_trades_taken += 1
            reason = (f"🧠 ML: P(win)={prob:.1%} | E[PnL]=${ev:+.2f} "
                     f"(${ev_per_dollar:+.3f}/$ risked) — TRADE ✅")
        else:
            decision = "skip"
            self.total_trades_skipped += 1
            reason = (f"🧠 ML: P(win)={prob:.1%} | E[PnL]=${ev:+.2f} "
                     f"(${ev_per_dollar:+.3f}/$ risked) — SKIP ❌")
        
        self._record_decision(trade_id, strategy_key, features, prob, ev, decision)
        
        return decision == "take", prob, reason
    
    def rank_signals(self, candidates):
        """
        Rank multiple strategy signals by expected value.
        Used when multiple strategies fire in the same window.
        
        Args:
            candidates: list of dicts with keys:
                {strategy_key, features, entry_price, trade_size, signal}
        
        Returns:
            Sorted list of (strategy_key, p_win, ev, ev_per_dollar) best first.
            Only includes positive EV signals.
        """
        if self.model is None or self.is_exploring:
            # During exploration, return all candidates as-is
            return [(c["strategy_key"], 0.5, 0.0, 0.0) for c in candidates]
        
        ranked = []
        for c in candidates:
            X = self._features_to_array(c["features"])
            if X is None:
                continue
            try:
                prob = self.model.predict_proba(X.reshape(1, -1))[0][1]
                ev, ev_per_dollar = self.calc_expected_value(
                    prob, c.get("entry_price", 0.5), c.get("trade_size", 5.0)
                )
                ranked.append((c["strategy_key"], prob, ev, ev_per_dollar))
            except Exception:
                continue
        
        # Sort by EV per dollar (best first), filter positive EV only
        ranked.sort(key=lambda x: x[3], reverse=True)
        return ranked
    
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
        
        if self.trades_since_retrain >= RETRAIN_INTERVAL:
            self.retrain()
    
    def retrain(self):
        """Retrain the model on recent trade data."""
        try:
            from sklearn.ensemble import GradientBoostingClassifier
            from sklearn.model_selection import cross_val_score
        except ImportError:
            try:
                import subprocess, sys
                subprocess.check_call([sys.executable, "-m", "pip", "install", "scikit-learn", "-q"])
                from sklearn.ensemble import GradientBoostingClassifier
                from sklearn.model_selection import cross_val_score
            except Exception:
                return False
        
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            """SELECT features, outcome, pnl FROM ml_features 
               WHERE outcome IS NOT NULL 
               ORDER BY id DESC LIMIT ?""",
            (TRAINING_WINDOW,)
        ).fetchall()
        conn.close()
        
        if len(rows) < MIN_TRAINING_SAMPLES:
            return False
        
        # Build training data
        X_list, y_list, pnl_list = [], [], []
        for feat_json, outcome, pnl in rows:
            features = json.loads(feat_json)
            X = self._features_to_array(features)
            if X is not None:
                X_list.append(X)
                y_list.append(outcome)
                pnl_list.append(pnl or 0)
        
        if len(X_list) < MIN_TRAINING_SAMPLES:
            return False
        
        X_train = np.array(X_list)
        y_train = np.array(y_list)
        pnl_arr = np.array(pnl_list)
        
        # Train classifier (P(win) prediction)
        model = GradientBoostingClassifier(
            n_estimators=50,
            max_depth=3,
            learning_rate=0.1,
            min_samples_leaf=5,
            subsample=0.8,
            random_state=42,
        )
        model.fit(X_train, y_train)
        
        # Cross-validation
        cv_folds = min(5, max(2, len(X_list) // 10))
        if cv_folds >= 2:
            cv_scores = cross_val_score(model, X_train, y_train, cv=cv_folds, scoring="accuracy")
            accuracy = cv_scores.mean()
        else:
            accuracy = model.score(X_train, y_train)
        
        # Feature importance
        importances = dict(zip(FEATURE_COLS, model.feature_importances_))
        top_features = dict(sorted(importances.items(), key=lambda x: x[1], reverse=True)[:10])
        
        # Evaluate with EV-based thresholding
        probs = model.predict_proba(X_train)[:, 1]
        
        # Calculate EV for each trade using its actual entry price (default $0.50)
        ev_per_trade = np.array([
            self.calc_expected_value(p, 0.50, 5.0)[1]  # ev_per_dollar
            for p in probs
        ])
        
        taken_mask = ev_per_trade >= MIN_EV_PER_DOLLAR
        skipped_mask = ~taken_mask
        
        wr_taken = y_train[taken_mask].mean() if taken_mask.sum() > 0 else 0
        wr_skipped = y_train[skipped_mask].mean() if skipped_mask.sum() > 0 else 0
        ev_taken = pnl_arr[taken_mask].mean() if taken_mask.sum() > 0 else 0
        ev_skipped = pnl_arr[skipped_mask].mean() if skipped_mask.sum() > 0 else 0
        
        # Deploy if model improves EV (not just accuracy)
        if ev_taken > ev_skipped or self.model is None:
            self.model = model
            self.model_version += 1
            self.trades_since_retrain = 0
            self._save_model()
            
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                """INSERT INTO ml_model_log 
                   (version, training_samples, accuracy, win_rate_taken, win_rate_skipped,
                    ev_taken, ev_skipped, feature_importance, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (self.model_version, len(X_list), accuracy, float(wr_taken), float(wr_skipped),
                 float(ev_taken), float(ev_skipped),
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
        total = conn.execute("SELECT COUNT(*) FROM ml_features WHERE outcome IS NOT NULL").fetchone()[0]
        taken_wins = conn.execute("SELECT COUNT(*) FROM ml_features WHERE decision='take' AND outcome=1").fetchone()[0]
        taken_total = conn.execute("SELECT COUNT(*) FROM ml_features WHERE decision='take' AND outcome IS NOT NULL").fetchone()[0]
        skipped_wins = conn.execute("SELECT COUNT(*) FROM ml_features WHERE decision='skip' AND outcome=1").fetchone()[0]
        skipped_total = conn.execute("SELECT COUNT(*) FROM ml_features WHERE decision='skip' AND outcome IS NOT NULL").fetchone()[0]
        
        # P&L stats
        taken_pnl = conn.execute("SELECT COALESCE(SUM(pnl), 0) FROM ml_features WHERE decision='take' AND outcome IS NOT NULL").fetchone()[0]
        skipped_pnl = conn.execute("SELECT COALESCE(SUM(pnl), 0) FROM ml_features WHERE decision='skip' AND outcome IS NOT NULL").fetchone()[0]
        
        conn.close()
        
        return {
            "model_version": self.model_version,
            "is_exploring": self.is_exploring,
            "total_samples": total,
            "taken": {"wins": taken_wins, "total": taken_total, 
                      "wr": taken_wins/taken_total if taken_total > 0 else 0,
                      "pnl": taken_pnl},
            "skipped": {"wins": skipped_wins, "total": skipped_total,
                        "wr": skipped_wins/skipped_total if skipped_total > 0 else 0,
                        "pnl": skipped_pnl},
            "trades_until_retrain": max(0, RETRAIN_INTERVAL - self.trades_since_retrain),
        }
    
    def _features_to_array(self, features):
        """Convert feature dict to numpy array in correct column order."""
        try:
            return np.array([features.get(col, 0.0) for col in FEATURE_COLS], dtype=np.float64)
        except (ValueError, TypeError):
            return None
    
    def _record_decision(self, trade_id, strategy_key, features, prediction, ev, decision):
        """Record a prediction for later learning."""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                """INSERT INTO ml_features (trade_id, strategy, features, prediction, expected_value, decision, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (trade_id, strategy_key, json.dumps(features), prediction, ev, decision,
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
