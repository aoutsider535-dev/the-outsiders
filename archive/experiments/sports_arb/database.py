"""
SQLite database for logging arbitrage scanner activity.

Logs every scan (not just edges) with rejection reasons.
Three tables: snapshots, opportunities, outcomes.
"""

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator

from .config import DB_PATH
from .edge_calculator import EdgeOpportunity

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    sport TEXT NOT NULL,
    pm_event_id TEXT,
    odds_api_event_id TEXT,
    match_confidence INTEGER,
    market_type TEXT,
    rejection_reason TEXT,
    metadata_json TEXT
);

CREATE TABLE IF NOT EXISTS opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    sport TEXT NOT NULL,
    game TEXT NOT NULL,
    market_type TEXT NOT NULL,
    line REAL,
    pm_side TEXT NOT NULL,
    pm_price REAL NOT NULL,
    sharp_no_vig_prob REAL NOT NULL,
    edge_pct REAL NOT NULL,
    edge_after_costs REAL NOT NULL,
    pm_liquidity REAL,
    pm_volume REAL,
    books_used TEXT,
    match_confidence INTEGER,
    status TEXT DEFAULT 'detected',
    raw_odds_json TEXT
);

CREATE TABLE IF NOT EXISTS outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_id INTEGER NOT NULL,
    timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    result TEXT,
    pm_resolution_price REAL,
    pnl REAL,
    notes TEXT,
    FOREIGN KEY (opportunity_id) REFERENCES opportunities(id)
);

CREATE INDEX IF NOT EXISTS idx_opportunities_sport ON opportunities(sport);
CREATE INDEX IF NOT EXISTS idx_opportunities_timestamp ON opportunities(timestamp);
CREATE INDEX IF NOT EXISTS idx_opportunities_edge ON opportunities(edge_pct);
CREATE INDEX IF NOT EXISTS idx_snapshots_timestamp ON snapshots(timestamp);
"""


class ArbDatabase:
    """SQLite database for the sports arbitrage scanner."""

    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        """Create tables if they don't exist."""
        with self._conn() as conn:
            conn.executescript(_SCHEMA)
        logger.info("Database initialized at %s", self.db_path)

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager for database connections."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def log_snapshot(
        self,
        sport: str,
        pm_event_id: str | None = None,
        odds_api_event_id: str | None = None,
        match_confidence: int | None = None,
        market_type: str | None = None,
        rejection_reason: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        """
        Log a scan snapshot — records every comparison, not just edges.

        Returns the snapshot row ID.
        """
        with self._conn() as conn:
            cursor = conn.execute(
                """
                INSERT INTO snapshots
                    (sport, pm_event_id, odds_api_event_id, match_confidence,
                     market_type, rejection_reason, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sport,
                    pm_event_id,
                    odds_api_event_id,
                    match_confidence,
                    market_type,
                    rejection_reason,
                    json.dumps(metadata) if metadata else None,
                ),
            )
            return cursor.lastrowid or 0

    def log_opportunity(self, edge: EdgeOpportunity) -> int:
        """
        Log a detected edge opportunity.

        Returns the opportunity row ID.
        """
        with self._conn() as conn:
            cursor = conn.execute(
                """
                INSERT INTO opportunities
                    (sport, game, market_type, line, pm_side, pm_price,
                     sharp_no_vig_prob, edge_pct, edge_after_costs,
                     pm_liquidity, pm_volume, books_used, match_confidence,
                     status, raw_odds_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    edge.sport,
                    edge.game,
                    edge.market_type,
                    edge.line,
                    edge.pm_side,
                    edge.pm_price,
                    edge.sharp_no_vig_prob,
                    edge.edge_pct,
                    edge.edge_after_costs,
                    edge.pm_liquidity,
                    edge.pm_volume,
                    json.dumps(edge.books_used),
                    edge.match_confidence,
                    "detected",
                    json.dumps(edge.raw_odds),
                ),
            )
            row_id = cursor.lastrowid or 0
            logger.info("Logged opportunity #%d: %s %.1f%%", row_id, edge.game, edge.edge_pct * 100)
            return row_id

    def log_outcome(
        self,
        opportunity_id: int,
        result: str | None = None,
        pm_resolution_price: float | None = None,
        pnl: float | None = None,
        notes: str | None = None,
    ) -> int:
        """Log the outcome of an opportunity (for Phase 2+)."""
        with self._conn() as conn:
            cursor = conn.execute(
                """
                INSERT INTO outcomes (opportunity_id, result, pm_resolution_price, pnl, notes)
                VALUES (?, ?, ?, ?, ?)
                """,
                (opportunity_id, result, pm_resolution_price, pnl, notes),
            )
            return cursor.lastrowid or 0

    def get_recent_opportunities(
        self,
        limit: int = 50,
        min_edge: float | None = None,
        sport: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch recent opportunities with optional filters."""
        query = "SELECT * FROM opportunities WHERE 1=1"
        params: list[Any] = []

        if min_edge is not None:
            query += " AND edge_pct >= ?"
            params.append(min_edge)
        if sport:
            query += " AND sport = ?"
            params.append(sport)

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()
            return [dict(row) for row in rows]

    def get_all_snapshots(self, limit: int = 100) -> list[dict[str, Any]]:
        """Fetch recent snapshots."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM snapshots ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_stats(self) -> dict[str, Any]:
        """Get summary statistics for the dashboard."""
        with self._conn() as conn:
            total_opps = conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
            total_scans = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]

            avg_edge = conn.execute(
                "SELECT AVG(edge_pct) FROM opportunities WHERE edge_pct > 0"
            ).fetchone()[0]

            by_sport = conn.execute(
                """
                SELECT sport, COUNT(*) as count, AVG(edge_pct) as avg_edge
                FROM opportunities WHERE edge_pct > 0
                GROUP BY sport ORDER BY count DESC
                """
            ).fetchall()

            by_hour = conn.execute(
                """
                SELECT CAST(strftime('%H', timestamp) AS INTEGER) as hour,
                       COUNT(*) as count
                FROM opportunities WHERE edge_pct > 0
                GROUP BY hour ORDER BY hour
                """
            ).fetchall()

            return {
                "total_opportunities": total_opps,
                "total_scans": total_scans,
                "avg_edge_pct": avg_edge or 0,
                "by_sport": [dict(row) for row in by_sport],
                "by_hour": [dict(row) for row in by_hour],
            }
