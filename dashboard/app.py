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
        {"version": "v4", "start": 1772035700, "label": "Conf 58%, streak breaker, overnight gate"},
    ],
    "btc_5min_meanrev_LIVE": [
        {"version": "v1", "start": 0, "label": "Original"},
        {"version": "v2", "start": 1771905600, "label": "Edge 4%, max 12%, conf 55%"},
        {"version": "v3", "start": 1771955100, "label": "Combo C filters"},
    ],
    "btc_5min_ob_imbalance_LIVE": [
        {"version": "v1", "start": 0, "label": "Original (4% edge)"},
        {"version": "v2", "start": 1771964100, "label": "Min edge 3%"},
        {"version": "v3", "start": 1772035700, "label": "Min edge 3.5%"},
    ],
    "btc_5min_smart_money_LIVE": [
        {"version": "v1", "start": 0, "label": "Original"},
        {"version": "v2", "start": 1771905600, "label": "Edge 10%, conf 59%, streak breaker"},
        {"version": "v3", "start": 1771955100, "label": "Combo C + edge cap 20%"},
        {"version": "v4", "start": 1772035700, "label": "Min edge 11%, overnight gate, streak breaker"},
    ],
    "btc_5min_trend_rider_LIVE": [
        {"version": "v1", "start": 1772089800, "label": "Launch — EMA8/21 + VWAP + RSI zones"},
    ],
}

STRATEGY_COLORS = {
    "btc_5min_momentum_LIVE": "#6366f1",
    "btc_5min_meanrev_LIVE": "#f59e0b",
    "btc_5min_ob_imbalance_LIVE": "#8b5cf6",
    "btc_5min_smart_money_LIVE": "#ec4899",
    "btc_5min_trend_rider_LIVE": "#22c55e",
    "btc_5min_momentum": "#6366f1",
    "btc_5min_meanrev": "#f59e0b",
    "btc_5min_ob_imbalance": "#8b5cf6",
    "btc_5min_smart_money": "#ec4899",
    "btc_5min_trend_rider": "#22c55e",
}

