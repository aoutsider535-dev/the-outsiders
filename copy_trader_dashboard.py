"""
Copy Trader Dashboard
Run: .venv/bin/python3 -m streamlit run copy_trader_dashboard.py --server.port 8503
"""
import streamlit as st
import sqlite3
import pandas as pd
import json
import time
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

st.set_page_config(page_title="Copy Trader", layout="wide", page_icon="🤖")

DB_PATH = "data/copy_trader.db"

# ═══════════════════════════════════════════════════════════
# DATABASE HELPERS
# ═══════════════════════════════════════════════════════════

@st.cache_resource
def get_db():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def query(sql, params=None):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        c = conn.cursor()
        c.execute(sql, params or [])
        rows = [dict(r) for r in c.fetchall()]
        return pd.DataFrame(rows) if rows else pd.DataFrame()
    finally:
        conn.close()

def query_one(sql, params=None):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        c = conn.cursor()
        c.execute(sql, params or [])
        row = c.fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()

# ═══════════════════════════════════════════════════════════
# CHECK BOT STATUS
# ═══════════════════════════════════════════════════════════

def is_bot_running():
    try:
        result = subprocess.run(['pgrep', '-f', 'copy_trader'], capture_output=True, text=True)
        return result.returncode == 0
    except:
        return False

# ═══════════════════════════════════════════════════════════
# HEADER
# ═══════════════════════════════════════════════════════════

bot_running = is_bot_running()
status_emoji = "🟢" if bot_running else "🔴"
status_text = "RUNNING" if bot_running else "STOPPED"

st.markdown(f"""
# 🤖 Copy Trader Dashboard
### {status_emoji} Bot Status: **{status_text}** (Paper Mode)
""")

# Auto-refresh
refresh_rate = st.sidebar.selectbox("Auto-refresh", [5, 10, 30, 60], index=1)
st.sidebar.markdown(f"*Refreshes every {refresh_rate}s*")

if not Path(DB_PATH).exists():
    st.warning("No database found. Start the copy trader first.")
    st.stop()

# ═══════════════════════════════════════════════════════════
# TOP METRICS
# ═══════════════════════════════════════════════════════════

col1, col2, col3, col4, col5, col6 = st.columns(6)

# Positions
open_positions = query("SELECT * FROM positions WHERE status = 'open'")
closed_positions = query("SELECT * FROM positions WHERE status IN ('resolved_win', 'resolved_loss')")
all_positions = query("SELECT * FROM positions")

total_open = len(open_positions)
total_deployed = open_positions['usdc_size'].sum() if len(open_positions) > 0 else 0
total_pnl = closed_positions['pnl'].sum() if len(closed_positions) > 0 else 0

# Filter stats
filter_log = query("SELECT * FROM filter_log")
total_copied = len(filter_log[filter_log['decision'] == 'COPY']) if len(filter_log) > 0 else 0
total_skipped = len(filter_log[filter_log['decision'] == 'SKIP']) if len(filter_log) > 0 else 0

# Leader trades
leader_trades = query("SELECT * FROM leader_trades")
total_seen = len(leader_trades)

col1.metric("📊 Trades Seen", f"{total_seen}")
col2.metric("✅ Copied", f"{total_copied}")
col3.metric("⏭️ Skipped", f"{total_skipped}")
col4.metric("📂 Open Positions", f"{total_open}")
col5.metric("💰 Deployed", f"${total_deployed:,.2f}")
col6.metric("📈 Realized P&L", f"${total_pnl:+,.2f}")

st.markdown("---")

# ═══════════════════════════════════════════════════════════
# TABS
# ═══════════════════════════════════════════════════════════

tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "📋 Live Feed", "📂 Positions", "👥 Leaders", "🔍 Filter Log", "📊 Analytics"
])

