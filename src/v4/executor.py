"""
Component 4 — Execution Engine
================================
Limit orders only. Never cross the spread. 
Place orders at our target price and wait.

Key principles:
- Maker only (0% fees vs 1-2% taker)
- Order staleness detection (cancel after 5 min if unfilled)
- No chasing — if market doesn't come to us, we don't trade
"""

import time
import requests
import json
from datetime import datetime
from typing import Optional, Dict, List
from dotenv import dotenv_values

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, PartialCreateOrderOptions, BalanceAllowanceParams
from py_clob_client.order_builder.constants import BUY, SELL

from .config import *
from .edge_detector import EdgeSignal


class OrderState:
    """Track a single order's lifecycle."""
    def __init__(self, signal: EdgeSignal, order_id: str, price: float, 
                 size: float, placed_at: float):
        self.signal = signal
        self.order_id = order_id
        self.price = price
        self.size = size  # In shares
        self.usd_size = price * size
        self.placed_at = placed_at
        self.status = "open"       # open, filled, cancelled, expired
        self.fill_price = None
        self.fill_shares = None


class Executor:
    """Manages order placement and lifecycle with limit-only execution."""
    
    def __init__(self):
        self.client = None
        self.proxy_addr = None
        self.open_orders: List[OrderState] = []
        self._init_client()
    
    def _init_client(self):
        """Initialize CLOB client."""
        config = dotenv_values(ENV_PATH)
        pk = config.get("POLYGON_PRIVATE_KEY", "")
        addr = config.get("POLYGON_WALLET_ADDRESS", "")
        
        self.client = ClobClient(CLOB_API, key=pk, chain_id=137)
        creds = self.client.create_or_derive_api_creds()
        self.client = ClobClient(
            CLOB_API, key=pk, chain_id=137,
            creds=creds, signature_type=1, funder=addr
        )
        self.proxy_addr = self.client.get_address()
    
    def get_balance(self) -> float:
        """Get USDC balance."""
        try:
            bal = self.client.get_balance_allowance(
                BalanceAllowanceParams(asset_type="COLLATERAL"))
            raw = float(bal.get("balance", 0))
            return raw / 1e6 if raw > 1000 else raw
        except:
            return 0.0
    
    def place_limit_buy(self, signal: EdgeSignal) -> Optional[OrderState]:
        """
        Place a LIMIT BUY order at our target price.
        We post on the bid side and wait for someone to sell to us.
        
        This is MAKER execution = 0% fees.
        """
        price = signal.market_price  # Buy at current market price
        
        # Actually, we want to buy BELOW market to capture our edge
        # Place bid at: market_price - (edge / 2)
        # This way we're buying at a price that still gives us edge
        target_price = round(signal.market_price - (signal.edge_pct / 2), 2)
        target_price = max(target_price, 0.01)
        
        # Calculate shares
        shares = signal.recommended_size / target_price
        
        tick_size = signal.market.tick_size
        tick = float(tick_size)
        n_decimals = len(tick_size.split('.')[-1]) if '.' in tick_size else 2
        target_price = round(round(target_price / tick) * tick, n_decimals)
        
        try:
            order = self.client.create_and_post_order(
                OrderArgs(
                    token_id=signal.token_id,
                    price=target_price,
                    size=round(shares, 2),
                    side=BUY,
                ),
                PartialCreateOrderOptions(
                    tick_size=tick_size,
                    neg_risk=signal.market.neg_risk,
                )
            )
            
            order_id = order.get("orderID") or order.get("id", "")
            
            state = OrderState(
                signal=signal,
                order_id=order_id,
                price=target_price,
                size=round(shares, 2),
                placed_at=time.time(),
            )
            self.open_orders.append(state)
            
            return state
            
        except Exception as e:
            print(f"Order placement failed: {e}")
            return None
    
    def check_fills(self) -> List[OrderState]:
        """Check if any open orders have been filled."""
        filled = []
        still_open = []
        
        for order in self.open_orders:
            if order.status != "open":
                continue
            
            try:
                # Check order status via CLOB
                resp = self.client.get_order(order.order_id)
                status = resp.get("status", "")
                
                if status == "MATCHED" or status == "FILLED":
                    order.status = "filled"
                    order.fill_price = float(resp.get("price", order.price))
                    order.fill_shares = float(resp.get("size_matched", order.size))
                    filled.append(order)
                elif status in ("CANCELLED", "EXPIRED"):
                    order.status = "cancelled"
                else:
                    still_open.append(order)
            except:
                still_open.append(order)
        
        self.open_orders = still_open + filled
        return filled
    
    def check_stale_orders(self) -> List[OrderState]:
        """Cancel orders that have been open too long."""
        cancelled = []
        now = time.time()
        
        for order in self.open_orders:
            if order.status != "open":
                continue
            
            if now - order.placed_at > ORDER_STALE_SECONDS:
                try:
                    self.client.cancel(order.order_id)
                    order.status = "cancelled"
                    cancelled.append(order)
                except:
                    pass
        
        self.open_orders = [o for o in self.open_orders if o.status == "open"]
        return cancelled
    
    def place_limit_sell(self, token_id: str, shares: float, price: float,
                         tick_size: str = "0.01", neg_risk: bool = False) -> Optional[str]:
        """
        Place a LIMIT SELL order.
        Used for exiting positions at our target price.
        """
        tick = float(tick_size)
        n_decimals = len(tick_size.split('.')[-1]) if '.' in tick_size else 2
        price = round(round(price / tick) * tick, n_decimals)
        
        try:
            resp = self.client.create_and_post_order(
                OrderArgs(
                    token_id=token_id,
                    price=price,
                    size=round(shares - 0.01, 2),
                    side=SELL,
                ),
                PartialCreateOrderOptions(
                    tick_size=tick_size,
                    neg_risk=neg_risk,
                )
            )
            return resp.get("orderID") or resp.get("id", "")
        except Exception as e:
            print(f"Sell failed: {e}")
            return None