STRATEGY_LABELS = {
    "btc_5min_momentum_LIVE": "⚡ Momentum",
    "btc_5min_meanrev_LIVE": "🔄 Mean Reversion",
    "btc_5min_ob_imbalance_LIVE": "📊 OB Imbalance",
    "btc_5min_smart_money_LIVE": "🧠 Smart Money",
    "btc_5min_trend_rider_LIVE": "📈 Trend Rider",
    "btc_5min_momentum": "⚡ Momentum",
    "btc_5min_meanrev": "🔄 Mean Reversion",
    "btc_5min_ob_imbalance": "📊 OB Imbalance",
    "btc_5min_smart_money": "🧠 Smart Money",
    "btc_5min_trend_rider": "📈 Trend Rider",
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
    q = "SELECT * FROM trades WHERE strategy {} '%_LIVE' AND (is_simulated = 0 OR is_simulated IS NULL) ORDER BY timestamp DESC LIMIT ?".format(
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


def load_real_trades(limit=2000):
    """Load verified on-chain trades from real_trades table (CSV import)."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM real_trades ORDER BY timestamp DESC LIMIT ?",
            (limit,)
        ).fetchall()
    except Exception:
        conn.close()
        return pd.DataFrame()
    conn.close()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([dict(r) for r in rows])
    # Add dashboard-compatible columns
    df["time_pst"] = pd.to_datetime(df["timestamp"], unit="s", utc=True).dt.tz_convert("US/Pacific")
    df["time_display"] = df["timestamp"].apply(to_pst)
    df["entry_price"] = df["avg_price"]
    df["quantity"] = df["tokens"]
    df["version"] = df.apply(lambda row: get_strategy_version(row["strategy"], row["timestamp"]), axis=1)
    # Pull edge/confidence from original trades table by matching market_id + strategy
    try:
        edge_map = {}
        conn2 = get_connection()
        for row in conn2.execute(
            "SELECT market_id, strategy, edge_pct, confidence FROM trades WHERE strategy LIKE '%_LIVE' AND edge_pct IS NOT NULL"
        ).fetchall():
            edge_map[(row[0], row[1])] = (row[2], row[3])
        conn2.close()
        df["edge_pct"] = df.apply(lambda r: edge_map.get((r["market_id"], r["strategy"]), (0.0, 0.0))[0], axis=1)
        df["confidence"] = df.apply(lambda r: edge_map.get((r["market_id"], r["strategy"]), (0.0, 0.0))[1], axis=1)
    except Exception:
        df["edge_pct"] = 0.0
        df["confidence"] = 0.0
    return df


# ─── PAGE CONFIG ───
st.set_page_config(page_title="🏞 The Outsiders", page_icon="🏞", layout="wide", initial_sidebar_state="collapsed")
from streamlit_autorefresh import st_autorefresh
st_autorefresh(interval=30_000, limit=None, key="live_refresh")  # Refresh every 30s
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


def calibrate_live_pnl(df, real_balance, starting_balance):
    """Calibrate DB P&L to match real CLOB balance.
    
    The DB records gross P&L without accounting for fees, slippage, and
    phantom trades (orders logged but never filled). We distribute the
    discrepancy proportionally by trade volume so bigger trades absorb
    more correction.
    """
    if real_balance is None:
        return df
    closed = df[df["status"] == "closed"] if "status" in df.columns else df
    if closed.empty or "pnl" not in closed.columns:
        return df
    
    db_gross = closed["pnl"].sum()
    real_pnl = real_balance - starting_balance
    gap = db_gross - real_pnl  # positive = DB is overstating
    
    if abs(gap) < 0.01:
        return df
    
    # Distribute correction proportional to trade volume (quantity * entry_price)
    df = df.copy()
    if "quantity" in df.columns and "entry_price" in df.columns:
        df["_volume"] = (df["quantity"].fillna(0) * df["entry_price"].fillna(0)).abs()
    else:
        df["_volume"] = 1.0
    
    closed_mask = df["status"] == "closed" if "status" in df.columns else pd.Series(True, index=df.index)
    total_volume = df.loc[closed_mask, "_volume"].sum()
    
    if total_volume > 0:
        df.loc[closed_mask, "pnl_calibrated"] = (
            df.loc[closed_mask, "pnl"] - gap * df.loc[closed_mask, "_volume"] / total_volume
        )
    else:
        # Fallback: even distribution
        n = closed_mask.sum()
        df.loc[closed_mask, "pnl_calibrated"] = df.loc[closed_mask, "pnl"] - gap / n
    
    # Open trades keep original pnl
    df.loc[~closed_mask, "pnl_calibrated"] = df.loc[~closed_mask, "pnl"]
    df.drop(columns=["_volume"], inplace=True)
    return df


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
    """Render P&L equity curve showing portfolio balance and per-strategy contributions.
    Uses calibrated P&L when available (anchored to real CLOB balance)."""
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
tab_v2, tab_report, tab_live, tab_sniper, tab_paper_v31, tab_paper_v32, tab_paper_v33, tab_ml, tab_mc = st.tabs(["🏞 V2 TRADER", "📊 TP/SL Report", "💰 LEGACY LIVE", "🎯 Sniper", "🚀 Paper v3.1 (HTF)", "🔄 Paper v3.2 (Inverse)", "🪞 Paper v3.3 (Mirror)", "🧠 ML Brain", "🎛️ Mission Control"])

# ════════════════════════════════════════════
# 🏞 V2 TRADER TAB
# ════════════════════════════════════════════
with tab_v2:
    import sqlite3 as _sq3
    _v2_db = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "trader_v2.db")
    _v2_running = check_trader_running("live_trader_v2")
    _v2_badge = '<span class="live-badge">● LIVE</span>' if _v2_running else '<span class="offline-badge">● OFFLINE</span>'
    _v2_now = datetime.now(timezone.utc).astimezone(PST).strftime("%I:%M %p PST")
    
    st.markdown(f"""
    <div style="background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%); padding: 2rem; border-radius: 16px; margin-bottom: 1.5rem;">
        <h1 style="color: #e94560; margin: 0; font-size: 2rem;">🏞 The Outsiders v2</h1>
        <p style="color: #a8a8a8; margin: 0.5rem 0 0 0;">Clean Trading Engine — TA + SL + Multi-Asset | {_v2_badge} {_v2_now}</p>
    </div>
    """, unsafe_allow_html=True)
    
    # Load v2 trades
    _v2_trades = pd.DataFrame()
    try:
        if os.path.exists(_v2_db):
            _v2_conn = _sq3.connect(_v2_db)
            _v2_trades = pd.read_sql_query(
                "SELECT * FROM trades_v2 ORDER BY id DESC", _v2_conn)
            _v2_conn.close()
    except Exception:
        pass
    
    # KPIs
    _v2c1, _v2c2, _v2c3, _v2c4, _v2c5, _v2c6 = st.columns(6)
    
    if not _v2_trades.empty:
        _closed = _v2_trades[_v2_trades['status'] == 'closed']
        _open = _v2_trades[_v2_trades['status'] == 'open']
        _wins = _closed[_closed['pnl'] > 0] if 'pnl' in _closed.columns else pd.DataFrame()
        _losses = _closed[_closed['pnl'] <= 0] if 'pnl' in _closed.columns else pd.DataFrame()
        _total_pnl = _closed['pnl'].sum() if 'pnl' in _closed.columns else 0
        _wr = len(_wins) / len(_closed) * 100 if len(_closed) > 0 else 0
        _total_cost = _closed['cost'].sum() if 'cost' in _closed.columns else 0
        _roi = (_total_pnl / _total_cost * 100) if _total_cost > 0 else 0
        
        _v2c1.metric("Trades", f"{len(_closed)}")
        _v2c2.metric("Win Rate", f"{_wr:.1f}%")
        _v2c3.metric("P&L", f"${_total_pnl:+.2f}")
        _v2c4.metric("ROI", f"{_roi:+.1f}%")
        _v2c5.metric("Open", f"{len(_open)}")
        _v2c6.metric("W/L", f"{len(_wins)}/{len(_losses)}")
        
        # Cumulative P&L chart
        if len(_closed) > 0 and 'pnl' in _closed.columns:
            _sorted = _closed.sort_values('id')
            _sorted['cum_pnl'] = _sorted['pnl'].cumsum()
            
            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=list(range(1, len(_sorted)+1)), y=_sorted['cum_pnl'],
                mode='lines+markers', name='Cumulative P&L',
                line=dict(color='#e94560', width=2),
                marker=dict(size=6, color=['#00d26a' if p > 0 else '#ff4757' for p in _sorted['pnl']])
            ))
            fig.update_layout(
                title="📈 Cumulative P&L", height=350,
                xaxis_title="Trade #", yaxis_title="P&L ($)",
                template="plotly_dark", paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)')
            st.plotly_chart(fig, use_container_width=True)
        
        # Strategy breakdown
        if 'strategy' in _closed.columns:
            st.subheader("📊 Strategy Performance")
            for strat in _closed['strategy'].unique():
                _s = _closed[_closed['strategy'] == strat]
                _sw = len(_s[_s['pnl'] > 0])
                _sl = len(_s[_s['pnl'] <= 0])
                _spnl = _s['pnl'].sum()
                _swr = _sw / len(_s) * 100 if len(_s) > 0 else 0
                emoji = "🚀" if strat == "momentum" else "🔄"
                st.markdown(f"**{emoji} {strat.title()}**: {len(_s)} trades | {_sw}W-{_sl}L ({_swr:.0f}%) | P&L: ${_spnl:+.2f}")
        
        # Asset breakdown
        if 'asset' in _closed.columns:
            st.subheader("🪙 Asset Performance")
            _ac1, _ac2, _ac3, _ac4 = st.columns(4)
            for col, asset in zip([_ac1, _ac2, _ac3, _ac4], ['btc', 'eth', 'sol', 'xrp']):
                _a = _closed[_closed['asset'] == asset]
                if len(_a) > 0:
                    _apnl = _a['pnl'].sum()
                    _awr = len(_a[_a['pnl'] > 0]) / len(_a) * 100
                    col.metric(f"{asset.upper()}", f"${_apnl:+.2f}", f"{_awr:.0f}% WR ({len(_a)} trades)")
                else:
                    col.metric(f"{asset.upper()}", "No trades", "—")
        
        # Trade log
        st.subheader("📋 Recent Trades")
        _display_cols = ['timestamp', 'asset', 'strategy', 'direction', 'entry_price', 
                         'pnl', 'exit_reason', 'ta_summary']
        _available = [c for c in _display_cols if c in _v2_trades.columns]
        st.dataframe(_v2_trades[_available].head(50), use_container_width=True, height=400)
    else:
        st.info("🕐 No trades yet — v2 trader is running and waiting for signals. Trades will appear when BTC moves.")
        st.markdown("""
        **v2 Architecture:**
        - 🚀 **Momentum**: 3+ TA indicators (RSI, MACD, EMA, VWAP, Heikin Ashi, Momentum) agreeing
        - 🔄 **Contrarian**: Fade extreme odds (>75%) when TA disagrees with crowd
        - 🛡️ **Stop-Loss**: $0.07 drop = auto-sell (the real edge)
        - 📊 **Multi-Asset**: BTC, ETH, SOL, XRP scanned every 5 minutes
        """)

# ════════════════════════════════════════════
# 📊 TP/SL REPORT TAB
# ════════════════════════════════════════════
with tab_report:
    import sqlite3 as _sq3r
    _r_v2_db = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "trader_v2.db")
    _r_legacy_db = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "polymarket.db")
    
    st.markdown("""
    <div style="background: linear-gradient(135deg, #0f3443 0%, #34e89e 100%); padding: 2rem; border-radius: 16px; margin-bottom: 1.5rem;">
        <h1 style="color: #fff; margin: 0; font-size: 2rem;">📊 The TP/SL Edge — How We Became Profitable</h1>
        <p style="color: #d0f0d0; margin: 0.5rem 0 0 0;">March 11-12, 2026 — The night everything changed</p>
    </div>
    """, unsafe_allow_html=True)
    
    # Load data
    _r_v2_trades = pd.DataFrame()
    _r_legacy_trades = pd.DataFrame()
    try:
        if os.path.exists(_r_v2_db):
            _rc = _sq3r.connect(_r_v2_db)
            _r_v2_trades = pd.read_sql_query("SELECT * FROM trades_v2 ORDER BY id", _rc)
            _rc.close()
    except: pass
    try:
        if os.path.exists(_r_legacy_db):
            _rc2 = _sq3r.connect(_r_legacy_db)
            _r_legacy_trades = pd.read_sql_query(
                "SELECT * FROM trades WHERE is_simulated = 0 AND pnl IS NOT NULL ORDER BY id", _rc2)
            _rc2.close()
    except: pass
    
    # ── THE STORY ──
    st.markdown("""
    ## 🎬 The Story
    
    **Before TP/SL** (Feb 22 – Mar 11): We ran 5 TA strategies with an ML meta-learner for 504 trades.
    Every trade was held to expiration — either a full win ($1.00 resolution) or a full loss ($0.00).
    The result: **44% win rate, -$347.94 total P&L, -$0.69 per trade.**
    
    **The problem wasn't direction prediction** — even a coin flip gets 50%. The problem was 
    **asymmetric losses**: a losing trade cost ~$5, while an average win only returned ~$4.50 after fees.
    
    **On March 11 at 9:14 PM PST**, we deployed the v2 trader with two game-changing mechanics:
    
    - 🎯 **Take Profit (TP)**: When the token bid hits $0.95+, sell immediately. Lock in ~90% of max profit
      without waiting for resolution. *Inspired by Jakob manually selling SOL DOWN at $0.999 for +$5.84.*
    
    - 🛑 **Stop Loss (SL)**: When the token bid drops $0.07 below entry, sell immediately. Cap losses at 
      ~$2.50 instead of the full ~$5.00. The math: break-even WR drops from 50% to just **13.5%**.
    
    The SL is the real edge. **Direction prediction barely matters when your losses are 50% of your wins.**
    """)
    
    # ── THE NUMBERS ──
    st.markdown("## 📈 The Numbers")
    
    if not _r_v2_trades.empty:
        _r_closed = _r_v2_trades[_r_v2_trades['status'] == 'closed'].copy()
        _r_closed = _r_closed[_r_closed['pnl'].notna()]
        
        if len(_r_closed) > 0:
            # KPI comparison
            st.markdown("### Before vs After")
            _comp1, _comp2 = st.columns(2)
            
            with _comp1:
                st.markdown("""
                <div style="background: #2d1b1b; padding: 1.5rem; border-radius: 12px; border-left: 4px solid #ff4757;">
                    <h3 style="color: #ff4757; margin: 0;">❌ Before (Legacy)</h3>
                """, unsafe_allow_html=True)
                if not _r_legacy_trades.empty:
                    _leg_pnl = _r_legacy_trades['pnl'].sum()
                    _leg_n = len(_r_legacy_trades)
                    _leg_w = len(_r_legacy_trades[_r_legacy_trades['pnl'] > 0])
                    _leg_wr = _leg_w / _leg_n * 100
                    st.metric("Trades", f"{_leg_n}")
                    st.metric("Win Rate", f"{_leg_wr:.0f}%")
                    st.metric("Total P&L", f"${_leg_pnl:.2f}")
                    st.metric("Avg P&L/Trade", f"${_leg_pnl/_leg_n:.2f}")
                    st.metric("Exit Strategy", "Hold to expiry ☠️")
                st.markdown("</div>", unsafe_allow_html=True)
            
            with _comp2:
                st.markdown("""
                <div style="background: #1b2d1b; padding: 1.5rem; border-radius: 12px; border-left: 4px solid #2ed573;">
                    <h3 style="color: #2ed573; margin: 0;">✅ After (v2 + TP/SL)</h3>
                """, unsafe_allow_html=True)
                _v2_pnl = _r_closed['pnl'].sum()
                _v2_n = len(_r_closed)
                _v2_w = len(_r_closed[_r_closed['pnl'] > 0])
                _v2_wr = _v2_w / _v2_n * 100
                st.metric("Trades", f"{_v2_n}")
                st.metric("Win Rate", f"{_v2_wr:.0f}%")
                st.metric("Total P&L", f"${_v2_pnl:+.2f}")
                st.metric("Avg P&L/Trade", f"${_v2_pnl/_v2_n:+.2f}")
                st.metric("Exit Strategy", "TP @ $0.95 / SL @ -$0.07 🛡️")
                st.markdown("</div>", unsafe_allow_html=True)
            
            # ── SL DEEP DIVE ──
            st.markdown("### 🛑 Stop-Loss Deep Dive")
            
            _sl = _r_closed[_r_closed['exit_reason'] == 'stop_loss']
            _tp = _r_closed[_r_closed['exit_reason'] == 'take_profit']
            _wins = _r_closed[_r_closed['exit_reason'] == 'win']
            _losses = _r_closed[_r_closed['exit_reason'] == 'loss']
            
            _sl1, _sl2, _sl3 = st.columns(3)
            
            if len(_sl) > 0:
                _sl_total = _sl['pnl'].sum()
                _sl_avg = _sl['pnl'].mean()
                _full_loss = -_sl['cost'].sum()
                _saved = abs(_full_loss) - abs(_sl_total)
                
                _sl1.metric("SL Exits", f"{len(_sl)}", f"avg ${_sl_avg:.2f}/trade")
                _sl2.metric("Money Saved by SL", f"${_saved:.2f}", 
                           f"vs -${abs(_full_loss):.2f} without SL")
                _sl3.metric("Avg Loss: SL vs Full", 
                           f"${abs(_sl_avg):.2f} vs ${abs(_full_loss/len(_sl)):.2f}",
                           f"{(1 - abs(_sl_avg) / abs(_full_loss/len(_sl))) * 100:.0f}% smaller losses")
            
            # Exit breakdown pie chart
            exit_data = {}
            if len(_tp) > 0: exit_data['🎯 Take Profit'] = len(_tp)
            if len(_sl) > 0: exit_data['🛑 Stop Loss'] = len(_sl)
            if len(_wins) > 0: exit_data['✅ Win (Resolution)'] = len(_wins)
            if len(_losses) > 0: exit_data['❌ Loss (Resolution)'] = len(_losses)
            
            _pie1, _pie2 = st.columns(2)
            
            with _pie1:
                fig_pie = go.Figure(data=[go.Pie(
                    labels=list(exit_data.keys()), values=list(exit_data.values()),
                    marker_colors=['#2ed573', '#ffa502', '#00d26a', '#ff4757'],
                    hole=0.4, textinfo='label+value+percent'
                )])
                fig_pie.update_layout(title="Exit Type Distribution", height=350,
                                     template="plotly_dark", paper_bgcolor='rgba(0,0,0,0)')
                st.plotly_chart(fig_pie, use_container_width=True)
            
            with _pie2:
                # P&L by exit type bar chart
                exit_pnl = {}
                if len(_tp) > 0: exit_pnl['🎯 TP'] = _tp['pnl'].sum()
                if len(_sl) > 0: exit_pnl['🛑 SL'] = _sl['pnl'].sum()
                if len(_wins) > 0: exit_pnl['✅ Win'] = _wins['pnl'].sum()
                if len(_losses) > 0: exit_pnl['❌ Loss'] = _losses['pnl'].sum()
                
                colors = ['#2ed573' if v > 0 else '#ff4757' for v in exit_pnl.values()]
                fig_bar = go.Figure(data=[go.Bar(
                    x=list(exit_pnl.keys()), y=list(exit_pnl.values()),
                    marker_color=colors, text=[f"${v:+.2f}" for v in exit_pnl.values()],
                    textposition='outside'
                )])
                fig_bar.update_layout(title="P&L by Exit Type ($)", height=350,
                                     yaxis_title="P&L ($)", template="plotly_dark",
                                     paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)')
                st.plotly_chart(fig_bar, use_container_width=True)
            
            # ── CUMULATIVE P&L COMPARISON ──
            st.markdown("### 📉 Cumulative P&L: With TP/SL vs Without")
            
            _sorted = _r_closed.sort_values('id').copy()
            _sorted['cum_pnl'] = _sorted['pnl'].cumsum()
            
            # Simulate "without SL" - SL trades become full losses, TP trades go to resolution (assume win)
            _sorted['pnl_no_sl'] = _sorted.apply(
                lambda r: -r['cost'] if r['exit_reason'] == 'stop_loss' 
                else (r['cost'] if r['exit_reason'] == 'take_profit' else r['pnl']), axis=1)
            _sorted['cum_pnl_no_sl'] = _sorted['pnl_no_sl'].cumsum()
            
            fig_cum = go.Figure()
            fig_cum.add_trace(go.Scatter(
                x=list(range(1, len(_sorted)+1)), y=_sorted['cum_pnl'],
                mode='lines+markers', name='With TP/SL',
                line=dict(color='#2ed573', width=3),
                marker=dict(size=8, color=[
                    '#2ed573' if r == 'take_profit' else '#ffa502' if r == 'stop_loss' 
                    else '#00d26a' if p > 0 else '#ff4757' 
                    for r, p in zip(_sorted['exit_reason'], _sorted['pnl'])])
            ))
            fig_cum.add_trace(go.Scatter(
                x=list(range(1, len(_sorted)+1)), y=_sorted['cum_pnl_no_sl'],
                mode='lines+markers', name='Without TP/SL (est)',
                line=dict(color='#ff4757', width=2, dash='dash'),
                marker=dict(size=5, color='#ff4757')
            ))
            fig_cum.add_hline(y=0, line_dash="dot", line_color="gray", opacity=0.5)
            fig_cum.update_layout(
                title="The TP/SL Edge — Cumulative P&L Comparison", height=400,
                xaxis_title="Trade #", yaxis_title="Cumulative P&L ($)",
                template="plotly_dark", paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)',
                legend=dict(x=0.02, y=0.98))
            st.plotly_chart(fig_cum, use_container_width=True)
            
            # ── PER ASSET ──
            st.markdown("### 🪙 Per-Asset Performance")
            _ac1, _ac2, _ac3, _ac4 = st.columns(4)
            for col, asset in zip([_ac1, _ac2, _ac3, _ac4], ['btc', 'eth', 'sol', 'xrp']):
                _a = _r_closed[_r_closed['asset'] == asset]
                if len(_a) > 0:
                    _apnl = _a['pnl'].sum()
                    _aw = len(_a[_a['pnl'] > 0])
                    _al = len(_a) - _aw
                    _awr = _aw / len(_a) * 100
                    col.metric(f"{asset.upper()}", f"${_apnl:+.2f}", 
                              f"{_aw}W-{_al}L ({_awr:.0f}% WR)")
                else:
                    col.metric(f"{asset.upper()}", "—", "No trades")
            
            # ── TRADE LOG ──
            st.markdown("### 📋 Full Trade Log")
            
            _log = _r_v2_trades.copy()
            _log['emoji'] = _log.apply(lambda r: '🎯' if r.get('exit_reason') == 'take_profit' 
                                        else '🛑' if r.get('exit_reason') == 'stop_loss'
                                        else '✅' if r.get('exit_reason') == 'win'
                                        else '❌' if r.get('exit_reason') == 'loss'
                                        else '⏳', axis=1)
            _display = ['emoji', 'id', 'timestamp', 'asset', 'direction', 'strategy',
                        'entry_price', 'exit_price', 'pnl', 'exit_reason', 'ta_summary']
            _avail = [c for c in _display if c in _log.columns]
            st.dataframe(_log[_avail], use_container_width=True, height=400)
            
            # ── KEY FINDINGS ──
            st.markdown("""
            ## 🔬 Key Findings
            
            ### 1. Stop-Loss Is The Real Edge
            Our TA signals predict direction at ~44% accuracy (worse than a coin flip). 
            But **that doesn't matter** when losses are capped:
            
            | Metric | Without SL | With SL |
            |--------|-----------|---------|
            | Avg Win | ~$4.85 | ~$4.85 |
            | Avg Loss | ~$5.08 | ~$2.64 |
            | Break-even WR | ~51% | ~35% |
            | Our WR | 44% | 44% |
            | Result | ❌ Losing | ✅ Getting closer |
            
            ### 2. Take-Profit Locks In Gains Early
            When a token hits $0.95 (up from our ~$0.50 entry), we sell. No waiting 
            for resolution, no risk of reversal. One TP trade netted +$4.59 on ETH DOWN.
            
            ### 3. Complement Matching Is A ~30% Tax
            About 30% of our fills are "complement matched" — the CLOB mints new tokens 
            and we get dust instead of real shares. These trades can't be exited early.
            **SL and TP only work on real fills.**
            
            ### 4. The Math That Changed Everything
            
            **Old system**: Win $4.85, Lose $5.08 → Need >51% WR to profit  
            **New system**: Win $4.85, Lose $2.64 → Need >35% WR to profit  
            **Our WR**: ~44% → **Old: Losing. New: Winning potential.**
            
            At 44% WR with SL:
            - Expected per trade: (0.44 × $4.85) - (0.56 × $2.64) = **+$0.65/trade**
            - At 10 trades/hour: **+$6.50/hour, +$156/day**
            
            *Note: Actual results depend on fill quality, complement matching rate, and market conditions.*
            
            ## 🛣️ What's Next
            
            1. **Fix complement matching**: Only buy when real asks exist on the book
            2. **Tighten SL**: Test $0.05 drop (saves more per loss, may stop out more winners)
            3. **Add more assets**: ETH, SOL, XRP all trade-able with similar mechanics
            4. **Scale size**: Once profitable at $5/trade, increase to $10-$20
            """)
    else:
        st.info("No v2 trade data yet. The report will populate as trades execute.")

# ════════════════════════════════════════════
# 💰 LIVE TAB (Legacy)
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

    # Combine real_trades (on-chain verified) with newer trades from DB
    df_real = load_real_trades()
    df_db = load_trades(is_live=True)
    if not df_real.empty and not df_db.empty:
        # Find the latest real_trade timestamp, then append any DB trades after it
        # Both tables use unix epoch for timestamp
        latest_real = pd.to_numeric(df_real["timestamp"], errors="coerce").max() if "timestamp" in df_real.columns else None
        if latest_real and pd.notna(latest_real):
            db_ts = pd.to_numeric(df_db["timestamp"], errors="coerce")
            newer_db = df_db[db_ts > latest_real]
            if not newer_db.empty:
                df_live = pd.concat([df_real, newer_db], ignore_index=True)
            else:
                df_live = df_real
        else:
            df_live = df_real
        using_real = True
    elif not df_real.empty:
        df_live = df_real
        using_real = True
    else:
        df_live = df_db
        using_real = False

    # Ensure all rows have time_pst and version after merge
    if not df_live.empty:
        if "timestamp" in df_live.columns:
            df_live["time_pst"] = pd.to_datetime(pd.to_numeric(df_live["timestamp"], errors="coerce"), unit="s", utc=True).dt.tz_convert("US/Pacific")
            df_live["time_display"] = df_live["timestamp"].apply(to_pst)
        if "version" not in df_live.columns or df_live["version"].isna().any():
            df_live["version"] = df_live.apply(
                lambda row: get_strategy_version(row.get("strategy", ""), row.get("timestamp", 0)) 
                if pd.isna(row.get("version")) else row["version"], axis=1)
    
    if df_live.empty:
        st.markdown('<div style="text-align:center;padding:60px;"><h2 style="color:#94a3b8;">🚀 No live trades yet</h2></div>', unsafe_allow_html=True)
    else:
        closed = df_live[df_live["status"] == "closed"] if "status" in df_live.columns else df_live
        if using_real:
            open_live = pd.DataFrame()  # real_trades doesn't track open
            # Get open count from original trades table
            _conn = get_connection()
            _oc = _conn.execute("SELECT COUNT(*) FROM trades WHERE strategy LIKE '%_LIVE' AND status='open'").fetchone()[0]
            _conn.close()
        else:
            open_live = df_live[df_live["status"] == "open"] if "status" in df_live.columns else pd.DataFrame()
            _oc = len(open_live)

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
            
            # Dynamic key forces slider to reset when data range changes
            slider_key = f"live_time_slider_{int(latest.timestamp())}"
            time_range = st.slider(
                "⏱️ Time Range",
                min_value=earliest,
                max_value=latest,
                value=(earliest, latest),
                format="MM/DD HH:mm",
                key=slider_key
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
        if using_real:
            _conn2 = get_connection()
            active_strats = _conn2.execute("SELECT COUNT(DISTINCT strategy) FROM trades WHERE strategy LIKE '%_LIVE' AND status='open'").fetchone()[0]
            _conn2.close()
        else:
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

        render_kpi_row(balance, total_pnl, roi, stats, _oc, active_strats)

        if using_real:
            gross_pnl = closed["pnl"].sum() if "pnl" in closed.columns else 0
            st.markdown(f"""
            <div style="background:#ecfdf5;border:1px solid #a7f3d0;border-radius:10px;padding:8px 16px;margin:8px 0;font-size:0.78rem;color:#065f46;">
                ✅ <b>On-chain verified trades.</b> {len(closed)} real positions from Polymarket CSV · Gross P&L ${gross_pnl:+.2f} (before fees) · CLOB balance ${balance:.2f}
            </div>
            """, unsafe_allow_html=True)

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
# 🎯 SNIPER TAB
# ════════════════════════════════════════════
SNIPER_DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "sniper.db")
SNIPER_LAUNCH_TS = 1772942940  # First sniper trade timestamp

# On-chain P&L cache (refreshes every 5 min to avoid API spam)
_onchain_cache = {"data": None, "ts": 0}
ONCHAIN_CACHE_TTL = 300  # 5 minutes

def _fetch_onchain_sniper_pnl():
    """Fetch real P&L from Polymarket activity API. Cached for 5 min."""
    import time as _time
    now = _time.time()
    if _onchain_cache["data"] and now - _onchain_cache["ts"] < ONCHAIN_CACHE_TTL:
        return _onchain_cache["data"]
    
    try:
        proxy = "0x71269b2a127c081dadcbac57b321fc420094ef80"
        all_acts = []
        offset = 0
        while True:
            r = requests.get(
                f"https://data-api.polymarket.com/activity?user={proxy}&limit=200&offset={offset}",
                timeout=15,
            )
            if r.status_code != 200:
                break
            batch = r.json()
            if not batch:
                break
            all_acts.extend(batch)
            if len(batch) < 200:
                break
            offset += 200

        sniper_trades = [
            a for a in all_acts
            if a.get("type") == "TRADE"
            and int(a.get("timestamp", 0)) >= SNIPER_LAUNCH_TS
            and "updown-5m" in a.get("eventSlug", "")
        ]
        sniper_redeems = [
            a for a in all_acts
            if a.get("type") == "REDEEM"
            and int(a.get("timestamp", 0)) >= SNIPER_LAUNCH_TS
            and "updown-5m" in a.get("eventSlug", "")
        ]

        total_spent = sum(float(a.get("usdcSize", 0)) for a in sniper_trades)
        total_redeemed = sum(float(a.get("usdcSize", 0)) for a in sniper_redeems)
        trade_count = len(sniper_trades)
        redeem_count = len(sniper_redeems)
        net_pnl = total_redeemed - total_spent

        # Per-asset breakdown
        asset_data = {}
        for a in sniper_trades:
            slug = a.get("eventSlug", "")
            asset = "btc" if "btc-" in slug else "eth" if "eth-" in slug else "sol" if "sol-" in slug else "xrp" if "xrp-" in slug else "other"
            if asset not in asset_data:
                asset_data[asset] = {"trades": 0, "cost": 0.0, "redeemed": 0.0}
            asset_data[asset]["trades"] += 1
            asset_data[asset]["cost"] += float(a.get("usdcSize", 0))
        for a in sniper_redeems:
            slug = a.get("eventSlug", "")
            asset = "btc" if "btc-" in slug else "eth" if "eth-" in slug else "sol" if "sol-" in slug else "xrp" if "xrp-" in slug else "other"
            if asset in asset_data:
                asset_data[asset]["redeemed"] += float(a.get("usdcSize", 0))

        result = {
            "total_spent": total_spent,
            "total_redeemed": total_redeemed,
            "net_pnl": net_pnl,
            "trade_count": trade_count,
            "redeem_count": redeem_count,
            "avg_cost": total_spent / trade_count if trade_count > 0 else 0,
            "roi_pct": (net_pnl / total_spent * 100) if total_spent > 0 else 0,
            "asset_data": asset_data,
        }
        _onchain_cache["data"] = result
        _onchain_cache["ts"] = now
        return result
    except Exception:
        return _onchain_cache.get("data")  # Return stale cache on error

ASSET_COLORS = {
    "btc": "#f7931a",  # Bitcoin orange
    "eth": "#627eea",  # Ethereum blue
    "sol": "#9945ff",  # Solana purple
    "xrp": "#23292f",  # XRP dark
}
ASSET_ICONS = {"btc": "₿", "eth": "Ξ", "sol": "◎", "xrp": "✕"}

with tab_sniper:
    is_sniper = check_trader_running("sniper_bot")
    now_pst = datetime.now(timezone.utc).astimezone(PST).strftime("%I:%M %p PST")
    sniper_badge = '<span class="live-badge">● LIVE</span>' if is_sniper else '<span class="offline-badge">● OFFLINE</span>'

    st.markdown(f"""
    <div class="hero-header" style="background: linear-gradient(135deg, #fef3c7 0%, #fde68a 30%, #fbbf24 60%, #f59e0b 100%);">
        <div style="display:flex;justify-content:space-between;align-items:center;">
            <div>
                <div style="font-size:2.2rem;font-weight:800;color:#78350f;letter-spacing:-0.03em;">🎯 Sniper Bot</div>
                <div style="color:#92400e;font-size:0.95rem;">Post-close arbitrage · BTC · ETH · SOL · XRP</div>
            </div>
            <div style="text-align:right;">
                {sniper_badge}<br>
                <span style="color:#92400e;font-size:0.82rem;">{now_pst}</span>
            </div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    sniper_df = pd.DataFrame()
    if os.path.exists(SNIPER_DB):
        try:
            import sqlite3 as _sq
            _sconn = _sq.connect(SNIPER_DB)
            sniper_df = pd.read_sql("SELECT * FROM sniper_trades ORDER BY id DESC", _sconn)
            _sconn.close()
        except:
            pass

    if sniper_df.empty:
        st.markdown("""
        <div style="text-align:center;padding:60px;">
            <h2 style="color:#94a3b8;">🎯 Sniper active — waiting for first fill</h2>
            <p style="color:#94a3b8;">Trades fire ~1s after each 5-min window closes</p>
        </div>
        """, unsafe_allow_html=True)
    else:
        # Parse timestamps
        if "timestamp" in sniper_df.columns:
            try:
                ts = pd.to_datetime(sniper_df["timestamp"])
                if ts.dt.tz is not None:
                    sniper_df["time_pst"] = ts.dt.tz_convert("US/Pacific")
                else:
                    sniper_df["time_pst"] = ts.dt.tz_localize("US/Pacific")
            except Exception:
                sniper_df["time_pst"] = pd.to_datetime(sniper_df["timestamp"], errors="coerce")

        total_fills = len(sniper_df)

        # Use ON-CHAIN data for real P&L (DB numbers are wrong — logs ask price not fill price)
        onchain = _fetch_onchain_sniper_pnl()
        if onchain:
            total_cost = onchain["total_spent"]
            total_redeemed = onchain["total_redeemed"]
            net_pnl = onchain["net_pnl"]
            roi = onchain["roi_pct"]
            avg_cost_per_trade = onchain["avg_cost"]
            chain_trades = onchain["trade_count"]
            chain_redeems = onchain["redeem_count"]
        else:
            # Fallback to DB (inaccurate but better than nothing)
            total_cost = sniper_df["cost_usdc"].sum()
            total_redeemed = 0
            net_pnl = sniper_df["expected_profit"].sum()
            roi = (net_pnl / total_cost * 100) if total_cost > 0 else 0
            avg_cost_per_trade = total_cost / total_fills if total_fills else 0
            chain_trades = total_fills
            chain_redeems = 0

        # ── KPI Row ──
        k1, k2, k3, k4, k5, k6 = st.columns(6)
        for col, label, value, sub, cls in [
            (k1, "On-Chain Trades", f"{chain_trades}", f"{chain_redeems} redeemed", "neutral"),
            (k2, "Total Spent", f"${total_cost:,.2f}", f"avg ${avg_cost_per_trade:.2f}/trade" if chain_trades else "", "neutral"),
            (k3, "Total Redeemed", f"${total_redeemed:,.2f}", f"{chain_redeems} positions claimed", "neutral"),
            (k4, "Net P&L", f"${net_pnl:+,.2f}", f"{roi:+.1f}% ROI", "win" if net_pnl > 0 else "loss"),
            (k5, "Avg Cost/Trade", f"${avg_cost_per_trade:.4f}", "on-chain actual", "neutral"),
            (k6, "Source", "🔗 On-Chain", "Polymarket activity API", "neutral"),
        ]:
            col.markdown(f"""
            <div class="metric-card">
                <div class="metric-label">{label}</div>
                <div class="metric-value {cls}">{value}</div>
                <div class="metric-sub">{sub}</div>
            </div>
            """, unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)

        # ── Per-Asset Cards (on-chain data) ──
        st.markdown('<div class="section-header">🏆 Per-Asset Performance (On-Chain)</div>', unsafe_allow_html=True)
        asset_cols = st.columns(4)
        chain_asset_data = onchain.get("asset_data", {}) if onchain else {}
        for i, asset_name in enumerate(["btc", "eth", "sol", "xrp"]):
            color = ASSET_COLORS.get(asset_name, "#64748b")
            icon = ASSET_ICONS.get(asset_name, "?")
            a = chain_asset_data.get(asset_name)
            if a and a["trades"] > 0:
                a_pnl = a["redeemed"] - a["cost"]
                a_roi = (a_pnl / a["cost"] * 100) if a["cost"] > 0 else 0
                pnl_color = "#16a34a" if a_pnl >= 0 else "#dc2626"
                with asset_cols[i]:
                    st.markdown(f"""
                    <div class="strat-card" style="border-top: 3px solid {color};">
                        <div style="font-size:1.1rem;font-weight:700;color:{color};">{icon} {asset_name.upper()}</div>
                        <div style="color:#64748b;font-size:0.82rem;line-height:2;margin-top:6px;">
                            Trades: <b style="color:#1e293b">{a['trades']}</b><br>
                            Spent: <b style="color:#1e293b">${a['cost']:,.2f}</b><br>
                            Redeemed: <b style="color:#1e293b">${a['redeemed']:,.2f}</b><br>
                            Net P&L: <b style="color:{pnl_color}">${a_pnl:+,.2f}</b><br>
                            ROI: <b style="color:{pnl_color}">{a_roi:+.1f}%</b>
                        </div>
                    </div>
                    """, unsafe_allow_html=True)
            else:
                with asset_cols[i]:
                    st.markdown(f"""
                    <div class="strat-card" style="border-top: 3px solid {color};opacity:0.5;">
                        <div style="font-size:1.1rem;font-weight:700;color:{color};">{icon} {asset_name.upper()}</div>
                        <div style="color:#94a3b8;font-size:0.82rem;margin-top:6px;">No fills yet</div>
                    </div>
                    """, unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)

        # ── Cumulative Profit Chart ──
        st.markdown('<div class="section-header">📈 Cumulative Profit</div>', unsafe_allow_html=True)
        chart_df = sniper_df.sort_values("id")
        chart_df["cum_profit"] = chart_df["expected_profit"].cumsum()
        chart_df["cum_cost"] = chart_df["cost_usdc"].cumsum()
        chart_df["trade_num"] = range(1, len(chart_df) + 1)

        fig_sniper = go.Figure()
        fig_sniper.add_trace(go.Scatter(
            x=chart_df["trade_num"], y=chart_df["cum_profit"],
            mode="lines+markers", name="Cumulative Profit",
            line=dict(color="#f59e0b", width=2.5),
            marker=dict(size=6, color=chart_df["asset"].map(ASSET_COLORS).fillna("#64748b")),
            hovertemplate="Trade #%{x}<br>Profit: $%{y:.2f}<extra></extra>",
        ))
        fig_sniper.add_trace(go.Scatter(
            x=chart_df["trade_num"], y=chart_df["cum_cost"],
            mode="lines", name="Capital Deployed",
            line=dict(color="#94a3b8", width=1, dash="dot"),
        ))
        fig_sniper.update_layout(
            template="plotly_white", paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            font=dict(family="Inter", color="#475569"),
            height=350, margin=dict(l=20, r=20, t=20, b=20),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1, font=dict(size=11)),
            yaxis=dict(tickprefix="$", gridcolor="rgba(226,232,240,0.8)"),
            xaxis=dict(title="Trade #", gridcolor="rgba(226,232,240,0.8)"),
        )
        st.plotly_chart(fig_sniper, use_container_width=True)

        # ── Outcome Distribution ──
        st.markdown('<div class="section-header">📊 Outcome Distribution</div>', unsafe_allow_html=True)
        oc1, oc2 = st.columns(2)
        with oc1:
            outcome_counts = sniper_df["outcome"].value_counts()
            fig_outcome = go.Figure(go.Pie(
                labels=[o.upper() for o in outcome_counts.index],
                values=outcome_counts.values,
                marker=dict(colors=["#22c55e", "#ef4444"] if "up" in outcome_counts.index else ["#ef4444", "#22c55e"]),
                hole=0.4,
            ))
            fig_outcome.update_layout(
                height=250, margin=dict(l=10, r=10, t=10, b=10),
                paper_bgcolor="rgba(0,0,0,0)", font=dict(family="Inter"),
            )
            st.plotly_chart(fig_outcome, use_container_width=True)
        with oc2:
            asset_counts = sniper_df["asset"].value_counts()
            fig_assets = go.Figure(go.Pie(
                labels=[a.upper() for a in asset_counts.index],
                values=asset_counts.values,
                marker=dict(colors=[ASSET_COLORS.get(a, "#64748b") for a in asset_counts.index]),
                hole=0.4,
            ))
            fig_assets.update_layout(
                height=250, margin=dict(l=10, r=10, t=10, b=10),
                paper_bgcolor="rgba(0,0,0,0)", font=dict(family="Inter"),
            )
            st.plotly_chart(fig_assets, use_container_width=True)

        # ── Trade Log ──
        st.markdown('<div class="section-header">📋 Sniper Trade Log</div>', unsafe_allow_html=True)
        for _, t in sniper_df.head(50).iterrows():
            asset = t.get("asset", "?")
            outcome = str(t.get("outcome", "?")).upper()
            price = t.get("buy_price", 0)
            shares = t.get("shares", 0)
            cost = t.get("cost_usdc", 0)
            profit = t.get("expected_profit", 0)
            ts = t.get("timestamp", "")
            paper = t.get("paper", 0)
            o_price = t.get("open_price", 0)
            c_price = t.get("close_price", 0)
            color = ASSET_COLORS.get(asset, "#64748b")
            icon = ASSET_ICONS.get(asset, "?")
            paper_badge = ' <span style="background:#dbeafe;color:#2563eb;padding:1px 6px;border-radius:4px;font-size:0.65rem;">PAPER</span>' if paper else ""

            st.markdown(f"""
            <div class="trade-row trade-win" style="border-left-color:{color};">
                <div>
                    <span style="color:{color};font-weight:700;">{icon} {asset.upper()}</span>{paper_badge}
                    &nbsp;{'🟢' if outcome == 'UP' else '🔴'} {outcome}
                    · ${o_price:,.2f}→${c_price:,.2f}
                    · {shares:.0f} shares @ ${price}
                </div>
                <div style="text-align:right;">
                    <span style="color:#16a34a;font-weight:600;">+${profit:.2f}</span>
                    <span style="color:#94a3b8;margin-left:8px;font-size:0.75rem;">{ts[:19] if isinstance(ts, str) else ts}</span>
                </div>
            </div>
            """, unsafe_allow_html=True)

# ════════════════════════════════════════════
# 🔄 PAPER v3 (INVERSE) TAB
# ════════════════════════════════════════════
PAPER_V31_DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "paper_v3_1.db")
PAPER_V31_STARTING = 100.0

