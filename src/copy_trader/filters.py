"""
Copy Trader Filter Pipeline
Each filter returns (passed: bool, reason: str, details: dict)
Trade must pass ALL filters to be copied.
"""
import time
from . import config
from .database import CopyTraderDB
from .market_rules import classify_market, parse_market_window, get_entry_rules


class FilterResult:
    def __init__(self, passed: bool, reason: str, details: dict = None):
        self.passed = passed
        self.reason = reason
        self.details = details or {}

    def __bool__(self):
        return self.passed

    def __repr__(self):
        status = "✅ PASS" if self.passed else "❌ SKIP"
        return f"{status}: {self.reason}"


class FilterPipeline:
    def __init__(self, db: CopyTraderDB):
        self.db = db

    def evaluate(self, leader_address: str, leader_name: str,
                 trade_data: dict, market_info: dict) -> FilterResult:
        """
        Run all filters. Returns first failure or final pass.
        """
        filters = [
            self._filter_leader_enabled,
            self._filter_leader_paused,
            self._filter_trade_side,
            self._filter_trade_age,
            self._filter_block_crypto_updown,
            self._filter_crypto_entry_window,
            self._filter_market_resolve_time,
            self._filter_market_age,
            self._filter_entry_price,
            self._filter_leader_trade_size,
            self._filter_market_liquidity,
            self._filter_market_volume,
            self._filter_market_category,
            self._filter_blocked_market,
            self._filter_duplicate_position,
            self._filter_recent_sell,
            self._filter_leader_win_rate,
            self._filter_leader_min_trades,
            self._filter_conflict,
            self._filter_max_positions_total,
            self._filter_max_positions_per_market,
            self._filter_max_exposure_total,
            self._filter_max_exposure_per_leader,
            self._filter_max_exposure_per_market,
            self._filter_max_exposure_per_category,
            self._filter_daily_loss_limit,
        ]

        for f in filters:
            result = f(leader_address, leader_name, trade_data, market_info)
            if not result:
                return result

        return FilterResult(True, "passed_all_filters")

    # ─── Block Crypto Up/Down ──────────────────────────

    def _filter_block_crypto_updown(self, leader_address, leader_name, trade, market):
        """Block ALL crypto up/down markets from copy trading.
        We run our own drift sniper for these — don't let copy trader interfere."""
        question = market.get('question', '')
        market_type = classify_market(question, market)
        if market_type.startswith('crypto_'):
            return FilterResult(False, "crypto_updown_blocked",
                                {"market_type": market_type,
                                 "reason": "drift sniper handles crypto"})
        return FilterResult(True, "not_crypto_updown", {"market_type": market_type})

    # ─── Crypto Entry Window ───────────────────────────

    def _filter_crypto_entry_window(self, leader_address, leader_name, trade, market):
        """For crypto markets: only copy if trade is within first 2 minutes of window."""
        question = market.get('question', '')
        market_type = classify_market(question, market)

        if not market_type.startswith('crypto_'):
            return FilterResult(True, "not_crypto", {"market_type": market_type})

        rules = get_entry_rules(market_type)
        max_entry_sec = rules.get('max_entry_sec')
        if max_entry_sec is None:
            return FilterResult(True, "no_entry_limit")

        # Parse window start time
        window_start, window_end = parse_market_window(question)
        if window_start is None:
            return FilterResult(True, "cant_parse_window")  # Allow if we can't parse

        trade_ts = trade.get('timestamp', 0)
        secs_into_window = trade_ts - window_start

        if secs_into_window < 0:
            return FilterResult(False, "crypto_before_window",
                                {"secs_before": -secs_into_window})

        if secs_into_window > max_entry_sec:
            return FilterResult(False, "crypto_entry_too_late",
                                {"secs_into_window": secs_into_window,
                                 "max_sec": max_entry_sec,
                                 "market_type": market_type})

        return FilterResult(True, "crypto_entry_ok",
                            {"secs_into_window": secs_into_window,
                             "market_type": market_type})

    # ─── Timing Filters ────────────────────────────────

    def _filter_leader_enabled(self, leader_address, leader_name, trade, market):
        leader_cfg = config.LEADERS.get(leader_address, {})
        if not leader_cfg.get('enabled', False):
            return FilterResult(False, "leader_disabled",
                                {"leader": leader_name})
        return FilterResult(True, "leader_enabled")

    def _filter_leader_paused(self, leader_address, leader_name, trade, market):
        stats = self.db.get_leader_stats(leader_address)
        if stats and stats.get('is_paused'):
            paused_until = stats.get('paused_until', 0)
            if paused_until and time.time() < paused_until:
                return FilterResult(False, "leader_paused",
                                    {"until": paused_until})
            else:
                # Cooldown expired, unpause
                self.db.unpause_leader(leader_address)
        return FilterResult(True, "leader_active")

    def _filter_trade_side(self, leader_address, leader_name, trade, market):
        side = trade.get('side', '').upper()
        if not config.REVERT_TRADE and side == 'SELL':
            return FilterResult(False, "sell_filtered",
                                {"side": side, "revert_trade": False})
        return FilterResult(True, "side_ok", {"side": side})

    def _filter_trade_age(self, leader_address, leader_name, trade, market):
        trade_ts = trade.get('timestamp', 0)
        age = time.time() - trade_ts
        if age > config.ENTRY_TRADE_SEC:
            return FilterResult(False, "trade_too_old",
                                {"age_sec": age, "max": config.ENTRY_TRADE_SEC})
        return FilterResult(True, "trade_fresh", {"age_sec": age})

    def _filter_market_resolve_time(self, leader_address, leader_name, trade, market):
        end_date = market.get('endDate', '')
        if not end_date:
            return FilterResult(True, "no_end_date")  # Many markets don't expose end dates

        try:
            from datetime import datetime
            if isinstance(end_date, str):
                if not end_date.strip():
                    return FilterResult(True, "empty_end_date")
                end_ts = datetime.fromisoformat(end_date.replace('Z', '+00:00')).timestamp()
            else:
                end_ts = float(end_date)

            time_to_resolve = end_ts - time.time()
            
            # If end date is in the past (already resolved/expired), skip
            if time_to_resolve < -86400:  # More than 1 day past
                return FilterResult(False, "market_expired",
                                    {"sec_past": -time_to_resolve})
            
            if 0 < time_to_resolve < config.TRADE_SEC_FROM_RESOLVE:
                return FilterResult(False, "too_close_to_resolve",
                                    {"sec_to_resolve": time_to_resolve,
                                     "min_required": config.TRADE_SEC_FROM_RESOLVE})
        except (ValueError, TypeError):
            pass  # Can't parse date, allow the trade

        return FilterResult(True, "resolve_time_ok")

    def _filter_market_age(self, leader_address, leader_name, trade, market):
        created = market.get('createdAt', '') or market.get('created_at', '')
        if not created:
            return FilterResult(True, "no_created_date")

        try:
            from datetime import datetime
            if isinstance(created, str):
                created_ts = datetime.fromisoformat(created.replace('Z', '+00:00')).timestamp()
            else:
                created_ts = float(created)

            market_age = time.time() - created_ts
            if market_age < config.MIN_MARKET_AGE_SEC:
                return FilterResult(False, "market_too_new",
                                    {"age_sec": market_age, "min": config.MIN_MARKET_AGE_SEC})
        except (ValueError, TypeError):
            pass

        return FilterResult(True, "market_age_ok")

    # ─── Price Filters ──────────────────────────────────

    def _filter_entry_price(self, leader_address, leader_name, trade, market):
        price = float(trade.get('price', 0) or 0)
        if price > config.MAX_ENTRY_PRICE:
            return FilterResult(False, "price_too_high",
                                {"price": price, "max": config.MAX_ENTRY_PRICE})
        if price < config.MIN_ENTRY_PRICE:
            return FilterResult(False, "price_too_low",
                                {"price": price, "min": config.MIN_ENTRY_PRICE})
        return FilterResult(True, "price_ok", {"price": price})

    def _filter_leader_trade_size(self, leader_address, leader_name, trade, market):
        usdc = float(trade.get('usdcSize', 0) or 0)
        if usdc < config.MIN_LEADER_TRADE_SIZE:
            return FilterResult(False, "leader_trade_too_small",
                                {"size": usdc, "min": config.MIN_LEADER_TRADE_SIZE})
        return FilterResult(True, "leader_size_ok", {"size": usdc})

    # ─── Market Quality ─────────────────────────────────

    def _filter_market_liquidity(self, leader_address, leader_name, trade, market):
        liquidity = float(market.get('liquidity', 0) or 0)
        if config.MIN_MARKET_LIQUIDITY > 0 and liquidity < config.MIN_MARKET_LIQUIDITY:
            return FilterResult(False, "low_liquidity",
                                {"liquidity": liquidity, "min": config.MIN_MARKET_LIQUIDITY})
        return FilterResult(True, "liquidity_ok", {"liquidity": liquidity})

    def _filter_market_volume(self, leader_address, leader_name, trade, market):
        volume = float(market.get('volume', 0) or market.get('volumeNum', 0) or 0)
        if config.MIN_MARKET_VOLUME > 0 and volume < config.MIN_MARKET_VOLUME:
            return FilterResult(False, "low_volume",
                                {"volume": volume, "min": config.MIN_MARKET_VOLUME})
        return FilterResult(True, "volume_ok", {"volume": volume})

    def _filter_market_category(self, leader_address, leader_name, trade, market):
        if "all" in config.ALLOWED_CATEGORIES:
            return FilterResult(True, "all_categories_allowed")

        category = (market.get('category', '') or '').lower()
        if category not in [c.lower() for c in config.ALLOWED_CATEGORIES]:
            return FilterResult(False, "category_blocked",
                                {"category": category, "allowed": config.ALLOWED_CATEGORIES})
        return FilterResult(True, "category_ok", {"category": category})

    def _filter_blocked_market(self, leader_address, leader_name, trade, market):
        cid = trade.get('conditionId', '')
        if cid in config.BLOCKED_MARKETS:
            return FilterResult(False, "market_blocked", {"condition_id": cid})
        return FilterResult(True, "market_not_blocked")

    # ─── Duplicate / Conflict ───────────────────────────

    def _filter_duplicate_position(self, leader_address, leader_name, trade, market):
        if not config.DUPLICATE_FILTER:
            return FilterResult(True, "duplicate_filter_disabled")

        cid = trade.get('conditionId', '')
        if self.db.has_position_in_market(cid):
            return FilterResult(False, "duplicate_position",
                                {"condition_id": cid})
        return FilterResult(True, "no_duplicate")

    def _filter_recent_sell(self, leader_address, leader_name, trade, market):
        if not config.RECENT_SELL_CHECK:
            return FilterResult(True, "sell_check_disabled")

        cid = trade.get('conditionId', '')
        if self.db.leader_recent_sells(leader_address, cid, config.RECENT_SELL_WINDOW_SEC):
            return FilterResult(False, "leader_recently_sold",
                                {"condition_id": cid, "window_sec": config.RECENT_SELL_WINDOW_SEC})
        return FilterResult(True, "no_recent_sell")

    def _filter_conflict(self, leader_address, leader_name, trade, market):
        if config.CONFLICT_RESOLUTION == "both":
            return FilterResult(True, "conflict_both_allowed")

        cid = trade.get('conditionId', '')
        open_positions = self.db.get_open_positions(condition_id=cid)

        # Check if any open position is on the opposite side from a different leader
        trade_side = trade.get('side', '')
        for pos in open_positions:
            if pos['leader_address'] != leader_address and pos['side'] != trade_side:
                if config.CONFLICT_RESOLUTION == "skip":
                    return FilterResult(False, "leader_conflict",
                                        {"existing_leader": pos['leader_name'],
                                         "existing_side": pos['side'],
                                         "new_side": trade_side})
        return FilterResult(True, "no_conflict")

    # ─── Leader Quality ─────────────────────────────────

    def _filter_leader_win_rate(self, leader_address, leader_name, trade, market):
        if config.MIN_LEADER_WIN_RATE <= 0:
            return FilterResult(True, "wr_filter_disabled")

        stats = self.db.get_leader_stats(leader_address)
        if not stats or (stats['wins'] + stats['losses']) == 0:
            return FilterResult(True, "no_stats_yet")  # No data, allow

        wr = stats['wins'] / (stats['wins'] + stats['losses'])
        if wr < config.MIN_LEADER_WIN_RATE:
            return FilterResult(False, "leader_wr_too_low",
                                {"wr": wr, "min": config.MIN_LEADER_WIN_RATE})
        return FilterResult(True, "leader_wr_ok", {"wr": wr})

    def _filter_leader_min_trades(self, leader_address, leader_name, trade, market):
        if config.MIN_LEADER_TRADES <= 0:
            return FilterResult(True, "min_trades_disabled")

        stats = self.db.get_leader_stats(leader_address)
        total = (stats['wins'] + stats['losses']) if stats else 0
        if total < config.MIN_LEADER_TRADES:
            return FilterResult(False, "not_enough_leader_history",
                                {"observed": total, "min": config.MIN_LEADER_TRADES})
        return FilterResult(True, "enough_history", {"observed": total})

    # ─── Risk Limits ────────────────────────────────────

    def _filter_max_positions_total(self, leader_address, leader_name, trade, market):
        count = self.db.count_open_positions()
        if count >= config.MAX_POSITIONS_TOTAL:
            return FilterResult(False, "max_positions_total",
                                {"current": count, "max": config.MAX_POSITIONS_TOTAL})
        return FilterResult(True, "positions_total_ok", {"current": count})

    def _filter_max_positions_per_market(self, leader_address, leader_name, trade, market):
        cid = trade.get('conditionId', '')
        count = self.db.count_open_positions(condition_id=cid)
        if count >= config.MAX_POSITIONS_PER_MARKET:
            return FilterResult(False, "max_positions_per_market",
                                {"current": count, "max": config.MAX_POSITIONS_PER_MARKET})
        return FilterResult(True, "positions_market_ok", {"current": count})

    def _filter_max_exposure_total(self, leader_address, leader_name, trade, market):
        exposure = self.db.get_total_exposure()
        if exposure >= config.MAX_EXPOSURE_TOTAL:
            return FilterResult(False, "max_exposure_total",
                                {"current": exposure, "max": config.MAX_EXPOSURE_TOTAL})
        return FilterResult(True, "exposure_total_ok", {"current": exposure})

    def _filter_max_exposure_per_leader(self, leader_address, leader_name, trade, market):
        exposure = self.db.get_total_exposure(leader_address=leader_address)
        if exposure >= config.MAX_EXPOSURE_PER_LEADER:
            return FilterResult(False, "max_exposure_per_leader",
                                {"current": exposure, "max": config.MAX_EXPOSURE_PER_LEADER})
        return FilterResult(True, "exposure_leader_ok", {"current": exposure})

    def _filter_max_exposure_per_market(self, leader_address, leader_name, trade, market):
        cid = trade.get('conditionId', '')
        exposure = self.db.get_total_exposure(condition_id=cid)
        if exposure >= config.MAX_EXPOSURE_PER_MARKET:
            return FilterResult(False, "max_exposure_per_market",
                                {"current": exposure, "max": config.MAX_EXPOSURE_PER_MARKET})
        return FilterResult(True, "exposure_market_ok", {"current": exposure})

    def _filter_max_exposure_per_category(self, leader_address, leader_name, trade, market):
        if config.MAX_EXPOSURE_PER_CATEGORY >= 1.0:
            return FilterResult(True, "category_cap_disabled")

        category = (market.get('category', '') or '').lower()
        if not category:
            return FilterResult(True, "no_category")

        cat_exposure = self.db.get_total_exposure(category=category)
        total_exposure = self.db.get_total_exposure()
        if total_exposure > 0:
            cat_pct = cat_exposure / max(total_exposure, config.MAX_EXPOSURE_TOTAL)
            if cat_pct >= config.MAX_EXPOSURE_PER_CATEGORY:
                return FilterResult(False, "max_exposure_per_category",
                                    {"category": category, "pct": cat_pct,
                                     "max_pct": config.MAX_EXPOSURE_PER_CATEGORY})
        return FilterResult(True, "category_exposure_ok")

    def _filter_daily_loss_limit(self, leader_address, leader_name, trade, market):
        daily_pnl = self.db.get_today_realized_pnl()
        if daily_pnl <= -config.DAILY_LOSS_LIMIT:
            return FilterResult(False, "daily_loss_limit_hit",
                                {"daily_pnl": daily_pnl, "limit": config.DAILY_LOSS_LIMIT})
        return FilterResult(True, "daily_pnl_ok", {"daily_pnl": daily_pnl})