# ─── TAB 1: LIVE FEED ──────────────────────────────────
with tab1:
    st.subheader("📋 Recent Leader Activity")
    
    recent = query("""
        SELECT lt.*, fl.decision, fl.reason
        FROM leader_trades lt
        LEFT JOIN filter_log fl ON fl.leader_trade_id = lt.id
        ORDER BY lt.seen_at DESC
        LIMIT 50
    """)
    
    if len(recent) > 0:
        recent['time'] = recent['timestamp'].apply(
            lambda ts: datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%m/%d %H:%M:%S') if ts else ''
        )
        recent['decision_display'] = recent.apply(
            lambda r: f"✅ COPIED" if r.get('decision') == 'COPY' 
            else f"⏭️ {r.get('reason', '')}" if r.get('decision') == 'SKIP'
            else "⏳ pending", axis=1
        )
        
        display_df = recent[['time', 'leader_name', 'side', 'usdc_size', 'price', 
                             'market_question', 'decision_display']].copy()
        display_df.columns = ['Time (UTC)', 'Leader', 'Side', 'Size ($)', 'Price', 
                              'Market', 'Decision']
        
        st.dataframe(
            display_df,
            use_container_width=True,
            hide_index=True,
            column_config={
                'Size ($)': st.column_config.NumberColumn(format='$%.2f'),
                'Price': st.column_config.NumberColumn(format='$%.3f'),
            }
        )
    else:
        st.info("No leader trades detected yet. The bot is monitoring...")
        st.markdown("""
        **What to expect:**
        - The bot polls all 9 leaders every ~10 seconds
        - When a leader makes a new trade, it appears here
        - Each trade runs through 24 filters before being copied or skipped
        - This feed will populate as leaders trade
        """)

# ─── TAB 2: POSITIONS ──────────────────────────────────
with tab2:
    st.subheader("📂 Open Positions")
    
    if len(open_positions) > 0:
        open_positions['opened'] = open_positions['opened_at'].apply(
            lambda ts: datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%m/%d %H:%M') if ts else ''
        )
        open_positions['hold_time'] = open_positions['opened_at'].apply(
            lambda ts: f"{(time.time() - ts) / 3600:.1f}h" if ts else ''
        )
        
        display = open_positions[['leader_name', 'market_question', 'side', 'entry_price',
                                   'shares', 'usdc_size', 'opened', 'hold_time', 'market_category']].copy()
        display.columns = ['Leader', 'Market', 'Side', 'Entry $', 'Shares', 'Size $', 
                          'Opened', 'Hold Time', 'Category']
        
        st.dataframe(display, use_container_width=True, hide_index=True,
                     column_config={
                         'Entry $': st.column_config.NumberColumn(format='$%.3f'),
                         'Size $': st.column_config.NumberColumn(format='$%.2f'),
                         'Shares': st.column_config.NumberColumn(format='%.1f'),
                     })
        
        # Exposure breakdown
        col1, col2 = st.columns(2)
        with col1:
            st.markdown("**Exposure by Leader**")
            by_leader = open_positions.groupby('leader_name')['usdc_size'].sum().sort_values(ascending=False)
            st.bar_chart(by_leader)
        with col2:
            st.markdown("**Exposure by Category**")
            by_cat = open_positions.groupby('market_category')['usdc_size'].sum().sort_values(ascending=False)
            if len(by_cat) > 0:
                st.bar_chart(by_cat)
    else:
        st.info("No open positions yet.")
    
    st.markdown("---")
    st.subheader("📜 Closed Positions")
    
    if len(closed_positions) > 0:
        closed_positions['closed'] = closed_positions['closed_at'].apply(
            lambda ts: datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%m/%d %H:%M') if ts else ''
        )
        closed_positions['result'] = closed_positions['pnl'].apply(
            lambda p: f"✅ ${p:+.2f}" if p and p > 0 else f"❌ ${p:+.2f}" if p else "—"
        )
        
        display = closed_positions[['leader_name', 'market_question', 'side', 'entry_price',
                                     'exit_price', 'usdc_size', 'result', 'exit_reason', 'closed']].copy()
        display.columns = ['Leader', 'Market', 'Side', 'Entry', 'Exit', 'Size', 'P&L', 'Reason', 'Closed']
        
        st.dataframe(display, use_container_width=True, hide_index=True)
        
        # P&L chart
        closed_positions = closed_positions.sort_values('closed_at')
        closed_positions['cum_pnl'] = closed_positions['pnl'].cumsum()
        if 'closed_at' in closed_positions.columns:
            closed_positions['date'] = closed_positions['closed_at'].apply(
                lambda ts: datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None
            )
            chart_data = closed_positions.dropna(subset=['date']).set_index('date')
            if len(chart_data) > 0:
                st.line_chart(chart_data['cum_pnl'], y_label="Cumulative P&L ($)")
    else:
        st.info("No closed positions yet (waiting for market resolutions).")

