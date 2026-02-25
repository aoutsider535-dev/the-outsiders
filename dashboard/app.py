"""
🏞 The Outsiders — Trading Dashboard v6
Light theme. Draggable timeline. Per-strategy version filters. Reimagined.
"""
import streamlit as st
import plotly.graph_objects as go
import pandas as pd
import sys
import os
import json
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from src.database import get_connection, init_db

PST = timezone(timedelta(hours=-8))
LIVE_STARTING_BALANCE = 105.16
PAPER_STARTING_BALANCE = 1000.0

# Strategy version history — per-strategy cutoff timestamps
# Each strategy independently tracks its own version changes
STRATEGY_VERSIONS = {
    "btc_5min_momentum_LIVE": [
        {"version": "v1", "start": 0, "label": "Original"},
        {"version": "v2", "start": 1771905600, "label": "Min edge 7%, conf 57%"},
        {"version": "v3", "start": 1771977600, "label": "Edge 8-12% sweet spot"},
    ],
    "btc_5min_meanrev_LIVE": [
        {"version": "v1", "start": 0, "label": "Original"},
        {"version": "v2", "start": 1771905600, "label": "Edge 4%, max 12%, conf 55%"},
        {"version": "v3", "start": 1771955100, "label": "Combo C filters"},
    ],
    "btc_5min_ob_imbalance_LIVE": [
        {"version": "v1", "start": 0, "label": "Original (4% edge)"},
        {"version": "v2", "start": 1771964100, "label": "Min edge 3%"},
    ],
    "btc_5min_smart_money_LIVE": [
        {"version": "v1", "start": 0, "label": "Original"},
        {"version": "v2", "start": 1771905600, "label": "Edge 10%, conf 59%, streak breaker"},
        {"version": "v3", "start": 1771955100, "label": "Combo C + edge cap 20%"},
    ],
}

STRATEGY_COLORS = {
    "btc_5min_momentum_LIVE": "#6366f1",
    "btc_5min_meanrev_LIVE": "#f59e0b",
    "btc_5min_ob_imbalance_LIVE": "#8b5cf6",
    "btc_5min_smart_money_LIVE": "#ec4899",
    "btc_5min_momentum": "#6366f1",
    "btc_5min_meanrev": "#f59e0b",
    "btc_5min_ob_imbalance": "#8b5cf6",
    "btc_5min_smart_money": "#ec4899",
}

STRATEGY_LABELS = {
    "btc_5min_momentum_LIVE": "⚡ Momentum",
    "btc_5min_meanrev_LIVE": "🔄 Mean Reversion",
    "btc_5min_ob_imbalance_LIVE": "📊 OB Imbalance",
    "btc_5min_smart_money_LIVE": "🧠 Smart Money",
    "btc_5min_momentum": "⚡ Momentum",
    "btc_5min_meanrev": "🔄 Mean Reversion",
    "btc_5min_ob_imbalance": "📊 OB Imbalance",
    "btc_5min_smart_money": "🧠 Smart Money",
}


def get_live_balance():
    try:
        from dotenv import dotenv_values
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import BalanceAllowanceParams
        env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
        config = dotenv_values(env_path)
        pk, addr = config.get("POLYGON_PRIVATE_KEY", ""), config.get("POLYGON_WALLET_ADDRESS", "")
        if not pk or not addr:
            return None
        host = "https://clob.polymarket.com"
        client = ClobClient(host, key=pk, chain_id=137)
        creds = client.create_or_derive_api_creds()
        client = ClobClient(host, key=pk, chain_id=137, creds=creds, signature_type=1, funder=addr)
        raw = int(client.get_balance_allowance(BalanceAllowanceParams(asset_type="COLLATERAL")).get("balance", 0))
        return raw / 1e6
    except Exception:
        return None


def strategy_label(name):
    return STRATEGY_LABELS.get(name, name)


def strategy_color(name):
    return STRATEGY_COLORS.get(name, "#64748b")


def to_pst(ts):
    if pd.isna(ts) or ts is None:
        return ""
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(PST).strftime("%b %d, %I:%M %p")
    except:
        return str(ts)


def get_strategy_version(strategy, ts):
    """Return version for a specific strategy at a given timestamp."""
    versions = STRATEGY_VERSIONS.get(strategy, [{"version": "v1", "start": 0, "label": "Original"}])
    result = "v1"
    for v in versions:
        if ts >= v["start"]:
            result = v["version"]
    return result


