"""
🏞 The Outsiders v2 — Clean Trading Engine

Architecture:
  1. ENTRY: Multi-indicator TA confirmation + Contrarian fade
  2. RISK: Take-Profit @ $0.95 + Stop-Loss @ -$0.07 during active window
  3. VOLUME: Trade every qualifying window across all assets

Strategies:
  🚀 Momentum: RSI + MACD + EMA + VWAP + Heikin Ashi confirmation
  🔄 Contrarian: Fade extreme odds (>75%) when TA disagrees
  
No fake "edge" or "confidence" metrics. No ML gate.
SL handles all risk management.
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
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Tuple
from dotenv import dotenv_values
from web3 import Web3

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, PartialCreateOrderOptions, BalanceAllowanceParams
from py_clob_client.order_builder.constants import BUY, SELL
from src.redeemer import Redeemer

# On-chain token balance check
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
CTF_ABI = [{"inputs":[{"name":"account","type":"address"},{"name":"id","type":"uint256"}],
            "name":"balanceOf","outputs":[{"name":"","type":"uint256"}],
            "stateMutability":"view","type":"function"}]

# ═══════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════

PST = timezone(timedelta(hours=-7))
ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "trader_v2.db")
LOG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "trader_v2.log")

ASSETS = ["sol", "xrp"]  # v2 handles SOL+XRP, v3 handles BTC+ETH
BINANCE_SYMBOLS = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT", "xrp": "XRPUSDT"}

# Risk
TRADE_SIZE = 5.0           # USD per trade
SL_DROP = 0.07             # Sell if token drops $0.07 from entry
TP_PRICE = 0.95            # Take profit when best bid >= $0.95 (sell at ~$0.94)
MAX_DAILY_LOSS = 9999.0    # Circuit breaker DISABLED per Jakob (Mar 12)
MAX_CONCURRENT = 8         # Max open positions across all assets

# Momentum strategy
MIN_BULL_SIGNALS = 3       # Need 3+ bullish indicators to buy UP
MIN_MOVE_PCT = 0.01        # BTC must have moved at least 0.01% (SL handles risk)

# Contrarian strategy
CONTRARIAN_THRESHOLD = 0.75  # Fade when token > 75% (buy underdog < 25%)
CONTRARIAN_MAX_ENTRY = 0.25  # Max price to pay for underdog token
MIN_TA_DISAGREE = 2          # Need 2+ TA indicators disagreeing with crowd

# Bad hours (PST) - data shows WR < 40%
BAD_HOURS = {1, 2, 7, 11, 18, 22}

# ═══════════════════════════════════════════════════════════
# TA INDICATORS
# ═══════════════════════════════════════════════════════════

def calc_ema(data: list, period: int) -> list:
    if not data: return []
    ema = [data[0]]
    k = 2 / (period + 1)
    for d in data[1:]:
        ema.append(d * k + ema[-1] * (1 - k))
    return ema

def calc_rsi(closes: list, period: int = 14) -> list:
    if len(closes) < period + 1: return [50] * len(closes)
    rsi = [50] * period
    for i in range(period, len(closes)):
        gains = [max(closes[j] - closes[j-1], 0) for j in range(i-period+1, i+1)]
        losses = [max(closes[j-1] - closes[j], 0) for j in range(i-period+1, i+1)]
        avg_gain = np.mean(gains) or 1e-10
        avg_loss = np.mean(losses) or 1e-10
        rs = avg_gain / avg_loss
        rsi.append(100 - (100 / (1 + rs)))
    return rsi

def calc_macd(closes: list) -> Tuple[list, list, list]:
    ema12 = calc_ema(closes, 12)
    ema26 = calc_ema(closes, 26)
    macd_line = [f - s for f, s in zip(ema12, ema26)]
    signal_line = calc_ema(macd_line, 9)
    histogram = [m - s for m, s in zip(macd_line, signal_line)]
    return macd_line, signal_line, histogram

def calc_atr(candles: list, period: int = 14) -> list:
    if len(candles) < 2: return [0]
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]['h'], candles[i]['l'], candles[i-1]['c']
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    atr = [np.mean(trs[:period]) if len(trs) >= period else np.mean(trs)] * min(period, len(trs))
    for i in range(period, len(trs)):
        atr.append(np.mean(trs[i-period:i]))
    return [0] + atr

def calc_vwap(candles: list) -> list:
    vwap = []
    cum_vol, cum_tp_vol = 0, 0
    for c in candles:
        tp = (c['h'] + c['l'] + c['c']) / 3
        cum_vol += c['v']
        cum_tp_vol += tp * c['v']
        vwap.append(cum_tp_vol / cum_vol if cum_vol > 0 else tp)
    return vwap

def heikin_ashi(candles: list) -> list:
    ha = []
    for i, c in enumerate(candles):
        if i == 0:
            ha_c = (c['o'] + c['h'] + c['l'] + c['c']) / 4
            ha.append({'o': c['o'], 'c': ha_c})
        else:
            ha_c = (c['o'] + c['h'] + c['l'] + c['c']) / 4
            ha_o = (ha[-1]['o'] + ha[-1]['c']) / 2
            ha.append({'o': ha_o, 'c': ha_c})
    return ha


@dataclass
class TASignals:
    """All TA signals for a given asset at a point in time."""
    rsi: float = 50.0
    rsi_bull: bool = False      # RSI < 40 (oversold)
    rsi_bear: bool = False      # RSI > 60 (overbought)
    macd_bull: bool = False     # MACD histogram positive & rising
    macd_bear: bool = False     # MACD histogram negative & falling
    ema_bull: bool = False      # EMA9 > EMA21
    ema_bear: bool = False      # EMA9 < EMA21
    vwap_bull: bool = False     # Price > VWAP
    vwap_bear: bool = False     # Price < VWAP
    ha_bull: bool = False       # Green Heikin Ashi
    ha_bear: bool = False       # Red Heikin Ashi
    momentum_bull: bool = False # Price > 3 candles ago
    momentum_bear: bool = False # Price < 3 candles ago
    atr_pct: float = 0.0       # ATR as % of price
    price_move_pct: float = 0.0 # Current candle move %
    
    @property
    def bull_count(self) -> int:
        return sum([self.rsi_bull, self.macd_bull, self.ema_bull, 
                    self.vwap_bull, self.ha_bull, self.momentum_bull])
    
    @property
    def bear_count(self) -> int:
        return sum([self.rsi_bear, self.macd_bear, self.ema_bear,
                    self.vwap_bear, self.ha_bear, self.momentum_bear])
    
    def direction(self) -> Optional[str]:
        """Returns 'up', 'down', or None."""
        if self.bull_count >= MIN_BULL_SIGNALS:
            return "up"
        if self.bear_count >= MIN_BULL_SIGNALS:
            return "down"
        return None
    
    def summary(self) -> str:
        bulls = []
        bears = []
        if self.rsi_bull: bulls.append(f"RSI({self.rsi:.0f})")
        if self.rsi_bear: bears.append(f"RSI({self.rsi:.0f})")
        if self.macd_bull: bulls.append("MACD↑")
        if self.macd_bear: bears.append("MACD↓")
        if self.ema_bull: bulls.append("EMA↑")
        if self.ema_bear: bears.append("EMA↓")
        if self.vwap_bull: bulls.append("VWAP↑")
        if self.vwap_bear: bears.append("VWAP↓")
        if self.ha_bull: bulls.append("HA🟢")
        if self.ha_bear: bears.append("HA🔴")
        if self.momentum_bull: bulls.append("Mom↑")
        if self.momentum_bear: bears.append("Mom↓")
        return f"Bull({self.bull_count}): {','.join(bulls)} | Bear({self.bear_count}): {','.join(bears)}"


def get_binance_candles(symbol: str, limit: int = 30) -> list:
    """Fetch 5m candles from Binance."""
    try:
        r = requests.get("https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": "5m", "limit": limit}, timeout=5)
        return [{"ts": c[0]//1000, "o": float(c[1]), "h": float(c[2]),
                 "l": float(c[3]), "c": float(c[4]), "v": float(c[5])} for c in r.json()]
    except Exception:
        return []


def analyze_ta(candles: list) -> TASignals:
    """Run all TA indicators on candle data."""
    if len(candles) < 27:  # Need at least 26 for MACD
        return TASignals()
    
    closes = [c['c'] for c in candles]
    ta = TASignals()
    
    # RSI
    rsi_vals = calc_rsi(closes)
    ta.rsi = rsi_vals[-1] if rsi_vals else 50
    ta.rsi_bull = ta.rsi < 40
    ta.rsi_bear = ta.rsi > 60
    
    # MACD
    _, _, hist = calc_macd(closes)
    if len(hist) >= 2:
        ta.macd_bull = hist[-1] > 0 and hist[-1] > hist[-2]
        ta.macd_bear = hist[-1] < 0 and hist[-1] < hist[-2]
    
    # EMA 9/21
    ema9 = calc_ema(closes, 9)
    ema21 = calc_ema(closes, 21)
    ta.ema_bull = ema9[-1] > ema21[-1]
    ta.ema_bear = ema9[-1] < ema21[-1]
    
    # VWAP
    vwap = calc_vwap(candles)
    ta.vwap_bull = closes[-1] > vwap[-1]
    ta.vwap_bear = closes[-1] < vwap[-1]
    
    # Heikin Ashi
    ha = heikin_ashi(candles)
    ta.ha_bull = ha[-1]['c'] > ha[-1]['o']
    ta.ha_bear = ha[-1]['c'] < ha[-1]['o']
    
    # Simple momentum (3-candle)
    if len(closes) >= 4:
        ta.momentum_bull = closes[-1] > closes[-4]
        ta.momentum_bear = closes[-1] < closes[-4]
    
    # ATR
    atr = calc_atr(candles)
    ta.atr_pct = (atr[-1] / closes[-1] * 100) if closes[-1] > 0 and atr else 0
    
    # Current candle move
    ta.price_move_pct = (closes[-1] - candles[-1]['o']) / candles[-1]['o'] * 100
    
    return ta


# ═══════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS trades_v2 (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT,
        asset TEXT,
        strategy TEXT,
        direction TEXT,
        entry_price REAL,
        exit_price REAL,
        quantity REAL,
        cost REAL,
        pnl REAL,
        pnl_pct REAL,
        exit_reason TEXT,
        ta_summary TEXT,
        market_slug TEXT,
        order_id TEXT,
        token_id TEXT,
        condition_id TEXT,
        window_ts INTEGER,
        status TEXT DEFAULT 'open',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        closed_at TEXT
    )""")
    conn.commit()
    conn.close()


