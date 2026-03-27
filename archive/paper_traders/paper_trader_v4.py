#!/usr/bin/env python3
"""
Paper Trader v4 — Realistic Simulation with Book Depth
=======================================================
- Uses real CLOB order book for entries/exits (no fake resolution)
- Simulates fill rate, slippage, and market impact via book depth walk
- Records book snapshots for offline backtesting
- Runs multiple TP/SL configs simultaneously
- No real orders placed, zero cost

Usage:
    python -m src.paper_trader_v4
"""

import time
import json
import os
import signal
import sqlite3
import sys
import requests
import numpy as np
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Tuple
from dotenv import dotenv_values

# Reuse TA + market helpers from v2
from src.live_trader_v2 import (
    get_binance_candles, analyze_ta, TASignals,
    BINANCE_SYMBOLS,
)

# ═══════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════

PST = timezone(timedelta(hours=-7))
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
DB_PATH = os.path.join(DATA_DIR, "paper_v4.db")
LOG_PATH = os.path.join(DATA_DIR, "paper_v4.log")
SNAPSHOT_DIR = os.path.join(DATA_DIR, "book_snapshots")

ASSETS = ["btc", "eth", "sol", "xrp"]
TRADE_SIZE = 5.0
MAX_CONCURRENT_PER_CONFIG = 8
CHECK_INTERVAL = 5
MIN_BULL_SIGNALS = 3
MIN_MOVE_PCT = 0.01
CONTRARIAN_THRESHOLD = 0.75
CONTRARIAN_MAX_ENTRY = 0.25
MIN_TA_DISAGREE = 2
BAD_HOURS = {1, 2, 3, 4, 5, 6}

# Slippage model (based on real trade data from Mar 12)
BUY_SLIPPAGE_PCT = 0.008    # We pay ~0.8% more than best ask
SELL_SLIPPAGE_PCT = 0.012   # We get ~1.2% less than best bid

# Test multiple configs simultaneously
CONFIGS = [
    {"name": "TP10_SL10", "tp_pct": 0.10, "sl_pct": 0.10},
    {"name": "TP15_SL10", "tp_pct": 0.15, "sl_pct": 0.10},
    {"name": "TP20_SL10", "tp_pct": 0.20, "sl_pct": 0.10},
    {"name": "TP25_SL10", "tp_pct": 0.25, "sl_pct": 0.10},
    {"name": "TP20_SL05", "tp_pct": 0.20, "sl_pct": 0.05},
    {"name": "TP20_SL15", "tp_pct": 0.20, "sl_pct": 0.15},
]

# Graduated exit timing
BREAKEVEN_SECS = 90   # T-90s: sell at breakeven+
FIRESALE_SECS = 60    # T-60s: sell at any price
EMERGENCY_SECS = 15   # T-15s: dump everything

