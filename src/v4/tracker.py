"""
Component 5 — Performance Tracking
=====================================
Comprehensive trade journal with thesis documentation,
edge measurement, and rolling statistics.

Every trade logs:
- Pre-trade probability estimate vs market price
- Thesis (why we thought we had an edge)
- Outcome and actual P&L
- Rolling Sharpe, win rate, edge captured, drawdown
"""

import sqlite3
import json
import math
from datetime import datetime
from typing import Optional, List, Dict
from .config import *
from .edge_detector import EdgeSignal


def init_db() -> sqlite3.Connection:
    """Initialize v4 database with comprehensive tracking."""
    conn = sqlite3.connect(DB_PATH)
    
    conn.execute("""CREATE TABLE IF NOT EXISTS positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        -- Market info
        condition_id TEXT,
        question TEXT,
        slug TEXT,
        category TEXT,
        
        -- Our analysis
        our_probability REAL,
        confidence REAL,
        market_price_at_entry REAL,
        edge_pct REAL,
        expected_value REAL,
        thesis TEXT,
        edge_sources TEXT,         -- JSON list of sources
        
        -- Execution
        side TEXT,                  -- YES or NO
        token_id TEXT,
        entry_price REAL,          -- Actual fill price
        target_exit_price REAL,    -- Where we want to exit
        quantity REAL,
        cost REAL,                 -- USD spent
        kelly_fraction REAL,
        
        -- Exit
        exit_price REAL,
        exit_reason TEXT,          -- resolution, manual, stale, stop_loss
        pnl REAL,
        fees_paid REAL,
        
        -- Status
        status TEXT DEFAULT 'pending',  -- pending, open, closed, cancelled
        
        -- Timestamps
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        filled_at TEXT,
        closed_at TEXT,
        
        -- Resolution
        resolved_outcome TEXT,     -- What actually happened
        resolution_price REAL      -- Final market price (0 or 1)
    )""")
    
    conn.execute("""CREATE TABLE IF NOT EXISTS daily_stats (
        date TEXT PRIMARY KEY,
        trades INTEGER DEFAULT 0,
        wins INTEGER DEFAULT 0,
        losses INTEGER DEFAULT 0,
        gross_pnl REAL DEFAULT 0,
        fees REAL DEFAULT 0,
        net_pnl REAL DEFAULT 0,
        avg_edge REAL DEFAULT 0,
        avg_confidence REAL DEFAULT 0,
        portfolio_value REAL DEFAULT 0,
        peak_value REAL DEFAULT 0,
        drawdown REAL DEFAULT 0
    )""")
    
    conn.execute("""CREATE TABLE IF NOT EXISTS market_scans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
        markets_scanned INTEGER,
        markets_passed INTEGER,
        edges_found INTEGER,
        trades_placed INTEGER,
        scan_data TEXT             -- JSON blob of scan results
    )""")
    
    conn.commit()
    return conn