# ─── TAB 3: LEADERS ─────────────────────────────────────
with tab3:
    st.subheader("👥 Leader Performance")
    
    leaders = query("SELECT * FROM leader_stats ORDER BY total_pnl DESC")
    
    if len(leaders) > 0:
        leaders['win_rate'] = leaders.apply(
            lambda r: f"{r['wins'] / (r['wins'] + r['losses']) * 100:.1f}%" 
            if (r['wins'] + r['losses']) > 0 else "—", axis=1
        )
        leaders['status'] = leaders.apply(
            lambda r: "⏸️ Paused" if r.get('is_paused') else "✅ Active", axis=1
        )
        leaders['last_trade'] = leaders['last_trade_at'].apply(
            lambda ts: datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%m/%d %H:%M') if ts else '—'
        )
        
        display = leaders[['leader_name', 'status', 'total_trades_seen', 'trades_copied',
                           'trades_skipped', 'wins', 'losses', 'win_rate', 'total_pnl', 
                           'consecutive_losses', 'last_trade']].copy()
        display.columns = ['Name', 'Status', 'Seen', 'Copied', 'Skipped', 'W', 'L', 
                          'WR', 'P&L', 'Consec L', 'Last Trade']
        
        st.dataframe(display, use_container_width=True, hide_index=True,
                     column_config={
                         'P&L': st.column_config.NumberColumn(format='$%.2f'),
                     })
    else:
        st.info("No leader data yet. Stats populate as trades are observed.")
    
    # Leader config
    st.markdown("---")
    st.subheader("⚙️ Leader Configuration")
    
    from src.copy_trader import config
    
    leader_data = []
    for addr, cfg in config.LEADERS.items():
        leader_data.append({
            'Address': f"{addr[:8]}...{addr[-6:]}",
            'Name': cfg.get('name', ''),
            'Enabled': '✅' if cfg.get('enabled') else '❌',
            'Weight': cfg.get('weight', 1.0),
        })
    
    st.dataframe(pd.DataFrame(leader_data), use_container_width=True, hide_index=True)

# ─── TAB 4: FILTER LOG ─────────────────────────────────
with tab4:
    st.subheader("🔍 Filter Decisions")
    
    if len(filter_log) > 0:
        filter_log['time'] = filter_log['timestamp'].apply(
            lambda ts: datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%m/%d %H:%M:%S') if ts else ''
        )
        
        # Filter reason breakdown
        col1, col2 = st.columns(2)
        with col1:
            st.markdown("**Skip Reasons**")
            skips = filter_log[filter_log['decision'] == 'SKIP']
            if len(skips) > 0:
                reasons = skips['reason'].value_counts()
                st.bar_chart(reasons)
        with col2:
            st.markdown("**Copy vs Skip**")
            decisions = filter_log['decision'].value_counts()
            st.bar_chart(decisions)
        
        # Detailed log
        st.markdown("**Recent Decisions**")
        display = filter_log.sort_values('timestamp', ascending=False).head(100)
        display_cols = display[['time', 'leader_address', 'decision', 'reason', 'filter_details']].copy()
        display_cols['leader'] = display_cols['leader_address'].apply(lambda a: f"{a[:8]}..." if a else '')
        display_cols = display_cols[['time', 'leader', 'decision', 'reason', 'filter_details']]
        display_cols.columns = ['Time', 'Leader', 'Decision', 'Reason', 'Details']
        
        st.dataframe(display_cols, use_container_width=True, hide_index=True)
    else:
        st.info("No filter decisions yet.")