def get_strategy_version_label(strategy, version):
    """Return human-readable label for a strategy version."""
    versions = STRATEGY_VERSIONS.get(strategy, [])
    for v in versions:
        if v["version"] == version:
            return v["label"]
    return version


def check_trader_running(name="live_trader"):
    try:
        import subprocess
        return subprocess.run(["pgrep", "-f", name], capture_output=True).returncode == 0
    except:
        return False


def load_trades(is_live=True, limit=2000):
    conn = get_connection()
    q = "SELECT * FROM trades WHERE strategy {} '%_LIVE' ORDER BY timestamp DESC LIMIT ?".format(
        "LIKE" if is_live else "NOT LIKE")
    rows = conn.execute(q, (limit,)).fetchall()
    conn.close()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([dict(r) for r in rows])
    if "timestamp" in df.columns:
        df["time_pst"] = pd.to_datetime(df["timestamp"], unit="s", utc=True).dt.tz_convert("US/Pacific")
        df["time_display"] = df["timestamp"].apply(to_pst)
        # Per-strategy versioning
        df["version"] = df.apply(lambda row: get_strategy_version(row["strategy"], row["timestamp"]), axis=1)
    if "signal_data" in df.columns:
        df["signal_data"] = df["signal_data"].apply(lambda x: json.loads(x) if isinstance(x, str) and x else {})
    return df


# ─── PAGE CONFIG ───
st.set_page_config(page_title="🏞 The Outsiders", page_icon="🏞", layout="wide", initial_sidebar_state="collapsed")
init_db()

