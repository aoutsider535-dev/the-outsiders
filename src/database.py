"""
Database layer — SQLite for trade history, market data, and performance metrics.
"""
import sqlite3
import json
import os
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "polymarket.db")


def get_connection():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_connection()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS candles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exchange TEXT NOT NULL,
            symbol TEXT NOT NULL,
            timestamp INTEGER NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume REAL NOT NULL,
            interval TEXT NOT NULL DEFAULT '1m',
            UNIQUE(exchange, symbol, timestamp, interval)
        );

        CREATE TABLE IF NOT EXISTS market_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER NOT NULL,
            market_id TEXT NOT NULL,
            market_slug TEXT,
            window_start INTEGER,
            window_end INTEGER,
            up_price REAL,
            down_price REAL,
            up_volume REAL,
            down_volume REAL,
            bid_depth_up REAL,
            ask_depth_up REAL,
            bid_depth_down REAL,
            ask_depth_down REAL,
            metadata TEXT,
            UNIQUE(market_id, timestamp)
        );

        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER NOT NULL,
            market_id TEXT NOT NULL,
            strategy TEXT NOT NULL,
            side TEXT NOT NULL,
            direction TEXT NOT NULL,  -- 'up' or 'down'
            entry_price REAL NOT NULL,
            exit_price REAL,
            quantity REAL NOT NULL,
            pnl REAL,
            pnl_pct REAL,
            edge_pct REAL,
            confidence REAL,
            signal_data TEXT,
            status TEXT NOT NULL DEFAULT 'open',  -- open, closed, cancelled
            exit_reason TEXT,
            created_at TEXT NOT NULL,
            closed_at TEXT,
            is_simulated INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS strategy_params (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy TEXT NOT NULL,
            params TEXT NOT NULL,
            performance_score REAL,
            backtest_trades INTEGER,
            backtest_pnl REAL,
            backtest_win_rate REAL,
            created_at TEXT NOT NULL,
            notes TEXT
        );

        CREATE TABLE IF NOT EXISTS performance_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER NOT NULL,
            strategy TEXT NOT NULL,
            metric_name TEXT NOT NULL,
            metric_value REAL NOT NULL,
            window TEXT NOT NULL DEFAULT 'daily',
            metadata TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_candles_ts ON candles(exchange, symbol, timestamp);
        CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON market_snapshots(market_id, timestamp);
        CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(timestamp, strategy);
        CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
    """)
    conn.commit()
    conn.close()


def insert_candles(candles, exchange="kraken", symbol="BTC/USD", interval="1m"):
    conn = get_connection()
    conn.executemany(
        """INSERT OR IGNORE INTO candles (exchange, symbol, timestamp, open, high, low, close, volume, interval)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [(exchange, symbol, c["timestamp"], c["open"], c["high"], c["low"], c["close"], c["volume"], interval)
         for c in candles]
    )
    conn.commit()
    conn.close()


def get_candles(exchange="kraken", symbol="BTC/USD", interval="1m", since=None, limit=100):
    conn = get_connection()
    query = "SELECT * FROM candles WHERE exchange=? AND symbol=? AND interval=?"
    params = [exchange, symbol, interval]
    if since:
        query += " AND timestamp >= ?"
        params.append(since)
    query += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def insert_trade(trade):
    conn = get_connection()
    conn.execute(
        """INSERT INTO trades (timestamp, market_id, strategy, side, direction, entry_price, 
           quantity, edge_pct, confidence, signal_data, status, created_at, is_simulated)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)""",
        (trade["timestamp"], trade["market_id"], trade["strategy"], trade["side"],
         trade["direction"], trade["entry_price"], trade["quantity"],
         trade.get("edge_pct"), trade.get("confidence"),
         json.dumps(trade.get("signal_data", {})),
         datetime.now(timezone.utc).isoformat(), trade.get("is_simulated", 1))
    )
    trade_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()
    return trade_id


def close_trade(trade_id, exit_price, pnl, pnl_pct, exit_reason="signal"):
    conn = get_connection()
    conn.execute(
        """UPDATE trades SET exit_price=?, pnl=?, pnl_pct=?, exit_reason=?, status='closed',
           closed_at=? WHERE id=?""",
        (exit_price, pnl, pnl_pct, exit_reason, datetime.now(timezone.utc).isoformat(), trade_id)
    )
    conn.commit()
    conn.close()


def get_trades(strategy=None, status=None, limit=100, simulated=None):
    conn = get_connection()
    query = "SELECT * FROM trades WHERE 1=1"
    params = []
    if strategy:
        query += " AND strategy=?"
        params.append(strategy)
    if status:
        query += " AND status=?"
        params.append(status)
    if simulated is not None:
        query += " AND is_simulated=?"
        params.append(int(simulated))
    query += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_performance_summary(strategy=None):
    conn = get_connection()
    query = """
        SELECT strategy,
               COUNT(*) as total_trades,
               SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) as losses,
               SUM(pnl) as total_pnl,
               AVG(pnl_pct) as avg_pnl_pct,
               MAX(pnl_pct) as best_trade,
               MIN(pnl_pct) as worst_trade,
               AVG(edge_pct) as avg_edge
        FROM trades WHERE status='closed'
    """
    params = []
    if strategy:
        query += " AND strategy=?"
        params.append(strategy)
    query += " GROUP BY strategy"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


if __name__ == "__main__":
    init_db()
    print(f"Database initialized at {DB_PATH}")
