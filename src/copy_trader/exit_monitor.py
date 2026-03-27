"""
Copy Trader Exit Monitor
Watches for exit signals on open positions:
  1. Leader exits (sells their position)
  2. Stop loss
  3. Take profit
  4. Trailing stop
  5. Max hold time
  6. Market resolution
"""
import os
import time
import logging
import requests
from datetime import datetime, timezone
from . import config
from .database import CopyTraderDB
from .market_rules import classify_market, get_entry_rules, is_esports

log = logging.getLogger("copy_trader")


class ExitMonitor:
    def __init__(self, db: CopyTraderDB, executor=None):
        self.db = db
        self.executor = executor  # Set by monitor when live
        self.redeemer = None     # Set by monitor when live
        self.session = requests.Session()
        self.price_cache = {}  # condition_id -> (price, timestamp)
        self.peak_prices = {}  # position_id -> highest price seen (for trailing stop)
        self._redeem_attempts = {}  # position_id -> count (prevent spam)
        self.last_reconcile = 0  # timestamp of last reconciliation
        self.reconcile_interval = 120  # seconds between reconciliation checks
        self.sell_failures = {}  # position_id -> {'count': int, 'last_attempt': float}
        self.max_sell_retries = 5  # stop trying after this many failures
        self.sell_retry_backoff = [30, 60, 120, 300, 600]  # seconds between retries
        self.our_wallet = (os.environ.get("POLYMARKET_PROXY_WALLET", "") or 
                           os.environ.get("POLYGON_WALLET_ADDRESS", "")).lower()

    def check_all_positions(self):
        """Check all open positions for exit signals."""
        positions = self.db.get_open_positions()
        if not positions:
            return

        # Periodic reconciliation: detect manual sells / missing positions
        now = time.time()
        if now - self.last_reconcile >= self.reconcile_interval:
            self._reconcile_positions(positions)
            self.last_reconcile = now

        # Re-fetch in case reconciliation closed some
        positions = self.db.get_open_positions()
        for pos in positions:
            exit_signal = self._check_position(pos)
            if exit_signal:
                self._execute_exit(pos, exit_signal)

    def _reconcile_positions(self, db_positions: list):
        """
        Reconcile DB positions against on-chain state.
        Detects manual sells, resolved positions we missed, etc.
        Uses Polymarket activity API to determine actual P&L (not just assume loss).
        """
        if not self.our_wallet:
            return

        try:
            # Fetch current on-chain positions
            r = self.session.get(
                "https://data-api.polymarket.com/positions",
                params={"user": self.our_wallet, "sizeThreshold": 0, "limit": 200},
                timeout=15,
            )
            if r.status_code != 200:
                return

            on_chain = {}
            for p in r.json():
                cid = p.get('conditionId', '')
                if cid:
                    on_chain[cid] = {
                        'size': float(p.get('size', 0) or 0),
                        'currentValue': float(p.get('currentValue', 0) or 0),
                        'initialValue': float(p.get('initialValue', 0) or 0),
                        'cashPnl': float(p.get('cashPnl', 0) or 0),
                        'curPrice': float(p.get('curPrice', 0) or 0),
                    }

            # Fetch recent activity to determine actual sell/redeem values
            activity_cache = {}  # market_name -> {sold: $, redeemed: $}
            try:
                ar = self.session.get(
                    "https://data-api.polymarket.com/activity",
                    params={"user": self.our_wallet, "limit": 100, "offset": 0},
                    timeout=15,
                )
                if ar.status_code == 200:
                    for a in ar.json():
                        if not isinstance(a, dict):
                            continue
                        title = a.get('title') or a.get('question') or ''
                        action = a.get('type', '')
                        usdc = float(a.get('usdcSize') or 0)
                        if title not in activity_cache:
                            activity_cache[title] = {'sold': 0, 'redeemed': 0, 'bought': 0}
                        if action == 'TRADE' and a.get('side') == 'SELL':
                            activity_cache[title]['sold'] += usdc
                        elif action == 'TRADE' and a.get('side') == 'BUY':
                            activity_cache[title]['bought'] += usdc
                        elif action in ('REDEEM',):
                            activity_cache[title]['redeemed'] += usdc
            except Exception:
                pass  # Activity API is best-effort

            for pos in db_positions:
                if pos.get('is_paper', True):
                    continue  # Only reconcile live positions

                cid = pos['condition_id']
                chain_pos = on_chain.get(cid)
                market_name = pos.get('market_question', '')

                # Check if position is gone or has 0 shares
                gone = False
                if not chain_pos:
                    gone = True
                elif chain_pos['size'] < 0.01 and chain_pos['currentValue'] < 0.01:
                    gone = True

                if not gone:
                    continue  # Position still active on-chain

                # Position is gone — figure out what happened using activity data
                activity = activity_cache.get(market_name, {})
                total_returned = activity.get('sold', 0) + activity.get('redeemed', 0)
                total_bought = activity.get('bought', 0)

                if total_bought > 0 and total_returned > 0:
                    # We have activity data — calculate real P&L
                    pnl = total_returned - total_bought
                    won = pnl > 0
                    if activity.get('redeemed', 0) > 0:
                        reason = 'resolution'
                    else:
                        reason = 'manual_sell'
                    exit_price = total_returned / pos['shares'] if pos['shares'] > 0 else 0
                elif chain_pos and abs(chain_pos.get('cashPnl', 0)) > 0.001:
                    # Fallback: use cashPnl from positions API
                    pnl = chain_pos['cashPnl']
                    won = pnl > 0
                    reason = 'resolution' if chain_pos.get('curPrice', 0) > 0.95 or chain_pos.get('curPrice', 0) < 0.05 else 'manual_sell'
                    exit_price = (pos['usdc_size'] + pnl) / pos['shares'] if pos['shares'] > 0 else 0
                else:
                    # Last resort: no data available, assume loss
                    pnl = -pos['usdc_size']
                    won = False
                    reason = 'manual_sell_unknown'
                    exit_price = 0.0
                    log.warning(
                        f"  ⚠️ No activity data for {market_name[:40]} — defaulting to full loss. "
                        f"Check Polymarket History for actual P&L."
                    )

                log.info(
                    f"🔄 RECONCILE: {market_name[:45]} — "
                    f"gone from chain | P&L: ${pnl:+.2f} | reason: {reason}"
                )
                self.db.close_position(pos['id'], exit_price, reason, pnl)

                emoji = "✅" if won else "❌"
                self._write_notification(
                    f"{emoji} RECONCILED: {market_name[:50]}\n"
                    f"Reason: {reason} | P&L: ${pnl:+.2f}"
                )

        except Exception as e:
            log.debug(f"Reconciliation error: {e}")

    def _check_position(self, pos: dict) -> dict | None:
        """
        Check a single position for exit signals.
        Returns exit info dict or None.
        Priority order: resolution > leader_exit > stop_loss > take_profit > trailing > max_hold
        """
        condition_id = pos['condition_id']

        # 0. Skip if sell retries exhausted — hold to resolution only
        pos_id = pos['id']
        fail_info = self.sell_failures.get(pos_id, {'count': 0})
        if fail_info['count'] >= self.max_sell_retries:
            # Only check resolution, nothing else
            return self._check_resolution(pos)

        # 1. Check market resolution
        resolution = self._check_resolution(pos)
        if resolution:
            return resolution

        # 2. Classify market type
        market_type = classify_market(pos.get('market_question', ''))
        rules = get_entry_rules(market_type)

        # 3. Check if leader exited — configurable per market type
        should_copy_exit = rules.get('copy_leader_exit', config.COPY_LEADER_EXIT)
        if should_copy_exit:
            leader_exit = self._check_leader_exit(pos)
            if leader_exit:
                return leader_exit

        # 4. ESPORTS + TENNIS: skip ALL price-based exits — hold to resolution
        # No exit liquidity when losing. SL attempts just spam the API.
        if is_esports(market_type) or market_type == 'tennis':
            return None

        # 5. Get current token price for P&L-based exits
        current_price = self._get_current_price(condition_id, pos['token_id'])
        if current_price is None:
            return None  # Can't check price-based exits without price

        entry_price = pos['entry_price']

        # --- GRACE PERIOD: skip price-based exits for first 120s after entry ---
        hold_secs = time.time() - pos['opened_at']
        if hold_secs < 120:
            log.debug(f"  ⏳ Grace period: {pos['market_question'][:40]}... ({hold_secs:.0f}s < 120s)")
            return None

        gain_pct = (current_price - entry_price) / entry_price if entry_price > 0 else 0

        # --- SANITY CHECK: if SL % is extreme (>50%) but price is within 20% of entry, ---
        # --- the price lookup probably returned the wrong token side. Skip. ---
        if gain_pct <= -0.50 and current_price > entry_price * 0.50:
            log.warning(
                f"  ⚠️ SL SANITY FAIL: {pos['market_question'][:40]}... "
                f"gain={gain_pct*100:+.1f}% but price ${current_price:.3f} vs entry ${entry_price:.3f} "
                f"— likely wrong-side price, skipping"
            )
            return None

        # Track peak for trailing stop
        pos_id = pos['id']
        if pos_id not in self.peak_prices:
            self.peak_prices[pos_id] = current_price
        self.peak_prices[pos_id] = max(self.peak_prices[pos_id], current_price)

        # 4. Stop loss
        if config.STOP_LOSS_PCT > 0 and gain_pct <= -config.STOP_LOSS_PCT:
            return {
                'reason': 'stop_loss',
                'exit_price': current_price,
                'details': f"SL hit: {gain_pct*100:+.1f}% (limit: -{config.STOP_LOSS_PCT*100:.0f}%)",
            }

        # 5. Take profit
        if config.TAKE_PROFIT_PCT > 0 and gain_pct >= config.TAKE_PROFIT_PCT:
            return {
                'reason': 'take_profit',
                'exit_price': current_price,
                'details': f"TP hit: {gain_pct*100:+.1f}% (limit: +{config.TAKE_PROFIT_PCT*100:.0f}%)",
            }

        # 6. Trailing stop
        if config.TRAILING_STOP_PCT > 0:
            peak = self.peak_prices[pos_id]
            if peak > entry_price:  # Only trail after we're in profit
                drop_from_peak = (peak - current_price) / peak
                if drop_from_peak >= config.TRAILING_STOP_PCT:
                    return {
                        'reason': 'trailing_stop',
                        'exit_price': current_price,
                        'details': f"Trailing SL: dropped {drop_from_peak*100:.1f}% from peak ${peak:.3f}",
                    }

        # 7. Max hold time
        if config.MAX_HOLD_TIME_DAYS > 0:
            hold_sec = time.time() - pos['opened_at']
            max_sec = config.MAX_HOLD_TIME_DAYS * 86400
            if hold_sec >= max_sec:
                return {
                    'reason': 'max_hold_time',
                    'exit_price': current_price,
                    'details': f"Held {hold_sec/86400:.1f} days (limit: {config.MAX_HOLD_TIME_DAYS}d)",
                }

        return None

    def _check_resolution(self, pos: dict) -> dict | None:
        """Check if the market has resolved via CLOB API."""
        condition_id = pos['condition_id']
        try:
            r = self.session.get(
                f"https://clob.polymarket.com/markets/{condition_id}",
                timeout=5,
            )
            if r.status_code != 200:
                return None

            market = r.json()

            # CLOB: closed=True means no more trading
            if not market.get('closed', False):
                return None

            # If closed and not accepting orders, check if it's resolved
            # For resolved markets, token prices go to 1.0 or 0.0
            tokens = market.get('tokens', [])
            if not tokens:
                return None

            # Check if any token has price = 1.0 (winner determined)
            for token in tokens:
                token_price = float(token.get('price', 0) or 0)
                token_id = token.get('token_id', '')
                outcome = token.get('outcome', '')

                if token_price >= 0.99:  # This token won
                    # Did we hold this token?
                    our_token = pos.get('token_id', '')
                    if our_token == token_id:
                        return {
                            'reason': 'resolution',
                            'exit_price': 1.0,
                            'details': f"Market resolved: {outcome} won",
                            'won': True,
                        }
                    else:
                        return {
                            'reason': 'resolution',
                            'exit_price': 0.0,
                            'details': f"Market resolved: {outcome} won (we held other side)",
                            'won': False,
                        }

        except Exception as e:
            log.debug(f"Resolution check error for {condition_id[:16]}: {e}")

        return None

    def _check_leader_exit(self, pos: dict) -> dict | None:
        """Check if the leader has sold their position."""
        leader_address = pos['leader_address']
        condition_id = pos['condition_id']
        opened_at = pos['opened_at']

        # Look for leader SELL trades in this market after our entry
        if self.db.leader_recent_sells(leader_address, condition_id, 
                                        window_sec=int(time.time() - opened_at)):
            # Leader sold — check delay
            if config.COPY_LEADER_EXIT_DELAY_SEC > 0:
                # TODO: Track when we first detected the leader sell and apply delay
                pass

            current_price = self._get_current_price(condition_id, pos['token_id'])
            return {
                'reason': 'leader_exit',
                'exit_price': current_price or pos['entry_price'],
                'details': f"Leader {pos['leader_name']} sold their position",
            }

        return None

    def _get_current_price(self, condition_id: str, token_id: str = None) -> float | None:
        """Get current token price from CLOB API."""
        cache_key = f"{condition_id}:{token_id or ''}"
        now = time.time()

        # Cache for 10 seconds
        if cache_key in self.price_cache:
            cached_price, cached_time = self.price_cache[cache_key]
            if now - cached_time < 10:
                return cached_price

        try:
            r = self.session.get(
                f"https://clob.polymarket.com/markets/{condition_id}",
                timeout=5,
            )
            if r.status_code != 200:
                return None

            market = r.json()
            tokens = market.get('tokens', [])

            for token in tokens:
                tid = token.get('token_id', '')
                price = float(token.get('price', 0) or 0)
                if token_id and tid == token_id:
                    self.price_cache[cache_key] = (price, now)
                    return price

            # Token ID didn't match — try complement (1 - other side)
            # This handles cases where CLOB returns different token_id format
            if token_id and len(tokens) == 2:
                # Binary market: our price = 1 - other_side_price
                for token in tokens:
                    tid = token.get('token_id', '')
                    if tid != token_id:
                        complement_price = 1.0 - float(token.get('price', 0) or 0)
                        log.warning(
                            f"  ⚠️ Token ID mismatch in price lookup — using complement: "
                            f"${complement_price:.3f} for {condition_id[:16]}"
                        )
                        self.price_cache[cache_key] = (complement_price, now)
                        return complement_price

            # Last resort: return first token price (DANGEROUS — may be wrong side)
            if tokens:
                price = float(tokens[0].get('price', 0) or 0)
                log.warning(f"  ⚠️ No token match, using tokens[0] price ${price:.3f} — may be wrong side")
                self.price_cache[cache_key] = (price, now)
                return price

        except Exception as e:
            log.debug(f"Price fetch error for {condition_id[:16]}: {e}")

        return None

    def _execute_exit(self, pos: dict, exit_signal: dict):
        """Close a position based on exit signal."""
        exit_price = exit_signal['exit_price']
        exit_reason = exit_signal['reason']
        entry_price = pos['entry_price']
        shares = pos['shares']
        usdc_size = pos['usdc_size']
        is_paper = pos.get('is_paper', True)

        # For live positions that need to SELL (not resolution), execute the sell
        if not is_paper and self.executor and exit_reason != 'resolution':
            if not shares or shares < 0.01:
                log.warning(f"  ⚠️ Skipping sell — 0 shares for {pos['market_question'][:40]}...")
                pnl = -usdc_size
                self.db.close_position(pos['id'], 0.0, f"{exit_reason}_zero_shares", pnl)
                return None

            # --- SELL RETRY TRACKING: cap retries with exponential backoff ---
            pos_id = pos['id']
            fail_info = self.sell_failures.get(pos_id, {'count': 0, 'last_attempt': 0})

            if fail_info['count'] >= self.max_sell_retries:
                # Exhausted retries — hold to resolution instead of infinite loop
                log.warning(
                    f"  🛑 SELL ABANDONED after {fail_info['count']} failures: "
                    f"{pos['market_question'][:40]}... — holding to resolution"
                )
                return None  # Don't close in DB, just stop trying to sell

            # Backoff check: wait longer between each retry
            if fail_info['count'] > 0:
                backoff_idx = min(fail_info['count'] - 1, len(self.sell_retry_backoff) - 1)
                backoff_sec = self.sell_retry_backoff[backoff_idx]
                elapsed = time.time() - fail_info['last_attempt']
                if elapsed < backoff_sec:
                    return None  # Not time to retry yet

            log.info(f"  🔴 LIVE SELL: {shares:.1f}sh of {pos['market_question'][:40]}...")
            fill = self.executor.sell(
                token_id=pos['token_id'],
                condition_id=pos['condition_id'],
                shares=shares,
            )
            if fill:
                exit_price = fill['fill_price']
                proceeds = fill.get('usdc_received', shares * exit_price)
                fee = fill.get('fee_estimate', 0)
                pnl = proceeds - usdc_size
                log.info(f"  ✅ SOLD: ${proceeds:.2f} received (fee ~${fee:.2f})")
                self.sell_failures.pop(pos_id, None)  # Clear failure tracker on success
            else:
                # Track failure for retry backoff
                self.sell_failures[pos_id] = {
                    'count': fail_info['count'] + 1,
                    'last_attempt': time.time(),
                }
                retry_num = self.sell_failures[pos_id]['count']
                log.warning(
                    f"  ⚠️ SELL FAILED ({retry_num}/{self.max_sell_retries}) — "
                    f"keeping position open | {pos['market_question'][:40]}..."
                )
                return None  # Don't close if sell failed
        elif not is_paper and exit_reason == 'resolution' and exit_signal.get('won'):
            # Live win — auto-redeem on-chain
            pnl = shares * 1.0 - usdc_size
            self._auto_redeem(pos, won=True)
        elif not is_paper and exit_reason == 'resolution' and not exit_signal.get('won'):
            # Live loss — try ONE redeem to burn tokens (returns $0 but cleans up position)
            pnl = -usdc_size
            self._auto_redeem(pos, won=False)
        else:
            # Paper mode — calculate P&L
            if exit_reason == 'resolution':
                if exit_signal.get('won'):
                    pnl = shares * 1.0 - usdc_size
                else:
                    pnl = -usdc_size
            else:
                proceeds = shares * exit_price * (1 - 0.02)  # 2% taker fee estimate
                pnl = proceeds - usdc_size

        # Close in DB
        self.db.close_position(pos['id'], exit_price, exit_reason, pnl)

        # Update leader stats
        won = pnl > 0
        self.db.update_leader_stats(pos['leader_address'], pos['leader_name'],
                                     won=won, pnl=pnl)

        # Check leader cooldown
        stats = self.db.get_leader_stats(pos['leader_address'])
        if stats and stats['consecutive_losses'] >= config.LEADER_COOLDOWN_LOSSES:
            cooldown_until = int(time.time()) + (config.LEADER_COOLDOWN_HOURS * 3600)
            self.db.pause_leader(pos['leader_address'], cooldown_until)
            log.warning(
                f"⏸️ PAUSED {pos['leader_name']}: {stats['consecutive_losses']} consecutive losses. "
                f"Resuming in {config.LEADER_COOLDOWN_HOURS}h"
            )

        emoji = "✅" if won else "❌"
        log.info(
            f"{emoji} EXIT: {pos['leader_name']} | {pos['market_question'][:50]} | "
            f"Reason: {exit_reason} | P&L: ${pnl:+.2f} | {exit_signal.get('details', '')}"
        )

        # Notify on live trade exits
        if not pos.get('is_paper', True):
            if exit_reason == 'stop_loss':
                self._write_notification(
                    f"🛑 STOP LOSS TRIGGERED\n"
                    f"{(pos.get('market_question') or '')[:50]}\n"
                    f"Entry: ${entry_price:.3f} → Exit: ${exit_price:.3f}\n"
                    f"P&L: ${pnl:+.2f} | Saved ~${usdc_size + pnl:.2f} vs full loss"
                )
            else:
                self._write_notification(
                    f"{emoji} TRADE CLOSED: {pos['leader_name']}\n"
                    f"{(pos.get('market_question') or '')[:50]}\n"
                    f"Reason: {exit_reason} | P&L: ${pnl:+.2f}"
                )

        return pnl

    def _auto_redeem(self, pos: dict, won: bool = True):
        """Auto-redeem tokens after market resolution.
        Only redeems winning positions — burning losers wastes gas for $0 return.
        Max 3 attempts per position to prevent spam (Man Utd WFC 40x bug).
        """
        if not self.redeemer:
            log.warning(f"  ⚠️ Redeemer not initialized — tokens need manual redemption")
            return

        pos_id = pos['id']
        market = (pos.get('market_question') or '')[:40]

        # Max attempts: 3 for winners, 1 for losers (losers return $0, just burns tokens)
        max_attempts = 3 if won else 1
        attempts = self._redeem_attempts.get(pos_id, 0)
        if attempts >= max_attempts:
            if won:
                log.warning(f"  ⚠️ Max redeem attempts ({max_attempts}) reached for {market} — needs manual claim")
            return
        self._redeem_attempts[pos_id] = attempts + 1

        condition_id = pos['condition_id']

        try:
            # Check if condition is actually resolved on-chain
            if not self.redeemer.is_condition_resolved(condition_id):
                log.info(f"  ⏳ {market} not yet resolved on-chain, will retry later")
                self._redeem_attempts[pos_id] = attempts  # Don't count this as an attempt
                return

            # Check token balance
            token_id = pos.get('token_id', '')
            balance = self.redeemer.check_token_balance(token_id) if token_id else 0
            if balance == 0:
                log.info(f"  ⏭️ No tokens to redeem for {market}")
                self._redeem_attempts[pos_id] = 999  # Don't retry — no tokens
                return

            log.info(f"  💰 Auto-redeeming {balance} tokens for {market} (attempt {attempts + 1}/3)...")
            result = self.redeemer.redeem(condition_id)

            if result.get('success'):
                tx_hash = result.get('tx_hash', '')
                log.info(f"  ✅ Redeemed! TX: {tx_hash}")
                self._redeem_attempts[pos_id] = 999  # Mark as done
            else:
                error = result.get('error', 'unknown')
                log.warning(f"  ⚠️ Redeem failed (attempt {attempts + 1}/3): {error}")
        except Exception as e:
            log.error(f"  ❌ Auto-redeem error: {e}")

    def _write_notification(self, message: str):
        """Write notification for external pickup."""
        try:
            import os, json
            notify_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                                        "data", "notifications.jsonl")
            entry = json.dumps({
                "timestamp": int(time.time()),
                "message": message,
                "delivered": False,
            })
            with open(notify_path, "a") as f:
                f.write(entry + "\n")
        except Exception as e:
            log.warning(f"Notification write failed: {e}")
