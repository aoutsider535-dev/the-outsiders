"""
🏞 The Outsiders v3 — The Scalper

Strategy: Quick in-and-out trades with tight profit/loss targets.
Key insight: We often fill BELOW our order price (market improvement).
             Tight exits capture quick moves without holding to expiry.

Architecture:
  1. ENTRY: Same TA signals as v2 (reused)
  2. EXIT: Graduated urgency — tight TP/SL early, forced exit before close
  3. NEVER EXPIRE: Every position sold before window closes

Exit Strategy (graduated urgency):
  - Normal:  TP +$0.05/share from fill price, SL -$0.03/share
  - T-2min:  Lower TP to breakeven (sell if bid >= entry price)
  - T-1min:  Sell at best bid if bid > $0.02
  - T-15s:   Emergency sell at any price (backstop, rarely hits)

Balance Tracking:
  - Syncs USDC from CLOB every loop
  - Auto-redeems winners every loop (rate-limited)
  - Reports true balance: USDC + unredeemed + open position value
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
from web3 import Web3

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, PartialCreateOrderOptions, BalanceAllowanceParams
from py_clob_client.order_builder.constants import BUY, SELL
from src.redeemer import Redeemer

# Import shared TA functions from v2
from src.live_trader_v2 import (
    get_binance_candles, analyze_ta, TASignals, BINANCE_SYMBOLS,
    CTF_ADDRESS, CTF_ABI
)

# ═══════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════

PST = timezone(timedelta(hours=-7))
ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "trader_v3.db")
LOG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "trader_v3.log")

ASSETS = ["btc", "eth"]  # v3 handles BTC+ETH, v2 handles SOL+XRP

# ── Scalping Config ──
TRADE_SIZE = 5.0            # USD per trade
TP_GAIN = 0.15              # Take profit: sell when bid >= entry + $0.15
SL_DROP = 0.03              # Stop loss: sell when bid <= entry - $0.03
MAX_CONCURRENT = 8          # Max open positions
CHECK_INTERVAL = 5          # Check positions every 5 seconds

# Entry signals (same as v2)
MIN_BULL_SIGNALS = 3
MIN_MOVE_PCT = 0.01
CONTRARIAN_THRESHOLD = 0.75
CONTRARIAN_MAX_ENTRY = 0.25
MIN_TA_DISAGREE = 2

# Bad hours (PST) — minimal for scalping (only overnight dead hours)
BAD_HOURS = {1, 2, 3, 4, 5, 6}

# ═══════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS trades_v3 (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT, asset TEXT, strategy TEXT, direction TEXT,
        entry_price REAL, quantity REAL, cost REAL,
        exit_price REAL, pnl REAL, pnl_pct REAL,
        exit_reason TEXT, status TEXT DEFAULT 'open',
        ta_summary TEXT, market_slug TEXT, order_id TEXT,
        token_id TEXT, condition_id TEXT, window_ts INTEGER,
        closed_at TEXT,
        order_price REAL,
        onchain_shares REAL,
        is_complement INTEGER DEFAULT 0
    )""")
    conn.commit()
    conn.close()

# ═══════════════════════════════════════════════════════════
# TRADER
# ═══════════════════════════════════════════════════════════