# ═══════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS paper_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        config TEXT NOT NULL,
        asset TEXT, direction TEXT, strategy TEXT,
        entry_price REAL, entry_ask REAL, simulated_fill REAL,
        exit_price REAL, exit_bid REAL, simulated_exit REAL,
        quantity REAL, cost REAL, pnl REAL,
        entry_book_depth REAL, exit_book_depth REAL,
        entry_slippage REAL, exit_slippage REAL,
        exit_reason TEXT, status TEXT DEFAULT 'open',
        token_id TEXT, window_ts INTEGER,
        timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
        exit_timestamp TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS book_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        asset TEXT, direction TEXT, token_id TEXT,
        window_ts INTEGER, timestamp TEXT,
        bid_json TEXT, ask_json TEXT,
        best_bid REAL, best_ask REAL,
        bid_depth_5pct REAL, ask_depth_5pct REAL,
        spread REAL
    )""")
    conn.commit()
    return conn


# ═══════════════════════════════════════════════════════════
# BOOK DEPTH SIMULATION
# ═══════════════════════════════════════════════════════════

def get_full_book(token_id: str) -> Optional[dict]:
    """Fetch full order book for a token."""
    try:
        r = requests.get(
            f"https://clob.polymarket.com/book?token_id={token_id}", timeout=3)
        return r.json()
    except:
        return None


def simulate_buy_fill(book: dict, usd_amount: float) -> dict:
    """Walk the ask side to simulate a realistic buy fill.
    
    Returns:
        {
            "filled": bool,
            "fill_price": float,      # Volume-weighted avg price
            "shares": float,          # Total shares acquired
            "slippage": float,        # Difference from best ask
            "depth_consumed": float,  # USD of liquidity consumed
            "levels_consumed": int,   # Number of price levels hit
        }
    """
    asks = sorted(book.get("asks", []), key=lambda x: float(x["price"]))
    if not asks:
        return {"filled": False, "fill_price": 0, "shares": 0, "slippage": 0,
                "depth_consumed": 0, "levels_consumed": 0}
    
    best_ask = float(asks[0]["price"])
    remaining_usd = usd_amount
    total_shares = 0
    total_cost = 0
    levels = 0
    
    for ask in asks:
        price = float(ask["price"])
        size = float(ask["size"])
        level_usd = price * size
        
        if remaining_usd <= 0:
            break
        
        levels += 1
        if level_usd >= remaining_usd:
            # Partial fill at this level
            shares_here = remaining_usd / price
            total_shares += shares_here
            total_cost += remaining_usd
            remaining_usd = 0
        else:
            # Consume entire level
            total_shares += size
            total_cost += level_usd
            remaining_usd -= level_usd
    
    if total_shares == 0:
        return {"filled": False, "fill_price": 0, "shares": 0, "slippage": 0,
                "depth_consumed": 0, "levels_consumed": 0}
    
    # Didn't fill completely
    if remaining_usd > usd_amount * 0.5:
        return {"filled": False, "fill_price": 0, "shares": 0, "slippage": 0,
                "depth_consumed": total_cost, "levels_consumed": levels}
    
    avg_price = total_cost / total_shares
    
    # Add realistic slippage on top
    slippage = avg_price * BUY_SLIPPAGE_PCT
    simulated_price = avg_price + slippage
    actual_shares = usd_amount / simulated_price if remaining_usd < usd_amount * 0.5 else total_shares
    
    return {
        "filled": True,
        "fill_price": simulated_price,
        "shares": actual_shares,
        "slippage": simulated_price - best_ask,
        "depth_consumed": total_cost,
        "levels_consumed": levels,
        "best_ask": best_ask,
    }


def simulate_sell_fill(book: dict, shares: float) -> dict:
    """Walk the bid side to simulate a realistic sell fill."""
    bids = sorted(book.get("bids", []), key=lambda x: -float(x["price"]))
    if not bids:
        return {"filled": False, "fill_price": 0, "proceeds": 0, "slippage": 0,
                "depth_consumed": 0, "levels_consumed": 0}
    
    best_bid = float(bids[0]["price"])
    remaining_shares = shares
    total_proceeds = 0
    levels = 0
    
    for bid in bids:
        price = float(bid["price"])
        size = float(bid["size"])
        
        if remaining_shares <= 0:
            break
        
        levels += 1
        if size >= remaining_shares:
            total_proceeds += remaining_shares * price
            remaining_shares = 0
        else:
            total_proceeds += size * price
            remaining_shares -= size
    
    if remaining_shares > shares * 0.5:
        return {"filled": False, "fill_price": 0, "proceeds": 0, "slippage": 0,
                "depth_consumed": total_proceeds, "levels_consumed": levels}
    
    shares_sold = shares - remaining_shares
    avg_price = total_proceeds / shares_sold if shares_sold > 0 else 0
    
    # Apply sell slippage
    slippage = avg_price * SELL_SLIPPAGE_PCT
    simulated_price = avg_price - slippage
    simulated_proceeds = simulated_price * shares_sold
    
    return {
        "filled": True,
        "fill_price": simulated_price,
        "proceeds": simulated_proceeds,
        "shares_sold": shares_sold,
        "slippage": best_bid - simulated_price,
        "depth_consumed": total_proceeds,
        "levels_consumed": levels,
        "best_bid": best_bid,
    }


def calc_book_depth(book: dict, side: str, pct_range: float = 0.05) -> float:
    """Calculate total USD liquidity within pct_range of best price."""
    if side == "bid":
        levels = sorted(book.get("bids", []), key=lambda x: -float(x["price"]))
    else:
        levels = sorted(book.get("asks", []), key=lambda x: float(x["price"]))
    
    if not levels:
        return 0.0
    
    best = float(levels[0]["price"])
    total = 0
    for level in levels:
        price = float(level["price"])
        if side == "bid" and price < best * (1 - pct_range):
            break
        if side == "ask" and price > best * (1 + pct_range):
            break
        total += float(level["size"]) * price
    return total


def save_book_snapshot(conn: sqlite3.Connection, asset: str, direction: str,
                       token_id: str, window_ts: int, book: dict):
    """Save order book snapshot for later backtesting."""
    bids = book.get("bids", [])
    asks = book.get("asks", [])
    best_bid = max(float(b["price"]) for b in bids) if bids else 0
    best_ask = min(float(a["price"]) for a in asks) if asks else 1
    
    conn.execute("""INSERT INTO book_snapshots 
        (asset, direction, token_id, window_ts, timestamp, bid_json, ask_json,
         best_bid, best_ask, bid_depth_5pct, ask_depth_5pct, spread)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (asset, direction, token_id, window_ts,
         datetime.now(PST).isoformat(),
         json.dumps(bids[:20]),  # Top 20 levels
         json.dumps(asks[:20]),
         best_bid, best_ask,
         calc_book_depth(book, "bid"),
         calc_book_depth(book, "ask"),
         best_ask - best_bid))
    conn.commit()


