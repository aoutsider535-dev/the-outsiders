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
import time
import logging
import requests
from datetime import datetime, timezone
from . import config
from .database import CopyTraderDB
from .market_rules import classify_market, get_entry_rules

log = logging.getLogger("copy_trader")


class ExitMonitor:
    def __init__(self, db: CopyTraderDB, executor=None):
        self.db = db
        self.executor = executor  # Set by monitor when live
        self.session = requests.Session()
        self.price_cache = {}  # condition_id -> (price, timestamp)
        self.peak_prices = {}  # position_id -> highest price seen (for trailing stop)

    def check_all_positions(self):
        """Check all open positions for exit signals."""
        positions = self.db.get_open_positions()
        if not positions:
            return

        for pos in positions:
            exit_signal = self._check_position(pos)
            if exit_signal:
                self._execute_exit(pos, exit_signal)

    def _check_position(self, pos: dict) -> dict | None:
        """
        Check a single position for exit signals.
        Returns exit info dict or None.
        Priority order: resolution > leader_exit > stop_loss > take_profit > trailing > max_hold
        """
        condition_id = pos['condition_id']

        # 1. Check market resolution
        resolution = self._check_resolution(pos)
        if resolution:
            return resolution

        # 2. Check if leader exited — always on for crypto, configurable for others
        market_type = classify_market(pos.get('market_question', ''))
        rules = get_entry_rules(market_type)
        should_copy_exit = rules.get('copy_leader_exit', config.COPY_LEADER_EXIT)

        if should_copy_exit:
            leader_exit = self._check_leader_exit(pos)
            if leader_exit:
                return leader_exit

        # 3. Get current token price for P&L-based exits
        current_price = self._get_current_price(condition_id, pos['token_id'])
        if current_price is None:
            return None  # Can't check price-based exits without price

        entry_price = pos['entry_price']
        gain_pct = (current_price - entry_price) / entry_price if entry_price > 0 else 0

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

        # Cache for 30 seconds
        if cache_key in self.price_cache:
            cached_price, cached_time = self.price_cache[cache_key]
            if now - cached_time < 30:
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

            # If no token_id match, return first token price
            if tokens:
                price = float(tokens[0].get('price', 0) or 0)
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
            else:
                log.warning(f"  ⚠️ SELL FAILED — keeping position open")
                return None  # Don't close if sell failed
        else:
            # Paper mode or resolution — calculate P&L
            if exit_reason == 'resolution':
                if exit_signal.get('won'):
                    pnl = shares * 1.0 - usdc_size  # Won: shares resolve to $1 each
                else:
                    pnl = -usdc_size  # Lost: shares worth $0
            else:
                # Paper selling on secondary market
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

        return pnl
