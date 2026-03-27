"""
Copy Trader — Live Execution Engine

Places real orders on the Polymarket CLOB.
Handles: BUY entry, SELL exit (leader exit), and redemption.
"""
import os
import time
import logging
import requests
from typing import Optional

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    OrderArgs, PartialCreateOrderOptions, BalanceAllowanceParams
)
from py_clob_client.order_builder.constants import BUY, SELL

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════
CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon

# Fee estimation (conservative)
TAKER_FEE_PCT = 0.02  # ~2% taker fee on Polymarket


class Executor:
    """Handles live order placement on Polymarket CLOB."""

    def __init__(self):
        pk = os.environ.get("POLYGON_PRIVATE_KEY", "")
        addr = os.environ.get("POLYGON_WALLET_ADDRESS", "")

        if not pk or not addr:
            raise RuntimeError("Missing POLYGON_PRIVATE_KEY or POLYGON_WALLET_ADDRESS")

        # Init CLOB client with API creds
        client = ClobClient(CLOB_HOST, key=pk, chain_id=CHAIN_ID)
        creds = client.create_or_derive_api_creds()
        self.client = ClobClient(
            CLOB_HOST, key=pk, chain_id=CHAIN_ID,
            creds=creds, signature_type=1, funder=addr
        )
        self.proxy_addr = addr
        logger.info(f"✅ Executor initialized | Proxy: {addr[:10]}...")

    # ─── Balance ──────────────────────────────────────

    def get_balance(self) -> float:
        """Get USDC balance."""
        try:
            bal = self.client.get_balance_allowance(
                BalanceAllowanceParams(asset_type="COLLATERAL")
            )
            raw = float(bal.get("balance", 0))
            return raw / 1e6 if raw > 1e6 else raw
        except Exception as e:
            logger.error(f"Balance check failed: {e}")
            return 0.0

    # ─── Market Data ──────────────────────────────────

    def get_book(self, token_id: str) -> dict:
        """Get order book for a token."""
        try:
            r = requests.get(
                f"{CLOB_HOST}/book",
                params={"token_id": token_id},
                timeout=5
            )
            return r.json() if r.status_code == 200 else {}
        except Exception as e:
            logger.error(f"Book fetch failed: {e}")
            return {}

    def get_best_ask(self, token_id: str) -> Optional[float]:
        """Get best ask price for a token."""
        book = self.get_book(token_id)
        asks = book.get("asks", [])
        if not asks:
            return None
        return min(float(a["price"]) for a in asks)

    def get_best_bid(self, token_id: str) -> Optional[float]:
        """Get best bid price for a token."""
        book = self.get_book(token_id)
        bids = book.get("bids", [])
        if not bids:
            return None
        return max(float(b["price"]) for b in bids)

    def get_tick_size(self, token_id: str) -> str:
        """Get tick size for a market from CLOB."""
        try:
            # Try to get from market info
            r = requests.get(
                f"{CLOB_HOST}/markets",
                params={"token_id": token_id},
                timeout=5
            )
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict):
                    ts = data.get("minimum_tick_size", "0.01")
                    return str(ts)
            return "0.01"  # Default
        except:
            return "0.01"

    def get_neg_risk(self, condition_id: str) -> bool:
        """Check if market uses neg_risk."""
        try:
            r = requests.get(
                f"{CLOB_HOST}/markets/{condition_id}",
                timeout=5
            )
            if r.status_code == 200:
                return r.json().get("neg_risk", False)
            return False
        except:
            return False

    # ─── Order Placement ──────────────────────────────

    def buy(self, token_id: str, condition_id: str, usdc_amount: float,
            max_price: float = 0.95, slippage: float = 0.03) -> Optional[dict]:
        """
        Place a BUY order. Aggressive pricing to ensure fill.

        Args:
            token_id: Token to buy
            condition_id: Market condition ID
            usdc_amount: How much USDC to spend
            max_price: Don't buy above this price
            slippage: Max slippage above best ask

        Returns:
            Fill info dict or None on failure
        """
        try:
            # Get market params
            tick_size = self.get_tick_size(token_id)
            neg_risk = self.get_neg_risk(condition_id)
            tick = float(tick_size)
            n_dec = len(tick_size.split('.')[-1]) if '.' in tick_size else 2

            # Get best ask
            best_ask = self.get_best_ask(token_id)
            if best_ask is None:
                logger.warning(f"  ⚠️ No asks in book for {token_id[:12]}...")
                return None

            # Aggressive price: best ask + 1 tick (to ensure fill)
            aggressive_price = round(
                round((best_ask + tick) / tick) * tick, n_dec
            )

            # Safety checks
            if aggressive_price > max_price:
                logger.warning(f"  ⚠️ Price ${aggressive_price:.3f} > max ${max_price:.3f}")
                return None

            if aggressive_price > best_ask * (1 + slippage):
                logger.warning(f"  ⚠️ Slippage too high: ask=${best_ask:.3f}, our=${aggressive_price:.3f}")
                return None

            # Calculate size in shares
            size = usdc_amount / aggressive_price
            size = round(size, 2)

            if size < 1:
                logger.warning(f"  ⚠️ Size too small: {size} shares")
                return None

            # Check balance
            balance = self.get_balance()
            cost = size * aggressive_price
            if cost > balance:
                logger.warning(f"  ⚠️ Insufficient balance: need ${cost:.2f}, have ${balance:.2f}")
                return None

            logger.info(f"  💰 BUY {size:.1f}sh @ ${aggressive_price:.3f} "
                        f"(ask=${best_ask:.3f}, tick={tick_size})")

            # Place order
            order_args = OrderArgs(
                token_id=token_id,
                price=aggressive_price,
                size=size,
                side=BUY
            )
            options = PartialCreateOrderOptions(
                tick_size=tick_size,
                neg_risk=neg_risk
            )
            resp = self.client.create_and_post_order(order_args, options)

            if resp and resp.get("success"):
                taking = float(resp.get("takingAmount") or 0)  # shares
                making = float(resp.get("makingAmount") or 0)  # USDC

                # Guard against zero-fill: CLOB can return success with 0 shares
                # But verify on-chain — CLOB sometimes lies about takingAmount
                # The data API has indexing lag, so we retry multiple times
                if taking < 0.01:
                    order_id = resp.get('orderID', '?')
                    logger.warning(f"  ⚠️ Zero-fill response: takingAmount={taking} "
                                   f"(orderID={order_id}). Verifying...")

                    # Strategy: check order status first (faster), then positions API
                    # Retry with increasing delays to handle data API indexing lag
                    verified = False
                    our_wallet = (os.environ.get("POLYMARKET_PROXY_WALLET", "") or 
                                  os.environ.get("POLYGON_WALLET_ADDRESS", "")).lower()

                    for attempt, delay in enumerate([2, 3, 5, 5], 1):
                        time.sleep(delay)

                        # Method 1: Check order status via CLOB
                        try:
                            if order_id and order_id != '?':
                                order_status = self.client.get_order(order_id)
                                if order_status:
                                    sm = float(order_status.get("size_matched", 0) or 0)
                                    if sm > 0.01:
                                        taking = sm
                                        making = sm * aggressive_price  # Estimate
                                        logger.info(f"  ✅ Order status check (attempt {attempt}): "
                                                   f"size_matched={sm:.1f}sh")
                                        verified = True
                                        break
                        except Exception as e:
                            logger.debug(f"  Order status check failed: {e}")

                        # Method 2: Check positions API (has indexing lag)
                        try:
                            r = requests.get("https://data-api.polymarket.com/positions",
                                            params={"user": our_wallet, "sizeThreshold": 0, "limit": 100},
                                            timeout=10)
                            for pos in r.json():
                                if pos.get("conditionId") == condition_id:
                                    chain_shares = float(pos.get("size", 0) or 0)
                                    chain_iv = float(pos.get("initialValue", 0) or 0)
                                    if chain_shares > 0.01:
                                        taking = chain_shares
                                        making = chain_iv
                                        real_price = making / taking if taking > 0 else aggressive_price
                                        logger.info(f"  ✅ On-chain verification (attempt {attempt}): "
                                                   f"ACTUALLY filled! {taking:.1f}sh, ${making:.2f} invested")
                                        verified = True
                                        break
                            if verified:
                                break
                        except Exception as e:
                            logger.debug(f"  Positions API check failed: {e}")

                        logger.info(f"  ⏳ Verification attempt {attempt}/4 — not found yet, retrying...")

                    if not verified:
                        logger.warning(f"  ⚠️ Confirmed zero-fill after 4 checks (~15s) — no position on-chain")
                        return None

                real_price = making / taking if taking > 0 else aggressive_price

                fill = {
                    "order_id": resp.get("orderID", ""),
                    "fill_price": round(real_price, 6),
                    "fill_shares": round(taking, 6),
                    "fill_cost": round(making, 6),
                    "order_price": aggressive_price,
                    "best_ask": best_ask,
                    "fee_estimate": round(making * TAKER_FEE_PCT, 4),
                }
                logger.info(f"  ✅ FILLED: {fill['fill_shares']:.1f}sh @ "
                            f"${fill['fill_price']:.3f} (cost ${fill['fill_cost']:.2f}, "
                            f"~${fill['fee_estimate']:.2f} fee)")
                return fill
            else:
                logger.warning(f"  ❌ Order rejected: {resp}")
                return None

        except Exception as e:
            logger.error(f"  ❌ BUY error: {e}")
            return None

    def sell(self, token_id: str, condition_id: str, shares: float,
             min_price: float = 0.01) -> Optional[dict]:
        """
        Place a SELL order. Aggressive pricing to ensure fill.

        Args:
            token_id: Token to sell
            condition_id: Market condition ID
            shares: Number of shares to sell
            min_price: Don't sell below this price

        Returns:
            Fill info dict or None on failure
        """
        try:
            tick_size = self.get_tick_size(token_id)
            neg_risk = self.get_neg_risk(condition_id)
            tick = float(tick_size)
            n_dec = len(tick_size.split('.')[-1]) if '.' in tick_size else 2

            # Get best bid
            best_bid = self.get_best_bid(token_id)
            if best_bid is None:
                logger.warning(f"  ⚠️ No bids in book for {token_id[:12]}...")
                return None

            # Aggressive price: best bid - 1 tick (to ensure fill)
            aggressive_price = max(
                round(round((best_bid - tick) / tick) * tick, n_dec),
                tick  # Floor at 1 tick
            )

            if aggressive_price < min_price:
                logger.warning(f"  ⚠️ Price ${aggressive_price:.3f} < min ${min_price:.3f}")
                return None

            size = round(shares, 2)

            logger.info(f"  📤 SELL {size:.1f}sh @ ${aggressive_price:.3f} "
                        f"(bid=${best_bid:.3f}, tick={tick_size})")

            order_args = OrderArgs(
                token_id=token_id,
                price=aggressive_price,
                size=size,
                side=SELL
            )
            options = PartialCreateOrderOptions(
                tick_size=tick_size,
                neg_risk=neg_risk
            )
            resp = self.client.create_and_post_order(order_args, options)

            if resp and resp.get("success"):
                taking = float(resp.get("takingAmount") or 0)  # USDC received
                making = float(resp.get("makingAmount") or 0)  # shares sold
                real_price = taking / making if making > 0 else aggressive_price

                fill = {
                    "order_id": resp.get("orderID", ""),
                    "fill_price": round(real_price, 6),
                    "fill_shares": round(making, 6),
                    "usdc_received": round(taking, 6),
                    "order_price": aggressive_price,
                    "best_bid": best_bid,
                    "fee_estimate": round(taking * TAKER_FEE_PCT, 4),
                }
                logger.info(f"  ✅ SOLD: {fill['fill_shares']:.1f}sh @ "
                            f"${fill['fill_price']:.3f} (received ${fill['usdc_received']:.2f})")
                return fill
            else:
                logger.warning(f"  ❌ Sell rejected: {resp}")
                return None

        except Exception as e:
            logger.error(f"  ❌ SELL error: {e}")
            return None

    def verify_fill(self, order_id: str, max_wait: int = 12) -> Optional[dict]:
        """Wait for order fill confirmation. Returns order status."""
        for _ in range(max_wait):
            time.sleep(1)
            try:
                order = self.client.get_order(order_id)
                if order:
                    status = order.get("status", "")
                    size_matched = float(order.get("size_matched", 0) or 0)
                    if size_matched > 0:
                        return {
                            "status": status,
                            "size_matched": size_matched,
                            "original_size": float(order.get("original_size", 0) or 0),
                        }
                    if status in ("CANCELLED", "EXPIRED"):
                        return {"status": status, "size_matched": 0}
            except Exception:
                pass
        return None