# ─── TAB 5: ANALYTICS ──────────────────────────────────
with tab5:
    st.subheader("📊 Analytics")
    
    if len(all_positions) > 0 and len(closed_positions) > 0:
        col1, col2, col3 = st.columns(3)
        
        with col1:
            wins = len(closed_positions[closed_positions['pnl'] > 0]) if 'pnl' in closed_positions.columns else 0
            losses = len(closed_positions[closed_positions['pnl'] <= 0]) if 'pnl' in closed_positions.columns else 0
            total = wins + losses
            st.metric("Win Rate", f"{wins/total*100:.1f}%" if total > 0 else "—")
            st.metric("Total Trades", f"{total}")
        
        with col2:
            if 'pnl' in closed_positions.columns:
                avg_win = closed_positions[closed_positions['pnl'] > 0]['pnl'].mean()
                avg_loss = closed_positions[closed_positions['pnl'] <= 0]['pnl'].mean()
                st.metric("Avg Win", f"${avg_win:+.2f}" if not pd.isna(avg_win) else "—")
                st.metric("Avg Loss", f"${avg_loss:+.2f}" if not pd.isna(avg_loss) else "—")
        
        with col3:
            total_pnl = closed_positions['pnl'].sum() if 'pnl' in closed_positions.columns else 0
            st.metric("Total P&L", f"${total_pnl:+,.2f}")
            if 'pnl' in closed_positions.columns:
                cum = closed_positions.sort_values('closed_at')['pnl'].cumsum()
                max_dd = float((cum.cummax() - cum).max()) if len(cum) > 0 else 0
                st.metric("Max Drawdown", f"${max_dd:.2f}")
        
        # P&L by category
        st.markdown("**P&L by Category**")
        by_cat = closed_positions.groupby('market_category')['pnl'].agg(['sum', 'count', 'mean'])
        by_cat.columns = ['Total P&L', 'Trades', 'Avg P&L']
        st.dataframe(by_cat, use_container_width=True)
        
        # P&L by leader
        st.markdown("**P&L by Leader**")
        by_leader = closed_positions.groupby('leader_name')['pnl'].agg(['sum', 'count', 'mean'])
        by_leader.columns = ['Total P&L', 'Trades', 'Avg P&L']
        st.dataframe(by_leader.sort_values('Total P&L', ascending=False), use_container_width=True)
    else:
        st.info("Analytics will populate once positions start resolving.")
    
    # Config summary
    st.markdown("---")
    st.subheader("⚙️ Active Configuration")
    
    from src.copy_trader import config
    
    config_data = {
        "Sizing Mode": config.SIZING_MODE,
        "Fixed Trade Size": f"${config.FIXED_TRADE_SIZE}",
        "Max Trade Size": f"${config.MAX_TRADE_SIZE}",
        "Max Positions Total": config.MAX_POSITIONS_TOTAL,
        "Max Exposure Total": f"${config.MAX_EXPOSURE_TOTAL}",
        "Max Exposure/Leader": f"${config.MAX_EXPOSURE_PER_LEADER}",
        "Max Exposure/Market": f"${config.MAX_EXPOSURE_PER_MARKET}",
        "Daily Loss Limit": f"${config.DAILY_LOSS_LIMIT}",
        "Entry Trade Age Max": f"{config.ENTRY_TRADE_SEC}s",
        "Min Leader Trade": f"${config.MIN_LEADER_TRADE_SIZE}",
        "Max Entry Price": f"${config.MAX_ENTRY_PRICE}",
        "Min Entry Price": f"${config.MIN_ENTRY_PRICE}",
        "Revert Trade (copy sells)": config.REVERT_TRADE,
        "Duplicate Filter": config.DUPLICATE_FILTER,
        "Conflict Resolution": config.CONFLICT_RESOLUTION,
        "Poll Interval": f"{config.POLL_INTERVAL_SEC}s",
        "Paper Mode": config.PAPER_MODE,
    }
    
    col1, col2 = st.columns(2)
    items = list(config_data.items())
    half = len(items) // 2
    with col1:
        for k, v in items[:half]:
            st.markdown(f"**{k}:** `{v}`")
    with col2:
        for k, v in items[half:]:
            st.markdown(f"**{k}:** `{v}`")

# ═══════════════════════════════════════════════════════════
# AUTO-REFRESH
# ═══════════════════════════════════════════════════════════

time.sleep(refresh_rate)
st.rerun()
