"""
🔴 The Outsiders — Live Copy Trader Dashboard
Clean, mobile-friendly, real-time monitoring.
"""
import streamlit as st
import sqlite3
import pandas as pd
import time
from datetime import datetime, timezone, timedelta

# ═══════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════
DB_PATH = "data/copy_trader.db"
PST = timezone(timedelta(hours=-7))
REFRESH_SEC = 15

st.set_page_config(
    page_title="The Outsiders — Live",
    page_icon="🔴",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ═══════════════════════════════════════════════════════════
# STYLES — Dark theme, mobile-first
# ═══════════════════════════════════════════════════════════
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

    * { font-family: 'Inter', sans-serif !important; }

    /* Dark base */
    .stApp { background: #0a0a0f; }
    section[data-testid="stSidebar"] { background: #0f0f18; }

    /* Hero banner */
    .hero {
        background: linear-gradient(135deg, #dc2626 0%, #991b1b 50%, #450a0a 100%);
        border-radius: 16px;
        padding: 28px 32px;
        margin-bottom: 24px;
        position: relative;
        overflow: hidden;
    }
    .hero::after {
        content: '';
        position: absolute;
        top: -50%;
        right: -20%;
        width: 300px;
        height: 300px;
        background: radial-gradient(circle, rgba(255,255,255,0.08) 0%, transparent 70%);
        border-radius: 50%;
    }
    .hero h1 {
        color: white;
        font-size: 28px;
        font-weight: 800;
        margin: 0 0 4px 0;
        letter-spacing: -0.5px;
    }
    .hero .sub {
        color: rgba(255,255,255,0.75);
        font-size: 14px;
        font-weight: 500;
    }
    .hero .live-dot {
        display: inline-block;
        width: 10px;
        height: 10px;
        background: #22c55e;
        border-radius: 50%;
        margin-right: 8px;
        animation: pulse 2s ease-in-out infinite;
        box-shadow: 0 0 8px #22c55e;
    }
    @keyframes pulse {
        0%, 100% { opacity: 1; }
        50% { opacity: 0.4; }
    }

    /* Stat cards */
    .stat-row {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
        gap: 12px;
        margin-bottom: 20px;
    }
    .stat-card {
        background: #13131f;
        border: 1px solid #1e1e30;
        border-radius: 12px;
        padding: 16px;
        text-align: center;
    }
    .stat-card .label {
        color: #6b7280;
        font-size: 11px;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.5px;
        margin-bottom: 4px;
    }
    .stat-card .value {
        font-size: 24px;
        font-weight: 700;
        letter-spacing: -0.5px;
    }
    .stat-card .value.green { color: #22c55e; }
    .stat-card .value.red { color: #ef4444; }
    .stat-card .value.white { color: #f3f4f6; }
    .stat-card .value.gold { color: #f59e0b; }

    /* Section headers */
    .section-header {
        color: #f3f4f6;
        font-size: 16px;
        font-weight: 700;
        margin: 24px 0 12px 0;
        padding-bottom: 8px;
        border-bottom: 1px solid #1e1e30;
    }

    /* Trade cards */
    .trade-card {
        background: #13131f;
        border: 1px solid #1e1e30;
        border-radius: 10px;
        padding: 14px 16px;
        margin-bottom: 8px;
        display: flex;
        justify-content: space-between;
        align-items: center;
    }
    .trade-card .left {
        flex: 1;
    }
    .trade-card .market {
        color: #e5e7eb;
        font-size: 13px;
        font-weight: 600;
        margin-bottom: 2px;
    }
    .trade-card .meta {
        color: #6b7280;
        font-size: 11px;
    }
    .trade-card .pnl {
        font-size: 18px;
        font-weight: 700;
        text-align: right;
        min-width: 80px;
    }
    .trade-card .pnl.win { color: #22c55e; }
    .trade-card .pnl.loss { color: #ef4444; }
    .trade-card .pnl.open { color: #f59e0b; }

    /* Leader cards */
    .leader-card {
        background: #13131f;
        border: 1px solid #1e1e30;
        border-radius: 10px;
        padding: 14px 16px;
        margin-bottom: 8px;
    }
    .leader-card .name {
        color: #e5e7eb;
        font-size: 14px;
        font-weight: 700;
    }
    .leader-card .stats {
        color: #9ca3af;
        font-size: 12px;
        margin-top: 4px;
    }
    .leader-card .stats .highlight {
        color: #22c55e;
        font-weight: 600;
    }
    .leader-card .stats .negative {
        color: #ef4444;
        font-weight: 600;
    }

    /* Hide Streamlit chrome */
    #MainMenu { visibility: hidden; }
    footer { visibility: hidden; }
    header { visibility: hidden; }

    /* Mobile tweaks */
    @media (max-width: 768px) {
        .hero h1 { font-size: 22px; }
        .stat-row { grid-template-columns: repeat(2, 1fr); }
        .stat-card .value { font-size: 20px; }
        .trade-card { flex-direction: column; align-items: flex-start; }
        .trade-card .pnl { margin-top: 8px; text-align: left; }
    }

    /* Tabs */
    .stTabs [data-baseweb="tab-list"] {
        gap: 4px;
        background: #0f0f18;
        border-radius: 10px;
        padding: 4px;
    }
    .stTabs [data-baseweb="tab"] {
        background: transparent;
        border-radius: 8px;
        color: #6b7280;
        font-weight: 600;
        font-size: 13px;
    }
    .stTabs [aria-selected="true"] {
        background: #dc2626 !important;
        color: white !important;
    }
</style>
""", unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════════
# DATA
# ═══════════════════════════════════════════════════════════

@st.cache_resource
def get_db():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def query(sql, params=None):
    conn = get_db()
    return pd.read_sql_query(sql, conn, params=params or [])

def ts_to_pst(ts):
    if not ts or pd.isna(ts):
        return "—"
    dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(PST)
    return dt.strftime("%-m/%d %-I:%M%p").lower()

def ts_to_pst_short(ts):
    if not ts or pd.isna(ts):
        return "—"
    dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(PST)
    return dt.strftime("%-I:%M%p").lower()


# ═══════════════════════════════════════════════════════════
# HEADER
# ═══════════════════════════════════════════════════════════

# Check if bot is running
import subprocess
bot_running = subprocess.run(
    ["pgrep", "-f", "copy_trader"], capture_output=True
).returncode == 0

status_dot = "live-dot" if bot_running else ""
status_text = "LIVE" if bot_running else "OFFLINE"

now_pst = datetime.now(PST).strftime("%-I:%M%p PST")

st.markdown(f"""
<div class="hero">
    <h1><span class="{status_dot}"></span>The Outsiders</h1>
    <div class="sub">Copy Trader • {status_text} • {now_pst}</div>
</div>
""", unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════════
# STATS
# ═══════════════════════════════════════════════════════════

positions = query("SELECT * FROM positions ORDER BY opened_at DESC")

if not positions.empty:
    resolved = positions[positions['status'].isin(['resolved_win', 'resolved_loss'])]
    open_pos = positions[positions['status'] == 'open']
    live_pos = positions[positions['is_paper'] == 0]
    paper_pos = positions[positions['is_paper'] == 1]

    wins = resolved[resolved['status'] == 'resolved_win']
    losses = resolved[resolved['status'] == 'resolved_loss']

    total_pnl = resolved['pnl'].sum() if not resolved.empty else 0
    live_resolved = live_pos[live_pos['status'].isin(['resolved_win', 'resolved_loss'])]
    live_pnl = live_resolved['pnl'].sum() if not live_resolved.empty else 0

    wr = len(wins) / len(resolved) * 100 if len(resolved) > 0 else 0
    avg_win = wins['pnl'].mean() if not wins.empty else 0
    avg_loss = losses['pnl'].mean() if not losses.empty else 0
    deployed = open_pos['usdc_size'].sum() if not open_pos.empty else 0

    # Live-only stats
    live_open = live_pos[live_pos['status'] == 'open']
    live_deployed = live_open['usdc_size'].sum() if not live_open.empty else 0
    live_wins = live_pos[live_pos['status'] == 'resolved_win']
    live_losses = live_pos[live_pos['status'] == 'resolved_loss']
    live_wr = len(live_wins) / len(live_resolved) * 100 if len(live_resolved) > 0 else 0

    pnl_class = "green" if total_pnl > 0 else "red" if total_pnl < 0 else "white"
    live_pnl_class = "green" if live_pnl > 0 else "red" if live_pnl < 0 else "white"

    st.markdown(f"""
    <div class="stat-row">
        <div class="stat-card">
            <div class="label">Total P&L</div>
            <div class="value {pnl_class}">${total_pnl:+,.2f}</div>
        </div>
        <div class="stat-card">
            <div class="label">Live P&L</div>
            <div class="value {live_pnl_class}">${live_pnl:+,.2f}</div>
        </div>
        <div class="stat-card">
            <div class="label">Win Rate</div>
            <div class="value white">{wr:.0f}%</div>
        </div>
        <div class="stat-card">
            <div class="label">Trades</div>
            <div class="value white">{len(resolved)}</div>
        </div>
        <div class="stat-card">
            <div class="label">Open</div>
            <div class="value gold">{len(open_pos)}</div>
        </div>
        <div class="stat-card">
            <div class="label">Deployed</div>
            <div class="value gold">${deployed:,.0f}</div>
        </div>
    </div>
    """, unsafe_allow_html=True)
else:
    st.markdown("""
    <div class="stat-row">
        <div class="stat-card">
            <div class="label">Status</div>
            <div class="value white">Waiting for trades...</div>
        </div>
    </div>
    """, unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════════
# TABS
# ═══════════════════════════════════════════════════════════

tab_trades, tab_open, tab_leaders, tab_log = st.tabs([
    "📊 Trades", "🟡 Open", "👥 Leaders", "📋 Log"
])


# ─── TRADES TAB ───────────────────────────────────────────
with tab_trades:
    if not positions.empty:
        resolved = positions[positions['status'].isin(['resolved_win', 'resolved_loss'])].copy()
        if not resolved.empty:
            resolved = resolved.sort_values('closed_at', ascending=False)

            # Running P&L
            running = 0
            cards_html = ""
            for _, row in resolved.iterrows():
                pnl = row['pnl'] or 0
                is_win = row['status'] == 'resolved_win'
                pnl_class = "win" if is_win else "loss"
                emoji = "✅" if is_win else "❌"
                mode = "🔴" if row.get('is_paper') == 0 else "📋"

                q = (row.get('market_question') or '')[:50]
                time_str = ts_to_pst(row.get('closed_at'))
                exit_reason = row.get('exit_reason', '')

                cards_html += f"""
                <div class="trade-card">
                    <div class="left">
                        <div class="market">{emoji} {q}</div>
                        <div class="meta">{mode} {row['leader_name']} • {row['side']} @ ${row['entry_price']:.2f} • {time_str} • {exit_reason}</div>
                    </div>
                    <div class="pnl {pnl_class}">${pnl:+.2f}</div>
                </div>
                """

            st.markdown(cards_html, unsafe_allow_html=True)

            # Cumulative P&L chart
            st.markdown('<div class="section-header">Cumulative P&L</div>', unsafe_allow_html=True)
            chart_data = resolved.sort_values('closed_at')
            chart_data['cumulative_pnl'] = chart_data['pnl'].cumsum()
            chart_data['time'] = chart_data['closed_at'].apply(
                lambda ts: datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(PST) if pd.notna(ts) else None
            )
            chart_data = chart_data.dropna(subset=['time'])
            if not chart_data.empty:
                st.line_chart(chart_data.set_index('time')['cumulative_pnl'], color='#dc2626')
        else:
            st.info("No resolved trades yet")
    else:
        st.info("No trades yet — waiting for leaders to make moves")


# ─── OPEN POSITIONS TAB ──────────────────────────────────
with tab_open:
    if not positions.empty:
        open_pos = positions[positions['status'] == 'open'].copy()
        if not open_pos.empty:
            open_pos = open_pos.sort_values('opened_at', ascending=False)
            cards_html = ""
            for _, row in open_pos.iterrows():
                q = (row.get('market_question') or '')[:50]
                time_str = ts_to_pst(row.get('opened_at'))
                mode = "🔴" if row.get('is_paper') == 0 else "📋"
                hold_sec = time.time() - (row.get('opened_at') or time.time())
                if hold_sec > 86400:
                    hold_str = f"{hold_sec/86400:.1f}d"
                elif hold_sec > 3600:
                    hold_str = f"{hold_sec/3600:.1f}h"
                else:
                    hold_str = f"{hold_sec/60:.0f}m"

                cards_html += f"""
                <div class="trade-card">
                    <div class="left">
                        <div class="market">🟡 {q}</div>
                        <div class="meta">{mode} {row['leader_name']} • {row['side']} @ ${row['entry_price']:.2f} • ${row['usdc_size']:.2f} • {hold_str}</div>
                    </div>
                    <div class="pnl open">OPEN</div>
                </div>
                """
            st.markdown(cards_html, unsafe_allow_html=True)
        else:
            st.info("No open positions")
    else:
        st.info("No positions yet")


# ─── LEADERS TAB ──────────────────────────────────────────
with tab_leaders:
    leaders = query("SELECT * FROM leader_stats ORDER BY total_pnl DESC")
    if not leaders.empty:
        cards_html = ""
        for _, row in leaders.iterrows():
            name = row['leader_name']
            w, l = int(row['wins']), int(row['losses'])
            total = w + l
            wr = f"{w/total*100:.0f}%" if total > 0 else "—"
            pnl = row['total_pnl'] or 0
            paused = bool(row.get('is_paused', 0))

            pnl_cls = "highlight" if pnl > 0 else "negative"
            status = " • ⏸️ PAUSED" if paused else ""

            cards_html += f"""
            <div class="leader-card">
                <div class="name">{name}{status}</div>
                <div class="stats">
                    {w}W / {l}L (<span class="{pnl_cls}">{wr}</span>)
                    &nbsp;•&nbsp;
                    P&L: <span class="{pnl_cls}">${pnl:+.2f}</span>
                    &nbsp;•&nbsp;
                    {total} trades
                </div>
            </div>
            """
        st.markdown(cards_html, unsafe_allow_html=True)
    else:
        st.info("No leader data yet")


# ─── LOG TAB ─────────────────────────────────────────────
with tab_log:
    st.markdown('<div class="section-header">Recent Filter Decisions</div>', unsafe_allow_html=True)
    log_data = query("""
        SELECT timestamp, leader_name, decision, reason, condition_id
        FROM filter_log
        ORDER BY timestamp DESC
        LIMIT 50
    """)
    if not log_data.empty:
        log_data['time'] = log_data['timestamp'].apply(ts_to_pst)
        log_data['emoji'] = log_data['decision'].apply(lambda d: "✅" if d == 'COPY' else "⏭️")
        for _, row in log_data.iterrows():
            st.text(f"{row['emoji']} {row['time']} | {row.get('leader_name', '?')} | {row['reason']}")
    else:
        st.info("No filter log entries yet")


# ═══════════════════════════════════════════════════════════
# AUTO-REFRESH
# ═══════════════════════════════════════════════════════════
st.markdown(f"""
<div style="text-align: center; color: #374151; font-size: 11px; margin-top: 32px;">
    Auto-refresh every {REFRESH_SEC}s • The Outsiders 🏞
</div>
""", unsafe_allow_html=True)

# Force re-run periodically
time.sleep(REFRESH_SEC)
st.rerun()
