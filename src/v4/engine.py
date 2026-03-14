"""
The Outsiders v4 — Information Edge Trading Engine
====================================================
A fundamentally different approach to Polymarket trading.

Instead of: TA indicators → predict direction → scalp 5-min crypto
Now:        Scan markets → estimate probability → find mispricing → limit orders → wait

Core principles:
1. Only trade when we have quantifiable edge (>4% divergence from market)
2. Limit orders only (0% maker fee, never cross spread)
3. Kelly Criterion position sizing (0.25x fractional)
4. 5-15 high-conviction trades per week, not hundreds of coin flips
5. Every trade has a documented thesis

Usage:
    python -m src.v4.engine
"""

import time
import signal
import sys
import os
from datetime import datetime

from .config import *
from .market_scanner import MarketScanner
from .edge_detector import EdgeDetector
from .executor import Executor
from .tracker import PerformanceTracker


class TradingEngine:
    """Main v4 trading engine — scan, evaluate, execute, track."""
    
    def __init__(self):
        self.scanner = MarketScanner()
        self.edge_detector = EdgeDetector()
        self.executor = Executor()
        self.tracker = PerformanceTracker()
        
        self.running = True
        signal.signal(signal.SIGINT, self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)
        
        # State
        self.positions = {}       # position_id -> OrderState
        self.scanned_markets = set()  # Avoid re-scanning same market within cycle
        self.daily_trades = 0
        self.daily_date = datetime.now(PST).date()
    
    def _shutdown(self, *args):
        self.running = False
        print("\n⏹️ Shutting down v4 engine...")
    
    def log(self, msg: str):
        ts = datetime.now(PST).strftime("%I:%M:%S %p")
        line = f"[{ts}] {msg}"
        print(line)
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    
    def run(self):
        """Main loop — scan every 15 minutes, manage positions."""
        print("🏞 The Outsiders v4 — Information Edge Engine")
        print("=" * 60)
        print(f"💰 Bankroll: ${self.edge_detector.bankroll:.2f}")
        print(f"🎯 Min edge: {MIN_EDGE_PCT*100:.0f}% | Kelly: {KELLY_FRACTION}x")
        print(f"📊 Limit orders only (0% maker fee)")
        print(f"🔍 Scanning every 15 minutes for mispriced markets")
        print(f"📋 Target: {TARGET_TRADES_PER_WEEK[0]}-{TARGET_TRADES_PER_WEEK[1]} trades/week")
        print("=" * 60)
        
        # Get initial balance
        balance = self.executor.get_balance()
        self.edge_detector.update_bankroll(balance)
        self.log(f"💰 Balance: ${balance:.2f}")
        
        last_scan = 0
        last_order_check = 0
        last_stats = 0
        scan_interval = 900      # 15 minutes between full scans
        order_check_interval = 60 # Check order status every minute
        stats_interval = 3600     # Print stats every hour
        
        while self.running:
            try:
                now = time.time()
                
                # ── Daily reset ──
                today = datetime.now(PST).date()
                if today != self.daily_date:
                    self.log(f"📅 New day: {today}")
                    self.daily_date = today
                    self.daily_trades = 0
                    self.scanned_markets.clear()
                
                # ── Risk check ──
                stats = self.tracker.get_rolling_stats()
                if stats["drawdown"] > MAX_DRAWDOWN_PCT:
                    self.log(f"🛑 DRAWDOWN LIMIT HIT ({stats['drawdown']*100:.1f}%). Pausing trading.")
                    time.sleep(3600)
                    continue
                
                # ── Scan for opportunities ──
                if now - last_scan > scan_interval:
                    self._scan_and_evaluate()
                    last_scan = now
                
                # ── Check order fills ──
                if now - last_order_check > order_check_interval:
                    self._check_orders()
                    last_order_check = now
                
                # ── Check for stale orders ──
                cancelled = self.executor.check_stale_orders()
                for order in cancelled:
                    self.log(f"⏰ Cancelled stale order: {order.signal.market.question[:50]}...")
                
                # ── Print stats ──
                if now - last_stats > stats_interval:
                    self.tracker.print_summary()
                    last_stats = now
                
                time.sleep(10)
                
            except Exception as e:
                self.log(f"❌ Error: {e}")
                import traceback
                traceback.print_exc()
                time.sleep(60)
        
        # Final summary
        self.tracker.print_summary()
    
    def _scan_and_evaluate(self):
        """Scan markets, detect edges, place orders."""
        self.log("🔍 Scanning Polymarket for opportunities...")
        
        # Scan
        markets = self.scanner.scan(limit=200)
        edges_found = 0
        trades_placed = 0
        
        self.log(f"  📊 {len(markets)} markets passed filters")
        
        for market in markets:
            if market.slug in self.scanned_markets:
                continue
            self.scanned_markets.add(market.slug)
            
            # Check if we already have a position in this market
            # (don't double up)
            existing = [p for p in self.positions.values() 
                       if p.signal.market.condition_id == market.condition_id]
            if existing:
                continue
            
            # Detect edge
            signal = self.edge_detector.evaluate(market)
            if not signal:
                continue
            
            if signal.recommended_size <= 0:
                continue
            
            edges_found += 1
            
            self.log(f"\n  🎯 EDGE DETECTED: {market.question[:60]}")
            self.log(f"     Market: ${signal.market_price:.3f} | Our estimate: ${signal.our_probability:.3f}")
            self.log(f"     Edge: {signal.edge_pct*100:.1f}% | EV: ${signal.expected_value:.3f}")
            self.log(f"     Kelly: {signal.kelly_fraction*100:.1f}% → ${signal.recommended_size:.2f}")
            self.log(f"     Thesis: {signal.thesis[:80]}")
            
            # Check portfolio limits
            if len(self.positions) >= MAX_OPEN_POSITIONS:
                self.log(f"     ⚠️ Max positions reached ({MAX_OPEN_POSITIONS})")
                continue
            
            # Record and execute
            position_id = self.tracker.record_signal(signal)
            order = self.executor.place_limit_buy(signal)
            
            if order:
                order.signal = signal
                self.positions[position_id] = order
                trades_placed += 1
                self.daily_trades += 1
                self.log(f"     ✅ Limit order placed: {order.size} shares @ ${order.price:.3f}")
            else:
                self.log(f"     ❌ Order placement failed")
        
        # Record scan
        self.tracker.record_scan(
            scanned=200,
            passed=len(markets),
            edges=edges_found,
            trades=trades_placed,
        )
        
        self.log(f"\n  📋 Scan complete: {len(markets)} markets, {edges_found} edges, {trades_placed} orders placed")
    
    def _check_orders(self):
        """Check order status and handle fills."""
        filled = self.executor.check_fills()
        
        for order in filled:
            self.log(f"  ✅ FILLED: {order.signal.market.question[:50]}...")
            self.log(f"     {order.fill_shares} shares @ ${order.fill_price:.3f}")
            
            # Find position ID
            for pid, pos in self.positions.items():
                if pos.order_id == order.order_id:
                    self.tracker.record_fill(pid, order.fill_price, order.fill_shares)
                    break
    
    def run_scan_only(self):
        """
        Dry-run mode: scan and evaluate markets without placing orders.
        Useful for testing the scanner and edge detector.
        """
        print("🔍 DRY RUN — Scanning Polymarket (no orders will be placed)")
        print("=" * 60)
        
        markets = self.scanner.scan(limit=200)
        
        print(f"\n📊 {len(markets)} markets passed liquidity/timing filters\n")
        
        if not markets:
            print("No markets found. Check filters or API connectivity.")
            return
        
        # Show top markets
        print(f"{'='*80}")
        print(f"TOP OPPORTUNITIES (by liquidity × edge potential)")
        print(f"{'='*80}")
        
        edges = []
        for i, market in enumerate(markets[:30]):
            signal = self.edge_detector.evaluate(market)
            
            score = market.liquidity_score * market.edge_potential
            print(f"\n{i+1}. {market.question[:70]}")
            print(f"   Category: {market.category} | Resolves in: {market.hours_to_resolution:.0f}h")
            print(f"   Prices: {' / '.join(f'{o}={p:.2f}' for o, p in zip(market.outcomes, market.prices))}")
            print(f"   Liquidity: bid ${market.bid_depth_usd:.0f} / ask ${market.ask_depth_usd:.0f} | Spread: {market.spread_pct*100:.1f}%")
            print(f"   Volume 24h: ${market.volume_24h:,.0f}")
            print(f"   Score: liquidity={market.liquidity_score:.2f} × edge={market.edge_potential:.2f} = {score:.2f}")
            
            if signal:
                edges.append(signal)
                print(f"   🎯 EDGE: {signal.edge_pct*100:.1f}% | Side: {signal.side} | ${signal.recommended_size:.2f}")
                print(f"   Thesis: {signal.thesis[:80]}")
        
        print(f"\n{'='*80}")
        print(f"SUMMARY: {len(markets)} tradeable markets, {len(edges)} with detected edge")
        print(f"{'='*80}")


def main():
    """Entry point."""
    engine = TradingEngine()
    
    if "--dry-run" in sys.argv or "--scan" in sys.argv:
        engine.run_scan_only()
    else:
        engine.run()


if __name__ == "__main__":
    main()