# ═══════════════════════════════════════════════════════════
# TRADER
# ═══════════════════════════════════════════════════════════

class TraderV2:
    def __init__(self):
        self.running = True
        self.client = None
        self.redeemer = None
        self.w3 = Web3(Web3.HTTPProvider("https://polygon.drpc.org"))
        self.ctf = self.w3.eth.contract(
            address=Web3.to_checksum_address(CTF_ADDRESS), abi=CTF_ABI)
        self.proxy_addr = None  # Set during init
        self.balance = 0.0
        self.daily_pnl = 0.0
        self.daily_date = None
        self.open_trades: List[dict] = []
        self.traded_windows: set = set()  # (asset, window_ts) pairs already traded
        
        # Stats
        self.wins = 0
        self.losses = 0
        self.total_pnl = 0.0
        
        init_db()
        signal.signal(signal.SIGINT, self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)
    
    def _shutdown(self, signum, frame):
        self.log("⏹️ Shutting down...")
        self.running = False
    
    def log(self, msg):
        ts = datetime.now(PST).strftime("%I:%M:%S %p")
        line = f"[{ts}] {msg}"
        print(line)
        try:
            with open(LOG_PATH, "a") as f:
                f.write(line + "\n")
        except Exception:
            pass
    
    def _init_client(self):
        config = dotenv_values(ENV_PATH)
        pk = config.get("POLYGON_PRIVATE_KEY", "")
        addr = config.get("POLYGON_WALLET_ADDRESS", "")
        host = "https://clob.polymarket.com"
        client = ClobClient(host, key=pk, chain_id=137)
        creds = client.create_or_derive_api_creds()
        self.client = ClobClient(host, key=pk, chain_id=137, creds=creds, 
                                  signature_type=1, funder=addr)
        self.proxy_addr = Web3.to_checksum_address(addr)
        try:
            self.redeemer = Redeemer(pk, addr)
            self.log("✅ Redeemer initialized")
        except Exception as e:
            self.log(f"⚠️ Redeemer failed: {e}")
    
    def _get_balance(self) -> float:
        try:
            bal = self.client.get_balance_allowance(
                BalanceAllowanceParams(asset_type="COLLATERAL"))
            raw = float(bal.get("balance", 0))
            return raw / 1e6 if raw > 1e6 else raw
        except Exception:
            return self.balance
    
    def _get_market(self, asset: str, window_ts: int) -> Optional[dict]:
        """Fetch Polymarket market data for an asset's 5-min window."""
        slug = f"{asset}-updown-5m-{window_ts}"
        try:
            r = requests.get(f"https://gamma-api.polymarket.com/events?slug={slug}", timeout=5)
            data = r.json()
            if not data: return None
            
            event = data[0]
            m = event['markets'][0]
            tokens = json.loads(m['clobTokenIds']) if isinstance(m['clobTokenIds'], str) else m['clobTokenIds']
            outcomes = json.loads(m['outcomes']) if isinstance(m['outcomes'], str) else m['outcomes']
            prices = json.loads(m['outcomePrices']) if isinstance(m['outcomePrices'], str) else m.get('outcomePrices', [])
            
            token_map = {}
            price_map = {}
            for i, outcome in enumerate(outcomes):
                key = outcome.lower()
                token_map[key] = tokens[i]
                price_map[key] = float(prices[i]) if i < len(prices) else 0.5
            
            # tick_size MUST be a string for PartialCreateOrderOptions
            raw_tick = m.get("orderPriceMinTickSize", 0.01)
            tick_str = str(raw_tick) if not isinstance(raw_tick, str) else raw_tick
            
            return {
                "slug": slug,
                "condition_id": m.get("conditionId", ""),
                "token_ids": token_map,
                "prices": price_map,
                "tick_size": tick_str,
                "neg_risk": m.get("negRisk", False),
                "closed": m.get("closed", False),
                "accepting_orders": m.get("acceptingOrders", True),
            }
        except Exception:
            return None
    
    def _get_onchain_balance(self, token_id: str) -> float:
        """Get on-chain token balance in shares (not raw)."""
        try:
            raw = self.ctf.functions.balanceOf(self.proxy_addr, int(token_id)).call()
            return raw / 1e6
        except Exception:
            return 0.0
    
    def _get_clob_token_balance(self, token_id: str) -> float:
        """Get CLOB's internal token balance (this is what matters for sells)."""
        try:
            bal = self.client.get_balance_allowance(
                BalanceAllowanceParams(asset_type="CONDITIONAL", token_id=token_id))
            raw = int(bal.get("balance", 0))
            return raw / 1e6
        except Exception:
            return 0.0
    
    def _get_best_bid(self, token_id: str) -> Optional[float]:
        """Get best bid for a token (for SL sell)."""
        try:
            book = requests.get(
                f"https://clob.polymarket.com/book?token_id={token_id}", timeout=3).json()
            bids = book.get("bids", [])
            return max(float(b["price"]) for b in bids) if bids else 0.0
        except Exception:
            return None
    
    def _place_buy(self, token_id: str, price: float, size: float, 
                    tick_size: str, neg_risk: bool) -> Optional[dict]:
        """Place a BUY order. Returns real fill price from on-chain data."""
        try:
            tick = float(tick_size)
            n_decimals = len(tick_size.split('.')[-1]) if '.' in tick_size else 0
            # Aggressive: pay 1 tick more to cross spread
            aggressive_price = min(round(round((price + tick) / tick) * tick, n_decimals), 0.99)
            
            order_args = OrderArgs(
                token_id=token_id, price=aggressive_price,
                size=round(size, 2), side=BUY)
            options = PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk)
            resp = self.client.create_and_post_order(order_args, options)
            
            if resp and resp.get("success"):
                # Extract REAL fill price from response
                taking = float(resp.get("takingAmount") or 0)  # shares received
                making = float(resp.get("makingAmount") or 0)  # USDC paid
                real_price = making / taking if taking > 0 else aggressive_price
                
                return {
                    "order_id": resp.get("orderID", ""),
                    "order_price": aggressive_price,  # what we asked for
                    "fill_price": round(real_price, 6),  # what we actually paid
                    "fill_size": round(taking, 6),  # shares actually received
                    "fill_cost": round(making, 6),  # USDC actually spent
                    "status": resp.get("status", ""),
                }
            self.log(f"  ⚠️ Order rejected: {resp}")
            return None
        except Exception as e:
            self.log(f"  ❌ Buy error: {e} | price={price:.4f} tick={tick_size} size={size:.2f}")
            return None
    
    def _place_sell(self, token_id: str, price: float, size: float,
                     tick_size: str, neg_risk: bool) -> Optional[dict]:
        """Place a SELL order (for TP/SL). Returns real fill price."""
        try:
            tick = float(tick_size)
            n_decimals = len(tick_size.split('.')[-1]) if '.' in tick_size else 0
            # Aggressive: sell 1 tick below to ensure fill
            sell_price = max(round(round((price - tick) / tick) * tick, n_decimals), tick)
            
            self.log(f"  📤 SELL {size:.1f} @ ${sell_price:.3f} (tick={tick_size})")
            
            order_args = OrderArgs(
                token_id=token_id, price=sell_price,
                size=round(size, 2), side=SELL)
            options = PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk)
            resp = self.client.create_and_post_order(order_args, options)
            
            if resp and resp.get("success"):
                taking = float(resp.get("takingAmount") or 0)  # USDC received
                making = float(resp.get("makingAmount") or 0)  # shares sold
                real_price = taking / making if making > 0 else sell_price
                
                return {
                    "order_id": resp.get("orderID", ""),
                    "order_price": sell_price,
                    "fill_price": round(real_price, 6),
                    "fill_size": round(making, 6),
                    "fill_cost": round(taking, 6),  # USDC received
                }
            self.log(f"  ⚠️ Sell rejected: {resp}")
            return None
        except Exception as e:
            self.log(f"  ❌ Sell error: {e}")
            return None
    
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
            except Exception:
                pass
        # Cancel unfilled order
        try:
            self.client.cancel_orders([order_id])
        except Exception:
            pass
        return {"filled": False}
    
    def _check_resolution(self, slug: str) -> Optional[dict]:
        """Check if a market has resolved."""
        try:
            r = requests.get(f"https://gamma-api.polymarket.com/events?slug={slug}", timeout=5)
            data = r.json()
            if not data: return None
            m = data[0]['markets'][0]
            prices = json.loads(m['outcomePrices']) if isinstance(m['outcomePrices'], str) else m.get('outcomePrices', [])
            if prices and (float(prices[0]) > 0.95 or float(prices[1]) > 0.95):
                winner = "up" if float(prices[0]) > 0.95 else "down"
                return {"resolved": True, "winner": winner}
            return {"resolved": False}
        except Exception:
            return None
    
    # ═══════════════════════════════════════════════════════
    # STRATEGIES
    # ═══════════════════════════════════════════════════════
    
    def _eval_momentum(self, asset: str, ta: TASignals, market: dict) -> Optional[dict]:
        """Momentum: trade when 3+ TA indicators agree on direction."""
        direction = ta.direction()
        if not direction:
            return None
        
        if abs(ta.price_move_pct) < MIN_MOVE_PCT:
            return None
        
        token_id = market["token_ids"].get(direction)
        entry_price = market["prices"].get(direction, 0.5)
        
        if not token_id or entry_price > 0.70 or entry_price < 0.30:
            return None
        
        return {
            "strategy": "momentum",
            "direction": direction,
            "token_id": token_id,
            "entry_price": entry_price,
            "ta": ta,
            "reason": f"🚀 {asset.upper()} {direction.upper()} | {ta.summary()} | Move: {ta.price_move_pct:+.3f}%",
        }
    
    def _eval_contrarian(self, asset: str, ta: TASignals, market: dict) -> Optional[dict]:
        """Contrarian: fade extreme odds when TA disagrees with the crowd."""
        up_price = market["prices"].get("up", 0.5)
        down_price = market["prices"].get("down", 0.5)
        
        # Check for extreme odds
        if up_price > CONTRARIAN_THRESHOLD:
            # Market heavily favors UP. Consider buying DOWN (underdog).
            # But only if TA suggests DOWN (crowd is wrong)
            if ta.bear_count >= MIN_TA_DISAGREE:
                return {
                    "strategy": "contrarian",
                    "direction": "down",
                    "token_id": market["token_ids"].get("down"),
                    "entry_price": down_price,
                    "ta": ta,
                    "reason": f"🔄 {asset.upper()} FADE UP@{up_price:.0%} → BUY DOWN@${down_price:.2f} | "
                             f"{ta.bear_count} bear signals disagree | {ta.summary()}",
                }
        
        elif down_price > CONTRARIAN_THRESHOLD:
            # Market heavily favors DOWN. Consider buying UP (underdog).
            if ta.bull_count >= MIN_TA_DISAGREE:
                return {
                    "strategy": "contrarian",
                    "direction": "up",
                    "token_id": market["token_ids"].get("up"),
                    "entry_price": up_price,
                    "ta": ta,
                    "reason": f"🔄 {asset.upper()} FADE DOWN@{down_price:.0%} → BUY UP@${up_price:.2f} | "
                             f"{ta.bull_count} bull signals disagree | {ta.summary()}",
                }
        
        return None
    
    # ═══════════════════════════════════════════════════════
    # TAKE-PROFIT & STOP-LOSS
    # ═══════════════════════════════════════════════════════
    
    def _check_tp_sl(self):
        """Monitor open positions for take-profit and stop-loss triggers."""
        resolved = []
        
        for i, trade in enumerate(self.open_trades):
            now_ts = int(time.time())
            window_end = trade["window_ts"] + 300
            
            # Only check during active window (before close)
            if now_ts >= window_end:
                continue
            
            # Skip TP/SL for complement fills (we don't actually hold the tokens)
            if not trade.get("real_fill", True):
                continue
            
            bid = self._get_best_bid(trade["token_id"])
            if bid is None:
                continue
            
            sl_price = trade["entry_price"] - SL_DROP
            
            # ── TAKE PROFIT ──
            if bid >= TP_PRICE and not trade.get("tp_attempted"):
                trade["tp_attempted"] = True
                profit_pct = (bid - trade["entry_price"]) / trade["entry_price"] * 100
                self.log(f"💰 TP HIT: {trade['asset'].upper()} {trade['direction'].upper()} | "
                        f"Entry ${trade['entry_price']:.3f} → Bid ${bid:.3f} (+{profit_pct:.0f}%)")
                
                # Query LIVE CLOB balance — this is what the sell checks against
                clob_bal = self._get_clob_token_balance(trade["token_id"])
                sell_qty = round(min(clob_bal, trade["quantity"]) - 0.01, 2)  # Round down slightly
                if sell_qty < 5.0:
                    self.log(f"  ⚠️ CLOB balance too low ({clob_bal:.2f}), skipping TP sell")
                    continue
                result = self._place_sell(
                    trade["token_id"], bid, sell_qty,
                    trade["tick_size"], trade["neg_risk"])
                
                if result and result.get("fill_cost", 0) > 0:
                    # Use real fill data from sell response
                    sell_price = result["fill_price"]
                    sell_got = result["fill_cost"]  # USDC received
                    pnl = sell_got - trade["cost"]  # Real P&L = USDC received - USDC spent
                    self.log(f"  🎯 SOLD @ ${sell_price:.3f} | Got ${sell_got:.2f} | PnL: ${pnl:+.2f}")
                    self._close_trade(trade, sell_price, pnl, "take_profit")
                    resolved.append(i)
                    self.balance += sell_got
                    continue
                elif result:
                    # Order placed but may not have filled yet — verify
                    fill = self._verify_fill(result["order_id"], max_wait=8)
                    if fill and fill.get("filled"):
                        pnl = (fill["price"] * fill["size"]) - trade["cost"]
                        self.log(f"  🎯 SOLD @ ${fill['price']:.3f} | PnL: ${pnl:+.2f}")
                        self._close_trade(trade, fill["price"], pnl, "take_profit")
                        resolved.append(i)
                        self.balance += fill["price"] * fill["size"]
                        continue
                    else:
                        self.log(f"  ⚠️ TP sell not filled — will retry next check")
                        trade["tp_attempted"] = False
                        continue
                else:
                    self.log(f"  ⚠️ TP sell failed — will retry next check")
                    trade["tp_attempted"] = False
                    continue
            
            # ── STOP LOSS ──
            if bid <= sl_price and not trade.get("sl_attempted"):
                trade["sl_attempted"] = True
                self.log(f"🛑 SL HIT: {trade['asset'].upper()} {trade['direction'].upper()} | "
                        f"Entry ${trade['entry_price']:.3f} → Bid ${bid:.3f}")
                
                # Query LIVE CLOB balance
                clob_bal = self._get_clob_token_balance(trade["token_id"])
                sell_qty = round(min(clob_bal, trade["quantity"]) - 0.01, 2)  # Round down
                if sell_qty < 5.0:
                    self.log(f"  ⚠️ CLOB balance too low ({clob_bal:.2f}), can't SL sell")
                    continue
                result = self._place_sell(
                    trade["token_id"], bid, sell_qty,
                    trade["tick_size"], trade["neg_risk"])
                
                if result and result.get("fill_cost", 0) > 0:
                    sell_price = result["fill_price"]
                    sell_got = result["fill_cost"]  # USDC received
                    pnl = sell_got - trade["cost"]  # Real P&L
                    saved = trade["cost"] - abs(pnl) if pnl < 0 else trade["cost"] + pnl
                    self.log(f"  💔 Sold @ ${sell_price:.3f} | Got ${sell_got:.2f} | PnL: ${pnl:+.2f} | Saved ${saved:.2f} vs full loss")
                    self._close_trade(trade, sell_price, pnl, "stop_loss")
                    resolved.append(i)
                    self.balance += sell_got
                elif result:
                    fill = self._verify_fill(result["order_id"], max_wait=8)
                    if fill and fill.get("filled"):
                        pnl = (fill["price"] * fill["size"]) - trade["cost"]
                        self.log(f"  💔 Sold @ ${fill['price']:.3f} | PnL: ${pnl:+.2f}")
                        self._close_trade(trade, fill["price"], pnl, "stop_loss")
                        resolved.append(i)
                        self.balance += fill["price"] * fill["size"]
                    else:
                        self.log(f"  ⚠️ SL sell not filled — holding to resolution")
                else:
                    self.log(f"  ⚠️ SL sell failed — holding to resolution")
        
        for idx in sorted(resolved, reverse=True):
            self.open_trades.pop(idx)
    
    def _check_resolutions(self):
        """Check open trades for market resolution."""
        resolved = []
        
        for i, trade in enumerate(self.open_trades):
            now_ts = int(time.time())
            window_end = trade["window_ts"] + 300
            
            if now_ts < window_end + 60:  # Wait at least 60s after close
                continue
            
            result = self._check_resolution(trade["market_slug"])
            if not result or not result.get("resolved"):
                # Auto-clean after 30 min
                if now_ts > window_end + 1800:
                    self.log(f"🧹 Auto-cleaning stuck trade: {trade['asset'].upper()} {trade['direction'].upper()}")
                    self._close_trade(trade, 0, -trade["cost"], "stuck_timeout")
                    resolved.append(i)
                continue
            
            won = trade["direction"] == result["winner"]
            if won:
                pnl = trade["quantity"] * (1.0 - trade["entry_price"])
                self.wins += 1
                self.log(f"✅ WIN: {trade['asset'].upper()} {trade['direction'].upper()} | "
                        f"+${pnl:.2f} | Entry ${trade['entry_price']:.3f}")
            else:
                pnl = -trade["cost"]
                self.losses += 1
                self.log(f"❌ LOSS: {trade['asset'].upper()} {trade['direction'].upper()} | "
                        f"-${abs(pnl):.2f} | Entry ${trade['entry_price']:.3f}")
            
            self._close_trade(trade, 1.0 if won else 0.0, pnl, "win" if won else "loss")
            
            # Auto-redeem
            if self.redeemer and trade.get("condition_id"):
                try:
                    self.redeemer.redeem(trade["condition_id"])
                except Exception:
                    pass
            
            resolved.append(i)
        
        for idx in sorted(resolved, reverse=True):
            self.open_trades.pop(idx)
    
    def _close_trade(self, trade: dict, exit_price: float, pnl: float, reason: str):
        """Record trade close in DB."""
        self.total_pnl += pnl
        self.daily_pnl += pnl
        
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute("""UPDATE trades_v2 SET exit_price=?, pnl=?, pnl_pct=?,
                           exit_reason=?, status='closed', closed_at=? WHERE id=?""",
                        (exit_price, pnl, (pnl / trade["cost"] * 100) if trade["cost"] else 0,
                         reason, datetime.now(PST).isoformat(), trade["id"]))
            conn.commit()
            conn.close()
        except Exception as e:
            self.log(f"  ⚠️ DB error: {e}")
    
    # ═══════════════════════════════════════════════════════
    # EXECUTION
    # ═══════════════════════════════════════════════════════
    
    def _execute_trade(self, signal: dict, asset: str, market: dict, window_ts: int):
        """Execute a trade from a signal."""
        direction = signal["direction"]
        token_id = signal["token_id"]
        
        # Use ACTUAL best ask from CLOB book, not Gamma probability
        try:
            book = requests.get(f"https://clob.polymarket.com/book?token_id={token_id}", timeout=3).json()
            asks = book.get("asks", [])
            if asks:
                entry_price = min(float(a["price"]) for a in asks)
            else:
                entry_price = signal["entry_price"]  # Fallback
                self.log(f"  ⚠️ No asks in book, using Gamma price ${entry_price:.3f}")
        except:
            entry_price = signal["entry_price"]
        
        if entry_price >= 0.99 or entry_price <= 0.01:
            self.log(f"  ⚠️ Bad entry price ${entry_price:.3f}, skipping")
            return
        
        # Note: complement fill detection now works correctly (was broken with asset_type=1)
        
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
        
        # Use REAL fill data from the CLOB response (not order price!)
        if order.get("status") == "matched" and order.get("fill_size", 0) > 0:
            # Immediate fill — use response data directly
            fill_price = order["fill_price"]
            fill_size = order["fill_size"]
            fill_cost = order["fill_cost"]
            self.log(f"  📍 Immediate fill: {fill_size:.1f} @ ${fill_price:.4f} = ${fill_cost:.2f} "
                    f"(order was ${order.get('order_price', 0):.3f})")
        else:
            # Not immediately matched — wait for fill via get_order
            # But first check if we have fill data from _place_buy
            if order.get("fill_size", 0) > 0:
                fill_price = order["fill_price"]
                fill_size = order["fill_size"]
                fill_cost = order["fill_cost"]
                self.log(f"  📍 Fill from response: {fill_size:.1f} @ ${fill_price:.4f} = ${fill_cost:.2f}")
            else:
                fill = self._verify_fill(order["order_id"])
                if not fill or not fill.get("filled"):
                    self.log(f"  ⚠️ Order not filled — cancelled")
                    return
                # _verify_fill returns ORDER price, not fill price
                # Query on-chain activity for real price
                fill_size = fill["size"]
                fill_price = fill["price"]  # This is order price — will fix below
                fill_cost = fill_price * fill_size
                self.log(f"  ⚠️ Using order price ${fill_price:.3f} (verify_fill doesn't have real fill price)")
        
        # Complement detection: use CLOB balance — needs 3s to settle after fill
        time.sleep(3)
        clob_shares = self._get_clob_token_balance(token_id)
        if clob_shares < 0.1:
            time.sleep(2)
            clob_shares = self._get_clob_token_balance(token_id)
        onchain_shares = clob_shares
        is_real_fill = clob_shares >= fill_size * 0.5
        
        if is_real_fill:
            self.log(f"  ✅ FILLED: {fill_size:.1f} @ ${fill_price:.3f} = ${fill_cost:.2f} | "
                    f"CLOB balance: {clob_shares:.1f} ✅")
        else:
            self.log(f"  ⚠️ COMPLEMENT: CLOB has {clob_shares:.2f}/{fill_size:.1f} | "
                    f"Cost ${fill_cost:.2f} — TP/SL disabled")
        
        # Record in DB with REAL fill price
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.execute("""INSERT INTO trades_v2 
                (timestamp, asset, strategy, direction, entry_price, quantity, cost,
                 ta_summary, market_slug, order_id, token_id, condition_id, window_ts, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')""",
                (datetime.now(PST).isoformat(), asset, signal["strategy"],
                 direction, fill_price, fill_size, fill_cost,
                 signal.get("ta", TASignals()).summary(), market["slug"],
                 order["order_id"], token_id, market["condition_id"], window_ts))
            trade_id = cursor.lastrowid
            conn.commit()
            conn.close()
        except Exception as e:
            self.log(f"  ⚠️ DB insert error: {e}")
            trade_id = 0
        
        # Track open position
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
        print("🏞 The Outsiders v2 — Clean Trading Engine")
        print("=" * 60)
        print(f"💰 LIVE MODE | Trade size: ${TRADE_SIZE}")
        print(f"🛡️ SL: ${SL_DROP} drop | Max daily loss: ${MAX_DAILY_LOSS}")
        print(f"📊 Assets: {', '.join(a.upper() for a in ASSETS)}")
        print(f"🚀 Momentum: {MIN_BULL_SIGNALS}+ indicators | Min move: {MIN_MOVE_PCT}%")
        print(f"🔄 Contrarian: Fade >{CONTRARIAN_THRESHOLD:.0%} odds")
        print("=" * 60)
        
        self._init_client()
        self.balance = self._get_balance()
        self.log(f"💰 USDC: ${self.balance:.2f} (use True portfolio for real balance)")
        
        last_balance_check = 0
        
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
                
                # Circuit breaker
                if self.daily_pnl < -MAX_DAILY_LOSS:
                    if secs_left % 60 < 2:
                        self.log(f"🚨 CIRCUIT BREAKER: Daily loss ${self.daily_pnl:.2f} > ${MAX_DAILY_LOSS}")
                    time.sleep(1)
                    continue
                
                # Check SL on open positions
                self._check_tp_sl()
                
                # Check resolutions
                self._check_resolutions()
                
                # Bad hour check
                hour = datetime.now(PST).hour
                if hour in BAD_HOURS:
                    time.sleep(10)
                    continue
                
                # Refresh balance every 2 min
                if now - last_balance_check > 120:
                    self.balance = self._get_balance()
                    last_balance_check = now
                
                # Scan all assets for signals in current window
                # Only run once per window per asset (when ~2-3 min into window for TA data)
                if 120 < secs_left < 180:  # Fire at ~2-3 min remaining
                    for asset in ASSETS:
                        key = (asset, current_window)
                        if key in self.traded_windows:
                            continue
                        
                        if len(self.open_trades) >= MAX_CONCURRENT:
                            break
                        
                        self.traded_windows.add(key)
                        
                        # Get TA data
                        candles = get_binance_candles(BINANCE_SYMBOLS[asset])
                        if not candles or len(candles) < 27:
                            continue
                        
                        ta = analyze_ta(candles)
                        
                        # Get market data
                        market = self._get_market(asset, current_window)
                        if not market or market.get("closed"):
                            continue
                        
                        # Evaluate strategies
                        signals = []
                        
                        mom = self._eval_momentum(asset, ta, market)
                        if mom: signals.append(mom)
                        
                        con = self._eval_contrarian(asset, ta, market)
                        if con: signals.append(con)
                        
                        if not signals:
                            if asset == "btc":  # Only log BTC to reduce noise
                                self.log(f"  {asset.upper()}: {ta.summary()} | Move: {ta.price_move_pct:+.3f}% | No signal")
                            continue
                        
                        # Execute best signal
                        best = signals[0]  # Momentum preferred over contrarian
                        self.log(f"  {best['reason']}")
                        self._execute_trade(best, asset, market, current_window)
                
                # Status update every 5 min
                if secs_left % 300 < 2 and secs_left < 5:
                    wr = self.wins / (self.wins + self.losses) * 100 if (self.wins + self.losses) > 0 else 0
                    self.log(f"📊 {self.wins}W-{self.losses}L ({wr:.0f}%) | "
                            f"PnL: ${self.total_pnl:+.2f} | Today: ${self.daily_pnl:+.2f} | "
                            f"Bal: ${self.balance:.2f} | Open: {len(self.open_trades)}")
                
                # Clean old window keys (keep memory bounded)
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
        print(f"🏞 SESSION SUMMARY")
        print(f"Trades: {total} ({self.wins}W-{self.losses}L) | WR: {wr:.1f}%")
        print(f"Total PnL: ${self.total_pnl:+.2f}")
        print(f"Balance: ${self.balance:.2f}")
        print(f"{'='*60}")


if __name__ == "__main__":
    trader = TraderV2()
    trader.run()
