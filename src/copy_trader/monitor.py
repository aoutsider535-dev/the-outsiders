"""
Copy Trader Leader Monitor
Polls leader wallets for new trades, runs through filter pipeline, executes copies.
"""
import time
import json
import logging
import requests
from datetime import datetime, timezone
from . import config
from .database import CopyTraderDB
from .filters import FilterPipeline
from .exit_monitor import ExitMonitor

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger("copy_trader")


class LeaderMonitor:
    def __init__(self, db: CopyTraderDB = None):
        self.db = db or CopyTraderDB(config.DB_PATH)
        self.filters = FilterPipeline(self.db)
        self.exit_monitor = ExitMonitor(self.db)
        self.last_seen = {}  # leader_address -> latest timestamp seen
        self.market_cache = {}  # condition_id -> market info
        self.session = requests.Session()
        self.running = False
        
        # Paper mode state
        self.paper_balance = 0.0
        self.paper_starting_balance = 0.0

    def _get_leader_activity(self, address: str, limit: int = 20):
        """Fetch recent activity for a leader wallet."""
        try:
            r = self.session.get(
                "https://data-api.polymarket.com/activity",
                params={"user": address, "limit": limit},
                timeout=10,
            )
            if r.status_code != 200:
                log.warning(f"Activity API {r.status_code} for {address[:10]}...")
                return []

            data = r.json()
            if not isinstance(data, list):
                return []

            return [a for a in data if isinstance(a, dict) and a.get('type') == 'TRADE']
        except Exception as e:
            log.error(f"Error fetching {address[:10]}...: {e}")
            return []

    def _get_market_info(self, condition_id: str) -> dict:
        """Fetch market details (cached). Uses CLOB API (gamma API is unreliable)."""
        if condition_id in self.market_cache:
            return self.market_cache[condition_id]

        try:
            # CLOB API is authoritative — gamma API returns wrong markets
            r = self.session.get(
                f"https://clob.polymarket.com/markets/{condition_id}",
                timeout=5,
            )
            if r.status_code == 200:
                data = r.json()
                info = {
                    'question': data.get('question', data.get('description', '')),
                    'active': data.get('active', False),
                    'closed': data.get('closed', False),
                    'endDate': data.get('end_date_iso', data.get('game_start_time', '')),
                    'category': data.get('market_type', data.get('category', '')),
                    'tokens': data.get('tokens', []),
                    'condition_id': condition_id,
                    'accepting_orders': data.get('accepting_orders', False),
                    'minimum_order_size': data.get('minimum_order_size', 0),
                    # Volume/liquidity not in CLOB, leave as 0
                    'volume': 0,
                    'liquidity': 0,
                }
                self.market_cache[condition_id] = info
                return info
        except Exception as e:
            log.warning(f"CLOB market lookup failed for {condition_id[:16]}: {e}")

        return {}

    def _calculate_trade_size(self, leader_address: str, trade_data: dict) -> float:
        """Determine how much we bet based on sizing mode."""
        leader_cfg = config.LEADERS.get(leader_address, {})
        weight = leader_cfg.get('weight', 1.0)

        if config.SIZING_MODE == "fixed":
            size = config.FIXED_TRADE_SIZE * weight
        elif config.SIZING_MODE == "proportional":
            leader_size = float(trade_data.get('usdcSize', 0) or 0)
            size = leader_size * config.COPY_FRACTION * weight
        elif config.SIZING_MODE == "kelly":
            # Kelly criterion based on leader's historical edge
            stats = self.db.get_leader_stats(leader_address)
            if stats and (stats['wins'] + stats['losses']) > 10:
                wr = stats['wins'] / (stats['wins'] + stats['losses'])
                price = float(trade_data.get('price', 0.5) or 0.5)
                # Simplified Kelly: f = (p * b - q) / b where b = (1/price - 1)
                b = (1.0 / price) - 1.0 if price > 0 else 1.0
                q = 1 - wr
                kelly_f = max(0, (wr * b - q) / b)
                kelly_f = min(kelly_f, 0.25)  # Cap at 25% of bankroll
                size = self.paper_balance * kelly_f * weight if self.paper_balance > 0 else config.FIXED_TRADE_SIZE
            else:
                size = config.FIXED_TRADE_SIZE * weight  # Fallback
        else:
            size = config.FIXED_TRADE_SIZE

        # Apply min/max
        size = max(size, config.MIN_TRADE_SIZE)
        size = min(size, config.MAX_TRADE_SIZE)

        return round(size, 2)

    def _execute_paper_trade(self, leader_trade_id: int, leader_address: str,
                              leader_name: str, trade_data: dict, market_info: dict,
                              trade_size: float):
        """Paper trade — record what we would have done."""
        price = float(trade_data.get('price', 0) or 0)
        if price <= 0:
            log.warning(f"Invalid price {price} for paper trade")
            return

        shares = trade_size / price
        condition_id = trade_data.get('conditionId', '')
        asset_id = trade_data.get('asset', '')  # This is the token_id the leader bought
        side = trade_data.get('side', 'BUY')
        question = market_info.get('question', '')[:80]
        category = market_info.get('category', '')
        
        # Map asset to outcome (Yes/No/Over/Under) using market tokens
        outcome_side = side
        for token in market_info.get('tokens', []):
            if token.get('token_id', '') == asset_id:
                outcome_side = token.get('outcome', side)
                break

        pos_id = self.db.open_position(
            leader_trade_id=leader_trade_id,
            leader_address=leader_address,
            leader_name=leader_name,
            condition_id=condition_id,
            token_id=asset_id,
            market_question=question,
            market_category=category,
            side=outcome_side,
            entry_price=price,
            shares=shares,
            usdc_size=trade_size,
            is_paper=True,
        )

        self.paper_balance -= trade_size

        log.info(
            f"📋 PAPER COPY: {leader_name} → {side} ${trade_size:.2f} "
            f"({shares:.1f}sh @ ${price:.3f}) | {question}"
        )
        log.info(f"   Paper balance: ${self.paper_balance:.2f}")

        return pos_id

    def process_leader(self, address: str):
        """Check one leader for new trades."""
        leader_cfg = config.LEADERS.get(address, {})
        if not leader_cfg.get('enabled', False):
            return

        leader_name = leader_cfg.get('name', address[:10])
        trades = self._get_leader_activity(address)

        if not trades:
            return

        # Get last seen timestamp for this leader
        last_ts = self.last_seen.get(address, 0)

        # Process new trades (newest first from API, but we want oldest first)
        new_trades = [t for t in trades if t.get('timestamp', 0) > last_ts]
        new_trades.sort(key=lambda t: t.get('timestamp', 0))

        if not new_trades:
            return

        for trade in new_trades:
            trade_ts = trade.get('timestamp', 0)
            self.last_seen[address] = max(self.last_seen.get(address, 0), trade_ts)

            # Record the leader's trade
            condition_id = trade.get('conditionId', '')
            market_info = self._get_market_info(condition_id)

            trade_id, is_new = self.db.record_leader_trade(
                leader_address=address,
                leader_name=leader_name,
                trade_data=trade,
                market_info=market_info,
            )

            if not is_new:
                continue  # Already processed

            # Update leader stats (trade seen)
            self.db.update_leader_stats(address, leader_name)

            # Always record the trade (even SELLs) so exit monitor can detect leader exits
            # The filter pipeline will decide whether to COPY it as a new position

            # Run through filters
            result = self.filters.evaluate(address, leader_name, trade, market_info)

            usdc_size = float(trade.get('usdcSize', 0) or 0)
            price = float(trade.get('price', 0) or 0)
            question = market_info.get('question', '')[:60]

            if not result:
                self.db.log_filter_decision(
                    trade_id, address, condition_id,
                    "SKIP", result.reason, result.details,
                )
                log.info(
                    f"⏭️  SKIP {leader_name}: {trade.get('side','')} ${usdc_size:.2f} @ ${price:.3f} "
                    f"| {question} | Reason: {result.reason}"
                )
                continue

            # Passed all filters — calculate size and execute
            trade_size = self._calculate_trade_size(address, trade)

            self.db.log_filter_decision(
                trade_id, address, condition_id,
                "COPY", "passed_all_filters", {"trade_size": trade_size},
            )

            if config.PAPER_MODE:
                self._execute_paper_trade(
                    trade_id, address, leader_name, trade, market_info, trade_size
                )
            else:
                # TODO: Live execution via Polymarket CLOB
                log.warning("LIVE MODE NOT IMPLEMENTED YET")

    def initialize(self, paper_balance: float = 500.0):
        """Initialize the monitor — set starting state and seed last_seen timestamps."""
        self.paper_balance = paper_balance
        self.paper_starting_balance = paper_balance

        log.info(f"{'='*60}")
        log.info(f"🤖 COPY TRADER {'(PAPER MODE)' if config.PAPER_MODE else '(LIVE MODE)'}")
        log.info(f"{'='*60}")
        log.info(f"Starting balance: ${paper_balance:.2f}")
        log.info(f"Leaders: {len([l for l in config.LEADERS.values() if l.get('enabled')])}")
        log.info(f"Poll interval: {config.POLL_INTERVAL_SEC}s")
        log.info(f"Sizing: {config.SIZING_MODE} (${config.FIXED_TRADE_SIZE})")
        log.info(f"Max exposure: ${config.MAX_EXPOSURE_TOTAL}")
        log.info(f"Daily loss limit: ${config.DAILY_LOSS_LIMIT}")
        log.info(f"{'='*60}")

        # Seed last_seen so we don't copy old trades on first run
        # Use current time as baseline — only trades AFTER bot start get processed
        now = int(time.time())
        log.info("Seeding last_seen timestamps (only new trades after bot start)...")
        for address, cfg in config.LEADERS.items():
            if not cfg.get('enabled'):
                continue
            self.last_seen[address] = now
            trades = self._get_leader_activity(address, limit=5)
            name = cfg.get('name', address[:10])
            if trades:
                max_ts = max(t.get('timestamp', 0) for t in trades)
                dt = datetime.fromtimestamp(max_ts, tz=timezone.utc)
                log.info(f"  {name}: last trade {dt.strftime('%m/%d %H:%M UTC')} (ignoring)")
            else:
                log.info(f"  {name}: no recent trades")
            time.sleep(0.5)  # Rate limiting

        log.info(f"\n✅ Monitoring started. Waiting for new leader trades...\n")

    def run(self, paper_balance: float = 500.0):
        """Main loop — poll leaders and process trades."""
        self.initialize(paper_balance)
        self.running = True

        while self.running:
            try:
                # Check leaders for new trades
                for address in config.LEADERS:
                    if not self.running:
                        break
                    self.process_leader(address)
                    time.sleep(0.5)  # Rate limit between leaders

                # Check open positions for exit signals
                self.exit_monitor.check_all_positions()

                time.sleep(config.POLL_INTERVAL_SEC)

            except KeyboardInterrupt:
                log.info("\n⛔ Stopping copy trader...")
                self.running = False
            except Exception as e:
                log.error(f"Error in main loop: {e}")
                time.sleep(5)

        self._print_summary()

    def _print_summary(self):
        """Print final paper trading summary."""
        positions = self.db.get_open_positions()
        log.info(f"\n{'='*60}")
        log.info(f"📊 SESSION SUMMARY")
        log.info(f"{'='*60}")
        log.info(f"Open positions: {len(positions)}")
        log.info(f"Paper balance: ${self.paper_balance:.2f}")
        total_deployed = sum(p['usdc_size'] for p in positions)
        log.info(f"Total deployed: ${total_deployed:.2f}")
        log.info(f"Total value: ${self.paper_balance + total_deployed:.2f}")
        log.info(f"P&L (unrealized): ${self.paper_balance + total_deployed - self.paper_starting_balance:.2f}")