# ═══════════════════════════════════════════════════════════
# PAPER TRADER
# ═══════════════════════════════════════════════════════════

class PaperTraderV4:
    def __init__(self):
        self.conn = init_db()
        os.makedirs(SNAPSHOT_DIR, exist_ok=True)
        self.running = True
        signal.signal(signal.SIGINT, self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)
        
        # Per-config state
        self.configs = {}
        for cfg in CONFIGS:
            self.configs[cfg["name"]] = {
                "tp_pct": cfg["tp_pct"],
                "sl_pct": cfg["sl_pct"],
                "open_trades": [],
                "wins": 0,
                "losses": 0,
                "total_pnl": 0.0,
                "daily_pnl": 0.0,
                "balance": 1000.0,  # Start with $1000 paper money
            }
        
        self.traded_windows = set()  # (asset, window_ts) - shared across configs
        self.daily_date = datetime.now(PST).date()
        self.log_file = open(LOG_PATH, "a")
        self.snapshot_count = 0
    
    def _shutdown(self, *args):
        self.running = False
    
    def log(self, msg: str):
        ts = datetime.now(PST).strftime("%I:%M:%S %p")
        line = f"[{ts}] {msg}"
        print(line)
        self.log_file.write(line + "\n")
        self.log_file.flush()
    
    def _get_market(self, asset: str, window_ts: int) -> Optional[dict]:
        """Fetch market data — same as v3."""
        slug = f"{asset}-updown-5m-{window_ts}"
        try:
            r = requests.get(f"https://gamma-api.polymarket.com/events?slug={slug}", timeout=5)
            data = r.json()
            if not data:
                return None
            
            event = data[0]
            markets = event.get("markets", [])
            if not markets:
                return None
            
            m = markets[0]
            tokens = json.loads(m["clobTokenIds"]) if isinstance(m["clobTokenIds"], str) else m["clobTokenIds"]
            outcomes = json.loads(m["outcomes"]) if isinstance(m["outcomes"], str) else m["outcomes"]
            prices = json.loads(m["outcomePrices"]) if isinstance(m["outcomePrices"], str) else m.get("outcomePrices", [])
            
            up_idx = 0 if outcomes[0].lower() == "up" else 1
            down_idx = 1 - up_idx
            
            return {
                "slug": slug,
                "condition_id": m.get("conditionId", ""),
                "up_token": tokens[up_idx],
                "down_token": tokens[down_idx],
                "up_price": float(prices[up_idx]) if prices else 0.5,
                "down_price": float(prices[down_idx]) if prices else 0.5,
                "tick_size": m.get("minimumTickSize", "0.01"),
                "neg_risk": m.get("negRisk", False),
                "closed": m.get("closed", False) or m.get("acceptingOrders") == False,
            }
        except Exception as e:
            return None
    
    def _eval_signals(self, asset: str, ta: TASignals, market: dict) -> Optional[dict]:
        """Evaluate entry signals — momentum + contrarian."""
        if abs(ta.price_move_pct) < MIN_MOVE_PCT:
            return None
        
        bull, bear = ta.bull_count, ta.bear_count
        
        # Momentum
        if bull >= MIN_BULL_SIGNALS and bull > bear:
            return {"strategy": "momentum", "direction": "up",
                    "token_id": market["up_token"], "entry_price": market["up_price"],
                    "reason": f"🚀 {asset.upper()} UP | {ta.summary()} | Move: {ta.price_move_pct:+.3f}%"}
        elif bear >= MIN_BULL_SIGNALS and bear > bull:
            return {"strategy": "momentum", "direction": "down",
                    "token_id": market["down_token"], "entry_price": market["down_price"],
                    "reason": f"🚀 {asset.upper()} DOWN | {ta.summary()} | Move: {ta.price_move_pct:+.3f}%"}
        
        # Contrarian
        if market["up_price"] > CONTRARIAN_THRESHOLD:
            if bear >= MIN_TA_DISAGREE and market["down_price"] <= CONTRARIAN_MAX_ENTRY:
                return {"strategy": "contrarian", "direction": "down",
                        "token_id": market["down_token"], "entry_price": market["down_price"],
                        "reason": f"🔄 {asset.upper()} DOWN (fade {market['up_price']:.0%}) | {ta.summary()}"}
        elif market["down_price"] > CONTRARIAN_THRESHOLD:
            if bull >= MIN_TA_DISAGREE and market["up_price"] <= CONTRARIAN_MAX_ENTRY:
                return {"strategy": "contrarian", "direction": "up",
                        "token_id": market["up_token"], "entry_price": market["up_price"],
                        "reason": f"🔄 {asset.upper()} UP (fade {market['down_price']:.0%}) | {ta.summary()}"}
        
        return None
    
    def _paper_entry(self, signal: dict, asset: str, window_ts: int):
        """Simulate entry across all configs using real book depth."""
        token_id = signal["token_id"]
        book = get_full_book(token_id)
        if not book:
            return
        
        # Save snapshot
        save_book_snapshot(self.conn, asset, signal["direction"], token_id, window_ts, book)
        self.snapshot_count += 1
        
        # Simulate fill
        fill = simulate_buy_fill(book, TRADE_SIZE)
        if not fill["filled"]:
            self.log(f"  📋 PAPER NO FILL: {asset.upper()} {signal['direction'].upper()} | "
                    f"Insufficient book depth (${fill['depth_consumed']:.2f} available)")
            return
        
        self.log(f"  📋 PAPER BUY: {asset.upper()} {signal['direction'].upper()} | "
                f"Ask ${fill.get('best_ask', 0):.3f} → Fill ${fill['fill_price']:.3f} | "
                f"{fill['shares']:.1f}sh | Slippage ${fill['slippage']:.3f} | "
                f"Depth: {fill['levels_consumed']} levels, ${fill['depth_consumed']:.2f}")
        
        # Add to ALL configs
        for cfg_name, cfg in self.configs.items():
            if len(cfg["open_trades"]) >= MAX_CONCURRENT_PER_CONFIG:
                continue
            
            trade = {
                "asset": asset,
                "direction": signal["direction"],
                "strategy": signal["strategy"],
                "token_id": token_id,
                "entry_price": fill["fill_price"],
                "entry_ask": fill.get("best_ask", 0),
                "entry_slippage": fill["slippage"],
                "entry_book_depth": fill["depth_consumed"],
                "quantity": fill["shares"],
                "cost": TRADE_SIZE,
                "window_ts": window_ts,
                "timestamp": datetime.now(PST).isoformat(),
            }
            cfg["open_trades"].append(trade)
            
            self.conn.execute("""INSERT INTO paper_trades 
                (config, asset, direction, strategy, entry_price, entry_ask, simulated_fill,
                 quantity, cost, entry_book_depth, entry_slippage, token_id, window_ts, timestamp, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')""",
                (cfg_name, asset, signal["direction"], signal["strategy"],
                 fill.get("best_ask", 0), fill.get("best_ask", 0), fill["fill_price"],
                 fill["shares"], TRADE_SIZE, fill["depth_consumed"],
                 fill["slippage"], token_id, window_ts, trade["timestamp"]))
        self.conn.commit()
    
    def _check_exits(self):
        """Check exits for all configs — same graduated logic, different thresholds."""
        now_ts = int(time.time())
        
        # Collect unique token_ids to minimize API calls
        token_books = {}
        for cfg_name, cfg in self.configs.items():
            for trade in cfg["open_trades"]:
                tid = trade["token_id"]
                if tid not in token_books:
                    wend = trade["window_ts"] + 300
                    secs = wend - now_ts
                    token_books[tid] = {"book": None, "secs_to_close": secs, "window_ts": trade["window_ts"]}
        
        # Fetch books (deduplicated)
        for tid, info in token_books.items():
            book = get_full_book(tid)
            if book:
                info["book"] = book
                # Save snapshot for backtesting
                # (find asset/direction from any config's trade)
                for cfg in self.configs.values():
                    for t in cfg["open_trades"]:
                        if t["token_id"] == tid:
                            save_book_snapshot(self.conn, t["asset"], t["direction"],
                                             tid, info["window_ts"], book)
                            self.snapshot_count += 1
                            break
                    break
        
        # Check exits per config
        for cfg_name, cfg in self.configs.items():
            tp_pct = cfg["tp_pct"]
            sl_pct = cfg["sl_pct"]
            resolved = []
            
            for i, trade in enumerate(cfg["open_trades"]):
                tid = trade["token_id"]
                info = token_books.get(tid, {})
                book = info.get("book")
                if not book:
                    continue
                
                bids = book.get("bids", [])
                if not bids:
                    continue
                
                best_bid = max(float(b["price"]) for b in bids)
                secs_to_close = info.get("secs_to_close", 999)
                entry = trade["entry_price"]
                gain_pct = (best_bid - entry) / entry if entry > 0 else 0
                
                reason = None
                
                # TP ALWAYS FIRST
                if gain_pct >= tp_pct:
                    reason = "scalp_tp"
                elif secs_to_close > BREAKEVEN_SECS:
                    if gain_pct <= -sl_pct:
                        reason = "scalp_sl"
                elif secs_to_close > FIRESALE_SECS:
                    if gain_pct >= 0:
                        reason = "breakeven_exit"
                    elif gain_pct <= -sl_pct:
                        reason = "urgent_sl"
                elif secs_to_close > EMERGENCY_SECS:
                    reason = "fire_sale"
                elif secs_to_close > 0:
                    reason = "emergency_exit"
                elif secs_to_close <= -30:
                    # Window closed 30+ seconds ago — resolve
                    reason = "expired"
                
                if not reason:
                    continue
                
                # Simulate sell
                sell_result = simulate_sell_fill(book, trade["quantity"])
                if sell_result["filled"]:
                    exit_price = sell_result["fill_price"]
                    proceeds = sell_result["proceeds"]
                else:
                    # Can't fill — use best bid with max slippage
                    exit_price = best_bid * (1 - SELL_SLIPPAGE_PCT)
                    proceeds = exit_price * trade["quantity"]
                
                pnl = proceeds - trade["cost"]
                
                if pnl > 0:
                    cfg["wins"] += 1
                else:
                    cfg["losses"] += 1
                cfg["total_pnl"] += pnl
                cfg["daily_pnl"] += pnl
                cfg["balance"] += pnl
                
                # Only log for first config to avoid spam
                if cfg_name == CONFIGS[0]["name"]:
                    emoji = "🟢" if pnl > 0 else "🔴"
                    self.log(f"  {emoji} [{cfg_name}] {trade['asset'].upper()} {trade['direction'].upper()} | "
                            f"${entry:.3f} → ${exit_price:.3f} ({gain_pct*100:+.0f}%) = ${pnl:+.2f} [{reason}]")
                
                # Update DB
                self.conn.execute("""UPDATE paper_trades SET 
                    exit_price=?, exit_bid=?, simulated_exit=?, pnl=?, 
                    exit_reason=?, status='closed', exit_book_depth=?, exit_slippage=?,
                    exit_timestamp=?
                    WHERE config=? AND token_id=? AND window_ts=? AND status='open'""",
                    (best_bid, best_bid, exit_price, pnl, reason,
                     sell_result.get("depth_consumed", 0),
                     sell_result.get("slippage", 0),
                     datetime.now(PST).isoformat(),
                     cfg_name, tid, trade["window_ts"]))
                
                resolved.append(i)
            
            self.conn.commit()
            for idx in sorted(resolved, reverse=True):
                cfg["open_trades"].pop(idx)
    
    def _check_resolutions(self):
        """Check if any expired trades resolved."""
        now_ts = int(time.time())
        
        for cfg_name, cfg in self.configs.items():
            resolved = []
            for i, trade in enumerate(cfg["open_trades"]):
                window_end = trade["window_ts"] + 300
                if now_ts < window_end + 60:
                    continue
                
                # Window closed 60+ seconds ago — check resolution
                try:
                    slug = f"{trade['asset']}-updown-5m-{trade['window_ts']}"
                    r = requests.get(f"https://gamma-api.polymarket.com/events?slug={slug}", timeout=3)
                    d = r.json()
                    if not d:
                        continue
                    m = d[0]["markets"][0]
                    prices = json.loads(m["outcomePrices"]) if isinstance(m["outcomePrices"], str) else m.get("outcomePrices", [])
                    outcomes = json.loads(m["outcomes"]) if isinstance(m["outcomes"], str) else m["outcomes"]
                    tokens = json.loads(m["clobTokenIds"]) if isinstance(m["clobTokenIds"], str) else m["clobTokenIds"]
                    
                    # Find our token
                    for idx, tok in enumerate(tokens):
                        if tok == trade["token_id"]:
                            p = float(prices[idx]) if idx < len(prices) else 0
                            if p > 0.95:
                                # Winner
                                pnl = trade["quantity"] * 1.0 - trade["cost"]
                                reason = "expired_win"
                            elif p < 0.05:
                                # Loser
                                pnl = -trade["cost"]
                                reason = "expired_loss"
                            else:
                                continue  # Not resolved yet
                            
                            if pnl > 0: cfg["wins"] += 1
                            else: cfg["losses"] += 1
                            cfg["total_pnl"] += pnl
                            cfg["daily_pnl"] += pnl
                            cfg["balance"] += pnl
                            
                            if cfg_name == CONFIGS[0]["name"]:
                                emoji = "🟢" if pnl > 0 else "🔴"
                                self.log(f"  {emoji} [{cfg_name}] EXPIRED: {trade['asset'].upper()} {trade['direction'].upper()} = ${pnl:+.2f} [{reason}]")
                            
                            self.conn.execute("""UPDATE paper_trades SET
                                exit_price=?, pnl=?, exit_reason=?, status='closed', exit_timestamp=?
                                WHERE config=? AND token_id=? AND window_ts=? AND status='open'""",
                                (p, pnl, reason, datetime.now(PST).isoformat(),
                                 cfg_name, tok, trade["window_ts"]))
                            
                            resolved.append(i)
                            break
                except:
                    pass
            
            self.conn.commit()
            for idx in sorted(resolved, reverse=True):
                cfg["open_trades"].pop(idx)
    
    def run(self):
        print("📋 The Outsiders — Paper Trader v4 (Realistic Simulation)")
        print("=" * 60)
        print(f"💰 PAPER MODE | Trade size: ${TRADE_SIZE} | No real money")
        print(f"📊 Assets: {', '.join(a.upper() for a in ASSETS)}")
        print(f"🧪 Testing {len(CONFIGS)} configs simultaneously:")
        for cfg in CONFIGS:
            print(f"   {cfg['name']}: TP +{cfg['tp_pct']*100:.0f}% / SL -{cfg['sl_pct']*100:.0f}%")
        print(f"📸 Book snapshots saved to {SNAPSHOT_DIR}")
        print(f"🎯 Graduated exits: TP/SL → Breakeven (T-{BREAKEVEN_SECS}s) → Fire sale (T-{FIRESALE_SECS}s) → Emergency (T-{EMERGENCY_SECS}s)")
        print("=" * 60)
        
        last_exit_check = 0
        last_status = 0
        
        while self.running:
            try:
                now = time.time()
                now_ts = int(now)
                current_window = (now_ts // 300) * 300
                window_end = current_window + 300
                secs_left = window_end - now_ts
                
                # Reset daily stats at midnight
                today = datetime.now(PST).date()
                if self.daily_date != today:
                    self.log("📅 New day — resetting daily stats")
                    self.daily_date = today
                    for cfg in self.configs.values():
                        cfg["daily_pnl"] = 0.0
                
                # Exit checks every 5s
                if now - last_exit_check >= CHECK_INTERVAL:
                    any_open = any(cfg["open_trades"] for cfg in self.configs.values())
                    if any_open:
                        self._check_exits()
                        self._check_resolutions()
                    last_exit_check = now
                
                # Bad hours
                hour = datetime.now(PST).hour
                if hour in BAD_HOURS:
                    time.sleep(10)
                    continue
                
                # Entry signals: 2-3 min into window
                if 120 < secs_left < 180:
                    for asset in ASSETS:
                        key = (asset, current_window)
                        if key in self.traded_windows:
                            continue
                        
                        self.traded_windows.add(key)
                        
                        candles = get_binance_candles(BINANCE_SYMBOLS[asset])
                        if not candles or len(candles) < 27:
                            continue
                        
                        ta = analyze_ta(candles)
                        market = self._get_market(asset, current_window)
                        if not market or market.get("closed"):
                            continue
                        
                        signal = self._eval_signals(asset, ta, market)
                        if not signal:
                            continue
                        
                        self.log(f"  {signal['reason']}")
                        self._paper_entry(signal, asset, current_window)
                
                # Status every 5 min
                if now - last_status > 300:
                    self.log(f"\n{'='*60}")
                    self.log(f"📋 PAPER TRADER STATUS | Snapshots: {self.snapshot_count}")
                    for cfg_name, cfg in self.configs.items():
                        total = cfg["wins"] + cfg["losses"]
                        wr = cfg["wins"] / total * 100 if total else 0
                        self.log(f"  {cfg_name}: {cfg['wins']}W-{cfg['losses']}L ({wr:.0f}%) | "
                                f"PnL: ${cfg['total_pnl']:+.2f} | Today: ${cfg['daily_pnl']:+.2f} | "
                                f"Bal: ${cfg['balance']:.2f} | Open: {len(cfg['open_trades'])}")
                    self.log(f"{'='*60}\n")
                    last_status = now
                
                # Clean old keys
                cutoff = current_window - 3600
                self.traded_windows = {k for k in self.traded_windows if k[1] > cutoff}
                
                time.sleep(1)
                
            except Exception as e:
                self.log(f"❌ Error: {e}")
                import traceback
                traceback.print_exc()
                time.sleep(10)
        
        # Final summary
        self.log(f"\n{'='*60}")
        self.log(f"📋 PAPER TRADER FINAL SUMMARY")
        self.log(f"Book snapshots recorded: {self.snapshot_count}")
        for cfg_name, cfg in self.configs.items():
            total = cfg["wins"] + cfg["losses"]
            wr = cfg["wins"] / total * 100 if total else 0
            self.log(f"  {cfg_name}: {total} trades ({cfg['wins']}W-{cfg['losses']}L, {wr:.0f}%) | "
                    f"PnL: ${cfg['total_pnl']:+.2f} | Balance: ${cfg['balance']:.2f}")
        self.log(f"{'='*60}")


if __name__ == "__main__":
    trader = PaperTraderV4()
    trader.run()