# ─── LIGHT THEME CSS ───
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap');
    
    .stApp {
        font-family: 'Inter', sans-serif;
        background-color: #f8fafc !important;
        color: #1e293b;
    }

    /* Override Streamlit dark elements */
    .stApp > header { background-color: #f8fafc !important; }
    section[data-testid="stSidebar"] { background-color: #f1f5f9 !important; }
    .stTabs [data-baseweb="tab-list"] { background-color: transparent; gap: 8px; }
    .stTabs [data-baseweb="tab"] {
        background-color: #e2e8f0;
        border-radius: 12px;
        color: #475569;
        font-weight: 600;
        padding: 8px 24px;
    }
    .stTabs [aria-selected="true"] {
        background-color: #6366f1 !important;
        color: #fff !important;
    }

    .hero-header {
        background: linear-gradient(135deg, #f0f4ff 0%, #e8eeff 30%, #f5f0ff 60%, #fef3f2 100%);
        border-radius: 20px;
        padding: 28px 36px;
        margin-bottom: 20px;
        border: 1px solid #e2e8f0;
        box-shadow: 0 4px 24px rgba(99, 102, 241, 0.08);
    }
    
    .hero-title {
        font-family: 'Inter', sans-serif;
        font-size: 2.4rem;
        font-weight: 800;
        background: linear-gradient(135deg, #4f46e5 0%, #7c3aed 50%, #ec4899 100%);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        letter-spacing: -0.03em;
    }
    
    .hero-sub {
        color: #64748b;
        font-size: 1rem;
        font-weight: 400;
        margin-top: 2px;
    }
    
    .live-badge {
        display: inline-block;
        background: linear-gradient(135deg, #22c55e, #16a34a);
        color: #fff;
        padding: 6px 16px;
        border-radius: 20px;
        font-size: 0.75rem;
        font-weight: 700;
        letter-spacing: 0.1em;
        text-transform: uppercase;
        box-shadow: 0 2px 12px rgba(34,197,94,0.3);
    }
    
    .offline-badge {
        display: inline-block;
        background: linear-gradient(135deg, #ef4444, #dc2626);
        color: #fff;
        padding: 6px 16px;
        border-radius: 20px;
        font-size: 0.75rem;
        font-weight: 700;
        letter-spacing: 0.1em;
    }
    
    .metric-card {
        background: #ffffff;
        border: 1px solid #e2e8f0;
        border-radius: 16px;
        padding: 18px 16px;
        text-align: center;
        transition: transform 0.2s, box-shadow 0.2s;
        box-shadow: 0 1px 3px rgba(0,0,0,0.04);
    }
    
    .metric-card:hover {
        transform: translateY(-2px);
        box-shadow: 0 8px 24px rgba(0,0,0,0.08);
    }
    
    .metric-label { color: #94a3b8; font-size: 0.72rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.08em; }
    .metric-value { font-size: 1.5rem; font-weight: 700; margin: 4px 0; color: #1e293b; }
    .metric-sub { color: #94a3b8; font-size: 0.72rem; }
    
    .win { color: #16a34a !important; }
    .loss { color: #dc2626 !important; }
    .neutral { color: #6366f1 !important; }
    
    .strat-card {
        background: #ffffff;
        border: 1px solid #e2e8f0;
        border-radius: 16px;
        padding: 18px;
        transition: transform 0.2s, box-shadow 0.2s;
        box-shadow: 0 1px 3px rgba(0,0,0,0.04);
    }
    
    .strat-card:hover {
        transform: translateY(-2px);
        box-shadow: 0 8px 24px rgba(0,0,0,0.08);
    }
    
    .filter-panel {
        background: #ffffff;
        border: 1px solid #e2e8f0;
        border-radius: 16px;
        padding: 20px 24px;
        margin: 8px 0 16px 0;
        box-shadow: 0 1px 3px rgba(0,0,0,0.04);
    }
    
    .filter-summary {
        background: linear-gradient(135deg, #eff6ff, #f5f3ff);
        border: 1px solid #c7d2fe;
        border-radius: 12px;
        padding: 10px 20px;
        margin: 8px 0 16px 0;
    }
    
    .trade-row {
        background: #ffffff;
        border-radius: 12px;
        padding: 12px 18px;
        margin: 6px 0;
        border-left: 4px solid;
        display: flex;
        justify-content: space-between;
        align-items: center;
        font-size: 0.85rem;
        border: 1px solid #f1f5f9;
        box-shadow: 0 1px 2px rgba(0,0,0,0.03);
    }
    
    .trade-win { border-left-color: #22c55e; }
    .trade-loss { border-left-color: #ef4444; }
    .trade-open { border-left-color: #6366f1; }
    
    .version-badge {
        display: inline-block;
        padding: 2px 8px;
        border-radius: 6px;
        font-size: 0.65rem;
        font-weight: 700;
        letter-spacing: 0.04em;
    }
    .version-v1 { background: #f1f5f9; color: #94a3b8; }
    .version-v2 { background: #dcfce7; color: #16a34a; }
    .version-v3 { background: #dbeafe; color: #2563eb; }
    
    .section-header {
        font-size: 1.15rem;
        font-weight: 700;
        color: #1e293b;
        margin: 24px 0 12px 0;
        padding-bottom: 8px;
        border-bottom: 2px solid #e2e8f0;
    }

    /* Streamlit widget overrides for light theme */
    .stSelectbox label, .stMultiSelect label, .stSlider label {
        color: #475569 !important;
        font-weight: 600 !important;
    }
    /* Selected value in the box — dark text on white */
    div[data-baseweb="select"] > div { background-color: #ffffff !important; border-color: #e2e8f0 !important; color: #1e293b !important; }
    div[data-baseweb="select"] span { color: #1e293b !important; }
    div[data-baseweb="select"] input { color: #1e293b !important; }
    /* Dropdown menu — white text on dark background */
    div[data-baseweb="popover"] { background-color: #1e293b !important; }
    div[data-baseweb="popover"] * { color: #ffffff !important; }
    div[data-baseweb="menu"] { background-color: #1e293b !important; }
    div[data-baseweb="menu"] * { color: #ffffff !important; }
    div[data-baseweb="menu"] li { color: #ffffff !important; }
    div[data-baseweb="menu"] li:hover { background-color: #334155 !important; }
    ul[role="listbox"] { background-color: #1e293b !important; }
    ul[role="listbox"] li { color: #ffffff !important; }
    ul[role="listbox"] li:hover { background-color: #334155 !important; }
    [data-baseweb="menu"] [role="option"] { color: #ffffff !important; }
    [data-baseweb="menu"] [role="option"]:hover { background-color: #334155 !important; }
    /* Multiselect tags */
    span[data-baseweb="tag"] { background-color: #e2e8f0 !important; color: #1e293b !important; }
    span[data-baseweb="tag"] span { color: #1e293b !important; }
    /* Slider */
    .stSlider div[data-baseweb="slider"] div { color: #1e293b !important; }
    .stMarkdown h3 { color: #1e293b; }
    /* General text */
    p, span, div { color: #1e293b; }
</style>
""", unsafe_allow_html=True)


def compute_stats(df):
    closed = df[df["status"] == "closed"] if "status" in df.columns else df
    total = len(closed)
    if total == 0:
        return {"total": 0, "wins": 0, "losses": 0, "wr": 0, "pnl": 0, "avg_pnl": 0}
    wins = len(closed[closed["pnl"] > 0]) if "pnl" in closed.columns else 0
    pnl = closed["pnl"].sum() if "pnl" in closed.columns else 0
    return {
        "total": total, "wins": wins, "losses": total - wins,
        "wr": wins / total * 100, "pnl": pnl, "avg_pnl": pnl / total,
    }


def render_kpi_row(balance, total_pnl, roi, stats, open_count, active_strats):
    k1, k2, k3, k4, k5, k6 = st.columns(6)
    bal_class = "win" if balance >= LIVE_STARTING_BALANCE else "loss"
    pnl_class = "win" if total_pnl >= 0 else "loss"
    wr_class = "win" if stats["wr"] >= 50 else "loss"

    for col, label, value, sub, cls in [
        (k1, "Balance", f"${balance:,.2f}", f"{roi:+.1f}% ROI", bal_class),
        (k2, "Total P&L", f"${total_pnl:+,.2f}", f"from ${LIVE_STARTING_BALANCE:.0f} start", pnl_class),
        (k3, "Record", f"{stats['wins']}W · {stats['losses']}L", f"{stats['total']} trades", "neutral"),
        (k4, "Win Rate", f"{stats['wr']:.1f}%", "target: 55%+", wr_class),
        (k5, "Open", f"{open_count}", "awaiting resolution", "neutral"),
        (k6, "Strategies", f"{active_strats}/4", "active now", "neutral"),
    ]:
        col.markdown(f"""
        <div class="metric-card">
            <div class="metric-label">{label}</div>
            <div class="metric-value {cls}">{value}</div>
            <div class="metric-sub">{sub}</div>
        </div>
        """, unsafe_allow_html=True)


def render_equity_chart(df, starting_balance, selected_strategies=None, real_balance=None):
    """Render P&L equity curve showing portfolio balance and per-strategy contributions."""
    closed = df[df["status"] == "closed"].copy() if "status" in df.columns else df.copy()
    if closed.empty:
        st.info("No closed trades to chart.")
        return

    if selected_strategies:
        closed = closed[closed["strategy"].isin(selected_strategies)]
    if closed.empty:
        st.info("No trades match filters.")
        return

    closed = closed.sort_values("timestamp")

    # Deduct estimated 2% Polymarket taker fee from each trade's P&L
    FEE_RATE = 0.02
    if "quantity" in closed.columns and "entry_price" in closed.columns:
        closed["fee"] = (closed["quantity"].fillna(0) * closed["entry_price"].fillna(0) * FEE_RATE)
        closed["net_pnl"] = closed["pnl"] - closed["fee"]
    else:
        closed["net_pnl"] = closed["pnl"]

    closed["cumulative_pnl"] = closed["net_pnl"].cumsum()
    closed["balance"] = starting_balance + closed["cumulative_pnl"]

    fig = go.Figure()

    strats = closed["strategy"].unique()

    # Overall equity curve
    if len(strats) > 1:
        fig.add_trace(go.Scatter(
            x=closed["time_pst"], y=closed["balance"],
            mode="lines", name="Overall",
            line=dict(color="#1e293b", width=2.5),
            hovertemplate="$%{y:.2f}<extra>Overall</extra>"
        ))

    # Per-strategy contribution (each starts at starting_balance)
    for strat in strats:
        s_df = closed[closed["strategy"] == strat]
        fig.add_trace(go.Scatter(
            x=s_df["time_pst"], y=starting_balance + s_df["net_pnl"].cumsum(),
            mode="markers+lines", name=strategy_label(strat),
            line=dict(color=strategy_color(strat), width=1.8),
            marker=dict(size=4, color=strategy_color(strat)),
            opacity=0.8,
            hovertemplate="$%{y:.2f}<extra>" + strategy_label(strat) + "</extra>"
        ))

    fig.add_hline(y=starting_balance, line_dash="dot", line_color="rgba(148,163,184,0.5)",
                  annotation_text=f"Start ${starting_balance:.0f}", annotation_font_color="#94a3b8")

    fig.update_layout(
        template="plotly_white",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="Inter", color="#475569"),
        height=400,
        margin=dict(l=20, r=20, t=40, b=20),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1,
                    font=dict(size=11, color="#475569"), bgcolor="rgba(0,0,0,0)"),
        xaxis=dict(gridcolor="rgba(226,232,240,0.8)", showgrid=True, linecolor="#e2e8f0"),
        yaxis=dict(gridcolor="rgba(226,232,240,0.8)", showgrid=True, tickprefix="$", linecolor="#e2e8f0"),
        hovermode="x unified",
    )
    st.plotly_chart(fig, use_container_width=True)


def render_strategy_cards(df):
    closed = df[df["status"] == "closed"] if "status" in df.columns else df
    if closed.empty:
        return
    strats = sorted(closed["strategy"].unique())
    cols = st.columns(min(len(strats), 4))

    for i, sname in enumerate(strats):
        s_df = closed[closed["strategy"] == sname]
        s = compute_stats(s_df)
        color = strategy_color(sname)
        pnl_c = "#16a34a" if s["pnl"] >= 0 else "#dc2626"
        wr_c = "#16a34a" if s["wr"] >= 50 else "#dc2626"
        
        # Per-strategy version breakdown
        version_counts = s_df["version"].value_counts().to_dict() if "version" in s_df.columns else {}
        version_html = " ".join(
            f'<span class="version-badge version-{v}">{v}: {c}</span>'
            for v, c in sorted(version_counts.items())
        )
        
        # Current version description
        versions = STRATEGY_VERSIONS.get(sname, [])
        current_v = versions[-1] if versions else {"version": "v1", "label": "Original"}
        
        with cols[i % len(cols)]:
            st.markdown(f"""
            <div class="strat-card" style="border-top: 3px solid {color};">
                <div style="font-size:1rem;font-weight:700;color:#1e293b;margin-bottom:4px;">
                    {strategy_label(sname)}
                </div>
                <div style="margin-bottom:8px;">
                    {version_html}
                </div>
                <div style="color:#64748b;font-size:0.82rem;line-height:2;">
                    Record: <b style="color:#1e293b">{s['wins']}W · {s['losses']}L</b><br>
                    Win Rate: <b style="color:{wr_c}">{s['wr']:.1f}%</b><br>
                    P&L: <b style="color:{pnl_c}">${s['pnl']:+.2f}</b><br>
                    Avg: <b style="color:{pnl_c}">${s['avg_pnl']:+.2f}</b>/trade
                </div>
                <div style="color:#94a3b8;font-size:0.7rem;margin-top:6px;font-style:italic;">
                    Current: {current_v['label']}
                </div>
            </div>
            """, unsafe_allow_html=True)


def render_trade_history(df, limit=50):
    if df.empty:
        return
    for _, trade in df.head(limit).iterrows():
        direction = str(trade.get("direction", "?")).upper()
        entry = trade.get("entry_price", 0)
        pnl = trade.get("pnl")
        status = trade.get("status", "")
        strat = trade.get("strategy", "")
        time_str = to_pst(trade.get("timestamp"))
        edge = trade.get("edge_pct", 0)
        version = trade.get("version", "v1")
        
        emoji_dir = "🟢" if direction == "UP" else "🔴"
        color = strategy_color(strat)
        v_label = get_strategy_version_label(strat, version)
        v_badge = f'<span class="version-badge version-{version}" title="{v_label}">{version}</span>'
        
        if status == "closed" and pnl is not None:
            if pnl > 0:
                css_class = "trade-win"
                result = f'<span style="color:#16a34a;font-weight:600;">✅ +${pnl:.2f}</span>'
            else:
                css_class = "trade-loss"
                result = f'<span style="color:#dc2626;font-weight:600;">❌ ${pnl:.2f}</span>'
        else:
            css_class = "trade-open"
            result = '<span style="color:#6366f1;font-weight:600;">⏳ Pending</span>'
        
        st.markdown(f"""
        <div class="trade-row {css_class}">
            <div>
                <span style="color:{color};font-weight:600;">{strategy_label(strat)}</span>
                {v_badge}
                &nbsp;{emoji_dir} {direction} @ ${entry:.3f} · Edge {edge:.1f}%
            </div>
            <div style="text-align:right;">
                {result}
                <span style="color:#94a3b8;margin-left:12px;font-size:0.75rem;">{time_str}</span>
            </div>
        </div>
        """, unsafe_allow_html=True)


# ─── TABS ───
tab_live, tab_paper, tab_paper_v2 = st.tabs(["💰 LIVE TRADING", "📝 Paper Trading", "🧪 Paper v2"])

# ════════════════════════════════════════════
# 💰 LIVE TAB
# ════════════════════════════════════════════
with tab_live:
    is_live = check_trader_running("live_trader")
    now_pst = datetime.now(timezone.utc).astimezone(PST).strftime("%I:%M %p PST")
    badge = '<span class="live-badge">● LIVE</span>' if is_live else '<span class="offline-badge">● OFFLINE</span>'

    st.markdown(f"""
    <div class="hero-header">
        <div style="display:flex;justify-content:space-between;align-items:center;">
            <div>
                <div class="hero-title">🏞 The Outsiders</div>
                <div class="hero-sub">Real money. Real edge. Not insiders — just smarter.</div>
            </div>
            <div style="text-align:right;">
                {badge}<br>
                <span style="color:#94a3b8;font-size:0.82rem;">{now_pst}</span>
            </div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    df_live = load_trades(is_live=True)

    if df_live.empty:
        st.markdown('<div style="text-align:center;padding:60px;"><h2 style="color:#94a3b8;">🚀 No live trades yet</h2></div>', unsafe_allow_html=True)
    else:
        closed = df_live[df_live["status"] == "closed"] if "status" in df_live.columns else df_live
        open_live = df_live[df_live["status"] == "open"] if "status" in df_live.columns else pd.DataFrame()

        all_strats = sorted(closed["strategy"].unique().tolist()) if "strategy" in closed.columns else []

        # ─── REIMAGINED FILTERS ───
        st.markdown('<div class="section-header">🎛️ Filters</div>', unsafe_allow_html=True)
        
        # Row 1: Strategy chips + draggable time range
        fc1, fc2 = st.columns([3, 2])
        with fc1:
            selected_strategies = st.multiselect(
                "Strategies", options=all_strats, default=all_strats,
                format_func=lambda x: strategy_label(x), key="live_strat")
        with fc2:
            # Dual-point datetime range picker
            if not closed.empty:
                earliest = closed["time_pst"].min().to_pydatetime()
                latest = closed["time_pst"].max().to_pydatetime()
            else:
                earliest = datetime.now(PST) - timedelta(days=7)
                latest = datetime.now(PST)
            
            time_range = st.slider(
                "⏱️ Time Range",
                min_value=earliest,
                max_value=latest,
                value=(earliest, latest),
                format="MM/DD HH:mm",
                key="live_time_slider"
            )
            time_start, time_end = time_range

        # Row 2: Per-strategy version filters
        fc3, fc4, fc5, fc6 = st.columns(4)
        version_filters = {}
        for col, strat in zip([fc3, fc4, fc5, fc6], all_strats[:4]):
            versions = STRATEGY_VERSIONS.get(strat, [{"version": "v1", "start": 0, "label": "Original"}])
            version_options = ["All"] + [f"{v['version']} ({v['label'][:20]})" for v in versions]
            with col:
                selected_v = st.selectbox(
                    f"{strategy_label(strat).split(' ', 1)[1]} ver.",
                    options=version_options,
                    key=f"ver_{strat}"
                )
                version_filters[strat] = selected_v

        # Row 3: Edge & confidence sliders
        fc7, fc8 = st.columns(2)
        with fc7:
            min_edge = st.slider("Min Edge %", 0.0, 20.0, 0.0, 0.5, key="live_edge")
        with fc8:
            min_conf = st.slider("Min Confidence", 0.50, 0.80, 0.50, 0.01, key="live_conf")

        # ─── APPLY FILTERS ───
        filtered = closed.copy()
        
        # Strategy filter
        if selected_strategies:
            filtered = filtered[filtered["strategy"].isin(selected_strategies)]
        
        # Per-strategy version filter
        mask = pd.Series(True, index=filtered.index)
        for strat, v_filter in version_filters.items():
            if v_filter != "All":
                v_code = v_filter.split(" ")[0]  # Extract "v1", "v2", "v3"
                strat_mask = (filtered["strategy"] != strat) | (filtered["version"] == v_code)
                mask = mask & strat_mask
        filtered = filtered[mask]
        
        # Time range filter
        if "time_pst" in filtered.columns:
            filtered = filtered[(filtered["time_pst"] >= time_start) & (filtered["time_pst"] <= time_end)]
        
        # Edge & confidence filters
        if min_edge > 0 and "edge_pct" in filtered.columns:
            filtered = filtered[filtered["edge_pct"] >= min_edge]
        if min_conf > 0.5 and "confidence" in filtered.columns:
            filtered = filtered[filtered["confidence"] >= min_conf]

        # ─── BALANCE & STATS ───
        real_balance = get_live_balance()
        balance = real_balance if real_balance is not None else LIVE_STARTING_BALANCE + (closed["pnl"].sum() if "pnl" in closed.columns else 0)
        total_pnl = balance - LIVE_STARTING_BALANCE
        roi = (total_pnl / LIVE_STARTING_BALANCE) * 100
        stats = compute_stats(filtered)
        active_strats = len(set(open_live["strategy"])) if not open_live.empty and "strategy" in open_live.columns else 0

        # Filter summary (if filters are active)
        if len(filtered) != len(closed):
            fs = compute_stats(filtered)
            wr_c = "#16a34a" if fs["wr"] >= 50 else "#dc2626"
            pnl_c = "#16a34a" if fs["pnl"] >= 0 else "#dc2626"
            st.markdown(f"""
            <div class="filter-summary">
                <b style="color:#6366f1;">🔍 Filtered:</b>
                <span style="color:#1e293b;">{fs['total']}/{len(closed)} trades</span> ·
                <span style="color:#1e293b;">{fs['wins']}W/{fs['losses']}L</span> ·
                <span style="color:{wr_c};">{fs['wr']:.1f}% WR</span> ·
                <span style="color:{pnl_c};">${fs['pnl']:+.2f} P&L</span>
            </div>
            """, unsafe_allow_html=True)

        render_kpi_row(balance, total_pnl, roi, stats, len(open_live), active_strats)
        st.markdown("<br>", unsafe_allow_html=True)

        # ─── P&L CHART ───
        st.markdown('<div class="section-header">📈 Equity Curve</div>', unsafe_allow_html=True)
        render_equity_chart(
            filtered, LIVE_STARTING_BALANCE,
            selected_strategies=selected_strategies,
            real_balance=balance
        )

        # ─── STRATEGY CARDS ───
        st.markdown('<div class="section-header">🏆 Strategy Performance</div>', unsafe_allow_html=True)
        render_strategy_cards(filtered)

        st.markdown("<br>", unsafe_allow_html=True)

        # ─── TRADE HISTORY ───
        st.markdown('<div class="section-header">📋 Trade History</div>', unsafe_allow_html=True)
        render_trade_history(filtered)


# ════════════════════════════════════════════
# 📝 PAPER TAB
# ════════════════════════════════════════════
with tab_paper:
    st.markdown(f"""
    <div class="hero-header">
        <div class="hero-title" style="background: linear-gradient(135deg, #0ea5e9 0%, #6366f1 100%); -webkit-background-clip: text; -webkit-text-fill-color: transparent;">
            📝 Paper Trading
        </div>
        <div class="hero-sub">Backtested with real market resolution</div>
    </div>
    """, unsafe_allow_html=True)

    df_paper = load_trades(is_live=False)

    if df_paper.empty:
        st.info("No paper trades found.")
    else:
        if "exit_reason" in df_paper.columns:
            df_paper_real = df_paper[df_paper["exit_reason"].str.contains("real", na=False)]
        else:
            df_paper_real = df_paper

        closed_paper = df_paper_real[df_paper_real["status"] == "closed"] if "status" in df_paper_real.columns else df_paper_real
        all_paper_strats = sorted(closed_paper["strategy"].unique().tolist()) if "strategy" in closed_paper.columns else []

        pc1, pc2 = st.columns([3, 2])
        with pc1:
            paper_strats = st.multiselect("Strategies", options=all_paper_strats, default=all_paper_strats,
                                          format_func=lambda x: strategy_label(x), key="paper_strat")
        with pc2:
            paper_res = st.selectbox("Resolution", ["Real Only", "All Trades"], key="paper_res")

        if paper_res == "All Trades":
            paper_closed = df_paper[df_paper["status"] == "closed"] if "status" in df_paper.columns else df_paper
        else:
            paper_closed = closed_paper

        if paper_strats:
            paper_closed = paper_closed[paper_closed["strategy"].isin(paper_strats)]

        p_stats = compute_stats(paper_closed)
        p_pnl = p_stats["pnl"]
        p_bal = PAPER_STARTING_BALANCE + p_pnl
        p_roi = (p_pnl / PAPER_STARTING_BALANCE) * 100

        pk1, pk2, pk3, pk4 = st.columns(4)
        for col, label, value, cls in [
            (pk1, "Balance", f"${p_bal:,.2f}", "win" if p_bal >= PAPER_STARTING_BALANCE else "loss"),
            (pk2, "P&L", f"${p_pnl:+,.2f} ({p_roi:+.1f}%)", "win" if p_pnl >= 0 else "loss"),
            (pk3, "Record", f"{p_stats['wins']}W · {p_stats['losses']}L", "neutral"),
            (pk4, "Win Rate", f"{p_stats['wr']:.1f}%", "win" if p_stats['wr'] >= 50 else "loss"),
        ]:
            col.markdown(f"""
            <div class="metric-card">
                <div class="metric-label">{label}</div>
                <div class="metric-value {cls}">{value}</div>
            </div>
            """, unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown('<div class="section-header">📈 Paper Equity Curve</div>', unsafe_allow_html=True)
        render_equity_chart(paper_closed, PAPER_STARTING_BALANCE)

        st.markdown('<div class="section-header">🏆 Strategy Performance</div>', unsafe_allow_html=True)
        render_strategy_cards(paper_closed)

        st.markdown('<div class="section-header">📋 Trade History</div>', unsafe_allow_html=True)
        render_trade_history(paper_closed)

# ════════════════════════════════════════════
# 🧪 PAPER v2 TAB
# ════════════════════════════════════════════
PAPER_V2_DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "paper_v2.db")
PAPER_V2_STARTING = 100.0

with tab_paper_v2:
    is_v2_running = check_trader_running("paper_trader_v2")
    now_pst = datetime.now(timezone.utc).astimezone(PST).strftime("%I:%M %p PST")

    st.markdown(f"""
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:24px">
        <div style="width:12px;height:12px;border-radius:50%;background:{'#22c55e' if is_v2_running else '#ef4444'}"></div>
        <span style="font-size:1.1rem;font-weight:600">Paper v2 {'Running' if is_v2_running else 'Stopped'}</span>
        <span style="color:#94a3b8;font-size:0.9rem">{now_pst}</span>
    </div>
    """, unsafe_allow_html=True)

    # Load paper v2 trades
    v2_closed = pd.DataFrame()
    if os.path.exists(PAPER_V2_DB):
        try:
            import sqlite3 as _sq
            _conn = _sq.connect(PAPER_V2_DB)
            v2_closed = pd.read_sql_query(
                "SELECT * FROM trades WHERE status='closed' ORDER BY timestamp", _conn
            )
            v2_open_count = pd.read_sql_query(
                "SELECT COUNT(*) as cnt FROM trades WHERE status='open'", _conn
            ).iloc[0]["cnt"]
            _conn.close()
        except Exception as e:
            st.warning(f"Paper v2 DB error: {e}")
            v2_open_count = 0

    if v2_closed.empty:
        st.info("🧪 Paper Trader v2 just started — waiting for trades to resolve...")
    else:
        total_v2 = len(v2_closed)
        wins_v2 = len(v2_closed[v2_closed["pnl"] > 0])
        losses_v2 = total_v2 - wins_v2
        wr_v2 = wins_v2 / total_v2 * 100 if total_v2 > 0 else 0
        total_pnl_v2 = v2_closed["pnl"].sum()
        balance_v2 = PAPER_V2_STARTING + total_pnl_v2

        col1, col2, col3, col4, col5 = st.columns(5)
        for col, label, value, cls in [
            (col1, "Balance", f"${balance_v2:,.2f}", "positive" if balance_v2 >= PAPER_V2_STARTING else "negative"),
            (col2, "P&L", f"${total_pnl_v2:+,.2f}", "positive" if total_pnl_v2 >= 0 else "negative"),
            (col3, "Win Rate", f"{wr_v2:.1f}%", "positive" if wr_v2 >= 55 else "negative" if wr_v2 < 45 else ""),
            (col4, "Trades", f"{total_v2}", ""),
            (col5, "Open", f"{v2_open_count}", ""),
        ]:
            col.markdown(f"""
            <div class="metric-card">
                <div class="metric-label">{label}</div>
                <div class="metric-value {cls}">{value}</div>
            </div>
            """, unsafe_allow_html=True)

        # Add time_pst column for charts
        if "time_pst" not in v2_closed.columns and "timestamp" in v2_closed.columns:
            v2_closed["time_pst"] = pd.to_datetime(v2_closed["timestamp"], unit="s", utc=True).dt.tz_convert("US/Pacific").dt.strftime("%m/%d %I:%M %p")

        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown('<div class="section-header">📈 Paper v2 Equity Curve</div>', unsafe_allow_html=True)
        render_equity_chart(v2_closed, PAPER_V2_STARTING)

        st.markdown('<div class="section-header">🏆 Strategy Performance</div>', unsafe_allow_html=True)
        render_strategy_cards(v2_closed)

        st.markdown('<div class="section-header">📋 Trade History</div>', unsafe_allow_html=True)
        render_trade_history(v2_closed)