class ScalperV3:
    def __init__(self):
        self.running = True
        self.client = None
        self.redeemer = None
        self.w3 = None
        self.ctf = None
        
        self.balance = 0.0
        self.open_trades = []
        self.traded_windows = set()
        
        self.wins = 0
        self.losses = 0
        self.total_pnl = 0.0
        self.daily_pnl = 0.0
        self.daily_date = None
        
        self.last_redeem_time = 0
        
        signal.signal(signal.SIGINT, self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)
        
        init_db()
    
    def _shutdown(self, *args):
        self.log("⏹️ Shutting down...")
        self.running = False
    
    def log(self, msg: str):
        ts = datetime.now(PST).strftime("%I:%M:%S %p")
        line = f"[{ts}] {msg}"
        print(line)
        try:
            with open(LOG_PATH, "a") as f:
                f.write(line + "\n")
        except:
            pass
    
    # ═══════════════════════════════════════════════════════
    # CLIENT & BALANCE
    # ═══════════════════════════════════════════════════════
    
    def _init_client(self):
        config = dotenv_values(ENV_PATH)
        pk = config.get("POLYGON_PRIVATE_KEY", "")
        addr = config.get("POLYGON_WALLET_ADDRESS", "")
        
        self.client = ClobClient("https://clob.polymarket.com", key=pk, chain_id=137)
        creds = self.client.create_or_derive_api_creds()
        self.client = ClobClient(
            "https://clob.polymarket.com", key=pk, chain_id=137,
            creds=creds, signature_type=1, funder=addr)
        
        try:
            self.redeemer = Redeemer(pk, addr)
            self.log("✅ Redeemer initialized")
        except Exception as e:
            self.log(f"⚠️ Redeemer failed: {e}")
        
        self.w3 = Web3(Web3.HTTPProvider("https://polygon.drpc.org"))
        self.ctf = self.w3.eth.contract(
            address=Web3.to_checksum_address(CTF_ADDRESS), abi=CTF_ABI)
        
        self.proxy_addr = addr
    
    def _get_usdc_balance(self) -> float:
        """Get USDC balance from CLOB."""
        try:
            bal = self.client.get_balance_allowance(
                BalanceAllowanceParams(asset_type="COLLATERAL"))
            raw = float(bal.get("balance", 0))
            return raw / 1e6 if raw > 1e6 else raw
        except:
            return self.balance
    
    def _get_true_balance(self) -> float:
        """Get REAL balance: USDC + all on-chain positions (open + unredeemed winners)."""
        usdc = self._get_usdc_balance()
        
        # Check recent markets for ANY held positions (not just this session's open_trades)
        pos_value = 0.0
        now_ts = int(time.time())
        try:
            for offset in range(8):  # Check last 8 windows (~40 min)
                window = ((now_ts // 300) * 300) - (offset * 300)
                for asset in list(set(ASSETS + ["btc", "eth", "sol", "xrp"])):  # Check all
                    slug = f"{asset}-updown-5m-{window}"
                    try:
                        r = requests.get(f"https://gamma-api.polymarket.com/events?slug={slug}", timeout=2)
                        d = r.json()
                        if not d: continue
                        m = d[0]["markets"][0]
                        tokens = json.loads(m["clobTokenIds"]) if isinstance(m["clobTokenIds"], str) else m["clobTokenIds"]
                        outcomes = json.loads(m["outcomes"]) if isinstance(m["outcomes"], str) else m["outcomes"]
                        prices = json.loads(m["outcomePrices"]) if isinstance(m["outcomePrices"], str) else m.get("outcomePrices", [])
                        
                        for i, (outcome, token) in enumerate(zip(outcomes, tokens)):
                            bal = self.client.get_balance_allowance(
                                BalanceAllowanceParams(asset_type="CONDITIONAL", token_id=token))
                            raw = float(bal.get("balance", 0))
                            shares = raw / 1e6 if raw > 1000 else raw
                            if shares > 0.1:
                                price = float(prices[i]) if i < len(prices) else 0
                                pos_value += shares * price
                    except:
                        pass
        except:
            pass
        
        return usdc + pos_value
    
    def _auto_redeem(self):
        """Auto-redeem winners by scanning recent windows. Rate limited to once per 60s."""
        now = time.time()
        if now - self.last_redeem_time < 60:
            return
        self.last_redeem_time = now
        
        if not self.redeemer:
            return
        
        # Track already-redeemed to avoid wasting gas
        if not hasattr(self, '_redeemed_cids'):
            self._redeemed_cids = set()
        
        try:
            # Scan last 15 windows (~75 min) for ANY held winning tokens
            now_ts = int(time.time())
            for offset in range(15):
                window = ((now_ts // 300) * 300) - (offset * 300)
                for asset in list(set(ASSETS + ["btc", "eth", "sol", "xrp"])):
                    slug = f"{asset}-updown-5m-{window}"
                    try:
                        r = requests.get(f"https://gamma-api.polymarket.com/events?slug={slug}", timeout=2)
                        d = r.json()
                        if not d: continue
                        m = d[0]["markets"][0]
                        cid = m.get("conditionId", "")
                        if not cid or cid in self._redeemed_cids: continue
                        
                        prices = json.loads(m["outcomePrices"]) if isinstance(m["outcomePrices"], str) else m.get("outcomePrices", [])
                        if not prices or (float(prices[0]) < 0.95 and float(prices[1]) < 0.95):
                            continue  # Not resolved yet
                        
                        # Check if we hold winning tokens
                        tokens = json.loads(m["clobTokenIds"]) if isinstance(m["clobTokenIds"], str) else m["clobTokenIds"]
                        outcomes = json.loads(m["outcomes"]) if isinstance(m["outcomes"], str) else m["outcomes"]
                        has_tokens = False
                        for i, (outcome, token) in enumerate(zip(outcomes, tokens)):
                            p = float(prices[i]) if i < len(prices) else 0
                            if p < 0.95: continue
                            bal = self.client.get_balance_allowance(
                                BalanceAllowanceParams(asset_type="CONDITIONAL", token_id=token))
                            raw = float(bal.get("balance", 0))
                            shares = raw / 1e6 if raw > 1000 else raw
                            if shares > 0.1:
                                has_tokens = True
                                break
                        
                        if not has_tokens:
                            self._redeemed_cids.add(cid)  # Nothing to redeem
                            continue
                        
                        result = self.redeemer.redeem(cid)
                        if result.get("success"):
                            self.log(f"💰 Auto-redeemed: {slug[-30:]}")
                            self._redeemed_cids.add(cid)
                            time.sleep(2)  # Avoid rate limiting
                    except:
                        pass
        except:
            pass
    
    # ═══════════════════════════════════════════════════════
    # MARKET DATA
    # ═══════════════════════════════════════════════════════
    
    def _get_market(self, asset: str, window_ts: int) -> Optional[dict]:
        """Fetch market data for an asset's current window."""
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
                "tokens": tokens,
                "outcomes": outcomes,
                "prices": [float(p) for p in prices],
                "up_token": tokens[up_idx],
                "down_token": tokens[down_idx],
                "up_price": float(prices[up_idx]),
                "down_price": float(prices[down_idx]),
                "tick_size": str(m.get("orderPriceMinTickSize", "0.01")),
                "neg_risk": m.get("negRisk", False),
                "closed": m.get("closed", False),
            }
        except Exception as e:
            return None
    
    def _get_best_bid(self, token_id: str) -> float:
        """Get best bid price for a token."""
        try:
            book = requests.get(
                f"https://clob.polymarket.com/book?token_id={token_id}", timeout=3).json()
            bids = book.get("bids", [])
            if bids:
                return max(float(b["price"]) for b in bids)
        except:
            pass
        return 0.0
    
    def _get_best_ask(self, token_id: str) -> float:
        """Get best ask price for a token."""
        try:
            book = requests.get(
                f"https://clob.polymarket.com/book?token_id={token_id}", timeout=3).json()
            asks = book.get("asks", [])
            if asks:
                return min(float(a["price"]) for a in asks)
        except:
            pass
        return 1.0
    
    # ═══════════════════════════════════════════════════════
    # ORDER EXECUTION
    # ═══════════════════════════════════════════════════════
    
    def _place_buy(self, token_id: str, price: float, size: float,
                   tick_size: str, neg_risk: bool) -> Optional[dict]:
        """Place a BUY order. Returns real fill price."""
        try:
            tick = float(tick_size)
            n_decimals = len(tick_size.split('.')[-1]) if '.' in tick_size else 0
            aggressive_price = min(round(round((price + tick) / tick) * tick, n_decimals), 0.99)
            
            order_args = OrderArgs(
                token_id=token_id, price=aggressive_price,
                size=round(size, 2), side=BUY)
            options = PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk)
            resp = self.client.create_and_post_order(order_args, options)
            
            if resp and resp.get("success"):
                taking = float(resp.get("takingAmount") or 0)
                making = float(resp.get("makingAmount") or 0)
                real_price = making / taking if taking > 0 else aggressive_price
                
                return {
                    "order_id": resp.get("orderID", ""),
                    "order_price": aggressive_price,
                    "fill_price": round(real_price, 6),
                    "fill_size": round(taking, 6),
                    "fill_cost": round(making, 6),
                    "status": resp.get("status", ""),
                }
            self.log(f"  ⚠️ Order rejected: {resp}")
            return None
        except Exception as e:
            self.log(f"  ❌ Buy error: {e}")
            return None
    
    def _place_sell(self, token_id: str, price: float, size: float,
                    tick_size: str, neg_risk: bool) -> Optional[dict]:
        """Place a SELL order. Returns real fill data."""
        try:
            tick = float(tick_size)
            n_decimals = len(tick_size.split('.')[-1]) if '.' in tick_size else 0
            # Aggressive: sell 1 tick below to cross spread
            aggressive_price = max(round(round((price - tick) / tick) * tick, n_decimals), 0.01)
            
            order_args = OrderArgs(
                token_id=token_id, price=aggressive_price,
                size=round(size, 2), side=SELL)
            options = PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk)
            resp = self.client.create_and_post_order(order_args, options)
            
            if resp and resp.get("success"):
                taking = float(resp.get("takingAmount") or 0)
                making = float(resp.get("makingAmount") or 0)
                real_price = taking / making if making > 0 else aggressive_price
                
                return {
                    "order_id": resp.get("orderID", ""),
                    "fill_price": round(real_price, 6),
                    "fill_size": round(making, 6),
                    "fill_cost": round(taking, 6),
                    "status": resp.get("status", ""),
                }
            self.log(f"  ⚠️ Sell rejected: {resp}")
            return None
        except Exception as e:
            self.log(f"  ❌ Sell error: {e}")
            return None
    
    def _get_clob_token_balance(self, token_id: str) -> float:
        """Get CLOB token balance in shares (what we can sell)."""
        try:
            params = BalanceAllowanceParams(asset_type="CONDITIONAL", token_id=token_id)
            bal = self.client.get_balance_allowance(params)
            raw = float(bal.get("balance", 0))
            # CLOB returns raw micro-units (1e6), convert to shares
            return raw / 1e6 if raw > 1000 else raw
        except:
            return 0.0
    
    def _get_onchain_balance(self, token_id: str) -> float:
        """Get on-chain token balance."""
        try:
            bal = self.ctf.functions.balanceOf(
                Web3.to_checksum_address(self.proxy_addr), int(token_id)).call()
            return bal / 1e6
        except:
            return 0.0
    
    def _verify_fill(self, order_id: str, max_wait: int = 12) -> Optional[dict]:
        """Wait for order fill confirmation."""
        for _ in range(max_wait):
            time.sleep(1)
            try:
                order = self.client.get_order(order_id)
                if order:
                    matched = float(order.get("size_matched", 0))
                    status = order.get("status", "")
                    if matched > 0:
                        return {"filled": True, "size": matched,
                                "price": float(order.get("price", 0))}
                    if status in ("CANCELED", "EXPIRED"):
                        return {"filled": False}
            except:
                pass
        try:
            self.client.cancel_orders([order_id])
        except:
            pass
        return {"filled": False}
    
    # ═══════════════════════════════════════════════════════
    # SIGNAL EVALUATION (reused from v2 logic)
    # ═══════════════════════════════════════════════════════
    
    def _eval_momentum(self, asset: str, ta: TASignals, market: dict) -> Optional[dict]:
        """Evaluate momentum signal."""
        if abs(ta.price_move_pct) < MIN_MOVE_PCT:
            return None
        
        bull = ta.bull_count
        bear = ta.bear_count
        
        if bull >= MIN_BULL_SIGNALS and bull > bear:
            return {
                "strategy": "momentum",
                "direction": "up",
                "token_id": market["up_token"],
                "entry_price": market["up_price"],
                "reason": f"🚀 {asset.upper()} UP | {ta.summary()} | Move: {ta.price_move_pct:+.3f}%",
                "ta": ta,
            }
        elif bear >= MIN_BULL_SIGNALS and bear > bull:
            return {
                "strategy": "momentum",
                "direction": "down",
                "token_id": market["down_token"],
                "entry_price": market["down_price"],
                "reason": f"🚀 {asset.upper()} DOWN | {ta.summary()} | Move: {ta.price_move_pct:+.3f}%",
                "ta": ta,
            }
        return None
    
    def _eval_contrarian(self, asset: str, ta: TASignals, market: dict) -> Optional[dict]:
        """Evaluate contrarian signal — fade extreme odds."""
        if market["up_price"] > CONTRARIAN_THRESHOLD:
            if ta.bear_count >= MIN_TA_DISAGREE and market["down_price"] <= CONTRARIAN_MAX_ENTRY:
                return {
                    "strategy": "contrarian",
                    "direction": "down",
                    "token_id": market["down_token"],
                    "entry_price": market["down_price"],
                    "reason": f"🔄 {asset.upper()} DOWN (fade {market['up_price']:.0%}) | {ta.summary()}",
                    "ta": ta,
                }
        elif market["down_price"] > CONTRARIAN_THRESHOLD:
            if ta.bull_count >= MIN_TA_DISAGREE and market["up_price"] <= CONTRARIAN_MAX_ENTRY:
                return {
                    "strategy": "contrarian",
                    "direction": "up",
                    "token_id": market["up_token"],
                    "entry_price": market["up_price"],
                    "reason": f"🔄 {asset.upper()} UP (fade {market['down_price']:.0%}) | {ta.summary()}",
                    "ta": ta,
                }
        return None
    
    # ═══════════════════════════════════════════════════════
    # SCALPING EXIT LOGIC (THE CORE INNOVATION)
    # ═══════════════════════════════════════════════════════
    
    def _sell_position(self, trade: dict, bid: float, reason: str) -> bool:
        """Attempt to sell a position. Returns True if sold."""
        clob_bal = self._get_clob_token_balance(trade["token_id"])
        sell_qty = round(min(clob_bal, trade["quantity"]) - 0.01, 2)
        
        if sell_qty < 5.0:
            self.log(f"  ⚠️ CLOB balance too low ({clob_bal:.2f}), can't sell")
            return False
        
        result = self._place_sell(
            trade["token_id"], bid, sell_qty,
            trade["tick_size"], trade["neg_risk"])
        
        if result and result.get("fill_cost", 0) > 0:
            sell_price = result["fill_price"]
            sell_got = result["fill_cost"]
            pnl = sell_got - trade["cost"]
            pnl_pct = (pnl / trade["cost"]) * 100 if trade["cost"] else 0
            
            emoji = "🎯" if pnl > 0 else "💔"
            self.log(f"  {emoji} {reason}: Sold @ ${sell_price:.3f} | Got ${sell_got:.2f} | "
                    f"PnL: ${pnl:+.2f} ({pnl_pct:+.1f}%)")
            
            self._close_trade(trade, sell_price, pnl, reason)
            self.balance += sell_got
            
            if pnl > 0:
                self.wins += 1
            else:
                self.losses += 1
            
            return True
        
        # Try verify fill if order was placed but no immediate fill
        if result and result.get("order_id"):
            fill = self._verify_fill(result["order_id"], max_wait=5)
            if fill and fill.get("filled"):
                sell_got = fill["price"] * fill["size"]
                pnl = sell_got - trade["cost"]
                self._close_trade(trade, fill["price"], pnl, reason)
                self.balance += sell_got
                if pnl > 0:
                    self.wins += 1
                else:
                    self.losses += 1
                return True
        
        return False
    
    def _check_exits(self):
        """Check all open positions for exit conditions with graduated urgency."""
        resolved = []
        
        for i, trade in enumerate(self.open_trades):
            if not trade.get("real_fill", True):
                continue  # Skip complement fills
            
            now_ts = int(time.time())
            window_end = trade["window_ts"] + 300
            secs_to_close = window_end - now_ts
            
            # Get current bid
            bid = self._get_best_bid(trade["token_id"])
            if bid <= 0:
                continue
            
            entry = trade["entry_price"]
            gain = bid - entry
            
            # ── GRADUATED EXIT STRATEGY ──
            
            # Phase 1: Normal scalping (>2 min to close)
            if secs_to_close > 120:
                if gain >= TP_GAIN:
                    # Take profit
                    self.log(f"💰 TP: {trade['asset'].upper()} {trade['direction'].upper()} | "
                            f"Entry ${entry:.3f} → Bid ${bid:.3f} (+${gain:.3f})")
                    if self._sell_position(trade, bid, "scalp_tp"):
                        resolved.append(i)
                        continue
                
                if gain <= -SL_DROP:
                    # Stop loss
                    self.log(f"🛑 SL: {trade['asset'].upper()} {trade['direction'].upper()} | "
                            f"Entry ${entry:.3f} → Bid ${bid:.3f} (${gain:+.3f})")
                    if self._sell_position(trade, bid, "scalp_sl"):
                        resolved.append(i)
                        continue
            
            # Phase 2: Breakeven mode (1-2 min to close)
            elif secs_to_close > 60:
                if gain >= 0:
                    # Sell at breakeven or better
                    self.log(f"⏰ T-{secs_to_close}s: {trade['asset'].upper()} {trade['direction'].upper()} | "
                            f"Selling at breakeven+ (bid ${bid:.3f}, entry ${entry:.3f})")
                    if self._sell_position(trade, bid, "breakeven_exit"):
                        resolved.append(i)
                        continue
                
                if gain <= -SL_DROP:
                    # Still honor SL
                    self.log(f"🛑 SL (urgent): {trade['asset'].upper()} | ${gain:+.3f}")
                    if self._sell_position(trade, bid, "urgent_sl"):
                        resolved.append(i)
                        continue
            
            # Phase 3: Fire sale (15s-1min to close)
            elif secs_to_close > 15:
                if bid >= 0.02:
                    self.log(f"🔥 T-{secs_to_close}s: {trade['asset'].upper()} {trade['direction'].upper()} | "
                            f"Fire sale @ ${bid:.3f} (entry ${entry:.3f}, PnL ${gain:+.3f})")
                    if self._sell_position(trade, bid, "fire_sale"):
                        resolved.append(i)
                        continue
            
            # Phase 4: Emergency (< 15s to close)
            elif secs_to_close > 0:
                self.log(f"🚨 EMERGENCY: {trade['asset'].upper()} {trade['direction'].upper()} | "
                        f"T-{secs_to_close}s, selling at ANY price (${bid:.3f})")
                if self._sell_position(trade, bid, "emergency_exit"):
                    resolved.append(i)
                    continue
                # If sell fails, try again at $0.01
                if self._sell_position(trade, 0.01, "emergency_dump"):
                    resolved.append(i)
                    continue
        
        for idx in sorted(resolved, reverse=True):
            self.open_trades.pop(idx)
    
    def _check_resolutions(self):
        """Check for expired trades that somehow survived (shouldn't happen with graduated exits)."""
        resolved = []
        
        for i, trade in enumerate(self.open_trades):
            now_ts = int(time.time())
            window_end = trade["window_ts"] + 300
            
            if now_ts < window_end + 60:
                continue
            
            # This trade expired — this should be RARE with graduated exits
            self.log(f"⚠️ EXPIRED TRADE (should not happen!): {trade['asset'].upper()} {trade['direction'].upper()}")
            
            # Check if it won on-chain
            try:
                slug = trade["market_slug"]
                r = requests.get(f"https://gamma-api.polymarket.com/events?slug={slug}", timeout=3)
                d = r.json()
                if d:
                    m = d[0]["markets"][0]
                    prices = json.loads(m["outcomePrices"]) if isinstance(m["outcomePrices"], str) else m.get("outcomePrices", [])
                    if prices:
                        up_p, down_p = float(prices[0]), float(prices[1]) if len(prices) > 1 else 0
                        if up_p > 0.95:
                            winner = "up"
                        elif down_p > 0.95:
                            winner = "down"
                        else:
                            continue  # Not resolved yet
                        
                        won = trade["direction"] == winner
                        if won:
                            pnl = trade["quantity"] - trade["cost"]  # Win: get $1/share back
                            self.wins += 1
                            self.log(f"  ✅ Won (expired): +${pnl:.2f}")
                        else:
                            pnl = -trade["cost"]
                            self.losses += 1
                            self.log(f"  ❌ Lost (expired): -${abs(pnl):.2f}")
                        
                        self._close_trade(trade, 1.0 if won else 0.0, pnl, 
                                         "expired_win" if won else "expired_loss")
                        
                        # Redeem
                        if self.redeemer and trade.get("condition_id"):
                            try:
                                self.redeemer.redeem(trade["condition_id"])
                            except:
                                pass
                        
                        resolved.append(i)
            except:
                pass
            
            # Auto-clean stuck trades after 30 min
            if now_ts > window_end + 1800:
                self.log(f"🧹 Cleaning stuck trade: {trade['asset'].upper()}")
                self._close_trade(trade, 0, -trade["cost"], "stuck_cleanup")
                self.losses += 1
                resolved.append(i)
        
        for idx in sorted(resolved, reverse=True):
            self.open_trades.pop(idx)
    
    # ═══════════════════════════════════════════════════════
    # TRADE RECORDING
    # ═══════════════════════════════════════════════════════
    
    def _close_trade(self, trade: dict, exit_price: float, pnl: float, reason: str):
        self.total_pnl += pnl
        self.daily_pnl += pnl
        
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute("""UPDATE trades_v3 SET exit_price=?, pnl=?, pnl_pct=?,
                           exit_reason=?, status='closed', closed_at=? WHERE id=?""",
                        (exit_price, pnl, (pnl / trade["cost"] * 100) if trade["cost"] else 0,
                         reason, datetime.now(PST).isoformat(), trade["id"]))
            conn.commit()
            conn.close()
        except Exception as e:
            self.log(f"  ⚠️ DB error: {e}")
    
    # ═══════════════════════════════════════════════════════
    # TRADE EXECUTION
    # ═══════════════════════════════════════════════════════
    
    def _execute_trade(self, signal: dict, asset: str, market: dict, window_ts: int):
        direction = signal["direction"]
        token_id = signal["token_id"]
        
        # Use ACTUAL best ask from CLOB book, not Gamma probability
        real_ask = self._get_best_ask(token_id)
        if real_ask <= 0 or real_ask >= 0.99:
            self.log(f"  ⚠️ No valid ask (${real_ask:.2f}), skipping")
            return
        
        entry_price = real_ask  # Buy at the ask, not Gamma price
        
        quantity = TRADE_SIZE / entry_price if entry_price > 0 else 0
        cost = quantity * entry_price
        
        if cost > self.balance:
            self.log(f"  ⚠️ Insufficient balance (${self.balance:.2f})")
            return
        
        self.log(f"  💸 Placing order: {direction.upper()} @ ${entry_price:.3f} × {quantity:.1f}")
        
        order = self._place_buy(token_id, entry_price, quantity,
                                market["tick_size"], market["neg_risk"])
        if not order:
            return
        
        # Extract real fill data
        if order.get("fill_size", 0) > 0:
            fill_price = order["fill_price"]
            fill_size = order["fill_size"]
            fill_cost = order["fill_cost"]
            self.log(f"  📍 Filled: {fill_size:.1f} @ ${fill_price:.4f} = ${fill_cost:.2f} "
                    f"(ordered @ ${order.get('order_price', 0):.3f})")
        else:
            fill = self._verify_fill(order["order_id"])
            if not fill or not fill.get("filled"):
                self.log(f"  ⚠️ Order not filled — cancelled")
                return
            fill_size = fill["size"]
            fill_price = fill["price"]
            fill_cost = fill_price * fill_size
            self.log(f"  ⚠️ Filled via verify: {fill_size:.1f} @ ${fill_price:.3f}")
        
        # Complement detection: use CLOB balance — needs 3s to settle after fill
        time.sleep(3)
        clob_shares = self._get_clob_token_balance(token_id)
        # If first check shows 0, retry once (settlement can take 2-3s)
        if clob_shares < 0.1:
            time.sleep(2)
            clob_shares = self._get_clob_token_balance(token_id)
        is_real_fill = clob_shares >= fill_size * 0.5
        onchain_shares = clob_shares  # Track for DB
        
        if is_real_fill:
            self.log(f"  ✅ FILLED: {fill_size:.1f} @ ${fill_price:.3f} = ${fill_cost:.2f} | "
                    f"CLOB balance: {clob_shares:.1f} ✅")
        else:
            self.log(f"  ⚠️ COMPLEMENT: CLOB has {clob_shares:.2f}/{fill_size:.1f} — TP/SL disabled")
        
        # Record in DB
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.execute("""INSERT INTO trades_v3 
                (timestamp, asset, strategy, direction, entry_price, quantity, cost,
                 ta_summary, market_slug, order_id, token_id, condition_id, window_ts,
                 status, order_price, onchain_shares, is_complement)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)""",
                (datetime.now(PST).isoformat(), asset, signal["strategy"],
                 direction, fill_price, fill_size, fill_cost,
                 signal.get("ta", TASignals()).summary(), market["slug"],
                 order["order_id"], token_id, market["condition_id"], window_ts,
                 order.get("order_price", 0), onchain_shares, 0 if is_real_fill else 1))
            trade_id = cursor.lastrowid
            conn.commit()
            conn.close()
        except Exception as e:
            self.log(f"  ⚠️ DB insert error: {e}")
            trade_id = 0
        
        self.open_trades.append({
            "id": trade_id,
            "asset": asset,
            "strategy": signal["strategy"],
            "direction": direction,
            "entry_price": fill_price,
            "quantity": fill_size,
            "onchain_shares": onchain_shares,
            "real_fill": is_real_fill,
            "cost": fill_cost,
            "token_id": token_id,
            "condition_id": market["condition_id"],
            "market_slug": market["slug"],
            "window_ts": window_ts,
            "tick_size": market["tick_size"],
            "neg_risk": market["neg_risk"],
        })
        
        self.balance -= fill_cost
    
    # ═══════════════════════════════════════════════════════
    # MAIN LOOP
    # ═══════════════════════════════════════════════════════
    
    def run(self):
        print("🏞 The Outsiders v3 — The Scalper")
        print("=" * 60)
        print(f"💰 LIVE MODE | Trade size: ${TRADE_SIZE}")
        print(f"⚡ TP: +${TP_GAIN} | SL: -${SL_DROP} | Check every {CHECK_INTERVAL}s")
        print(f"📊 Assets: {', '.join(a.upper() for a in ASSETS)}")
        print(f"🎯 Graduated exits: TP/SL → Breakeven → Fire sale → Emergency")
        print(f"🚫 NO EXPIRIES — every position sold before close")
        print("=" * 60)
        
        self._init_client()
        self.balance = self._get_usdc_balance()
        true_bal = self._get_true_balance()
        self.log(f"💰 USDC: ${self.balance:.2f} | True portfolio: ${true_bal:.2f}")
        
        last_exit_check = 0
        last_balance_sync = 0
        
        while self.running:
            try:
                now = time.time()
                now_ts = int(now)
                current_window = (now_ts // 300) * 300
                window_end = current_window + 300
                secs_left = window_end - now_ts
                
                # Reset daily stats at midnight PST
                today = datetime.now(PST).date()
                if self.daily_date != today:
                    if self.daily_date:
                        self.log(f"📅 Day closed: PnL ${self.daily_pnl:+.2f} | {self.wins}W-{self.losses}L")
                    self.daily_date = today
                    self.daily_pnl = 0.0
                
                # ── EXIT CHECKS: Every 10 seconds ──
                if now - last_exit_check >= CHECK_INTERVAL and self.open_trades:
                    self._check_exits()
                    last_exit_check = now
                
                # ── RESOLUTION CHECKS: For any expired trades ──
                self._check_resolutions()
                
                # ── AUTO-REDEEM: Every 60s ──
                self._auto_redeem()
                
                # ── BALANCE SYNC: Every 2 min ──
                if now - last_balance_sync > 120:
                    self.balance = self._get_usdc_balance()
                    last_balance_sync = now
                
                # ── BAD HOURS ──
                hour = datetime.now(PST).hour
                if hour in BAD_HOURS:
                    time.sleep(10)
                    continue
                
                # ── ENTRY SIGNALS: 2-3 min into window ──
                if 120 < secs_left < 180:
                    for asset in ASSETS:
                        key = (asset, current_window)
                        if key in self.traded_windows:
                            continue
                        
                        if len(self.open_trades) >= MAX_CONCURRENT:
                            break
                        
                        self.traded_windows.add(key)
                        
                        candles = get_binance_candles(BINANCE_SYMBOLS[asset])
                        if not candles or len(candles) < 27:
                            continue
                        
                        ta = analyze_ta(candles)
                        
                        market = self._get_market(asset, current_window)
                        if not market or market.get("closed"):
                            continue
                        
                        signals = []
                        mom = self._eval_momentum(asset, ta, market)
                        if mom:
                            signals.append(mom)
                        con = self._eval_contrarian(asset, ta, market)
                        if con:
                            signals.append(con)
                        
                        if not signals:
                            continue
                        
                        best = signals[0]
                        self.log(f"  {best['reason']}")
                        self._execute_trade(best, asset, market, current_window)
                
                # ── STATUS: Every 5 min ──
                if secs_left % 300 < 2 and secs_left < 5:
                    wr = self.wins / (self.wins + self.losses) * 100 if (self.wins + self.losses) > 0 else 0
                    true_bal = self._get_true_balance()
                    self.log(f"📊 {self.wins}W-{self.losses}L ({wr:.0f}%) | "
                            f"PnL: ${self.total_pnl:+.2f} | Today: ${self.daily_pnl:+.2f} | "
                            f"USDC: ${self.balance:.2f} | True: ${true_bal:.2f} | Open: {len(self.open_trades)}")
                
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
        total = self.wins + self.losses
        wr = self.wins / total * 100 if total else 0
        print(f"\n{'='*60}")
        print(f"🏞 V3 SCALPER SESSION SUMMARY")
        print(f"Trades: {total} ({self.wins}W-{self.losses}L) | WR: {wr:.1f}%")
        print(f"Total PnL: ${self.total_pnl:+.2f}")
        print(f"Balance: ${self.balance:.2f}")
        print(f"{'='*60}")


if __name__ == "__main__":
    trader = ScalperV3()
    trader.run()
