"""
Copy Trader Database — SQLite storage for all state.
"""
import sqlite3
import time
import json
from pathlib import Path


class CopyTraderDB:
    def __init__(self, db_path: str = "data/copy_trader.db"):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._create_tables()

    def _create_tables(self):
        c = self.conn.cursor()

        # Leader activity we've seen (raw feed)
        c.execute("""
            CREATE TABLE IF NOT EXISTS leader_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                leader_address TEXT NOT NULL,
                leader_name TEXT,
                timestamp INTEGER NOT NULL,
                condition_id TEXT NOT NULL,
                asset_id TEXT,
                side TEXT,          -- BUY / SELL
                size REAL,          -- shares
                usdc_size REAL,     -- dollar amount
                price REAL,
                tx_hash TEXT,
                market_question TEXT,
                market_end_date TEXT,
                market_category TEXT,
                seen_at INTEGER NOT NULL,
                UNIQUE(leader_address, tx_hash)
            )
        """)

        # Filter decisions (why we did / didn't copy)
        c.execute("""
            CREATE TABLE IF NOT EXISTS filter_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                leader_trade_id INTEGER,
                timestamp INTEGER NOT NULL,
                leader_address TEXT,
                condition_id TEXT,
                decision TEXT NOT NULL,   -- COPY / SKIP
                reason TEXT,              -- which filter blocked it (or "passed_all")
                filter_details TEXT,      -- JSON with filter values at decision time
                FOREIGN KEY (leader_trade_id) REFERENCES leader_trades(id)
            )
        """)

        # Our positions (paper or live)
        c.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                leader_trade_id INTEGER,
                leader_address TEXT,
                leader_name TEXT,
                condition_id TEXT NOT NULL,
                token_id TEXT,
                market_question TEXT,
                market_category TEXT,
                side TEXT,               -- YES / NO
                entry_price REAL,
                shares REAL,
                usdc_size REAL,
                status TEXT DEFAULT 'open',  -- open / closed / resolved_win / resolved_loss
                exit_price REAL,
                exit_reason TEXT,         -- leader_exit / stop_loss / take_profit / trailing / max_hold / resolution
                pnl REAL,
                opened_at INTEGER,
                closed_at INTEGER,
                is_paper INTEGER DEFAULT 1,
                tx_hash_entry TEXT,
                tx_hash_exit TEXT,
                FOREIGN KEY (leader_trade_id) REFERENCES leader_trades(id)
            )
        """)

        # Leader performance tracking
        c.execute("""
            CREATE TABLE IF NOT EXISTS leader_stats (
                leader_address TEXT PRIMARY KEY,
                leader_name TEXT,
                total_trades_seen INTEGER DEFAULT 0,
                trades_copied INTEGER DEFAULT 0,
                trades_skipped INTEGER DEFAULT 0,
                wins INTEGER DEFAULT 0,
                losses INTEGER DEFAULT 0,
                total_pnl REAL DEFAULT 0,
                consecutive_losses INTEGER DEFAULT 0,
                is_paused INTEGER DEFAULT 0,
                paused_until INTEGER,
                last_trade_at INTEGER,
                updated_at INTEGER
            )
        """)

        # Daily P&L tracking
        c.execute("""
            CREATE TABLE IF NOT EXISTS daily_pnl (
                date TEXT PRIMARY KEY,
                realized_pnl REAL DEFAULT 0,
                unrealized_pnl REAL DEFAULT 0,
                trades_copied INTEGER DEFAULT 0,
                trades_skipped INTEGER DEFAULT 0,
                positions_opened INTEGER DEFAULT 0,
                positions_closed INTEGER DEFAULT 0
            )
        """)

        self.conn.commit()

    # ─── Leader Trades ──────────────────────────────────

    def record_leader_trade(self, leader_address, leader_name, trade_data, market_info=None):
        """Record a leader's trade. Returns (id, is_new)."""
        c = self.conn.cursor()
        try:
            c.execute("""
                INSERT INTO leader_trades 
                (leader_address, leader_name, timestamp, condition_id, asset_id,
                 side, size, usdc_size, price, tx_hash, 
                 market_question, market_end_date, market_category, seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                leader_address,
                leader_name,
                trade_data.get('timestamp', 0),
                trade_data.get('conditionId', ''),
                trade_data.get('asset', ''),
                trade_data.get('side', ''),
                float(trade_data.get('size', 0) or 0),
                float(trade_data.get('usdcSize', 0) or 0),
                float(trade_data.get('price', 0) or 0),
                trade_data.get('transactionHash', ''),
                market_info.get('question', '') if market_info else '',
                market_info.get('endDate', '') if market_info else '',
                market_info.get('category', '') if market_info else '',
                int(time.time()),
            ))
            self.conn.commit()
            return c.lastrowid, True
        except sqlite3.IntegrityError:
            # Already seen this trade
            return None, False

    # ─── Filter Log ─────────────────────────────────────

    def log_filter_decision(self, leader_trade_id, leader_address, condition_id,
                            decision, reason, details=None):
        c = self.conn.cursor()
        c.execute("""
            INSERT INTO filter_log
            (leader_trade_id, timestamp, leader_address, condition_id, decision, reason, filter_details)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            leader_trade_id, int(time.time()), leader_address, condition_id,
            decision, reason, json.dumps(details) if details else None,
        ))
        self.conn.commit()

    # ─── Positions ──────────────────────────────────────

    def open_position(self, leader_trade_id, leader_address, leader_name,
                      condition_id, token_id, market_question, market_category,
                      side, entry_price, shares, usdc_size, is_paper=True,
                      tx_hash=None):
        c = self.conn.cursor()
        c.execute("""
            INSERT INTO positions
            (leader_trade_id, leader_address, leader_name, condition_id, token_id,
             market_question, market_category, side, entry_price, shares, usdc_size,
             status, opened_at, is_paper, tx_hash_entry)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)
        """, (
            leader_trade_id, leader_address, leader_name, condition_id, token_id,
            market_question, market_category, side, entry_price, shares, usdc_size,
            int(time.time()), 1 if is_paper else 0, tx_hash,
        ))
        self.conn.commit()
        return c.lastrowid

    def close_position(self, position_id, exit_price, exit_reason, pnl, tx_hash=None):
        c = self.conn.cursor()
        c.execute("""
            UPDATE positions SET status = ?, exit_price = ?, exit_reason = ?,
            pnl = ?, closed_at = ?, tx_hash_exit = ?
            WHERE id = ?
        """, (
            'resolved_win' if pnl > 0 else 'resolved_loss',
            exit_price, exit_reason, pnl, int(time.time()), tx_hash, position_id,
        ))
        self.conn.commit()

    def get_open_positions(self, leader_address=None, condition_id=None):
        c = self.conn.cursor()
        query = "SELECT * FROM positions WHERE status = 'open'"
        params = []
        if leader_address:
            query += " AND leader_address = ?"
            params.append(leader_address)
        if condition_id:
            query += " AND condition_id = ?"
            params.append(condition_id)
        c.execute(query, params)
        return [dict(r) for r in c.fetchall()]

    def get_total_exposure(self, leader_address=None, condition_id=None, category=None):
        c = self.conn.cursor()
        query = "SELECT COALESCE(SUM(usdc_size), 0) FROM positions WHERE status = 'open'"
        params = []
        if leader_address:
            query += " AND leader_address = ?"
            params.append(leader_address)
        if condition_id:
            query += " AND condition_id = ?"
            params.append(condition_id)
        if category:
            query += " AND market_category = ?"
            params.append(category)
        c.execute(query, params)
        return c.fetchone()[0]

    # ─── Leader Stats ───────────────────────────────────

    def update_leader_stats(self, leader_address, leader_name, won=None, pnl=0):
        c = self.conn.cursor()
        # Upsert
        c.execute("""
            INSERT INTO leader_stats (leader_address, leader_name, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(leader_address) DO UPDATE SET
                leader_name = excluded.leader_name,
                updated_at = excluded.updated_at
        """, (leader_address, leader_name, int(time.time())))

        if won is not None:
            if won:
                c.execute("""
                    UPDATE leader_stats SET 
                        wins = wins + 1, total_pnl = total_pnl + ?,
                        consecutive_losses = 0, updated_at = ?
                    WHERE leader_address = ?
                """, (pnl, int(time.time()), leader_address))
            else:
                c.execute("""
                    UPDATE leader_stats SET 
                        losses = losses + 1, total_pnl = total_pnl + ?,
                        consecutive_losses = consecutive_losses + 1, updated_at = ?
                    WHERE leader_address = ?
                """, (pnl, int(time.time()), leader_address))

        c.execute("""
            UPDATE leader_stats SET last_trade_at = ? WHERE leader_address = ?
        """, (int(time.time()), leader_address))

        self.conn.commit()

    def get_leader_stats(self, leader_address):
        c = self.conn.cursor()
        c.execute("SELECT * FROM leader_stats WHERE leader_address = ?", (leader_address,))
        row = c.fetchone()
        return dict(row) if row else None

    def pause_leader(self, leader_address, until_ts):
        c = self.conn.cursor()
        c.execute("""
            UPDATE leader_stats SET is_paused = 1, paused_until = ? WHERE leader_address = ?
        """, (until_ts, leader_address))
        self.conn.commit()

    def unpause_leader(self, leader_address):
        c = self.conn.cursor()
        c.execute("""
            UPDATE leader_stats SET is_paused = 0, paused_until = NULL WHERE leader_address = ?
        """, (leader_address,))
        self.conn.commit()

    # ─── Daily P&L ──────────────────────────────────────

    def get_daily_pnl(self, date_str):
        c = self.conn.cursor()
        c.execute("SELECT * FROM daily_pnl WHERE date = ?", (date_str,))
        row = c.fetchone()
        return dict(row) if row else {'realized_pnl': 0}

    def get_today_realized_pnl(self):
        import datetime
        today = datetime.date.today().isoformat()
        c = self.conn.cursor()
        # Only count LIVE trades toward daily loss limit (paper losses don't matter)
        c.execute("""
            SELECT COALESCE(SUM(pnl), 0) FROM positions 
            WHERE closed_at >= ? AND status IN ('resolved_win', 'resolved_loss')
            AND is_paper = 0
        """, (int(time.time()) - 86400,))
        return c.fetchone()[0]

    # ─── Queries ────────────────────────────────────────

    def has_position_in_market(self, condition_id):
        """Check if we already have an open position in this market."""
        return self.count_positions_in_market(condition_id) > 0

    def count_positions_in_market(self, condition_id):
        """Count how many open positions we have in this market."""
        c = self.conn.cursor()
        c.execute("""
            SELECT COUNT(*) FROM positions 
            WHERE condition_id = ? AND status = 'open'
        """, (condition_id,))
        return c.fetchone()[0]

    def leader_recent_sells(self, leader_address, condition_id, window_sec=300):
        """Check if leader recently sold in this market."""
        c = self.conn.cursor()
        cutoff = int(time.time()) - window_sec
        c.execute("""
            SELECT COUNT(*) FROM leader_trades
            WHERE leader_address = ? AND condition_id = ? 
            AND side = 'SELL' AND timestamp >= ?
        """, (leader_address, condition_id, cutoff))
        return c.fetchone()[0] > 0

    def count_open_positions(self, condition_id=None):
        c = self.conn.cursor()
        if condition_id:
            c.execute("SELECT COUNT(*) FROM positions WHERE status = 'open' AND condition_id = ?",
                      (condition_id,))
        else:
            c.execute("SELECT COUNT(*) FROM positions WHERE status = 'open'")
        return c.fetchone()[0]
