"""
🏞 The Outsiders — Wallet Analyzer

Scans Polymarket on-chain trade data for BTC 5-min markets to find:
- Consistently winning wallets
- Trading patterns (timing, sizing, entry prices, direction bias)
- Signals we can reverse-engineer into our strategy

Uses the public data-api.polymarket.com endpoint.
"""
import requests
import json
import time
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from collections import defaultdict

BASE_URL = "https://data-api.polymarket.com"
GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "wallet_analysis.db")


def init_wallet_db():
    """Create wallet analysis database."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS market_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT NOT NULL,
            window_ts INTEGER NOT NULL,
            winner TEXT,
            wallet TEXT NOT NULL,
            pseudonym TEXT,
            side TEXT NOT NULL,
            outcome TEXT NOT NULL,
            price REAL NOT NULL,
            size REAL NOT NULL,
            timestamp INTEGER NOT NULL,
            tx_hash TEXT,
            won INTEGER,
            UNIQUE(slug, wallet, timestamp, side, outcome)
        );
        
        CREATE TABLE IF NOT EXISTS wallet_profiles (
            wallet TEXT PRIMARY KEY,
            pseudonym TEXT,
            total_markets INTEGER DEFAULT 0,
            total_trades INTEGER DEFAULT 0,
            wins INTEGER DEFAULT 0,
            losses INTEGER DEFAULT 0,
            win_rate REAL DEFAULT 0,
            total_volume REAL DEFAULT 0,
            est_profit REAL DEFAULT 0,
            avg_entry_price REAL DEFAULT 0,
            avg_timing_secs REAL DEFAULT 0,
            direction_bias TEXT,
            last_updated TEXT
        );
        
        CREATE TABLE IF NOT EXISTS scan_log (
            slug TEXT PRIMARY KEY,
            scanned_at TEXT NOT NULL,
            trade_count INTEGER DEFAULT 0,
            winner TEXT
        );
        
        CREATE INDEX IF NOT EXISTS idx_mt_slug ON market_trades(slug);
        CREATE INDEX IF NOT EXISTS idx_mt_wallet ON market_trades(wallet);
        CREATE INDEX IF NOT EXISTS idx_mt_won ON market_trades(won);
    """)
    conn.commit()
    return conn


def get_market_resolution(slug):
    """Check if a BTC 5-min market has resolved and who won."""
    try:
        r = requests.get(f"{GAMMA_URL}/events", params={"slug": slug}, timeout=10)
        if r.status_code != 200 or not r.json():
            return None
        
        event = r.json()[0]
        market = event.get("markets", [{}])[0]
        
        if not market.get("closed"):
            return None
        
        # Check outcomes
        prices = market.get("outcomePrices")
        outcomes = market.get("outcomes")
        if isinstance(prices, str): prices = json.loads(prices)
        if isinstance(outcomes, str): outcomes = json.loads(outcomes)
        
        if prices and outcomes:
            for i, p in enumerate(prices):
                if float(p) >= 0.95:
                    return outcomes[i].lower()
        
        # Try CLOB
        condition_id = market.get("conditionId")
        if condition_id:
            time.sleep(0.5)
            cr = requests.get(f"{CLOB_URL}/markets/{condition_id}", timeout=10)
            if cr.status_code == 200:
                for token in cr.json().get("tokens", []):
                    if token.get("winner"):
                        return token["outcome"].lower()
        
        return None
    except:
        return None


def get_market_trades(slug, limit=200):
    """Get all trades for a specific market slug."""
    try:
        r = requests.get(f"{BASE_URL}/trades", params={"slug": slug, "limit": limit}, timeout=15)
        if r.status_code == 200:
            return r.json()
    except:
        pass
    return []