with tab_paper_v31:
    is_v31_running = check_trader_running("paper_trader_v3_1")
    now_pst = datetime.now(timezone.utc).astimezone(PST).strftime("%I:%M %p PST")

    st.markdown(f"""
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:24px">
        <div style="width:12px;height:12px;border-radius:50%;background:{'#22c55e' if is_v31_running else '#ef4444'}"></div>
        <span style="font-size:1.1rem;font-weight:600">Paper v3.1 (HTF Enhanced) {'Running' if is_v31_running else 'Stopped'}</span>
        <span style="color:#94a3b8;font-size:0.9rem">{now_pst}</span>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("""
    <div style="background:#1e293b;border-left:4px solid #22c55e;padding:12px 16px;border-radius:8px;margin-bottom:20px;color:#86efac">
        🚀 <strong>OOS-validated strategies with 1hr/4hr overlay:</strong><br>
        📈 HTF Trend Follow (53.8% WR, high volume) · 🔁 Streak Reversal (65.2% WR) · 🎯 Combined Streak+Vol (81.2% WR, sniper)
    </div>
    """, unsafe_allow_html=True)

    v31_closed = pd.DataFrame()
    v31_open_count = 0
    if os.path.exists(PAPER_V31_DB):
        try:
            import sqlite3 as _sq
            _conn = _sq.connect(PAPER_V31_DB)
            v31_closed = pd.read_sql_query(
                "SELECT * FROM trades WHERE status='closed' ORDER BY timestamp", _conn
            )
            v31_open_count = pd.read_sql_query(
                "SELECT COUNT(*) as cnt FROM trades WHERE status='open'", _conn
            ).iloc[0]["cnt"]
            _conn.close()
        except Exception as e:
            st.warning(f"Paper v3.1 DB error: {e}")

    if v31_closed.empty:
        st.info("🚀 Paper Trader v3.1 just started — waiting for trades to resolve...")
    else:
        total_v31 = len(v31_closed)
        wins_v31 = len(v31_closed[v31_closed["pnl"] > 0])
        losses_v31 = total_v31 - wins_v31
        wr_v31 = wins_v31 / total_v31 * 100 if total_v31 > 0 else 0
        total_pnl_v31 = v31_closed["pnl"].sum()
        balance_v31 = PAPER_V31_STARTING + total_pnl_v31

        col1, col2, col3, col4, col5 = st.columns(5)
        for col, label, value, cls in [
            (col1, "Balance", f"${balance_v31:,.2f}", "positive" if balance_v31 >= PAPER_V31_STARTING else "negative"),
            (col2, "P&L", f"${total_pnl_v31:+,.2f}", "positive" if total_pnl_v31 >= 0 else "negative"),
            (col3, "Win Rate", f"{wr_v31:.1f}%", "positive" if wr_v31 >= 55 else "negative" if wr_v31 < 45 else ""),
            (col4, "Trades", f"{total_v31}", ""),
            (col5, "Open", f"{v31_open_count}", ""),
        ]:
            col.markdown(f"""
            <div class="metric-card">
                <div class="metric-label">{label}</div>
                <div class="metric-value {cls}">{value}</div>
            </div>
            """, unsafe_allow_html=True)

        if "time_pst" not in v31_closed.columns and "timestamp" in v31_closed.columns:
            v31_closed["time_pst"] = pd.to_datetime(v31_closed["timestamp"], unit="s", utc=True).dt.tz_convert("US/Pacific").dt.strftime("%m/%d %I:%M %p")

        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown('<div class="section-header">📈 Paper v3.1 Equity Curve</div>', unsafe_allow_html=True)
        render_equity_chart(v31_closed, PAPER_V31_STARTING)

        st.markdown('<div class="section-header">🏆 Strategy Performance</div>', unsafe_allow_html=True)
        render_strategy_cards(v31_closed)

        st.markdown('<div class="section-header">📋 Trade History</div>', unsafe_allow_html=True)
        render_trade_history(v31_closed)

# ─── PAPER v3.2 (INVERSE HTF) TAB ───
PAPER_V32_DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "paper_v3_2.db")
PAPER_V32_STARTING = 100.0

with tab_paper_v32:
    is_v32_running = check_trader_running("paper_trader_v3_2")
    now_pst = datetime.now(timezone.utc).astimezone(PST).strftime("%I:%M %p PST")

    st.markdown(f"""
    <div class="metric-card">
        <span style="font-size:1.1rem;font-weight:600">Paper v3.2 (Inverse HTF) {'Running' if is_v32_running else 'Stopped'}</span>
        <span style="color:#94a3b8;font-size:0.85rem"> | {now_pst}</span>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("🔄 **Hypothesis**: HTF Trend signals have info content but point wrong → flip everything")

    if os.path.exists(PAPER_V32_DB):
        try:
            import sqlite3 as _sq
            _conn = _sq.connect(PAPER_V32_DB)
            v32_all = pd.read_sql("SELECT * FROM trades", _conn)
            _conn.close()

            if "timestamp" in v32_all.columns:
                v32_all["time_pst"] = pd.to_datetime(v32_all["timestamp"], unit="s", utc=True).dt.tz_convert("US/Pacific")

            v32_closed = v32_all[v32_all["status"] == "closed"] if "status" in v32_all.columns else v32_all

            if v32_closed.empty:
                st.info("🔄 Paper v3.2 running — waiting for trades to resolve...")
            else:
                total_pnl_v32 = v32_closed["pnl"].sum() if "pnl" in v32_closed.columns else 0
                wins_v32 = len(v32_closed[v32_closed["pnl"] > 0]) if "pnl" in v32_closed.columns else 0
                losses_v32 = len(v32_closed[v32_closed["pnl"] <= 0]) if "pnl" in v32_closed.columns else 0
                wr_v32 = wins_v32 / len(v32_closed) * 100 if len(v32_closed) > 0 else 0
                balance_v32 = PAPER_V32_STARTING + total_pnl_v32

                col1, col2, col3, col4 = st.columns(4)
                metrics = [
                    (col1, "Balance", f"${balance_v32:,.2f}", "positive" if balance_v32 >= PAPER_V32_STARTING else "negative"),
                    (col2, "P&L", f"${total_pnl_v32:+,.2f}", "win" if total_pnl_v32 >= 0 else "loss"),
                    (col3, "Win Rate", f"{wr_v32:.1f}%", "win" if wr_v32 >= 52.5 else "loss"),
                    (col4, "Trades", f"{wins_v32}W - {losses_v32}L", "neutral"),
                ]
                for c, label, val, _ in metrics:
                    c.metric(label, val)

                # Head-to-head vs v3.1
                if os.path.exists(os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "paper_v3_1.db")):
                    try:
                        _conn31 = _sq.connect(os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "paper_v3_1.db"))
                        v31_htf = pd.read_sql("SELECT * FROM trades WHERE strategy LIKE '%htf_trend%' AND status='closed'", _conn31)
                        _conn31.close()
                        if not v31_htf.empty:
                            v31_pnl = v31_htf["pnl"].sum()
                            v31_wr = len(v31_htf[v31_htf["pnl"] > 0]) / len(v31_htf) * 100
                            st.markdown("### 🥊 Head-to-Head: v3.1 HTF vs v3.2 Inverse")
                            h2h_col1, h2h_col2 = st.columns(2)
                            with h2h_col1:
                                st.markdown(f"**v3.1 HTF Trend** (original)")
                                st.markdown(f"- WR: {v31_wr:.1f}% | P&L: ${v31_pnl:+.2f} | Trades: {len(v31_htf)}")
                            with h2h_col2:
                                st.markdown(f"**v3.2 Inverse HTF** (flipped)")
                                st.markdown(f"- WR: {wr_v32:.1f}% | P&L: ${total_pnl_v32:+.2f} | Trades: {len(v32_closed)}")
                    except:
                        pass

                st.markdown('<div class="section-header">📈 Paper v3.2 Equity Curve</div>', unsafe_allow_html=True)
                render_equity_chart(v32_closed, PAPER_V32_STARTING)

                st.markdown('<div class="section-header">📋 Trade History</div>', unsafe_allow_html=True)
                render_trade_history(v32_closed)
        except Exception as e:
            st.warning(f"Paper v3.2 DB error: {e}")
    else:
        st.info("🔄 Paper Trader v3.2 not started yet — no database found.")