class PerformanceTracker:
    """Track and analyze all trading performance."""
    
    def __init__(self):
        self.conn = init_db()
        self.peak_value = INITIAL_BANKROLL
        self.current_value = INITIAL_BANKROLL
    
    def record_signal(self, signal: EdgeSignal) -> int:
        """Record a detected edge signal (before execution)."""
        cursor = self.conn.execute("""INSERT INTO positions
            (condition_id, question, slug, category,
             our_probability, confidence, market_price_at_entry, edge_pct,
             expected_value, thesis, edge_sources, side, token_id,
             kelly_fraction, cost, quantity, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
            (signal.market.condition_id, signal.market.question,
             signal.market.slug, signal.market.category,
             signal.our_probability, signal.confidence,
             signal.market_price, signal.edge_pct,
             signal.expected_value, signal.thesis,
             json.dumps(signal.edge_sources), signal.side,
             signal.token_id, signal.kelly_fraction,
             signal.recommended_size, 0))
        self.conn.commit()
        return cursor.lastrowid
    
    def record_fill(self, position_id: int, fill_price: float, shares: float):
        """Record that an order was filled."""
        self.conn.execute("""UPDATE positions SET 
            entry_price=?, quantity=?, cost=?, status='open', filled_at=?
            WHERE id=?""",
            (fill_price, shares, fill_price * shares,
             datetime.now(PST).isoformat(), position_id))
        self.conn.commit()
    
    def record_close(self, position_id: int, exit_price: float, reason: str):
        """Record position close with P&L calculation."""
        row = self.conn.execute(
            "SELECT entry_price, quantity, cost FROM positions WHERE id=?",
            (position_id,)).fetchone()
        
        if not row:
            return
        
        entry_price, quantity, cost = row
        proceeds = exit_price * quantity
        
        # Fee calculation (2% on winnings only)
        fees = max(proceeds - cost, 0) * 0.02
        pnl = proceeds - cost - fees
        
        self.conn.execute("""UPDATE positions SET
            exit_price=?, exit_reason=?, pnl=?, fees_paid=?,
            status='closed', closed_at=?
            WHERE id=?""",
            (exit_price, reason, pnl, fees,
             datetime.now(PST).isoformat(), position_id))
        self.conn.commit()
        
        # Update running stats
        self.current_value += pnl
        self.peak_value = max(self.peak_value, self.current_value)
    
    def get_rolling_stats(self, last_n: int = 50) -> dict:
        """Calculate rolling performance statistics."""
        rows = self.conn.execute("""SELECT pnl, edge_pct, confidence, 
            our_probability, market_price_at_entry, cost
            FROM positions WHERE status='closed' 
            ORDER BY closed_at DESC LIMIT ?""", (last_n,)).fetchall()
        
        if not rows:
            return {
                "trades": 0, "win_rate": 0, "sharpe": 0,
                "avg_pnl": 0, "avg_edge": 0, "total_pnl": 0,
                "drawdown": 0, "max_drawdown": 0,
            }
        
        pnls = [r[0] for r in rows if r[0] is not None]
        edges = [r[1] for r in rows if r[1] is not None]
        
        wins = len([p for p in pnls if p > 0])
        total = len(pnls)
        avg_pnl = sum(pnls) / total if total else 0
        std_pnl = (sum((p - avg_pnl)**2 for p in pnls) / max(total - 1, 1)) ** 0.5
        
        # Sharpe ratio (annualized assuming daily trading)
        sharpe = (avg_pnl / std_pnl * math.sqrt(252)) if std_pnl > 0 else 0
        
        # Drawdown
        cumulative = 0
        peak = 0
        max_dd = 0
        for pnl in reversed(pnls):  # Oldest first
            cumulative += pnl
            peak = max(peak, cumulative)
            dd = (peak - cumulative) / max(peak, 1)
            max_dd = max(max_dd, dd)
        
        return {
            "trades": total,
            "win_rate": wins / total if total else 0,
            "sharpe": sharpe,
            "avg_pnl": avg_pnl,
            "total_pnl": sum(pnls),
            "avg_edge": sum(edges) / len(edges) if edges else 0,
            "avg_confidence": sum(r[2] for r in rows if r[2]) / total if total else 0,
            "drawdown": (self.peak_value - self.current_value) / max(self.peak_value, 1),
            "max_drawdown": max_dd,
            "portfolio_value": self.current_value,
        }
    
    def record_scan(self, scanned: int, passed: int, edges: int, trades: int, data: dict = None):
        """Record a market scan cycle."""
        self.conn.execute("""INSERT INTO market_scans
            (markets_scanned, markets_passed, edges_found, trades_placed, scan_data)
            VALUES (?, ?, ?, ?, ?)""",
            (scanned, passed, edges, trades, json.dumps(data) if data else None))
        self.conn.commit()
    
    def print_summary(self):
        """Print human-readable performance summary."""
        stats = self.get_rolling_stats()
        
        print(f"\n{'='*60}")
        print(f"📊 V4 PERFORMANCE SUMMARY")
        print(f"{'='*60}")
        print(f"  Trades: {stats['trades']}")
        print(f"  Win Rate: {stats['win_rate']*100:.1f}%")
        print(f"  Total PnL: ${stats['total_pnl']:+.2f}")
        print(f"  Avg PnL/trade: ${stats['avg_pnl']:+.2f}")
        print(f"  Sharpe Ratio: {stats['sharpe']:.2f}")
        print(f"  Avg Edge: {stats['avg_edge']*100:.1f}%")
        print(f"  Portfolio: ${stats['portfolio_value']:.2f}")
        print(f"  Drawdown: {stats['drawdown']*100:.1f}% (max: {stats['max_drawdown']*100:.1f}%)")
        print(f"{'='*60}")
