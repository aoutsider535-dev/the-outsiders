"""
🎯 The Outsiders — SNIPER BOT v3

Post-close arbitrage on Polymarket 5-min up/down markets.
Strategy: After the 5-min price window ends but before trading closes (~20-50s gap),
BUY the winning token at $0.99 for guaranteed ~1% profit.

KEY INSIGHT: Both UP and DOWN tokens have full orderbook depth (~14K+ shares at $0.99)
during the active trading window. The book only clears when the market officially closes.
We must fire WITHIN the gap between price observation end and market close.

Assets: BTC, ETH, SOL, XRP
"""
import time
import json
import os
import sys
import sqlite3
import requests
import signal
from datetime import datetime, timezone, timedelta
from dotenv import dotenv_values

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, PartialCreateOrderOptions, BalanceAllowanceParams
from py_clob_client.order_builder.constants import BUY
from src.chainlink import ChainlinkReader, CHAINLINK_FEEDS

PST = timezone(timedelta(hours=-8))
ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "sniper.db")
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"

SWEEP_INTERVAL = 600  # Sweep positions every 10 minutes

# --- Configuration ---
ASSETS = {
    "btc": {"binance": "BTCUSDT", "slug_prefix": "btc-updown-5m"},
    "eth": {"binance": "ETHUSDT", "slug_prefix": "eth-updown-5m"},
    "sol": {"binance": "SOLUSDT", "slug_prefix": "sol-updown-5m"},
    "xrp": {"binance": "XRPUSDT", "slug_prefix": "xrp-updown-5m"},
}

MAX_BUY_PRICE = 0.995       # Max price to pay per share
MIN_MARGIN = 0.003           # Min profit margin (1 - price)
SNIPE_DELAY = 1              # 1s after window end (Binance candle needs to close)
MAX_SNIPE_WINDOW = 12        # Max seconds (tighter — if we haven't filled by +12s, Sharky already won)
MIN_PRICE_MOVE_PCT = 0.03    # 0.03% — Binance=100% accuracy at >=0.02%, buffer for safety
PAPER_MODE = False
DEFAULT_TRADE_SIZE = 50.0


