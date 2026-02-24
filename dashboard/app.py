"""
🏞 The Outsiders — Trading Dashboard v5
Brighter, sleeker. Time-range P&L. Strategy version filtering.
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

# v1→v2 cutoff: Feb 23 2026 6:00 AM PST (when thresholds were updated)
V2_CUTOFF_TS = 1771905600
# v2→v3 cutoff: Feb 24 2026 7:45 AM PST (Mean Reversion Combo C filters)
V3_CUTOFF_TS = 1771955100

STRATEGY_COLORS = {
    "btc_5min_momentum_LIVE": "#00ff88",
    "btc_5min_meanrev_LIVE": "#ffb347",
    "btc_5min_ob_imbalance_LIVE": "#a29bfe",
    "btc_5min_smart_money_LIVE": "#fd79a8",
    "btc_5min_momentum": "#00d4aa",
    "btc_5min_meanrev": "#ff9f43",
    "btc_5min_ob_imbalance": "#6c5ce7",
    "btc_5min_smart_money": "#e84393",
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
    return STRATEGY_COLORS.get(name, "#8892b0")


def to_pst(ts):
    if pd.isna(ts) or ts is None:
        return ""
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(PST).strftime("%b %d, %I:%M:%S %p")
    except:
        return str(ts)


def get_version(ts):
    """Return strategy version based on timestamp."""
    try:
        ts = int(ts)
        if ts >= V3_CUTOFF_TS:
            return "v3"
        elif ts >= V2_CUTOFF_TS:
            return "v2"
        return "v1"
    except:
        return "v1"


def check_trader_running(name="live_trader"):
    try:
        import subprocess
        return subprocess.run(["pgrep", "-f", name], capture_output=True).returncode == 0
    except:
        return False


def load_trades(is_live=True, limit=1000):
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
        df["version"] = df["timestamp"].apply(get_version)
    if "signal_data" in df.columns:
        df["signal_data"] = df["signal_data"].apply(lambda x: json.loads(x) if isinstance(x, str) and x else {})
    return df


# ─── PAGE CONFIG ───
st.set_page_config(page_title="🏞 The Outsiders", page_icon="🏞", layout="wide", initial_sidebar_state="collapsed")
init_db()

# ─── GLOBAL CSS ───
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@300;400;500;600;700&display=swap');
    
    .stApp { font-family: 'Space Grotesk', sans-serif; }
    
    .glass-card {
        background: rgba(255,255,255,0.05);
        backdrop-filter: blur(20px);
        -webkit-backdrop-filter: blur(20px);
        border: 1px solid rgba(255,255,255,0.1);
        border-radius: 16px;
        padding: 20px;
        margin: 8px 0;
    }
    
    .hero-header {
        background: linear-gradient(135deg, #0a0a2e 0%, #1a1a4e 30%, #2d1b69 60%, #1a1a4e 100%);
        border-radius: 24px;
        padding: 32px 40px;
        margin-bottom: 24px;
        border: 1px solid rgba(255,255,255,0.08);
        box-shadow: 0 12px 40px rgba(0,255,136,0.08), 0 4px 12px rgba(162,155,254,0.06);
    }
    
    .hero-title {
        font-family: 'Space Grotesk', sans-serif;
        font-size: 2.8rem;
        font-weight: 700;
        background: linear-gradient(135deg, #00ff88 0%, #00d4ff 50%, #a29bfe 100%);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        letter-spacing: -0.02em;
    }
    
    .hero-sub {
        color: #b0b0d0;
        font-size: 1.05rem;
        font-weight: 300;
        margin-top: -4px;
    }
    
    .live-badge {
        display: inline-block;
        background: linear-gradient(135deg, #00ff88, #00d4ff);
        color: #000;
        padding: 6px 18px;
        border-radius: 25px;
        font-size: 0.78rem;
        font-weight: 700;
        letter-spacing: 0.12em;
        text-transform: uppercase;
        box-shadow: 0 0 20px rgba(0,255,136,0.3);
    }
    
    .offline-badge {
        display: inline-block;
        background: linear-gradient(135deg, #ff6b6b, #ee5a24);
        color: #fff;
        padding: 6px 18px;
        border-radius: 25px;
        font-size: 0.78rem;
        font-weight: 700;
        letter-spacing: 0.12em;
    }
    
    .metric-card {
        background: linear-gradient(145deg, rgba(255,255,255,0.06), rgba(255,255,255,0.02));
        backdrop-filter: blur(16px);
        border: 1px solid rgba(255,255,255,0.08);
        border-radius: 16px;
        padding: 18px 16px;
        text-align: center;
        transition: transform 0.2s, box-shadow 0.2s;
    }
    
    .metric-card:hover {
        transform: translateY(-2px);
        box-shadow: 0 8px 24px rgba(0,0,0,0.3);
    }
    
    .metric-label { color: #8888aa; font-size: 0.75rem; font-weight: 500; text-transform: uppercase; letter-spacing: 0.1em; }
    .metric-value { font-size: 1.6rem; font-weight: 700; margin: 4px 0; }
    .metric-sub { color: #6666888; font-size: 0.75rem; }
    
    .win { color: #00ff88; }
    .loss { color: #ff6b6b; }
    .neutral { color: #a29bfe; }
    
    .strat-card {
        background: linear-gradient(145deg, rgba(255,255,255,0.06), rgba(255,255,255,0.02));
        backdrop-filter: blur(16px);
        border: 1px solid rgba(255,255,255,0.08);
        border-radius: 16px;
        padding: 18px;
        transition: transform 0.2s;
    }
    
    .strat-card:hover { transform: translateY(-2px); }
    
    .filter-summary {
        background: linear-gradient(135deg, rgba(162,155,254,0.1), rgba(0,255,136,0.05));
        border: 1px solid rgba(162,155,254,0.2);
        border-radius: 12px;
        padding: 10px 20px;
        margin: 8px 0 16px 0;
    }
    
    .trade-row {
        background: rgba(255,255,255,0.03);
        border-radius: 10px;
        padding: 10px 16px;
        margin: 4px 0;
        border-left: 3px solid;
        display: flex;
        justify-content: space-between;
        align-items: center;
        font-size: 0.85rem;
    }
    
    .trade-win { border-left-color: #00ff88; }
    .trade-loss { border-left-color: #ff6b6b; }
    .trade-open { border-left-color: #a29bfe; }
    
    .version-badge {
        display: inline-block;
        padding: 2px 8px;
        border-radius: 8px;
        font-size: 0.65rem;
        font-weight: 700;
        letter-spacing: 0.05em;
    }
    .version-v1 { background: rgba(255,255,255,0.1); color: #888; }
    .version-v2 { background: rgba(0,255,136,0.15); color: #00ff88; }
    .version-v3 { background: rgba(0,136,255,0.15); color: #0088ff; }
</style>
""", unsafe_allow_html=True)