def scan_markets(hours=6, conn=None):
    """
    Scan N hours of BTC 5-min markets and store all trade data.
    Skips already-scanned markets.
    """
    if conn is None:
        conn = init_wallet_db()
    
    now = int(time.time())
    base_ts = ((now // 300) * 300)
    windows = int(hours * 12)  # 12 windows per hour
    
    # Get already scanned slugs
    scanned = set(row[0] for row in 
                  conn.execute("SELECT slug FROM scan_log").fetchall())
    
    new_markets = 0
    new_trades = 0
    
    for i in range(3, windows + 3):  # skip last 3 (may not be resolved)
        ts = base_ts - (i * 300)
        slug = f"btc-updown-5m-{ts}"
        
        if slug in scanned:
            continue
        
        time.sleep(0.5)
        winner = get_market_resolution(slug)
        if winner is None:
            continue
        
        time.sleep(0.5)
        trades = get_market_trades(slug)
        
        if not trades:
            continue
        
        # Filter to BTC-specific trades only
        btc_trades = []
        for t in trades:
            outcome = t.get("outcome", "").lower()
            # Only keep Up/Down outcomes (BTC 5-min specific)
            if outcome not in ("up", "down"):
                continue
            
            price = float(t.get("price", 0))
            size = float(t.get("size", 0))
            side = t.get("side", "").upper()
            
            # Skip dust trades
            if size < 0.5:
                continue
            
            # Determine win/loss
            won = (side == "BUY" and outcome == winner) or \
                  (side == "SELL" and outcome != winner)
            
            btc_trades.append({
                "slug": slug,
                "window_ts": ts,
                "winner": winner,
                "wallet": t["proxyWallet"],
                "pseudonym": t.get("pseudonym", "") or t.get("name", ""),
                "side": side,
                "outcome": outcome,
                "price": price,
                "size": size,
                "timestamp": t.get("timestamp", 0),
                "tx_hash": t.get("transactionHash", ""),
                "won": 1 if won else 0,
            })
        
        # Insert trades
        for trade in btc_trades:
            try:
                conn.execute("""
                    INSERT OR IGNORE INTO market_trades 
                    (slug, window_ts, winner, wallet, pseudonym, side, outcome, 
                     price, size, timestamp, tx_hash, won)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (trade["slug"], trade["window_ts"], trade["winner"],
                      trade["wallet"], trade["pseudonym"], trade["side"],
                      trade["outcome"], trade["price"], trade["size"],
                      trade["timestamp"], trade["tx_hash"], trade["won"]))
            except sqlite3.IntegrityError:
                pass
        
        conn.execute("""
            INSERT OR REPLACE INTO scan_log (slug, scanned_at, trade_count, winner)
            VALUES (?, ?, ?, ?)
        """, (slug, datetime.now(timezone.utc).isoformat(), len(btc_trades), winner))
        
        conn.commit()
        new_markets += 1
        new_trades += len(btc_trades)
        
        if new_markets % 10 == 0:
            print(f"  Scanned {new_markets} new markets, {new_trades} trades...")
    
    return new_markets, new_trades


def build_wallet_profiles(conn=None, min_markets=5, min_volume=50):
    """
    Analyze all collected trade data and build wallet profiles.
    Only includes wallets with meaningful trade history.
    """
    if conn is None:
        conn = init_wallet_db()
    
    # Get all trades grouped by wallet
    rows = conn.execute("""
        SELECT wallet, pseudonym, slug, side, outcome, price, size, 
               timestamp, window_ts, won, winner
        FROM market_trades
        WHERE price >= 0.10 AND price <= 0.90
        ORDER BY wallet, timestamp
    """).fetchall()
    
    wallets = defaultdict(lambda: {
        "pseudonym": "", "markets": set(), "trades": [],
        "wins": 0, "losses": 0, "volume": 0, "profit": 0,
        "prices": [], "timings": [], "directions": defaultdict(int),
    })
    
    for r in rows:
        w = r["wallet"]
        wallets[w]["pseudonym"] = r["pseudonym"] or wallets[w]["pseudonym"]
        wallets[w]["markets"].add(r["slug"])
        wallets[w]["volume"] += r["size"]
        wallets[w]["prices"].append(r["price"])
        
        timing = r["timestamp"] - r["window_ts"]
        if 0 <= timing <= 300:
            wallets[w]["timings"].append(timing)
        
        wallets[w]["directions"][f"{r['side']}_{r['outcome']}"] += 1
        
        if r["won"]:
            wallets[w]["wins"] += 1
            if r["side"] == "BUY":
                wallets[w]["profit"] += r["size"] * (1 - r["price"])
            else:
                wallets[w]["profit"] += r["size"] * r["price"]
        else:
            wallets[w]["losses"] += 1
            if r["side"] == "BUY":
                wallets[w]["profit"] -= r["size"] * r["price"]
            else:
                wallets[w]["profit"] -= r["size"] * (1 - r["price"])
        
        wallets[w]["trades"].append(dict(r))
    
    # Filter and save profiles
    conn.execute("DELETE FROM wallet_profiles")
    
    profiles = []
    for w, d in wallets.items():
        n_markets = len(d["markets"])
        if n_markets < min_markets or d["volume"] < min_volume:
            continue
        
        total = d["wins"] + d["losses"]
        wr = d["wins"] / total if total > 0 else 0
        avg_price = sum(d["prices"]) / len(d["prices"]) if d["prices"] else 0
        avg_timing = sum(d["timings"]) / len(d["timings"]) if d["timings"] else -1
        
        # Direction bias
        dirs = dict(d["directions"])
        buy_up = dirs.get("BUY_up", 0)
        buy_down = dirs.get("BUY_down", 0)
        total_buys = buy_up + buy_down
        if total_buys > 0:
            up_pct = buy_up / total_buys
            if up_pct > 0.65:
                bias = "bullish"
            elif up_pct < 0.35:
                bias = "bearish"
            else:
                bias = "neutral"
        else:
            bias = "unknown"
        
        profile = {
            "wallet": w,
            "pseudonym": d["pseudonym"],
            "total_markets": n_markets,
            "total_trades": total,
            "wins": d["wins"],
            "losses": d["losses"],
            "win_rate": wr,
            "total_volume": d["volume"],
            "est_profit": d["profit"],
            "avg_entry_price": avg_price,
            "avg_timing_secs": avg_timing,
            "direction_bias": bias,
        }
        profiles.append(profile)
        
        conn.execute("""
            INSERT OR REPLACE INTO wallet_profiles
            (wallet, pseudonym, total_markets, total_trades, wins, losses,
             win_rate, total_volume, est_profit, avg_entry_price, avg_timing_secs,
             direction_bias, last_updated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (w, d["pseudonym"], n_markets, total, d["wins"], d["losses"],
              wr, d["volume"], d["profit"], avg_price, avg_timing,
              bias, datetime.now(timezone.utc).isoformat()))
    
    conn.commit()
    return sorted(profiles, key=lambda x: -x["win_rate"])


def get_smart_money_signals(conn=None, min_wr=0.55, min_markets=10, min_volume=200):
    """
    Get current trading patterns from top-performing wallets.
    Returns aggregate direction bias from winning wallets.
    """
    if conn is None:
        conn = init_wallet_db()
    
    rows = conn.execute("""
        SELECT wallet, pseudonym, win_rate, total_volume, direction_bias,
               total_markets, wins, losses, est_profit
        FROM wallet_profiles
        WHERE win_rate >= ? AND total_markets >= ? AND total_volume >= ?
        ORDER BY win_rate DESC
    """, (min_wr, min_markets, min_volume)).fetchall()
    
    return [dict(r) for r in rows]


def print_leaderboard(profiles, top_n=20):
    """Print formatted leaderboard of top wallets."""
    PST = timezone(timedelta(hours=-8))
    now = datetime.now(timezone.utc).astimezone(PST).strftime("%I:%M %p PST")
    
    print(f"\n🏞 The Outsiders — Wallet Leaderboard")
    print(f"Generated: {now}")
    print("=" * 90)
    print(f"{'#':>3} {'Name':<28} {'Markets':>7} {'Record':>10} {'WR%':>6} "
          f"{'Volume':>10} {'Est P&L':>10} {'Bias':>8}")
    print("=" * 90)
    
    for i, p in enumerate(profiles[:top_n], 1):
        wr = p["win_rate"] * 100
        record = f"{p['wins']}W/{p['losses']}L"
        
        if wr >= 60:
            marker = "🏆"
        elif wr >= 55:
            marker = "📈"
        elif wr <= 40:
            marker = "📉"
        else:
            marker = "  "
        
        print(f"{i:>3} {marker}{p['pseudonym'][:26]:<26} {p['total_markets']:>7} "
              f"{record:>10} {wr:>5.1f}% ${p['total_volume']:>9,.1f} "
              f"${p['est_profit']:>+9,.1f} {p['direction_bias']:>8}")
    
    # Summary stats
    if profiles:
        avg_wr = sum(p["win_rate"] for p in profiles) / len(profiles)
        total_vol = sum(p["total_volume"] for p in profiles)
        profitable = sum(1 for p in profiles if p["est_profit"] > 0)
        
        print("=" * 90)
        print(f"Total wallets: {len(profiles)} | "
              f"Avg WR: {avg_wr*100:.1f}% | "
              f"Profitable: {profitable}/{len(profiles)} ({profitable/len(profiles)*100:.0f}%) | "
              f"Total volume: ${total_vol:,.0f}")


def extract_patterns(conn=None):
    """
    Extract actionable trading patterns from the data.
    This is the intelligence we feed back into our strategies.
    """
    if conn is None:
        conn = init_wallet_db()
    
    print("\n🔍 PATTERN ANALYSIS")
    print("=" * 60)
    
    # 1. Do winners buy at different prices than losers?
    rows = conn.execute("""
        SELECT won, AVG(price) as avg_price, COUNT(*) as cnt,
               AVG(size) as avg_size
        FROM market_trades
        WHERE price >= 0.10 AND price <= 0.90 AND side = 'BUY'
        GROUP BY won
    """).fetchall()
    
    print("\n📊 Entry Price: Winners vs Losers")
    for r in rows:
        label = "Winners" if r["won"] else "Losers"
        print(f"  {label}: avg price=${r['avg_price']:.4f}, "
              f"avg size={r['avg_size']:.1f}, count={r['cnt']}")
    
    # 2. Does entry timing matter?
    rows = conn.execute("""
        SELECT 
            CASE 
                WHEN (timestamp - window_ts) < 60 THEN 'early_0-60s'
                WHEN (timestamp - window_ts) < 180 THEN 'mid_60-180s'
                ELSE 'late_180-300s'
            END as timing_bucket,
            AVG(won) as win_rate,
            COUNT(*) as cnt
        FROM market_trades
        WHERE price >= 0.10 AND price <= 0.90
            AND (timestamp - window_ts) >= 0 
            AND (timestamp - window_ts) <= 300
        GROUP BY timing_bucket
        ORDER BY timing_bucket
    """).fetchall()
    
    print("\n⏱️ Win Rate by Entry Timing")
    for r in rows:
        print(f"  {r['timing_bucket']:>15}: WR={r['win_rate']*100:.1f}%, trades={r['cnt']}")
    
    # 3. Do bigger traders win more?
    rows = conn.execute("""
        SELECT 
            CASE 
                WHEN size < 10 THEN 'small_<$10'
                WHEN size < 50 THEN 'medium_$10-50'
                WHEN size < 200 THEN 'large_$50-200'
                ELSE 'whale_$200+'
            END as size_bucket,
            AVG(won) as win_rate,
            COUNT(*) as cnt
        FROM market_trades
        WHERE price >= 0.10 AND price <= 0.90
        GROUP BY size_bucket
        ORDER BY size_bucket
    """).fetchall()
    
    print("\n💰 Win Rate by Trade Size")
    for r in rows:
        print(f"  {r['size_bucket']:>20}: WR={r['win_rate']*100:.1f}%, trades={r['cnt']}")
    
    # 4. Entry price vs win rate
    rows = conn.execute("""
        SELECT 
            CASE 
                WHEN price < 0.30 THEN 'low_<0.30'
                WHEN price < 0.45 THEN 'mid-low_0.30-0.45'
                WHEN price < 0.55 THEN 'mid_0.45-0.55'
                WHEN price < 0.70 THEN 'mid-high_0.55-0.70'
                ELSE 'high_0.70+'
            END as price_bucket,
            AVG(won) as win_rate,
            COUNT(*) as cnt,
            AVG(size) as avg_size
        FROM market_trades
        WHERE price >= 0.10 AND price <= 0.90 AND side = 'BUY'
        GROUP BY price_bucket
        ORDER BY price_bucket
    """).fetchall()
    
    print("\n🎯 Win Rate by Entry Price")
    for r in rows:
        print(f"  {r['price_bucket']:>20}: WR={r['win_rate']*100:.1f}%, "
              f"avg_size=${r['avg_size']:.1f}, trades={r['cnt']}")
    
    # 5. Direction bias in recent markets (are ups or downs winning more?)
    rows = conn.execute("""
        SELECT winner, COUNT(DISTINCT slug) as markets
        FROM market_trades
        GROUP BY winner
    """).fetchall()
    
    print("\n🎲 Market Outcome Distribution")
    for r in rows:
        print(f"  {r['winner'].upper()}: {r['markets']} markets")
    
    # 6. Contrarian vs momentum analysis
    rows = conn.execute("""
        SELECT 
            CASE WHEN outcome = winner THEN 'with_winner' ELSE 'against_winner' END as alignment,
            side, AVG(won) as win_rate, COUNT(*) as cnt
        FROM market_trades
        WHERE price >= 0.10 AND price <= 0.90
        GROUP BY alignment, side
    """).fetchall()
    
    print("\n🔄 Trade Alignment Analysis")
    for r in rows:
        print(f"  {r['side']} {r['alignment']:>15}: WR={r['win_rate']*100:.1f}%, trades={r['cnt']}")


def run_full_analysis(hours=12):
    """Run complete wallet analysis pipeline."""
    conn = init_wallet_db()
    
    print(f"🏞 The Outsiders — Wallet Analyzer")
    print(f"Scanning last {hours} hours of BTC 5-min markets...")
    print("-" * 50)
    
    # Step 1: Scan markets
    new_markets, new_trades = scan_markets(hours=hours, conn=conn)
    
    total_markets = conn.execute("SELECT COUNT(*) FROM scan_log").fetchone()[0]
    total_trades = conn.execute("SELECT COUNT(*) FROM market_trades").fetchone()[0]
    
    print(f"\n✅ Scan complete: {new_markets} new markets, {new_trades} new trades")
    print(f"   Total in DB: {total_markets} markets, {total_trades} trades")
    
    # Step 2: Build profiles
    print("\n📊 Building wallet profiles...")
    profiles = build_wallet_profiles(conn=conn, min_markets=5, min_volume=50)
    print(f"   Found {len(profiles)} qualifying wallets")
    
    # Step 3: Print leaderboard
    print_leaderboard(profiles)
    
    # Step 4: Extract patterns
    extract_patterns(conn)
    
    # Step 5: Smart money summary
    smart = get_smart_money_signals(conn, min_wr=0.55, min_markets=8, min_volume=100)
    if smart:
        print(f"\n🧠 SMART MONEY ({len(smart)} wallets with >55% WR, 8+ markets, $100+ vol)")
        bullish = sum(1 for s in smart if s["direction_bias"] == "bullish")
        bearish = sum(1 for s in smart if s["direction_bias"] == "bearish")
        neutral = len(smart) - bullish - bearish
        print(f"   Bias: {bullish} bullish | {bearish} bearish | {neutral} neutral")
    
    conn.close()
    return profiles


if __name__ == "__main__":
    import sys
    hours = float(sys.argv[1]) if len(sys.argv) > 1 else 12
    run_full_analysis(hours=hours)