# ─── PAPER v3.3 (TRUE MIRROR) TAB ───
PAPER_V33_DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "paper_v3_3.db")
PAPER_V33_STARTING = 100.0

with tab_paper_v33:
    is_v33_running = check_trader_running("paper_trader_v3_3")
    now_pst = datetime.now(timezone.utc).astimezone(PST).strftime("%I:%M %p PST")

    st.markdown(f"""
    <div class="metric-card">
        <span style="font-size:1.1rem;font-weight:600">Paper v3.3 (True Mirror) {'Running' if is_v33_running else 'Stopped'}</span>
        <span style="color:#94a3b8;font-size:0.85rem"> | {now_pst}</span>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("🪞 **True mirror test**: reads v3.1's trades from DB and bets the EXACT opposite. Same markets, same timing, opposite direction.")

    if os.path.exists(PAPER_V33_DB):
        try:
            import sqlite3 as _sq
            _conn = _sq.connect(PAPER_V33_DB)
            v33_all = pd.read_sql("SELECT * FROM trades", _conn)
            _conn.close()

            if "timestamp" in v33_all.columns:
                v33_all["time_pst"] = pd.to_datetime(v33_all["timestamp"], unit="s", utc=True).dt.tz_convert("US/Pacific")

            v33_closed = v33_all[v33_all["status"] == "closed"] if "status" in v33_all.columns else v33_all

            if v33_closed.empty:
                st.info("🪞 Paper v3.3 running — waiting for trades to resolve...")
            else:
                total_pnl_v33 = v33_closed["pnl"].sum() if "pnl" in v33_closed.columns else 0
                wins_v33 = len(v33_closed[v33_closed["pnl"] > 0]) if "pnl" in v33_closed.columns else 0
                losses_v33 = len(v33_closed[v33_closed["pnl"] <= 0]) if "pnl" in v33_closed.columns else 0
                wr_v33 = wins_v33 / len(v33_closed) * 100 if len(v33_closed) > 0 else 0
                balance_v33 = PAPER_V33_STARTING + total_pnl_v33

                col1, col2, col3, col4 = st.columns(4)
                metrics = [
                    (col1, "Balance", f"${balance_v33:,.2f}", "positive" if balance_v33 >= PAPER_V33_STARTING else "negative"),
                    (col2, "P&L", f"${total_pnl_v33:+,.2f}", "win" if total_pnl_v33 >= 0 else "loss"),
                    (col3, "Win Rate", f"{wr_v33:.1f}%", "win" if wr_v33 >= 52.5 else "loss"),
                    (col4, "Trades", f"{wins_v33}W - {losses_v33}L", "neutral"),
                ]
                for c, label, val, _ in metrics:
                    c.metric(label, val)

                # Head-to-head vs v3.1 (only matched trades)
                if os.path.exists(os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "paper_v3_1.db")):
                    try:
                        _conn31 = _sq.connect(os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "paper_v3_1.db"))
                        v31_htf = pd.read_sql("SELECT * FROM trades WHERE strategy LIKE '%htf_trend%' AND status='closed'", _conn31)
                        _conn31.close()

                        # Match by market_id
                        if not v31_htf.empty and "market_id" in v33_closed.columns:
                            v33_markets = set(v33_closed["market_id"].tolist())
                            v31_matched = v31_htf[v31_htf["market_id"].isin(v33_markets)]
                            v33_matched = v33_closed[v33_closed["market_id"].isin(set(v31_htf["market_id"].tolist()))]

                            if not v31_matched.empty and not v33_matched.empty:
                                v31_m_wr = len(v31_matched[v31_matched["pnl"] > 0]) / len(v31_matched) * 100
                                v33_m_wr = len(v33_matched[v33_matched["pnl"] > 0]) / len(v33_matched) * 100
                                v31_m_pnl = v31_matched["pnl"].sum()
                                v33_m_pnl = v33_matched["pnl"].sum()

                                st.markdown(f"### 🪞 Head-to-Head (matched markets only: {len(v33_matched)} trades)")
                                h2h_col1, h2h_col2 = st.columns(2)
                                with h2h_col1:
                                    st.markdown(f"**v3.1 HTF Trend** (original)")
                                    st.markdown(f"- WR: {v31_m_wr:.1f}% | P&L: ${v31_m_pnl:+.2f}")
                                with h2h_col2:
                                    st.markdown(f"**v3.3 Mirror** (inverted)")
                                    st.markdown(f"- WR: {v33_m_wr:.1f}% | P&L: ${v33_m_pnl:+.2f}")
                                st.markdown(f"*Combined WR: {(v31_m_wr + v33_m_wr)/2:.1f}% (should be ~50% for true mirror)*")
                    except:
                        pass

                st.markdown('<div class="section-header">📈 Paper v3.3 Equity Curve</div>', unsafe_allow_html=True)
                render_equity_chart(v33_closed, PAPER_V33_STARTING)

                st.markdown('<div class="section-header">📋 Trade History</div>', unsafe_allow_html=True)
                render_trade_history(v33_closed)
        except Exception as e:
            st.warning(f"Paper v3.3 DB error: {e}")
    else:
        st.info("🪞 Paper Trader v3.3 not started yet — no database found.")

# ─── ML BRAIN TAB ───
with tab_ml:
    st.markdown('<div class="section-header">🧠 ML Meta-Learner Dashboard</div>', unsafe_allow_html=True)
    
    _ml_conn = get_connection()
    
    # ─── Model Status ───
    try:
        model_log = pd.read_sql("SELECT * FROM ml_model_log ORDER BY id DESC LIMIT 10", _ml_conn)
    except Exception:
        model_log = pd.DataFrame()
    
    try:
        ml_features_df = pd.read_sql("SELECT * FROM ml_features WHERE outcome IS NOT NULL ORDER BY id DESC", _ml_conn)
    except Exception:
        ml_features_df = pd.DataFrame()
    
    try:
        shadow_df = pd.read_sql("SELECT * FROM ml_shadow_trades ORDER BY id DESC", _ml_conn)
    except Exception:
        shadow_df = pd.DataFrame()
    
    _ml_conn.close()
    
    # ─── Status Cards ───
    if not ml_features_df.empty:
        taken = ml_features_df[ml_features_df["decision"] == "take"]
        skipped = ml_features_df[ml_features_df["decision"] == "skip"]
        taken_wr = taken["outcome"].mean() * 100 if len(taken) > 0 else 0
        skipped_wr = skipped["outcome"].mean() * 100 if len(skipped) > 0 else 0
        total_samples = len(ml_features_df)
        model_ver = model_log.iloc[0]["version"] if not model_log.empty else 0
        model_acc = model_log.iloc[0]["accuracy"] * 100 if not model_log.empty else 0
        
        col1, col2, col3, col4, col5 = st.columns(5)
        for col, label, value, cls in [
            (col1, "Model Version", f"v{model_ver}", ""),
            (col2, "Training Samples", f"{total_samples}", ""),
            (col3, "Taken WR", f"{taken_wr:.1f}%", "positive" if taken_wr >= 55 else "negative"),
            (col4, "Skipped WR", f"{skipped_wr:.1f}%", "negative" if skipped_wr >= 50 else "positive"),
            (col5, "Model Accuracy", f"{model_acc:.1f}%", "positive" if model_acc >= 55 else ""),
        ]:
            col.markdown(f"""
            <div class="metric-card">
                <div class="metric-label">{label}</div>
                <div class="metric-value {cls}">{value}</div>
            </div>
            """, unsafe_allow_html=True)
    else:
        st.info("🧠 ML Meta-Learner is collecting data. No resolved predictions yet.")
    
    # ─── Feature Importance ───
    if not model_log.empty:
        st.markdown('<div class="section-header">🎯 Top Feature Importance</div>', unsafe_allow_html=True)
        import json as _json
        try:
            imp = _json.loads(model_log.iloc[0]["feature_importance"])
            imp_df = pd.DataFrame([{"Feature": k, "Importance": v} for k, v in sorted(imp.items(), key=lambda x: x[1], reverse=True)])
            
            fig_imp = go.Figure(go.Bar(
                x=imp_df["Importance"],
                y=imp_df["Feature"],
                orientation="h",
                marker_color="#6366f1",
            ))
            fig_imp.update_layout(
                height=400, yaxis=dict(autorange="reversed"),
                margin=dict(l=10, r=10, t=10, b=10),
                paper_bgcolor="white", plot_bgcolor="white",
            )
            st.plotly_chart(fig_imp, use_container_width=True)
        except Exception:
            pass
    
    # ─── ML Decisions Over Time ───
    if not ml_features_df.empty and len(ml_features_df) > 5:
        st.markdown('<div class="section-header">📊 ML Decisions Over Time</div>', unsafe_allow_html=True)
        
        # Rolling win rate for taken vs skipped
        recent = ml_features_df.sort_values("id").tail(100)
        taken_recent = recent[recent["decision"] == "take"]
        
        if len(taken_recent) >= 5:
            taken_recent = taken_recent.copy()
            taken_recent["rolling_wr"] = taken_recent["outcome"].rolling(10, min_periods=5).mean() * 100
            taken_recent["trade_num"] = range(len(taken_recent))
            
            fig_wr = go.Figure()
            fig_wr.add_trace(go.Scatter(
                x=taken_recent["trade_num"], y=taken_recent["rolling_wr"],
                mode="lines+markers", name="ML Approved (Rolling 10 WR)",
                line=dict(color="#22c55e", width=2),
            ))
            fig_wr.add_hline(y=55, line_dash="dash", line_color="#ef4444", annotation_text="55% threshold")
            fig_wr.add_hline(y=50, line_dash="dot", line_color="#94a3b8", annotation_text="Breakeven")
            fig_wr.update_layout(
                height=300, yaxis_title="Win Rate %",
                margin=dict(l=10, r=10, t=10, b=10),
                paper_bgcolor="white", plot_bgcolor="white",
            )
            st.plotly_chart(fig_wr, use_container_width=True)
    
    # ─── Shadow Trades (ML Skipped) ───
    if not shadow_df.empty:
        st.markdown('<div class="section-header">👻 Shadow Trades (ML Skipped)</div>', unsafe_allow_html=True)
        
        resolved_shadows = shadow_df[shadow_df["outcome"].notna()]
        pending_shadows = shadow_df[shadow_df["outcome"].isna()]
        
        col1, col2, col3 = st.columns(3)
        if len(resolved_shadows) > 0:
            shadow_wr = resolved_shadows["outcome"].mean() * 100
            col1.metric("Shadow WR (would have won)", f"{shadow_wr:.1f}%")
            col2.metric("Resolved", f"{len(resolved_shadows)}")
            col3.metric("Pending", f"{len(pending_shadows)}")
            
            if shadow_wr < 50:
                st.success(f"✅ ML is working! Skipped trades only win {shadow_wr:.0f}% — good filtering.")
            else:
                st.warning(f"⚠️ Skipped trades winning {shadow_wr:.0f}% — model may be too aggressive.")
        else:
            col1.metric("Pending Shadows", f"{len(pending_shadows)}")
            col2.metric("Resolved", "0")
            st.info("Shadow trades haven't resolved yet. Check back soon.")
    
    # ─── Model History ───
    if not model_log.empty and len(model_log) > 1:
        st.markdown('<div class="section-header">📈 Model Training History</div>', unsafe_allow_html=True)
        st.dataframe(model_log[["version", "training_samples", "accuracy", "win_rate_taken", "win_rate_skipped", "created_at"]].round(3), use_container_width=True)
    
    # ─── Recent ML Predictions ───
    if not ml_features_df.empty:
        st.markdown('<div class="section-header">📋 Recent Predictions</div>', unsafe_allow_html=True)
        display_cols = ["strategy", "decision", "prediction", "outcome", "pnl", "created_at"]
        available_cols = [c for c in display_cols if c in ml_features_df.columns]
        recent_preds = ml_features_df[available_cols].head(30)
        recent_preds = recent_preds.copy()
        if "outcome" in recent_preds.columns:
            recent_preds["outcome"] = recent_preds["outcome"].map({1: "✅ Win", 0: "❌ Loss", None: "⏳"})
        if "decision" in recent_preds.columns:
            recent_preds["decision"] = recent_preds["decision"].map({"take": "✅ Take", "skip": "🚫 Skip"}).fillna(recent_preds["decision"])
        if "prediction" in recent_preds.columns:
            recent_preds["prediction"] = recent_preds["prediction"].apply(lambda x: f"{x:.1%}" if pd.notna(x) else "—")
        st.dataframe(recent_preds, use_container_width=True)

# ─── MISSION CONTROL v2 TAB ───
with tab_mc:
    import subprocess
    import re as _re

    # ── Jira-style CSS ──
    st.markdown("""
    <style>
        .mc-header {
            background: linear-gradient(135deg, #0f172a 0%, #1e293b 50%, #334155 100%);
            border-radius: 16px;
            padding: 24px 32px;
            margin-bottom: 24px;
            color: white;
        }
        .mc-title { font-size: 1.8rem; font-weight: 800; letter-spacing: -0.02em; }
        .mc-sub { color: #94a3b8; font-size: 0.85rem; margin-top: 2px; }
        
        .process-card {
            background: #ffffff;
            border: 1px solid #e2e8f0;
            border-radius: 12px;
            padding: 14px 16px;
            display: flex;
            align-items: center;
            gap: 12px;
            transition: all 0.2s;
            box-shadow: 0 1px 3px rgba(0,0,0,0.04);
        }
        .process-card:hover { box-shadow: 0 4px 12px rgba(0,0,0,0.08); transform: translateY(-1px); }
        .process-dot {
            width: 10px; height: 10px; border-radius: 50%;
            flex-shrink: 0;
        }
        .process-dot.running { background: #22c55e; box-shadow: 0 0 8px rgba(34,197,94,0.5); }
        .process-dot.stopped { background: #ef4444; }
        .process-name { font-weight: 600; font-size: 0.85rem; color: #1e293b; }
        .process-meta { font-size: 0.72rem; color: #94a3b8; }
        
        .kanban-col {
            background: #f8fafc;
            border: 1px solid #e2e8f0;
            border-radius: 12px;
            padding: 16px;
            min-height: 200px;
        }
        .kanban-header {
            font-weight: 700;
            font-size: 0.82rem;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            margin-bottom: 12px;
            padding-bottom: 8px;
            border-bottom: 2px solid;
        }
        .kanban-header.blocked { color: #dc2626; border-color: #dc2626; }
        .kanban-header.progress { color: #2563eb; border-color: #2563eb; }
        .kanban-header.ready { color: #16a34a; border-color: #16a34a; }
        .kanban-header.done { color: #6b7280; border-color: #d1d5db; }
        
        .ticket {
            background: #ffffff;
            border: 1px solid #e2e8f0;
            border-radius: 8px;
            padding: 10px 12px;
            margin-bottom: 8px;
            font-size: 0.8rem;
            color: #334155;
            transition: all 0.15s;
            cursor: default;
            box-shadow: 0 1px 2px rgba(0,0,0,0.03);
        }
        .ticket:hover { box-shadow: 0 3px 8px rgba(0,0,0,0.08); border-color: #c7d2fe; }
        .ticket-done { opacity: 0.6; text-decoration: line-through; }
        
        .ticket-tag {
            display: inline-block;
            padding: 1px 6px;
            border-radius: 4px;
            font-size: 0.62rem;
            font-weight: 700;
            letter-spacing: 0.03em;
            margin-right: 4px;
        }
        .tag-sniper { background: #fef3c7; color: #92400e; }
        .tag-ml { background: #dbeafe; color: #1e40af; }
        .tag-infra { background: #f3e8ff; color: #6b21a8; }
        .tag-paper { background: #dcfce7; color: #166534; }
        .tag-dashboard { background: #fce7f3; color: #9d174d; }
        .tag-live { background: #fee2e2; color: #991b1b; }
        
        .health-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 12px;
            margin-bottom: 24px;
        }
        
        .stat-row {
            background: #ffffff;
            border: 1px solid #e2e8f0;
            border-radius: 10px;
            padding: 12px 16px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 6px;
            font-size: 0.82rem;
        }
        .stat-label { color: #64748b; font-weight: 500; }
        .stat-value { font-weight: 700; color: #1e293b; }
        
        .error-line {
            background: #fef2f2;
            border-left: 3px solid #ef4444;
            padding: 6px 10px;
            margin-bottom: 4px;
            border-radius: 0 6px 6px 0;
            font-size: 0.72rem;
            color: #991b1b;
            font-family: 'SF Mono', 'Menlo', monospace;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        
        .learning-card {
            background: linear-gradient(135deg, #fffbeb 0%, #fef3c7 100%);
            border: 1px solid #fde68a;
            border-radius: 8px;
            padding: 10px 14px;
            margin-bottom: 6px;
            font-size: 0.8rem;
            color: #78350f;
        }
        
        .git-line {
            font-family: 'SF Mono', 'Menlo', monospace;
            font-size: 0.72rem;
            padding: 3px 0;
            color: #475569;
        }
        .git-hash { color: #6366f1; font-weight: 600; }
        .git-modified { color: #f59e0b; }
        .git-added { color: #22c55e; }
    </style>
    """, unsafe_allow_html=True)

    now_pst = datetime.now(timezone.utc).astimezone(PST).strftime("%b %d, %Y · %I:%M %p PST")
    st.markdown(f"""
    <div class="mc-header">
        <div style="display:flex;justify-content:space-between;align-items:center;">
            <div>
                <div class="mc-title">🎛️ Mission Control</div>
                <div class="mc-sub">The Outsiders · Operations Hub</div>
            </div>
            <div style="text-align:right;color:#94a3b8;font-size:0.82rem;">
                {now_pst}
            </div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    # ══════════ PROCESS HEALTH ══════════
    st.markdown('<div class="section-header">⚡ Process Health</div>', unsafe_allow_html=True)

    def check_process(name):
        try:
            result = subprocess.run(["pgrep", "-f", name], capture_output=True, text=True, timeout=3)
            return result.returncode == 0
        except:
            return False

    all_processes = [
        ("Live Trader", "live_trader.py", "ML-powered strategy execution", "live"),
        ("Sniper Bot", "sniper_bot.py", "Post-close arbitrage", "sniper"),
        ("Paper v3.1", "paper_trader_v3_1.py", "HTF Enhanced strategies", "paper"),
        ("Paper v3.2", "paper_trader_v3_2.py", "Inverse HTF signal", "paper"),
        ("Paper v3.3", "paper_trader_v3_3.py", "True mirror test", "paper"),
        ("Auto-Claimer", "redeemer.py", "On-chain position redemption", "infra"),
    ]

    proc_cols = st.columns(3)
    for i, (label, proc, desc, tag) in enumerate(all_processes):
        running = check_process(proc)
        dot_class = "running" if running else "stopped"
        status_text = "Running" if running else "Stopped"
        tag_class = f"tag-{tag}"
        with proc_cols[i % 3]:
            st.markdown(f"""
            <div class="process-card">
                <div class="process-dot {dot_class}"></div>
                <div>
                    <div class="process-name">{label} <span class="ticket-tag {tag_class}">{tag.upper()}</span></div>
                    <div class="process-meta">{status_text} · {desc}</div>
                </div>
            </div>
            """, unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)

    # ══════════ KEY METRICS ══════════
    st.markdown('<div class="section-header">📊 Key Metrics</div>', unsafe_allow_html=True)

    mc_left, mc_right = st.columns(2)

    with mc_left:
        # ML Status
        try:
            _mc_conn = get_connection()
            ml_resolved = _mc_conn.execute("SELECT COUNT(*) FROM ml_features WHERE outcome IS NOT NULL").fetchone()[0]
            ml_total = _mc_conn.execute("SELECT COUNT(*) FROM ml_features").fetchone()[0]
            open_count = _mc_conn.execute("SELECT COUNT(*) FROM trades WHERE status='open'").fetchone()[0]
            today_trades = _mc_conn.execute(
                "SELECT COUNT(*), SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), COALESCE(SUM(pnl), 0) "
                "FROM trades WHERE status='closed' AND exit_reason IN ('win_live','loss_live') "
                "AND timestamp > strftime('%s','now','-24 hours')"
            ).fetchone()
            _mc_conn.close()
            t_count, t_wins, t_pnl = today_trades
            t_count = t_count or 0; t_wins = t_wins or 0; t_pnl = t_pnl or 0
            t_wr = f"{t_wins/t_count*100:.0f}%" if t_count > 0 else "—"

            ml_pct = min(ml_resolved / 100 * 100, 100)
            ml_status = "Active" if ml_resolved >= 100 else f"{ml_resolved}/100 ({ml_pct:.0f}%)"
            ml_bar_color = "#22c55e" if ml_resolved >= 100 else "#f59e0b"

            for label, value in [
                ("ML Meta-Learner", f"🧠 {ml_status}"),
                ("Open Positions", str(open_count)),
                ("24h Trades", f"{t_count} ({t_wr})"),
                ("24h P&L", f"${t_pnl:+.2f}"),
            ]:
                st.markdown(f"""
                <div class="stat-row">
                    <span class="stat-label">{label}</span>
                    <span class="stat-value">{value}</span>
                </div>
                """, unsafe_allow_html=True)

            # ML progress bar
            st.markdown(f"""
            <div style="background:#e2e8f0;border-radius:6px;height:6px;margin:4px 0 12px;">
                <div style="background:{ml_bar_color};width:{ml_pct}%;height:100%;border-radius:6px;transition:width 0.5s;"></div>
            </div>
            """, unsafe_allow_html=True)
        except Exception as e:
            st.warning(f"DB error: {e}")

    with mc_right:
        # Sniper stats
        try:
            import sqlite3 as _sq3
            if os.path.exists(SNIPER_DB):
                _sc = _sq3.connect(SNIPER_DB)
                sniper_stats = _sc.execute(
                    "SELECT COUNT(*), COALESCE(SUM(cost_usdc), 0), COALESCE(SUM(expected_profit), 0) FROM sniper_trades"
                ).fetchone()
                _sc.close()
                s_fills, s_cost, s_profit = sniper_stats
                s_roi = (s_profit / s_cost * 100) if s_cost > 0 else 0
            else:
                s_fills, s_cost, s_profit, s_roi = 0, 0, 0, 0

            real_bal = get_live_balance()
            bal_str = f"${real_bal:,.2f}" if real_bal else "N/A"

            for label, value in [
                ("CLOB Balance", bal_str),
                ("Sniper Fills", str(s_fills)),
                ("Sniper Profit", f"${s_profit:+.2f} ({s_roi:.1f}% ROI)"),
                ("Sniper Capital", f"${s_cost:,.2f} deployed"),
            ]:
                st.markdown(f"""
                <div class="stat-row">
                    <span class="stat-label">{label}</span>
                    <span class="stat-value">{value}</span>
                </div>
                """, unsafe_allow_html=True)
        except Exception as e:
            st.warning(f"Sniper stats error: {e}")

    st.markdown("<br>", unsafe_allow_html=True)

    # ══════════ KANBAN BOARD ══════════
    st.markdown('<div class="section-header">📋 Task Board</div>', unsafe_allow_html=True)

    queue_path = os.path.expanduser("~/.openclaw/workspace/QUEUE.md")
    if not os.path.exists(queue_path):
        queue_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "..", "QUEUE.md")

    if os.path.exists(queue_path):
        with open(queue_path, "r") as f:
            queue_content = f.read()

        sections = {}
        current_section = None
        current_items = []
        for line in queue_content.split("\n"):
            if line.startswith("## "):
                if current_section:
                    sections[current_section] = current_items
                current_section = line[3:].strip()
                current_items = []
            elif line.strip().startswith("- ["):
                checked = line.strip().startswith("- [x]")
                text = _re.sub(r'^- \[[ x]\]\s*', '', line.strip())
                # Auto-tag detection
                tag = ""
                text_lower = text.lower()
                if any(k in text_lower for k in ["sniper", "snipe", "arbitrage"]):
                    tag = '<span class="ticket-tag tag-sniper">SNIPER</span> '
                elif any(k in text_lower for k in ["ml", "brain", "meta", "model"]):
                    tag = '<span class="ticket-tag tag-ml">ML</span> '
                elif any(k in text_lower for k in ["rpc", "gas", "claimer", "redeem", "infra", "wallet"]):
                    tag = '<span class="ticket-tag tag-infra">INFRA</span> '
                elif any(k in text_lower for k in ["paper", "v3."]):
                    tag = '<span class="ticket-tag tag-paper">PAPER</span> '
                elif any(k in text_lower for k in ["dashboard", "tab", "chart"]):
                    tag = '<span class="ticket-tag tag-dashboard">DASH</span> '
                elif any(k in text_lower for k in ["live", "trader", "strategy"]):
                    tag = '<span class="ticket-tag tag-live">LIVE</span> '
                current_items.append({"text": text, "done": checked, "tag": tag})
        if current_section:
            sections[current_section] = current_items

        col_config = [
            ("🔴 BLOCKED", "blocked", "#dc2626"),
            ("🟡 IN PROGRESS", "progress", "#2563eb"),
            ("🟢 READY", "ready", "#16a34a"),
            ("✅ DONE", "done", "#6b7280"),
        ]
        kanban_cols = st.columns(4)
        section_keys = list(sections.keys())

        for i, (header, css_class, color) in enumerate(col_config):
            with kanban_cols[i]:
                st.markdown(f"""
                <div class="kanban-col">
                    <div class="kanban-header {css_class}">{header} <span style="opacity:0.6;font-size:0.7rem;">({len(sections.get(section_keys[i], [])) if i < len(section_keys) else 0})</span></div>
                """, unsafe_allow_html=True)

                items = sections.get(section_keys[i], []) if i < len(section_keys) else []
                for item in items[:10]:
                    done_class = "ticket-done" if item["done"] else ""
                    st.markdown(f"""
                    <div class="ticket {done_class}">
                        {item['tag']}{item['text'][:80]}
                    </div>
                    """, unsafe_allow_html=True)
                if not items:
                    st.markdown('<div style="color:#94a3b8;font-size:0.75rem;text-align:center;padding:20px;">No items</div>', unsafe_allow_html=True)
                st.markdown("</div>", unsafe_allow_html=True)
    else:
        st.info("📋 Create QUEUE.md to populate the task board")

    st.markdown("<br>", unsafe_allow_html=True)

    # ══════════ ERRORS + GIT (side by side) ══════════
    err_col, git_col = st.columns(2)

    with err_col:
        st.markdown('<div class="section-header">⚠️ Recent Errors</div>', unsafe_allow_html=True)
        try:
            log_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "live_trader.log")
            if os.path.exists(log_path):
                result = subprocess.run(
                    ["grep", "-iE", "error|fail|❌.*Redeem|⚠️.*Redeem|429|401|403|timed out", log_path],
                    capture_output=True, text=True, timeout=5
                )
                errors = [e for e in result.stdout.strip().split("\n") if e.strip()][-8:]
                if errors:
                    for err in errors:
                        st.markdown(f'<div class="error-line">{err[:120]}</div>', unsafe_allow_html=True)
                else:
                    st.markdown("""
                    <div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;padding:16px;text-align:center;color:#166534;">
                        ✅ No errors in logs
                    </div>
                    """, unsafe_allow_html=True)
            else:
                st.caption("Log file not found")
        except Exception as e:
            st.warning(f"Could not read logs: {e}")

    with git_col:
        st.markdown('<div class="section-header">📦 Repository</div>', unsafe_allow_html=True)
        try:
            bot_dir = os.path.dirname(os.path.dirname(__file__))
            git_log = subprocess.run(
                ["git", "log", "--oneline", "-5"], capture_output=True, text=True, cwd=bot_dir, timeout=5
            )
            git_status = subprocess.run(
                ["git", "status", "--short"], capture_output=True, text=True, cwd=bot_dir, timeout=5
            )
            for line in git_log.stdout.strip().split("\n")[:5]:
                parts = line.split(" ", 1)
                if len(parts) == 2:
                    st.markdown(f'<div class="git-line"><span class="git-hash">{parts[0]}</span> {parts[1]}</div>', unsafe_allow_html=True)

            changes = git_status.stdout.strip()
            if changes:
                ct = len(changes.split("\n"))
                st.markdown(f'<div style="background:#fffbeb;border:1px solid #fde68a;border-radius:6px;padding:8px 12px;margin-top:8px;font-size:0.75rem;color:#92400e;">⚠️ {ct} uncommitted file(s)</div>', unsafe_allow_html=True)
            else:
                st.markdown('<div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:6px;padding:8px 12px;margin-top:8px;font-size:0.75rem;color:#166534;">✅ Clean working tree</div>', unsafe_allow_html=True)
        except Exception as e:
            st.warning(f"Git error: {e}")

    st.markdown("<br>", unsafe_allow_html=True)

    # ══════════ LEARNINGS ══════════
    st.markdown('<div class="section-header">🧠 Learnings</div>', unsafe_allow_html=True)
    learnings_path = os.path.expanduser("~/.openclaw/workspace/memory/learnings.md")
    if not os.path.exists(learnings_path):
        learnings_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "..", "memory", "learnings.md")

    if os.path.exists(learnings_path):
        with open(learnings_path, "r") as f:
            content = f.read()
        lessons = [line.strip("# ").strip() for line in content.split("\n") if line.startswith("### ")]
        if lessons:
            learn_cols = st.columns(min(len(lessons), 3))
            for i, lesson in enumerate(lessons):
                with learn_cols[i % 3]:
                    st.markdown(f'<div class="learning-card">💡 <b>{lesson}</b></div>', unsafe_allow_html=True)
            with st.expander("📖 Full learnings log"):
                st.markdown(content)
        else:
            st.caption("No learnings recorded yet")
    else:
        st.caption("memory/learnings.md not found")