def compute_stats(df):
    """Compute KPI stats from a filtered dataframe."""
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
        (k3, "Record", f"{stats['wins']}W / {stats['losses']}L", f"{stats['total']} trades", "neutral"),
        (k4, "Win Rate", f"{stats['wr']:.1f}%", "target: 50%+", wr_class),
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


def render_equity_chart(df, starting_balance, time_range="All", selected_strategies=None, real_balance=None):
    """Render P&L equity curve with time range and strategy filters."""
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

    # Time range filter
    now_ts = datetime.now(timezone.utc).timestamp()
    range_map = {"1h": 3600, "6h": 21600, "12h": 43200, "24h": 86400, "7d": 604800, "All": None}
    if time_range != "All" and range_map.get(time_range):
        cutoff = now_ts - range_map[time_range]
        closed = closed[closed["timestamp"] >= cutoff]
    if closed.empty:
        st.info("No trades in selected time range.")
        return

    # Build equity curve — adjust for untracked P&L if real balance known
    closed["cumulative_pnl"] = closed["pnl"].cumsum()
    db_final = starting_balance + closed["cumulative_pnl"].iloc[-1] if len(closed) else starting_balance
    if real_balance is not None and real_balance > 0 and abs(db_final - real_balance) > 1.0:
        # DB is missing some losses/gains — adjust starting balance so chart endpoint = real balance
        adjustment = real_balance - db_final
        effective_start = starting_balance + adjustment
    else:
        effective_start = starting_balance
    closed["balance"] = effective_start + closed["cumulative_pnl"]

    fig = go.Figure()

    # Add line for each strategy
    strats = closed["strategy"].unique()
    if len(strats) > 1:
        # Overall line
        fig.add_trace(go.Scatter(
            x=closed["time_pst"], y=closed["balance"],
            mode="lines", name="Overall",
            line=dict(color="#ffffff", width=2.5),
            hovertemplate="$%{y:.2f}<extra>Overall</extra>"
        ))

    for strat in strats:
        s_df = closed[closed["strategy"] == strat]
        fig.add_trace(go.Scatter(
            x=s_df["time_pst"], y=effective_start + s_df["pnl"].cumsum(),
            mode="markers+lines", name=strategy_label(strat),
            line=dict(color=strategy_color(strat), width=1.5),
            marker=dict(size=4, color=strategy_color(strat)),
            opacity=0.7,
            hovertemplate="$%{y:.2f}<extra>" + strategy_label(strat) + "</extra>"
        ))

    # Starting balance line
    fig.add_hline(y=starting_balance, line_dash="dot", line_color="rgba(255,255,255,0.2)",
                  annotation_text=f"Start ${starting_balance:.0f}", annotation_font_color="rgba(255,255,255,0.4)")

    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="Space Grotesk", color="#b0b0d0"),
        height=380,
        margin=dict(l=20, r=20, t=40, b=20),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1,
                    font=dict(size=11), bgcolor="rgba(0,0,0,0)"),
        xaxis=dict(gridcolor="rgba(255,255,255,0.05)", showgrid=True),
        yaxis=dict(gridcolor="rgba(255,255,255,0.05)", showgrid=True, tickprefix="$"),
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
        pnl_c = "#00ff88" if s["pnl"] >= 0 else "#ff6b6b"
        wr_c = "#00ff88" if s["wr"] >= 50 else "#ff6b6b"
        
        # Version breakdown
        v1 = len(s_df[s_df["version"] == "v1"]) if "version" in s_df.columns else 0
        v2 = len(s_df[s_df["version"] == "v2"]) if "version" in s_df.columns else 0
        v3 = len(s_df[s_df["version"] == "v3"]) if "version" in s_df.columns else 0

        with cols[i % len(cols)]:
            st.markdown(f"""
            <div class="strat-card" style="border-top: 3px solid {color};">
                <div style="font-size:1.05rem;font-weight:600;color:#fff;margin-bottom:6px;">
                    {strategy_label(sname)}
                    <span class="version-badge version-v1">v1: {v1}</span>
                    <span class="version-badge version-v2">v2: {v2}</span>
                    <span class="version-badge version-v3">v3: {v3}</span>
                </div>
                <div style="color:#b0b0d0;font-size:0.82rem;line-height:2;">
                    Record: <b style="color:#fff">{s['wins']}W / {s['losses']}L</b><br>
                    Win Rate: <b style="color:{wr_c}">{s['wr']:.1f}%</b><br>
                    P&L: <b style="color:{pnl_c}">${s['pnl']:+.2f}</b><br>
                    Avg: <b style="color:{pnl_c}">${s['avg_pnl']:+.2f}</b>/trade
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
        pnl_pct = trade.get("pnl_pct")
        status = trade.get("status", "")
        strat = trade.get("strategy", "")
        time_str = to_pst(trade.get("timestamp"))
        edge = trade.get("edge_pct", 0)
        version = trade.get("version", "v1")
        
        emoji_dir = "🟢" if direction == "UP" else "🔴"
        color = strategy_color(strat)
        v_badge = f'<span class="version-badge version-{version}">{version}</span>'
        
        if status == "closed" and pnl is not None:
            if pnl > 0:
                css_class = "trade-win"
                result = f"✅ WON ${pnl:+.2f}"
            else:
                css_class = "trade-loss"
                result = f"❌ LOST ${pnl:.2f}"
        else:
            css_class = "trade-open"
            result = "⏳ Pending"
        
        st.markdown(f"""
        <div class="trade-row {css_class}">
            <div>
                <span style="color:{color};font-weight:600;">{strategy_label(strat)}</span> {v_badge}
                {emoji_dir} {direction} @ ${entry:.3f} | Edge: {edge:.1f}%
            </div>
            <div style="text-align:right;">
                <span style="font-weight:600;">{result}</span>
                <span style="color:#666;margin-left:12px;font-size:0.78rem;">{time_str}</span>
            </div>
        </div>
        """, unsafe_allow_html=True)


# ─── TABS ───
tab_live, tab_paper = st.tabs(["💰 LIVE TRADING", "📝 Paper Trading"])

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
                <span style="color:#8888aa;font-size:0.82rem;">{now_pst}</span>
            </div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    df_live = load_trades(is_live=True)

    if df_live.empty:
        st.markdown('<div style="text-align:center;padding:60px;"><h2 style="color:#8888aa;">🚀 No live trades yet</h2></div>', unsafe_allow_html=True)
    else:
        closed = df_live[df_live["status"] == "closed"] if "status" in df_live.columns else df_live
        open_live = df_live[df_live["status"] == "open"] if "status" in df_live.columns else pd.DataFrame()

        # ─── FILTERS ───
        all_strats = sorted(closed["strategy"].unique().tolist()) if "strategy" in closed.columns else []

        fc1, fc2, fc3, fc4 = st.columns([3, 2, 1.5, 1.5])
        with fc1:
            selected_strategies = st.multiselect(
                "📊 Strategy", options=all_strats, default=all_strats,
                format_func=lambda x: strategy_label(x), key="live_strat")
        with fc2:
            version_filter = st.selectbox("🏷️ Version", ["All", "v1 (old thresholds)", "v2 (optimized)", "v3 (Combo C)"], key="live_ver")
        with fc3:
            min_edge = st.slider("Min Edge %", 0.0, 20.0, 0.0, 0.5, key="live_edge")
        with fc4:
            min_conf = st.slider("Min Conf", 0.50, 0.80, 0.50, 0.01, key="live_conf")

        # Apply filters
        filtered = closed.copy()
        if selected_strategies:
            filtered = filtered[filtered["strategy"].isin(selected_strategies)]
        if version_filter.startswith("v1"):
            filtered = filtered[filtered["version"] == "v1"]
        elif version_filter.startswith("v2"):
            filtered = filtered[filtered["version"] == "v2"]
        elif version_filter.startswith("v3"):
            filtered = filtered[filtered["version"] == "v3"]
        if min_edge > 0 and "edge_pct" in filtered.columns:
            filtered = filtered[filtered["edge_pct"] >= min_edge]
        if min_conf > 0.5 and "confidence" in filtered.columns:
            filtered = filtered[filtered["confidence"] >= min_conf]

        # Balance + stats
        real_balance = get_live_balance()
        balance = real_balance if real_balance is not None else LIVE_STARTING_BALANCE + (closed["pnl"].sum() if "pnl" in closed.columns else 0)
        total_pnl = balance - LIVE_STARTING_BALANCE
        roi = (total_pnl / LIVE_STARTING_BALANCE) * 100
        stats = compute_stats(filtered)
        active_strats = len(set(open_live["strategy"])) if not open_live.empty and "strategy" in open_live.columns else 0

        # Filter summary
        if len(filtered) != len(closed):
            fs = compute_stats(filtered)
            wr_c = "#00ff88" if fs["wr"] >= 50 else "#ff6b6b"
            pnl_c = "#00ff88" if fs["pnl"] >= 0 else "#ff6b6b"
            st.markdown(f"""
            <div class="filter-summary">
                <b style="color:#a29bfe;">🔍 Filtered:</b>
                <span style="color:#fff;">{fs['total']}/{len(closed)} trades</span> ·
                <span style="color:#fff;">{fs['wins']}W/{fs['losses']}L</span> ·
                <span style="color:{wr_c};">{fs['wr']:.1f}% WR</span> ·
                <span style="color:{pnl_c};">${fs['pnl']:+.2f} P&L</span>
            </div>
            """, unsafe_allow_html=True)

        render_kpi_row(balance, total_pnl, roi, stats, len(open_live), active_strats)
        st.markdown("<br>", unsafe_allow_html=True)

        # ─── P&L CHART ───
        st.markdown("### 📈 Equity Curve")
        tr1, tr2, tr3, tr4, tr5, tr6 = st.columns(6)
        time_ranges = {"1h": tr1, "6h": tr2, "12h": tr3, "24h": tr4, "7d": tr5, "All": tr6}
        time_range = "All"
        for label, col in time_ranges.items():
            if col.button(label, key=f"tr_{label}", use_container_width=True):
                time_range = label

        # Use session state for time range persistence
        if "live_time_range" not in st.session_state:
            st.session_state.live_time_range = "All"
        for label in time_ranges:
            if st.session_state.get(f"tr_{label}_clicked"):
                st.session_state.live_time_range = label

        render_equity_chart(filtered, LIVE_STARTING_BALANCE, time_range, selected_strategies, real_balance=balance)

        # ─── STRATEGY CARDS ───
        st.markdown("### 🏆 Strategy Performance")
        render_strategy_cards(filtered)

        st.markdown("<br>", unsafe_allow_html=True)

        # ─── TRADE HISTORY ───
        st.markdown("### 📋 Trade History")
        display_df = filtered if len(filtered) < len(df_live) else df_live
        if selected_strategies:
            display_df = display_df[display_df["strategy"].isin(selected_strategies)]
        if version_filter.startswith("v1"):
            display_df = display_df[display_df["version"] == "v1"]
        elif version_filter.startswith("v2"):
            display_df = display_df[display_df["version"] == "v2"]
        elif version_filter.startswith("v3"):
            display_df = display_df[display_df["version"] == "v3"]
        render_trade_history(display_df)


# ════════════════════════════════════════════
# 📝 PAPER TAB
# ════════════════════════════════════════════
with tab_paper:
    st.markdown("""
    <div class="hero-header" style="background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%);">
        <div class="hero-title" style="background: linear-gradient(135deg, #00d4ff 0%, #a29bfe 100%); -webkit-background-clip: text; -webkit-text-fill-color: transparent;">
            📝 Paper Trading
        </div>
        <div class="hero-sub">Backtested with real market resolution</div>
    </div>
    """, unsafe_allow_html=True)

    df_paper = load_trades(is_live=False)

    if df_paper.empty:
        st.info("No paper trades found.")
    else:
        # Filter to real resolution only
        if "exit_reason" in df_paper.columns:
            df_paper_real = df_paper[df_paper["exit_reason"].str.contains("real", na=False)]
        else:
            df_paper_real = df_paper

        closed_paper = df_paper_real[df_paper_real["status"] == "closed"] if "status" in df_paper_real.columns else df_paper_real

        all_paper_strats = sorted(closed_paper["strategy"].unique().tolist()) if "strategy" in closed_paper.columns else []

        pc1, pc2 = st.columns([3, 2])
        with pc1:
            paper_strats = st.multiselect("📊 Strategy", options=all_paper_strats, default=all_paper_strats,
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
            (pk3, "Record", f"{p_stats['wins']}W / {p_stats['losses']}L", "neutral"),
            (pk4, "Win Rate", f"{p_stats['wr']:.1f}%", "win" if p_stats['wr'] >= 50 else "loss"),
        ]:
            col.markdown(f"""
            <div class="metric-card">
                <div class="metric-label">{label}</div>
                <div class="metric-value {cls}">{value}</div>
            </div>
            """, unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown("### 📈 Paper Equity Curve")
        render_equity_chart(paper_closed, PAPER_STARTING_BALANCE, "All", paper_strats)

        st.markdown("### 🏆 Strategy Performance")
        render_strategy_cards(paper_closed)

        st.markdown("### 📋 Trade History")
        render_trade_history(paper_closed)