class SniperBot:
    def __init__(self, paper_mode=None, trade_size=None, max_price=None):
        self.paper_mode = paper_mode if paper_mode is not None else PAPER_MODE
        self.trade_size = trade_size or DEFAULT_TRADE_SIZE
        self.max_buy_price = max_price or MAX_BUY_PRICE
        self.client = None
        self.balance = 0
        self.running = True
        self.sniped_markets = set()
        self.stats = {"attempts": 0, "fills": 0, "misses": 0, "total_cost": 0, "total_payout": 0, "skipped": 0}
        self.preloaded = {}  # {asset: {market_data, open_price, window_ts}}
        self.redeemer = None
        self._last_sweep = 0
        self.chainlink = ChainlinkReader()
        self._init_db()

    def log(self, msg):
        ts = datetime.now(PST).strftime("%H:%M:%S")
        print(f"[{ts}] {msg}", flush=True)
        log_path = os.path.join(os.path.dirname(DB_PATH), "sniper.log")
        try:
            with open(log_path, "a") as f:
                f.write(f"[{datetime.now(PST).strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
        except:
            pass

    def _init_db(self):
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sniper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                asset TEXT NOT NULL,
                market_slug TEXT NOT NULL,
                window_start_ts INTEGER NOT NULL,
                outcome TEXT NOT NULL,
                open_price REAL,
                close_price REAL,
                buy_price REAL NOT NULL,
                shares REAL NOT NULL,
                cost_usdc REAL NOT NULL,
                expected_profit REAL NOT NULL,
                order_id TEXT,
                status TEXT DEFAULT 'filled',
                paper INTEGER DEFAULT 0,
                reconciled INTEGER DEFAULT NULL
            )
        """)
        # Add reconciled column to existing DBs
        try:
            conn.execute("ALTER TABLE sniper_trades ADD COLUMN reconciled INTEGER DEFAULT NULL")
        except Exception:
            pass  # Column already exists
        conn.commit()
        conn.close()

    def _init_client(self):
        if self.paper_mode:
            self.log("📝 PAPER MODE — no real orders")
            self.balance = 1000.0
            return True
        config = dotenv_values(ENV_PATH)
        pk = config.get("POLYGON_PRIVATE_KEY", "")
        addr = config.get("POLYGON_WALLET_ADDRESS", "")
        if not pk or not addr:
            self.log("❌ Missing POLYGON_PRIVATE_KEY or POLYGON_WALLET_ADDRESS")
            return False
        host = "https://clob.polymarket.com"
        client = ClobClient(host, key=pk, chain_id=137)
        creds = client.create_or_derive_api_creds()
        self.client = ClobClient(host, key=pk, chain_id=137, creds=creds, signature_type=1, funder=addr)
        self.funder = addr
        self.log("✅ Connected to Polymarket CLOB")
        self._refresh_balance()
        # Initialize redeemer for auto-claiming
        try:
            from src.redeemer import Redeemer
            self.redeemer = Redeemer(pk, addr)
            self.log("✅ Redeemer initialized (auto-sweep enabled)")
        except Exception as e:
            self.log(f"⚠️ Redeemer init failed (manual claims only): {e}")
            self.redeemer = None
        return True

    def _refresh_balance(self):
        if self.paper_mode:
            return
        try:
            bal = self.client.get_balance_allowance(BalanceAllowanceParams(asset_type="COLLATERAL"))
            raw = float(bal.get("balance", 0)) if isinstance(bal, dict) else 0
            self.balance = raw / 1e6 if raw > 1e6 else raw
        except Exception as e:
            self.log(f"⚠️ Balance check failed: {e}")

    def _get_binance_price(self, symbol):
        try:
            r = requests.get(f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}", timeout=2)
            return float(r.json()["price"])
        except:
            return None

    def _get_binance_candle_open(self, symbol, window_start_ts):
        try:
            r = requests.get(
                "https://api.binance.com/api/v3/klines",
                params={"symbol": symbol, "interval": "5m", "startTime": window_start_ts * 1000, "limit": 1},
                timeout=2,
            )
            candles = r.json()
            if candles:
                return float(candles[0][1])
        except:
            pass
        return None

    def _find_market(self, asset, window_ts):
        slug = f"{ASSETS[asset]['slug_prefix']}-{window_ts}"
        try:
            r = requests.get(f"{GAMMA_BASE}/events", params={"slug": slug}, timeout=5)
            if r.status_code != 200:
                return None
            data = r.json()
            if not data:
                return None
            event = data[0]
            market = event["markets"][0]
            clob_token_ids = market.get("clobTokenIds")
            if isinstance(clob_token_ids, str):
                clob_token_ids = json.loads(clob_token_ids)
            return {
                "slug": slug,
                "condition_id": market.get("conditionId"),
                "accepting_orders": market.get("acceptingOrders", False),
                "closed": market.get("closed", False) or event.get("closed", False),
                "token_ids": {
                    "up": clob_token_ids[0] if clob_token_ids else None,
                    "down": clob_token_ids[1] if clob_token_ids else None,
                },
                "neg_risk": market.get("negRisk", False),
                "tick_size": str(market.get("orderPriceMinTickSize", "0.01")),
            }
        except:
            return None

    def _preload_window(self, window_ts):
        """Pre-fetch market data and record open prices BEFORE the window ends."""
        self.preloaded = {}
        for asset, cfg in ASSETS.items():
            market = self._find_market(asset, window_ts)
            if not market or market["closed"]:
                continue
            # Get current price as fallback open price
            open_price = self._get_binance_price(cfg["binance"])
            if open_price is None:
                continue
            self.preloaded[asset] = {
                "market": market,
                "open_price": open_price,
                "window_ts": window_ts,
            }
            time.sleep(0.1)  # Rate limit between assets

        if self.preloaded:
            self.log(f"📦 Pre-loaded {len(self.preloaded)} markets for window {window_ts}")

    def _update_open_prices(self, window_ts):
        """Update open prices from Binance 5m candle (more accurate than spot)."""
        for asset, data in self.preloaded.items():
            if data["window_ts"] != window_ts:
                continue
            candle_open = self._get_binance_candle_open(ASSETS[asset]["binance"], window_ts)
            if candle_open:
                data["open_price"] = candle_open

    def _get_all_candle_directions(self, window_ts):
        """Fetch Binance 5m candle for each asset — ONE call per asset, parallel.
        Returns {asset: (direction, open, close, move_pct)} using the ACTUAL
        candle open+close, not pre-fetched spot prices."""
        import concurrent.futures

        def fetch_candle(asset_symbol):
            asset, symbol = asset_symbol
            try:
                r = requests.get(
                    "https://api.binance.com/api/v3/klines",
                    params={"symbol": symbol, "interval": "5m",
                            "startTime": window_ts * 1000, "limit": 1},
                    timeout=2,
                )
                candles = r.json()
                if candles:
                    o = float(candles[0][1])
                    c = float(candles[0][4])
                    direction = "up" if c >= o else "down"
                    move = abs(c - o) / o * 100
                    return asset, (direction, o, c, move)
            except Exception:
                pass
            return asset, None

        tasks = [(a, ASSETS[a]["binance"]) for a in self.preloaded]
        results = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            for asset, result in ex.map(fetch_candle, tasks):
                if result is not None:
                    results[asset] = result
        return results

    def _snipe_all(self, window_ts):
        """SPEED-OPTIMIZED: Fire snipe orders ASAP after window ends.
        Uses Binance 5m CANDLE (open+close) for direction — NOT pre-fetched spot.
        One API call per asset, parallel. ~500ms total."""
        t_start = time.time()

        # Get candle open+close for all assets in parallel
        directions = self._get_all_candle_directions(window_ts)
        t_prices = time.time()

        for asset, data in self.preloaded.items():
            market_key = f"{asset}-{window_ts}"
            if market_key in self.sniped_markets:
                continue
            self.sniped_markets.add(market_key)

            market = data["market"]

            if asset not in directions:
                self.log(f"⚠️ {asset.upper()}: No candle data")
                continue

            outcome, open_price, close_price, pct_move = directions[asset]

            # Skip if too close to call — must be unambiguous
            if pct_move < MIN_PRICE_MOVE_PCT:
                self.log(f"⏭️ {asset.upper()}: Move too small ({pct_move:.4f}%), skip")
                self.stats["skipped"] += 1
                continue

            # Get the winning token
            token_id = market["token_ids"][outcome]
            if not token_id:
                continue

            # BLIND ORDER: Fire at $0.991 without checking the book.
            # Post-close, winner asks are empty. Fills come via complement matching:
            # Our BUY WINNER@$0.991 + MM's BUY LOSER@$0.009 = $1.00 mint.
            # If we're first: filled at $0.991. If not: unfilled, no loss.
            best_price = 0.99
            margin = 1.0 - best_price  # $0.01/share profit on resolution

            # Size the order (min 5 shares per Polymarket)
            shares = max(self.trade_size / best_price, 5.0)
            cost = shares * best_price

            if not self.paper_mode and cost > self.balance:
                shares = max(self.balance / best_price, 5.0)
                cost = shares * best_price
                if cost > self.balance:
                    self.log(f"⏭️ {asset.upper()}: Insufficient balance (${self.balance:.2f})")
                    continue

            expected_profit = shares * margin

            elapsed = time.time() - t_start
            self.log(
                f"🎯 SNIPE {asset.upper()} {outcome.upper()} [{elapsed:.1f}s] | "
                f"${open_price:,.2f}→${close_price:,.2f} ({pct_move:+.3f}%) | "
                f"{shares:.0f} shares @ ${best_price} | "
                f"cost ${cost:.2f} → +${expected_profit:.2f}"
            )

            self.stats["attempts"] += 1

            # Place order
            if self.paper_mode:
                order_id = f"paper_{int(time.time())}_{asset}"
                result = {"order_id": order_id, "status": "matched"}
            else:
                result = self._place_order(token_id, best_price, shares, market)

            if result:
                self.stats["fills"] += 1
                self.stats["total_cost"] += cost
                self.stats["total_payout"] += shares  # $1/share on resolution
                if self.paper_mode:
                    self.balance -= cost

                # Record to DB
                conn = sqlite3.connect(DB_PATH)
                conn.execute(
                    """INSERT INTO sniper_trades 
                       (timestamp, asset, market_slug, window_start_ts, outcome, open_price, close_price,
                        buy_price, shares, cost_usdc, expected_profit, order_id, status, paper)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        datetime.now(PST).isoformat(), asset, market["slug"], window_ts, outcome,
                        open_price, close_price, best_price, shares, cost,
                        expected_profit, result.get("order_id", ""),
                        result.get("status", "filled"), 1 if self.paper_mode else 0,
                    ),
                )
                conn.commit()
                conn.close()

                status_str = result.get("status", "?")
                self.log(f"✅ {asset.upper()} {status_str}! +${expected_profit:.2f}")
            else:
                self.stats["misses"] += 1
                self.log(f"❌ {asset.upper()} order failed")

    def _place_order(self, token_id, price, size, market):
        try:
            order_args = OrderArgs(
                token_id=token_id,
                price=round(price, 2),
                size=round(size, 2),
                side=BUY,
            )
            options = PartialCreateOrderOptions(
                tick_size=market["tick_size"],
                neg_risk=market["neg_risk"],
            )
            resp = self.client.create_and_post_order(order_args, options)
            if resp and resp.get("success"):
                return {
                    "order_id": resp.get("orderID", ""),
                    "status": resp.get("status", "unknown"),
                }
            else:
                self.log(f"⚠️ Order response: {resp}")
                return None
        except Exception as e:
            self.log(f"❌ Order error: {e}")
            return None

    def _reconcile_fills(self):
        """Query Polymarket activity API to update DB with actual fill prices/costs.
        Runs once after each snipe cycle. 1 API call per reconciliation."""
        if self.paper_mode:
            return

        try:
            proxy = self.funder
            r = requests.get(
                f"{DATA_API}/activity?user={proxy}&limit=50&type=TRADE",
                timeout=10,
            )
            if r.status_code != 200:
                return

            activities = r.json()

            conn = sqlite3.connect(DB_PATH)
            # Find unreconciled trades (where buy_price still matches our ask price, not actual fill)
            unreconciled = conn.execute(
                "SELECT id, market_slug, order_id, buy_price FROM sniper_trades WHERE paper=0 AND reconciled IS NULL"
            ).fetchall()

            if not unreconciled:
                conn.close()
                return

            updated = 0
            for row in unreconciled:
                db_id, slug, order_id, db_price = row
                # Match by slug (market) — activity API has eventSlug
                match = None
                for act in activities:
                    if act.get("eventSlug") == slug or act.get("slug") == slug:
                        match = act
                        break

                if match:
                    real_price = float(match.get("price", 0))
                    real_cost = float(match.get("usdcSize", 0))
                    real_shares = float(match.get("size", 0))
                    real_profit = real_shares - real_cost  # $1/share on win minus cost

                    conn.execute(
                        """UPDATE sniper_trades 
                           SET buy_price=?, shares=?, cost_usdc=?, expected_profit=?, reconciled=1
                           WHERE id=?""",
                        (real_price, real_shares, real_cost, real_profit, db_id),
                    )
                    updated += 1
                else:
                    # No match in recent 50 — mark as reconciled anyway to avoid re-checking
                    # (might be too old for the API window)
                    conn.execute(
                        "UPDATE sniper_trades SET reconciled=1 WHERE id=?",
                        (db_id,),
                    )

            if updated:
                conn.commit()
                self.log(f"🔄 Reconciled {updated} trades with on-chain fill data")
            conn.close()

        except Exception as e:
            self.log(f"⚠️ Reconciliation error: {e}")

    def _sweep_positions(self):
        """Sweep all resolved positions — redeem winners (get USDC) and burn losers (clear clutter)."""
        if self.paper_mode or not self.redeemer:
            return

        now = int(time.time())
        if now - self._last_sweep < SWEEP_INTERVAL:
            return
        self._last_sweep = now

        try:
            # Fetch our positions from Polymarket data API (1 call)
            proxy = self.funder
            r = requests.get(
                f"{DATA_API}/positions?user={proxy}",
                timeout=10,
            )
            if r.status_code != 200:
                return
            positions = r.json()

            # Filter to 5-min up/down markets only
            updown_positions = [
                p for p in positions
                if "Up or Down" in p.get("title", "")
            ]

            if not updown_positions:
                return

            self.log(f"🔍 Sweep: {len(updown_positions)} sniper positions to check")

            redeemed = 0
            burned = 0
            skipped = 0

            for pos in updown_positions:
                cid = pos.get("conditionId", "")
                cur_price = float(pos.get("curPrice", 0))
                size = float(pos.get("size", 0))
                title = pos.get("title", "")[:45]

                if not cid or size == 0:
                    continue

                # Check if resolved on-chain (1 RPC call per position)
                try:
                    if not self.redeemer.is_condition_resolved(cid):
                        skipped += 1
                        continue
                except Exception:
                    skipped += 1
                    continue

                # Resolved — redeem it (works for both winners and losers)
                # Winners: get USDC back. Losers: burns worthless tokens.
                try:
                    result = self.redeemer.redeem(cid)
                    if result.get("success"):
                        if cur_price >= 0.5:
                            self.log(f"💰 Redeemed WINNER: {title} ({size:.0f} shares → ${size:.2f})")
                            redeemed += 1
                        else:
                            self.log(f"🗑️ Burned loser: {title} ({size:.0f} worthless shares)")
                            burned += 1
                    elif result.get("error") == "rate_limited":
                        self.log("⏸️ Redeemer rate limited, stopping sweep")
                        break
                    elif result.get("error") == "tx_pending":
                        self.log("⏳ Tx pending, will retry next sweep")
                        break
                    else:
                        skipped += 1
                except Exception as e:
                    self.log(f"⚠️ Redeem error for {title}: {e}")
                    skipped += 1

                time.sleep(2)  # Rate limit between redeems

            if redeemed or burned:
                self.log(f"🧹 Sweep done: {redeemed} redeemed, {burned} burned, {skipped} skipped")
                self._refresh_balance()

        except Exception as e:
            self.log(f"⚠️ Sweep error: {e}")

    def _print_stats(self):
        profit = self.stats["total_payout"] - self.stats["total_cost"]
        roi = (profit / self.stats["total_cost"] * 100) if self.stats["total_cost"] > 0 else 0
        self.log(
            f"📊 {self.stats['fills']}/{self.stats['attempts']} fills, "
            f"{self.stats['misses']} misses, {self.stats['skipped']} skipped | "
            f"cost ${self.stats['total_cost']:.2f} → profit ${profit:.2f} ({roi:.1f}%) | "
            f"bal ${self.balance:,.2f}"
        )

    def run(self):
        mode_str = "📝 PAPER" if self.paper_mode else "💰 LIVE"
        self.log(f"🎯 Sniper Bot v3 — {mode_str} MODE")
        self.log(f"Assets: {', '.join(a.upper() for a in ASSETS)}")
        self.log(f"Trade size: ${self.trade_size} | Max price: ${self.max_buy_price}")

        if not self._init_client():
            return

        self.log(f"💰 Balance: ${self.balance:,.2f}")
        self.log("Watching for 5-min windows...\n")

        last_preload_window = 0
        last_stats_time = 0
        last_balance_refresh = 0

        while self.running:
            try:
                now = int(time.time())
                current_window = (now // 300) * 300
                window_end = current_window + 300
                secs_to_end = window_end - now

                # PHASE 1: Pre-load markets 5-120s before window ends
                if 5 < secs_to_end <= 120 and current_window != last_preload_window:
                    self._preload_window(current_window)
                    last_preload_window = current_window

                # PHASE 2: No pre-close price fetch — candle open+close fetched post-close

                # PHASE 3: SNIPE right after window ends
                prev_window = current_window - 300
                secs_after_prev = now - current_window  # current_window IS prev window's end

                if SNIPE_DELAY <= secs_after_prev <= MAX_SNIPE_WINDOW:
                    if any(d.get("window_ts") == prev_window for d in self.preloaded.values()):
                        self._snipe_all(prev_window)

                # Reconcile fill data after snipe window passes
                if secs_after_prev == MAX_SNIPE_WINDOW + 3:
                    self._reconcile_fills()

                # Sleep scheduling — INSTANT during snipe, lazy otherwise
                if 0 <= secs_after_prev <= MAX_SNIPE_WINDOW + 2:
                    time.sleep(0.05)  # 50ms poll during snipe window
                elif secs_to_end <= 3:
                    time.sleep(0.05)  # Tight poll right before window end
                elif secs_to_end <= 125:
                    time.sleep(1)    # Preparing
                else:
                    sleep_time = min(max(1, secs_to_end - 120), 30)
                    time.sleep(sleep_time)

                # Stats every 5 min
                if now - last_stats_time > 300 and self.stats["attempts"] > 0:
                    self._print_stats()
                    last_stats_time = now

                # Balance refresh every 5 min
                if not self.paper_mode and now - last_balance_refresh > 300:
                    self._refresh_balance()
                    last_balance_refresh = now

                # Sweep resolved positions every 10 min
                if not self.paper_mode and secs_to_end > 120:
                    # Only sweep during idle time (not near window end)
                    self._sweep_positions()

                # Cleanup old sniped entries
                if len(self.sniped_markets) > 200:
                    self.sniped_markets = set(list(self.sniped_markets)[-100:])

            except KeyboardInterrupt:
                break
            except Exception as e:
                self.log(f"❌ Loop error: {e}")
                time.sleep(5)

        self.log("🛑 Sniper Bot stopped")
        if self.stats["attempts"] > 0:
            self._print_stats()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="🎯 Sniper Bot v3")
    parser.add_argument("--paper", action="store_true", help="Paper mode")
    parser.add_argument("--size", type=float, default=DEFAULT_TRADE_SIZE, help="USDC per snipe")
    parser.add_argument("--max-price", type=float, default=MAX_BUY_PRICE, help="Max buy price")
    args = parser.parse_args()

    bot = SniperBot(paper_mode=args.paper, trade_size=args.size, max_price=args.max_price)

    def shutdown(sig, frame):
        bot.running = False
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    bot.run()


if __name__ == "__main__":
    main()
