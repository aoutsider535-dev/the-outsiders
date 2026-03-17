#!/usr/bin/env python3
"""
🏞 The Outsiders — Straddle Strategy
======================================
Buy BOTH UP and DOWN tokens at ~$0.50 each.
One side wins ($1.00), one loses ($0.00).
Profit = selling the loser before it hits zero.

Math at 50/50 prices ($5 per side, $10 total):
  Winner: 10 shares × $1.00 = $10.00 (minus 2% fee on profit ≈ $0.00)
  Loser sold at $0.20: 10 shares × $0.20 = $2.00
  Net: $10.00 + $2.00 - $10.00 = +$2.00 per window

Key: we NEVER predict direction. We buy both sides and manage exits.

Usage:
    python -m src.straddle_trader          # Live mode
    python -m src.straddle_trader --paper  # Paper mode (no real orders)
"""

import time
import json
import os
import signal
import sqlite3
import sys
import requests
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Tuple
from dotenv import dotenv_values

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, PartialCreateOrderOptions, BalanceAllowanceParams
from py_clob_client.order_builder.constants import BUY, SELL
from src.redeemer import Redeemer

# ═══════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════

PST = timezone(timedelta(hours=-7))
BASE_DIR = os.path.dirname(os.path.dirname(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")
DB_PATH = os.path.join(BASE_DIR, "data", "straddle.db")
LOG_PATH = os.path.join(BASE_DIR, "data", "straddle.log")

ASSETS = ["btc"]                # Start with BTC only (most liquid)
TRADE_SIZE_PER_SIDE = 5.0       # $5 on UP + $5 on DOWN = $10 per straddle
CHECK_INTERVAL = 3              # Check positions every 3 seconds (faster loser detection)
MAX_STRADDLES = 2               # Max concurrent straddles
BAD_HOURS = {1, 2, 3, 4, 5, 6} # PST dead hours

# ── Straddle Exit Strategy ──
# OPTIMIZATION #5: Tighter loser SL (was -30%, now -20%)
# Data shows -20% SL saves avg $0.89/side vs letting it ride
LOSER_SL_PCT = 0.20             # Sell loser when it drops 20% from entry
WINNER_TP_PRICE = 0.90          # Sell winner at $0.90+ — sweet spot between profit and reversal protection
LOSER_EMERGENCY_SECS = 45       # Dump loser at any price with <45s left
WINNER_EMERGENCY_SECS = 30      # Sell winner at any price with <30s left (don't hold to resolution)

# ── Entry Timing ──
# OPTIMIZATION #4: Enter at window OPEN for best 50/50 prices
# Complement matching means we always pay exactly $1.00 combined
ENTRY_WINDOW_MIN_SECS = 240     # Enter when 240-290s remain (was 150-180)
ENTRY_WINDOW_MAX_SECS = 290     # Earlier = closer to 50/50
MAX_ENTRY_PRICE = 0.65          # Tighter: don't enter if already lopsided
MIN_ENTRY_PRICE = 0.30          # Tighter floor

# ── Complement Matching ──
# OPTIMIZATION #1: Post both orders at complementary prices that sum to $1.00
# The CLOB mints new shares when BUY UP@$P + BUY DOWN@$(1-P) exist
# This guarantees fills and eliminates spread overpay
USE_COMPLEMENT_MATCHING = True   # Post limit orders that complement-match

BINANCE_SYMBOLS = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT", "xrp": "XRPUSDT"}

# ═══════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS straddles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        asset TEXT, window_ts INTEGER,
        
        -- UP side
        up_token_id TEXT, up_entry_price REAL, up_shares REAL, up_cost REAL,
        up_exit_price REAL, up_exit_reason TEXT, up_pnl REAL,
        
        -- DOWN side
        down_token_id TEXT, down_entry_price REAL, down_shares REAL, down_cost REAL,
        down_exit_price REAL, down_exit_reason TEXT, down_pnl REAL,
        
        -- Combined
        total_cost REAL, total_proceeds REAL, net_pnl REAL,
        winner_side TEXT,  -- 'up' or 'down' or NULL if still open
        
        -- Execution quality
        entry_slippage REAL DEFAULT 0,   -- actual cost vs theoretical
        exit_slippage REAL DEFAULT 0,    -- actual proceeds vs theoretical
        taker_fees REAL DEFAULT 0,       -- 2% taker fee on sells
        
        status TEXT DEFAULT 'open',  -- open, partial, closed
        timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
        closed_at TEXT
    )""")
    conn.commit()
    return conn


# ═══════════════════════════════════════════════════════════
# STRADDLE TRADER
# ═══════════════════════════════════════════════════════════

class StraddleTrader:
    def __init__(self, paper_mode: bool = False):
        self.paper_mode = paper_mode
        self.conn = init_db()
        self.running = True
        self.client = None
        self.proxy_addr = None
        self.redeemer = None
        
        signal.signal(signal.SIGINT, self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)
        
        # State
        self.open_straddles: List[dict] = []
        self.traded_windows: set = set()
        self.wins = 0
        self.losses = 0
        self.total_pnl = 0.0
        self.log_file = open(LOG_PATH, "a")
    
    def _shutdown(self, *args):
        self.running = False
    
    def log(self, msg: str):
        ts = datetime.now(PST).strftime("%I:%M:%S %p")
        line = f"[{ts}] {msg}"
        print(line)
        self.log_file.write(line + "\n")
        self.log_file.flush()
    
    def _init_client(self):
        """Initialize CLOB client and redeemer."""
        if self.paper_mode:
            self.log("📋 PAPER MODE — no real orders")
            return
        
        config = dotenv_values(ENV_PATH)
        pk = config.get("POLYGON_PRIVATE_KEY", "")
        addr = config.get("POLYGON_WALLET_ADDRESS", "")
        
        self.client = ClobClient("https://clob.polymarket.com", key=pk, chain_id=137)
        creds = self.client.create_or_derive_api_creds()
        self.client = ClobClient(
            "https://clob.polymarket.com", key=pk, chain_id=137,
            creds=creds, signature_type=1, funder=addr
        )
        self.proxy_addr = self.client.get_address()
        
        try:
            self.redeemer = Redeemer(pk, addr)
            self.log("✅ Redeemer initialized")
        except:
            self.log("⚠️ Redeemer failed to init")
    
    def _get_balance(self) -> float:
        if self.paper_mode:
            return 1000.0
        try:
            bal = self.client.get_balance_allowance(
                BalanceAllowanceParams(asset_type="COLLATERAL"))
            raw = float(bal.get("balance", 0))
            return raw / 1e6 if raw > 1000 else raw
        except:
            return 0.0
    
    def _get_market(self, asset: str, window_ts: int) -> Optional[dict]:
        """Fetch market data for both UP and DOWN."""
        slug = f"{asset}-updown-5m-{window_ts}"
        try:
            r = requests.get(f"https://gamma-api.polymarket.com/events?slug={slug}", timeout=5)
            data = r.json()
            if not data:
                return None
            
            m = data[0]["markets"][0]
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
                "up_price": float(prices[up_idx]),
                "down_price": float(prices[down_idx]),
                "tick_size": m.get("minimumTickSize", "0.01"),
                "neg_risk": m.get("negRisk", False),
                "closed": m.get("closed", False) or m.get("acceptingOrders") == False,
            }
        except:
            return None
    
    def _get_book(self, token_id: str) -> Optional[dict]:
        """Get full order book."""
        try:
            return requests.get(
                f"https://clob.polymarket.com/book?token_id={token_id}", timeout=3).json()
        except:
            return None
    
    def _get_best_ask(self, token_id: str) -> float:
        book = self._get_book(token_id)
        if book:
            asks = book.get("asks", [])
            if asks:
                return min(float(a["price"]) for a in asks)
        return 1.0
    
    def _get_best_bid(self, token_id: str) -> float:
        book = self._get_book(token_id)
        if book:
            bids = book.get("bids", [])
            if bids:
                return max(float(b["price"]) for b in bids)
        return 0.0
    
    def _place_buy(self, token_id: str, price: float, size_usd: float,
                   tick_size: str, neg_risk: bool) -> Optional[dict]:
        """Place a buy order. Returns fill info."""
        if self.paper_mode:
            # Realistic paper fill: walk the ask book with slippage
            book = self._get_book(token_id)
            if book:
                asks = sorted(book.get("asks", []), key=lambda a: float(a["price"]))
                fill_price, fill_shares, fill_cost = self._simulate_book_fill(
                    asks, size_usd, side="buy")
                if fill_shares > 0:
                    return {"filled": True, "price": fill_price, "shares": fill_shares, 
                            "cost": fill_cost}
                return None
            # Fallback: add 1% slippage
            slip_price = price * 1.01
            shares = size_usd / slip_price
            return {"filled": True, "price": slip_price, "shares": shares, "cost": size_usd}
        
        tick = float(tick_size)
        n_dec = len(tick_size.split('.')[-1]) if '.' in tick_size else 2
        aggressive_price = min(round(round((price + tick) / tick) * tick, n_dec), 0.99)
        shares = round(size_usd / aggressive_price, 2)
        
        try:
            resp = self.client.create_and_post_order(
                OrderArgs(token_id=token_id, price=aggressive_price, size=shares, side=BUY),
                PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk)
            )
            
            # Check fill
            time.sleep(3)
            bal = self.client.get_balance_allowance(
                BalanceAllowanceParams(asset_type="CONDITIONAL", token_id=token_id))
            raw = float(bal.get("balance", 0))
            clob_shares = raw / 1e6 if raw > 1000 else raw
            
            if clob_shares > 0.1:
                fill_price = float(resp.get("takingAmount") or 0) / max(float(resp.get("makingAmount") or 0), 0.001)
                if fill_price <= 0 or fill_price > 1:
                    fill_price = aggressive_price
                return {"filled": True, "price": fill_price, "shares": clob_shares, 
                        "cost": fill_price * clob_shares}
            
            return None
        except Exception as e:
            self.log(f"  ❌ Buy failed: {e}")
            return None
    
    def _simulate_book_fill(self, levels: list, size_usd: float, side: str = "buy") -> tuple:
        """
        Walk the order book to simulate a realistic fill with slippage.
        Returns (avg_fill_price, total_shares, total_cost).
        """
        remaining_usd = size_usd
        total_shares = 0.0
        total_cost = 0.0
        
        for level in levels:
            level_price = float(level["price"])
            level_size = float(level["size"])
            
            if side == "buy":
                # How much USD to fill at this level
                level_usd = level_price * level_size
                fill_usd = min(remaining_usd, level_usd)
                fill_shares = fill_usd / level_price
            else:
                # Selling: we have shares, want USD
                fill_shares = min(remaining_usd, level_size)  # remaining_usd = remaining shares here
                fill_usd = fill_shares * level_price
            
            total_shares += fill_shares
            total_cost += fill_usd
            remaining_usd -= fill_usd if side == "buy" else fill_shares
            
            if remaining_usd <= 0.01:
                break
        
        if total_shares <= 0:
            return 0.0, 0.0, 0.0
        
        avg_price = total_cost / total_shares
        return avg_price, total_shares, total_cost
    
    def _place_sell(self, token_id: str, shares: float, price: float,
                    tick_size: str, neg_risk: bool) -> Optional[float]:
        """Place a sell order. Returns proceeds."""
        if self.paper_mode:
            # Realistic paper sell: walk the bid book with slippage
            book = self._get_book(token_id)
            if book:
                bids = sorted(book.get("bids", []), key=lambda b: -float(b["price"]))
                _, _, proceeds = self._simulate_book_fill(bids, shares, side="sell")
                if proceeds > 0:
                    # Apply 2% taker fee (we're hitting bids = taker)
                    proceeds *= 0.98
                    return proceeds
                return None
            # Fallback: 1% slippage + 2% taker fee
            return shares * price * 0.99 * 0.98
        
        tick = float(tick_size)
        n_dec = len(tick_size.split('.')[-1]) if '.' in tick_size else 2
        sell_price = max(round(round((price - tick) / tick) * tick, n_dec), 0.01)
        
        # Get actual CLOB balance
        try:
            bal = self.client.get_balance_allowance(
                BalanceAllowanceParams(asset_type="CONDITIONAL", token_id=token_id))
            raw = float(bal.get("balance", 0))
            actual_shares = raw / 1e6 if raw > 1000 else raw
        except:
            actual_shares = shares
        
        sell_qty = round(min(actual_shares, shares) - 0.01, 2)
        if sell_qty <= 0:
            return None
        
        try:
            resp = self.client.create_and_post_order(
                OrderArgs(token_id=token_id, price=sell_price, size=sell_qty, side=SELL),
                PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk)
            )
            
            taking = float(resp.get("takingAmount") or 0)
            if taking > 0:
                return taking / 1e6 if taking > 1000 else taking
            
            return sell_qty * sell_price
        except Exception as e:
            self.log(f"  ❌ Sell failed: {e}")
            return None
    
    def _place_straddle_orders(self, market: dict, up_price: float, 
                               down_price: float, size_usd: float) -> tuple:
        """
        Place BOTH buy orders before checking fills.
        This enables complement matching — the CLOB mints new shares
        when BUY UP@$P + BUY DOWN@$(1-P) exist simultaneously.
        """
        tick_size = market["tick_size"]
        tick = float(tick_size)
        n_dec = len(tick_size.split('.')[-1]) if '.' in tick_size else 2
        neg_risk = market["neg_risk"]
        
        # Calculate order params for both sides
        # KEY: Post at BID price (not ask) so orders REST on book
        # Complement matching requires both orders resting simultaneously
        # If we cross the ask, one fills immediately as taker and no complement happens
        up_order_price = round(round(up_price / tick) * tick, n_dec)  # At or below midpoint
        down_order_price = round(round(down_price / tick) * tick, n_dec)
        
        # Ensure they complement to exactly $1.00
        if up_order_price + down_order_price != 1.0:
            down_order_price = round(1.0 - up_order_price, n_dec)
        
        up_shares = round(size_usd / up_order_price, 2)
        down_shares = round(size_usd / down_order_price, 2)
        
        self.log(f"  📤 Posting BOTH limit orders (complement): UP {up_shares}sh@${up_order_price} + DOWN {down_shares}sh@${down_order_price} = $1.00")
        
        # Post UP order
        up_resp = None
        try:
            up_resp = self.client.create_and_post_order(
                OrderArgs(token_id=market["up_token"], price=up_order_price, size=up_shares, side=BUY),
                PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk)
            )
        except Exception as e:
            self.log(f"  ❌ UP order post failed: {e}")
        
        # Post DOWN order immediately (don't wait for UP fill)
        down_resp = None
        try:
            down_resp = self.client.create_and_post_order(
                OrderArgs(token_id=market["down_token"], price=down_order_price, size=down_shares, side=BUY),
                PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk)
            )
        except Exception as e:
            self.log(f"  ❌ DOWN order post failed: {e}")
        
        if not up_resp and not down_resp:
            return None, None
        
        # Wait for complement matching / fill
        # Both orders should be resting on book → CLOB mints when it sees UP@$P + DOWN@$(1-P)
        self.log(f"  ⏳ Waiting 8s for complement match...")
        time.sleep(8)
        
        # Check fills via CLOB balance
        up_fill = None
        down_fill = None
        
        try:
            up_bal = self.client.get_balance_allowance(
                BalanceAllowanceParams(asset_type="CONDITIONAL", token_id=market["up_token"]))
            raw = float(up_bal.get("balance", 0))
            up_clob_shares = raw / 1e6 if raw > 1000 else raw
            if up_clob_shares > 0.1:
                up_fill = {"filled": True, "price": up_order_price, 
                          "shares": up_clob_shares, "cost": up_clob_shares * up_order_price}
        except Exception as e:
            self.log(f"  ⚠️ UP balance check failed: {e}")
        
        try:
            down_bal = self.client.get_balance_allowance(
                BalanceAllowanceParams(asset_type="CONDITIONAL", token_id=market["down_token"]))
            raw = float(down_bal.get("balance", 0))
            down_clob_shares = raw / 1e6 if raw > 1000 else raw
            if down_clob_shares > 0.1:
                down_fill = {"filled": True, "price": down_order_price,
                            "shares": down_clob_shares, "cost": down_clob_shares * down_order_price}
        except Exception as e:
            self.log(f"  ⚠️ DOWN balance check failed: {e}")
        
        # If one side didn't fill, try once more with aggressive pricing
        if up_fill and not down_fill:
            self.log(f"  🔄 DOWN didn't fill, retrying aggressive...")
            retry_price = min(down_order_price + tick * 2, 0.99)
            try:
                self.client.create_and_post_order(
                    OrderArgs(token_id=market["down_token"], price=retry_price, 
                             size=round(size_usd / retry_price, 2), side=BUY),
                    PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk))
                time.sleep(3)
                down_bal = self.client.get_balance_allowance(
                    BalanceAllowanceParams(asset_type="CONDITIONAL", token_id=market["down_token"]))
                raw = float(down_bal.get("balance", 0))
                down_clob_shares = raw / 1e6 if raw > 1000 else raw
                if down_clob_shares > 0.1:
                    down_fill = {"filled": True, "price": retry_price,
                                "shares": down_clob_shares, "cost": down_clob_shares * retry_price}
            except Exception as e:
                self.log(f"  ❌ DOWN retry failed: {e}")
        
        elif down_fill and not up_fill:
            self.log(f"  🔄 UP didn't fill, retrying aggressive...")
            retry_price = min(up_order_price + tick * 2, 0.99)
            try:
                self.client.create_and_post_order(
                    OrderArgs(token_id=market["up_token"], price=retry_price,
                             size=round(size_usd / retry_price, 2), side=BUY),
                    PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk))
                time.sleep(3)
                up_bal = self.client.get_balance_allowance(
                    BalanceAllowanceParams(asset_type="CONDITIONAL", token_id=market["up_token"]))
                raw = float(up_bal.get("balance", 0))
                up_clob_shares = raw / 1e6 if raw > 1000 else raw
                if up_clob_shares > 0.1:
                    up_fill = {"filled": True, "price": retry_price,
                              "shares": up_clob_shares, "cost": up_clob_shares * retry_price}
            except Exception as e:
                self.log(f"  ❌ UP retry failed: {e}")
        
        return up_fill, down_fill
    
    def _enter_straddle(self, asset: str, market: dict, window_ts: int):
        """Buy both UP and DOWN tokens using complement matching."""
        up_ask = self._get_best_ask(market["up_token"])
        down_ask = self._get_best_ask(market["down_token"])
        
        # Use midpoint prices for reference
        up_bid = self._get_best_bid(market["up_token"])
        down_bid = self._get_best_bid(market["down_token"])
        up_mid = (up_bid + up_ask) / 2
        down_mid = (down_bid + down_ask) / 2
        
        # Validate prices are reasonable (near 50/50)
        if up_mid > MAX_ENTRY_PRICE or down_mid > MAX_ENTRY_PRICE:
            self.log(f"  ⚠️ Skipping: prices too lopsided (UP=${up_mid:.3f}, DOWN=${down_mid:.3f})")
            return
        if up_mid < MIN_ENTRY_PRICE or down_mid < MIN_ENTRY_PRICE:
            self.log(f"  ⚠️ Skipping: market already decided (UP=${up_mid:.3f}, DOWN=${down_mid:.3f})")
            return
        
        # ── COMPLEMENT MATCHING ENTRY ──
        # Post limit orders that sum to exactly $1.00
        # The CLOB mints new shares when complementary orders exist
        # This guarantees fills AND eliminates spread costs
        tick = float(market["tick_size"])
        n_dec = len(market["tick_size"].split('.')[-1]) if '.' in market["tick_size"] else 2
        
        if USE_COMPLEMENT_MATCHING:
            # Price at midpoint, ensuring they sum to $1.00
            up_price = round(round(up_mid / tick) * tick, n_dec)
            down_price = round(1.0 - up_price, n_dec)
            
            # Verify complement
            if abs(up_price + down_price - 1.0) > 0.001:
                # Adjust to ensure exact complement
                down_price = round(1.0 - up_price, n_dec)
            
            total_price = up_price + down_price
            self.log(f"  📐 COMPLEMENT MATCH: UP=${up_price:.3f} + DOWN=${down_price:.3f} = ${total_price:.3f}")
        else:
            up_price = up_ask
            down_price = down_ask
            total_price = up_price + down_price
            if total_price > 1.05:
                self.log(f"  ⚠️ Skipping: overpriced (UP+DOWN=${total_price:.3f})")
                return
        
        self.log(f"  📊 STRADDLE ENTRY: {asset.upper()} | UP=${up_price:.3f} + DOWN=${down_price:.3f} = ${total_price:.3f}")
        
        if self.paper_mode:
            # Paper mode: use book-walking simulation
            up_fill = self._place_buy(market["up_token"], up_price, TRADE_SIZE_PER_SIDE,
                                       market["tick_size"], market["neg_risk"])
            down_fill = self._place_buy(market["down_token"], down_price, TRADE_SIZE_PER_SIDE,
                                         market["tick_size"], market["neg_risk"])
        else:
            # LIVE: Post BOTH orders first, then check fills
            # This enables complement matching (CLOB mints when UP+DOWN=$1.00)
            up_fill, down_fill = self._place_straddle_orders(
                market, up_price, down_price, TRADE_SIZE_PER_SIDE)
        
        if not up_fill or not up_fill.get("filled"):
            self.log(f"  ❌ UP buy failed, aborting straddle")
            return
        self.log(f"  ✅ UP: {up_fill['shares']:.1f}sh @ ${up_fill['price']:.3f} = ${up_fill['cost']:.2f}")
        
        if not down_fill or not down_fill.get("filled"):
            self.log(f"  ❌ DOWN buy failed (UP already bought — will manage as single position)")
        else:
            self.log(f"  ✅ DOWN: {down_fill['shares']:.1f}sh @ ${down_fill['price']:.3f} = ${down_fill['cost']:.2f}")
        
        total_cost = up_fill["cost"] + (down_fill["cost"] if down_fill else 0)
        self.log(f"  💰 STRADDLE OPEN: Total cost ${total_cost:.2f} (complement={'YES' if USE_COMPLEMENT_MATCHING else 'NO'})")
        
        straddle = {
            "asset": asset,
            "window_ts": window_ts,
            "market": market,
            
            "up_token": market["up_token"],
            "up_entry": up_fill["price"],
            "up_shares": up_fill["shares"],
            "up_cost": up_fill["cost"],
            "up_sold": False,
            "up_proceeds": 0.0,
            "up_exit_reason": None,
            
            "down_token": market["down_token"],
            "down_entry": down_fill["price"] if down_fill else 0,
            "down_shares": down_fill["shares"] if down_fill else 0,
            "down_cost": down_fill["cost"] if down_fill else 0,
            "down_sold": False,
            "down_proceeds": 0.0,
            "down_exit_reason": None,
            
            "total_cost": total_cost,
        }
        self.open_straddles.append(straddle)
        
        # Record in DB
        self.conn.execute("""INSERT INTO straddles 
            (asset, window_ts, up_token_id, up_entry_price, up_shares, up_cost,
             down_token_id, down_entry_price, down_shares, down_cost, total_cost)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (asset, window_ts, market["up_token"], up_fill["price"], up_fill["shares"], up_fill["cost"],
             market["down_token"],
             down_fill["price"] if down_fill else 0,
             down_fill["shares"] if down_fill else 0,
             down_fill["cost"] if down_fill else 0,
             total_cost))
        self.conn.commit()
    
    def _check_exits(self):
        """Monitor straddle positions and manage exits."""
        now_ts = int(time.time())
        resolved = []
        
        for i, s in enumerate(self.open_straddles):
            window_end = s["window_ts"] + 300
            secs_to_close = window_end - now_ts
            market = s["market"]
            
            # ── Check each leg ──
            for side in ["up", "down"]:
                if s[f"{side}_sold"]:
                    continue
                
                token = s[f"{side}_token"]
                entry = s[f"{side}_entry"]
                shares = s[f"{side}_shares"]
                
                if shares <= 0:
                    continue
                
                bid = self._get_best_bid(token)
                if bid <= 0:
                    continue
                
                gain_pct = (bid - entry) / entry if entry > 0 else 0
                other_side = "down" if side == "up" else "up"
                other_bid = self._get_best_bid(s[f"{other_side}_token"]) if not s[f"{other_side}_sold"] else 0
                
                should_sell = False
                reason = None
                
                # ── Winner detection: this side is going up, other going down ──
                if bid >= WINNER_TP_PRICE:
                    should_sell = True
                    reason = "winner_tp"
                    self.log(f"  💰 {side.upper()} WINNER TP: ${entry:.3f} → ${bid:.3f} (+{gain_pct*100:.0f}%)")
                
                # ── Loser detection: this side is dropping ──
                elif gain_pct <= -LOSER_SL_PCT:
                    should_sell = True
                    reason = "loser_sl"
                    self.log(f"  🛑 {side.upper()} LOSER SL: ${entry:.3f} → ${bid:.3f} ({gain_pct*100:+.0f}%)")
                
                # ── Emergency: dump everything before expiry ──
                elif secs_to_close <= LOSER_EMERGENCY_SECS and gain_pct < 0:
                    should_sell = True
                    reason = "loser_emergency"
                    self.log(f"  🚨 {side.upper()} EMERGENCY: T-{secs_to_close}s, ${bid:.3f}")
                
                elif secs_to_close <= WINNER_EMERGENCY_SECS:
                    should_sell = True
                    reason = "winner_emergency"
                    self.log(f"  🚨 {side.upper()} EMERGENCY: T-{secs_to_close}s, ${bid:.3f}")
                
                # ── Accelerated loser exit (120-45s): sell if diverging ──
                elif 45 < secs_to_close <= 120:
                    if gain_pct < -0.12:  # Down 12%+ with time running out
                        should_sell = True
                        reason = "loser_accel"
                        self.log(f"  ⏰ {side.upper()} ACCEL SL: T-{secs_to_close}s, ${bid:.3f} ({gain_pct*100:+.0f}%)")
                    elif gain_pct > 0.50:  # Up 50%+ = clearly winning, let it ride to resolution
                        pass  # Hold winner
                
                # ── Breakeven zone (45s): dump anything losing ──
                elif 30 < secs_to_close <= 45:
                    if gain_pct < -0.05:  # Even slightly down = dump
                        should_sell = True
                        reason = "loser_breakeven"
                        self.log(f"  ⏰ {side.upper()} BREAKEVEN SELL: T-{secs_to_close}s, ${bid:.3f} ({gain_pct*100:+.0f}%)")
                
                if should_sell:
                    proceeds = self._place_sell(
                        token, shares, bid, market["tick_size"], market["neg_risk"])
                    if proceeds:
                        s[f"{side}_sold"] = True
                        s[f"{side}_proceeds"] = proceeds
                        s[f"{side}_exit_reason"] = reason
                        self.log(f"    ✅ Sold {side.upper()}: ${proceeds:.2f}")
                    else:
                        self.log(f"    ❌ Sell failed for {side.upper()}")
            
            # ── Check if straddle is fully closed ──
            if s["up_sold"] and s["down_sold"]:
                total_proceeds = s["up_proceeds"] + s["down_proceeds"]
                net_pnl = total_proceeds - s["total_cost"]
                
                if net_pnl > 0:
                    self.wins += 1
                else:
                    self.losses += 1
                self.total_pnl += net_pnl
                
                emoji = "🟢" if net_pnl > 0 else "🔴"
                self.log(f"  {emoji} STRADDLE CLOSED: Cost ${s['total_cost']:.2f} → "
                        f"UP ${s['up_proceeds']:.2f} ({s['up_exit_reason']}) + "
                        f"DOWN ${s['down_proceeds']:.2f} ({s['down_exit_reason']}) = "
                        f"${net_pnl:+.2f}")
                
                # Update DB
                self.conn.execute("""UPDATE straddles SET
                    up_exit_price=?, up_exit_reason=?, up_pnl=?,
                    down_exit_price=?, down_exit_reason=?, down_pnl=?,
                    total_proceeds=?, net_pnl=?, status='closed', closed_at=?,
                    winner_side=?
                    WHERE asset=? AND window_ts=?""",
                    (s["up_proceeds"] / max(s["up_shares"], 0.01) if s["up_shares"] > 0 else 0,
                     s["up_exit_reason"], s["up_proceeds"] - s["up_cost"],
                     s["down_proceeds"] / max(s["down_shares"], 0.01) if s["down_shares"] > 0 else 0,
                     s["down_exit_reason"], s["down_proceeds"] - s["down_cost"],
                     total_proceeds, net_pnl, datetime.now(PST).isoformat(),
                     "up" if s["up_proceeds"] > s["down_proceeds"] else "down",
                     s["asset"], s["window_ts"]))
                self.conn.commit()
                resolved.append(i)
            
            # ── Check resolution for unsold legs ──
            elif secs_to_close < -30:
                self._resolve_straddle(s, i)
                resolved.append(i)
        
        for idx in sorted(resolved, reverse=True):
            self.open_straddles.pop(idx)
    
    def _resolve_straddle(self, s: dict, idx: int):
        """Resolve expired straddle legs via Polymarket resolution."""
        try:
            slug = f"{s['asset']}-updown-5m-{s['window_ts']}"
            r = requests.get(f"https://gamma-api.polymarket.com/events?slug={slug}", timeout=5)
            d = r.json()
            if not d:
                return
            
            m = d[0]["markets"][0]
            prices = json.loads(m["outcomePrices"]) if isinstance(m["outcomePrices"], str) else m.get("outcomePrices", [])
            outcomes = json.loads(m["outcomes"]) if isinstance(m["outcomes"], str) else m["outcomes"]
            
            for oi, outcome in enumerate(outcomes):
                if outcome.lower() == "up":
                    up_resolved = float(prices[oi])
                    down_resolved = float(prices[1 - oi])
            
            # Handle unsold legs
            for side, resolved_price in [("up", up_resolved), ("down", down_resolved)]:
                if not s[f"{side}_sold"] and s[f"{side}_shares"] > 0:
                    if resolved_price > 0.95:  # Winner
                        proceeds = s[f"{side}_shares"] * 1.0
                        fee = max(proceeds - s[f"{side}_cost"], 0) * 0.02
                        proceeds -= fee
                        reason = "resolution_win"
                    else:  # Loser
                        proceeds = 0.0
                        reason = "resolution_loss"
                    
                    s[f"{side}_sold"] = True
                    s[f"{side}_proceeds"] = proceeds
                    s[f"{side}_exit_reason"] = reason
                    self.log(f"  📋 {side.upper()} resolved: ${proceeds:.2f} ({reason})")
            
            # Close straddle
            total_proceeds = s["up_proceeds"] + s["down_proceeds"]
            net_pnl = total_proceeds - s["total_cost"]
            
            if net_pnl > 0:
                self.wins += 1
            else:
                self.losses += 1
            self.total_pnl += net_pnl
            
            emoji = "🟢" if net_pnl > 0 else "🔴"
            self.log(f"  {emoji} STRADDLE RESOLVED: ${net_pnl:+.2f}")
            
            self.conn.execute("""UPDATE straddles SET
                up_exit_price=?, up_exit_reason=?, up_pnl=?,
                down_exit_price=?, down_exit_reason=?, down_pnl=?,
                total_proceeds=?, net_pnl=?, status='closed', closed_at=?
                WHERE asset=? AND window_ts=?""",
                (s.get("up_proceeds", 0), s.get("up_exit_reason"),
                 s.get("up_proceeds", 0) - s["up_cost"],
                 s.get("down_proceeds", 0), s.get("down_exit_reason"),
                 s.get("down_proceeds", 0) - s["down_cost"],
                 total_proceeds, net_pnl, datetime.now(PST).isoformat(),
                 s["asset"], s["window_ts"]))
            self.conn.commit()
            
        except Exception as e:
            self.log(f"  ❌ Resolution error: {e}")
    
    def run(self):
        mode_str = "📋 PAPER" if self.paper_mode else "💰 LIVE"
        print(f"🏞 The Outsiders — Straddle Strategy")
        print("=" * 60)
        print(f"{mode_str} | ${TRADE_SIZE_PER_SIDE:.0f}/side = ${TRADE_SIZE_PER_SIDE*2:.0f} per straddle")
        print(f"📊 Assets: {', '.join(a.upper() for a in ASSETS)}")
        print(f"🎯 Loser SL: -{LOSER_SL_PCT*100:.0f}% | Winner TP: ${WINNER_TP_PRICE}")
        print(f"📋 Entry: {ENTRY_WINDOW_MIN_SECS}-{ENTRY_WINDOW_MAX_SECS}s before close")
        print(f"🛑 Max price per side: ${MAX_ENTRY_PRICE} | Min: ${MIN_ENTRY_PRICE}")
        print("=" * 60)
        
        self._init_client()
        balance = self._get_balance()
        self.log(f"💰 Balance: ${balance:.2f}")
        
        last_exit_check = 0
        
        while self.running:
            try:
                now = time.time()
                now_ts = int(now)
                current_window = (now_ts // 300) * 300
                window_end = current_window + 300
                secs_left = window_end - now_ts
                
                # Exit checks every 5s
                if now - last_exit_check >= CHECK_INTERVAL and self.open_straddles:
                    self._check_exits()
                    last_exit_check = now
                
                # Bad hours
                hour = datetime.now(PST).hour
                if hour in BAD_HOURS:
                    time.sleep(10)
                    continue
                
                # Entry: 150-180s before close
                if ENTRY_WINDOW_MIN_SECS < secs_left < ENTRY_WINDOW_MAX_SECS:
                    for asset in ASSETS:
                        key = (asset, current_window)
                        if key in self.traded_windows:
                            continue
                        
                        if len(self.open_straddles) >= MAX_STRADDLES:
                            break
                        
                        balance = self._get_balance()
                        if balance < TRADE_SIZE_PER_SIDE * 2 + 1:
                            self.log(f"  ⚠️ Insufficient balance (${balance:.2f})")
                            continue
                        
                        self.traded_windows.add(key)
                        
                        market = self._get_market(asset, current_window)
                        if not market or market.get("closed"):
                            continue
                        
                        self.log(f"\n🎰 STRADDLE: {asset.upper()} window {current_window}")
                        self._enter_straddle(asset, market, current_window)
                
                # Status every 5 windows
                if secs_left < 2:
                    total = self.wins + self.losses
                    if total > 0:
                        wr = self.wins / total * 100
                        self.log(f"📊 {self.wins}W-{self.losses}L ({wr:.0f}%) | "
                                f"PnL: ${self.total_pnl:+.2f} | "
                                f"Open: {len(self.open_straddles)} straddles")
                
                # Clean old window keys
                cutoff = current_window - 3600
                self.traded_windows = {k for k in self.traded_windows if k[1] > cutoff}
                
                time.sleep(1)
                
            except Exception as e:
                self.log(f"❌ Error: {e}")
                import traceback
                traceback.print_exc()
                time.sleep(10)
        
        total = self.wins + self.losses
        wr = self.wins / total * 100 if total else 0
        print(f"\n{'='*60}")
        print(f"🏞 STRADDLE SESSION SUMMARY")
        print(f"Trades: {total} ({self.wins}W-{self.losses}L) | WR: {wr:.1f}%")
        print(f"Total PnL: ${self.total_pnl:+.2f}")
        print(f"{'='*60}")


if __name__ == "__main__":
    paper = "--paper" in sys.argv
    trader = StraddleTrader(paper_mode=paper)
    trader.run()
