"""
═══════════════════════════════════════════════════════════════════════════════
🎯 THE OUTSIDERS — Late Drift Sniper Dashboard v3
═══════════════════════════════════════════════════════════════════════════════

Real-time monitoring dashboard for the Late Drift Sniper.
Shows live performance, signal analysis, filter effectiveness, and trade log.

Usage:
  streamlit run src/dashboard_sniper.py --server.port 8502

═══════════════════════════════════════════════════════════════════════════════
"""

import os
import sys
import json
import sqlite3
import time
from datetime import datetime, timezone, timedelta

import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# ─── Config ─────────────────────────────────────────────────────────────────
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT_DIR, "data", "drift_sniper.db")
STATE_FILE = os.path.join(ROOT_DIR, "data", "drift_sniper_state.json")
LOG_FILE = os.path.join(ROOT_DIR, "data", "drift_sniper.log")

PST = timezone(timedelta(hours=-7))

# ─── Page Config ─────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="🎯 Drift Sniper Dashboard",
    page_icon="🎯",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─── Custom CSS ──────────────────────────────────────────────────────────────
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');

    * { font-family: 'Inter', sans-serif; }

    .stApp {
        background: linear-gradient(135deg, #0a0a0f 0%, #1a1a2e 50%, #0a0a0f 100%);
    }

    /* Hero banner */
    .hero-banner {
        background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%);
        border: 1px solid #e2b714;
        border-radius: 12px;
        padding: 1.5rem 2rem;
        margin-bottom: 1.5rem;
        text-align: center;
    }
    .hero-banner h1 {
        color: #e2b714;
        font-size: 2rem;
        margin: 0;
        text-shadow: 0 0 20px rgba(226, 183, 20, 0.3);
    }
    .hero-banner p {
        color: #8892b0;
        margin: 0.5rem 0 0 0;
    }

    /* Metric cards */
    .metric-card {
        background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
        border: 1px solid #2d2d44;
        border-radius: 10px;
        padding: 1.2rem;
        text-align: center;
        transition: border-color 0.3s;
    }
    .metric-card:hover { border-color: #e2b714; }
    .metric-card .label {
        color: #8892b0;
        font-size: 0.75rem;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        margin-bottom: 0.3rem;
    }
    .metric-card .value {
        font-size: 1.8rem;
        font-weight: 700;
        margin: 0;
    }
    .metric-card .sub {
        color: #8892b0;
        font-size: 0.8rem;
        margin-top: 0.2rem;
    }

    .value-gold { color: #e2b714; }
    .value-green { color: #00d26a; }
    .value-red { color: #ff4757; }
    .value-blue { color: #45aaf2; }
    .value-white { color: #e6f1ff; }

    /* Filter effectiveness cards */
    .filter-card {
        background: #1a1a2e;
        border: 1px solid #2d2d44;
        border-radius: 8px;
        padding: 1rem;
    }
    .filter-card h4 {
        color: #e2b714;
        margin: 0 0 0.5rem 0;
        font-size: 0.9rem;
    }

    /* Trade log */
    .trade-win { color: #00d26a; }
    .trade-loss { color: #ff4757; }
    .trade-pending { color: #ffa502; }

    /* Hide Streamlit branding */
    #MainMenu, footer, header { visibility: hidden; }

    div[data-testid="stMetricValue"] { font-size: 1.4rem; }
</style>
""", unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════════════════════════════
# DATA LOADING
# ═══════════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=10)
def load_observations():
    """Load all observations from SQLite."""
    if not os.path.exists(DB_PATH):
        return pd.DataFrame()
    try:
        conn = sqlite3.connect(DB_PATH)
        df = pd.read_sql_query("SELECT * FROM observations ORDER BY id DESC", conn)
        conn.close()
        if not df.empty and "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"])
        return df
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=10)
def load_trades():
    """Load all trades from SQLite."""
    if not os.path.exists(DB_PATH):
        return pd.DataFrame()
    try:
        conn = sqlite3.connect(DB_PATH)
        df = pd.read_sql_query("SELECT * FROM trades ORDER BY id DESC", conn)
        conn.close()
        if not df.empty and "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"])
        return df
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=10)
def load_resolutions():
    """Load all resolutions from SQLite."""
    if not os.path.exists(DB_PATH):
        return pd.DataFrame()
    try:
        conn = sqlite3.connect(DB_PATH)
        df = pd.read_sql_query("SELECT * FROM resolutions ORDER BY window_ts DESC", conn)
        conn.close()
        return df
    except Exception:
        return pd.DataFrame()


def load_state():
    """Load current bot state from JSON."""
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def load_recent_logs(n=50):
    """Load last N lines from log file."""
    if not os.path.exists(LOG_FILE):
        return []
    try:
        with open(LOG_FILE, "r") as f:
            lines = f.readlines()
        return lines[-n:]
    except Exception:
        return []


# ═══════════════════════════════════════════════════════════════════════════════
# HERO BANNER
# ═══════════════════════════════════════════════════════════════════════════════

st.markdown("""
<div class="hero-banner">
    <h1>🎯 Late Drift Sniper v3</h1>
    <p>Post-open arbitrage on Polymarket BTC 5-min markets</p>
</div>
""", unsafe_allow_html=True)

# ─── Sidebar ─────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("### ⚙️ Controls")
    auto_refresh = st.checkbox("Auto-refresh (10s)", value=True)
    if auto_refresh:
        try:
            from streamlit_autorefresh import st_autorefresh
            st_autorefresh(interval=10_000, key="sniper_refresh")
        except ImportError:
            st.info("Install `streamlit-autorefresh` for auto-refresh")

    st.markdown("---")

    state = load_state()
    if state:
        updated = state.get("updated", "unknown")
        st.markdown(f"**Last state update:** {updated}")
        streak = state.get("streak_history", [])
        if streak:
            recent = streak[-5:]
            streak_str = " → ".join(
                [f"{'🟢' if s == 'up' else '🔴'} {s.upper()}" for s in recent]
            )
            st.markdown(f"**Recent outcomes:**\n{streak_str}")
        stats = state.get("stats", {})
        if stats:
            st.markdown(f"**Balance:** ${state.get('balance', 0):.2f}")

    st.markdown("---")
    st.markdown("### 📂 Data Files")
    if os.path.exists(DB_PATH):
        size_mb = os.path.getsize(DB_PATH) / 1024 / 1024
        st.markdown(f"DB: `{size_mb:.2f} MB`")
    if os.path.exists(LOG_FILE):
        size_kb = os.path.getsize(LOG_FILE) / 1024
        st.markdown(f"Log: `{size_kb:.1f} KB`")


# ═══════════════════════════════════════════════════════════════════════════════
# TAB LAYOUT
# ═══════════════════════════════════════════════════════════════════════════════

tab_overview, tab_signals, tab_filters, tab_trades, tab_logs = st.tabs([
    "📊 Overview", "📡 Signal Analysis", "🔬 Filter Effectiveness",
    "💰 Trade Log", "📋 Live Logs"
])

# Load data
obs_df = load_observations()
trades_df = load_trades()
res_df = load_resolutions()

# ═══════════════════════════════════════════════════════════════════════════════
# TAB 1: OVERVIEW
# ═══════════════════════════════════════════════════════════════════════════════

with tab_overview:
    if trades_df.empty and obs_df.empty:
        st.info("🔄 No data yet. Start the sniper in shadow mode to begin collecting data.")
        st.code("python src/sniper_v3.py --shadow", language="bash")
    else:
        # ─── Key Metrics ─────────────────────────────────────────────────
        total_obs = len(obs_df) if not obs_df.empty else 0
        total_signals = int(obs_df["signal_fired"].sum()) if not obs_df.empty and "signal_fired" in obs_df.columns else 0
        total_trades = len(trades_df) if not trades_df.empty else 0

        resolved = trades_df[trades_df["resolved"] == 1] if not trades_df.empty and "resolved" in trades_df.columns else pd.DataFrame()
        wins = int(resolved["won"].sum()) if not resolved.empty and "won" in resolved.columns else 0
        losses = len(resolved) - wins
        total_pnl = float(resolved["pnl"].sum()) if not resolved.empty and "pnl" in resolved.columns else 0.0
        wr = wins / len(resolved) * 100 if len(resolved) > 0 else 0
        signal_rate = total_signals / total_obs * 100 if total_obs > 0 else 0

        c1, c2, c3, c4, c5, c6 = st.columns(6)

        with c1:
            pnl_class = "value-green" if total_pnl >= 0 else "value-red"
            st.markdown(f"""
            <div class="metric-card">
                <div class="label">Total P&L</div>
                <div class="value {pnl_class}">${total_pnl:+.2f}</div>
                <div class="sub">{total_trades} trades</div>
            </div>
            """, unsafe_allow_html=True)

        with c2:
            wr_class = "value-green" if wr >= 65 else ("value-gold" if wr >= 50 else "value-red")
            st.markdown(f"""
            <div class="metric-card">
                <div class="label">Win Rate</div>
                <div class="value {wr_class}">{wr:.1f}%</div>
                <div class="sub">{wins}W-{losses}L</div>
            </div>
            """, unsafe_allow_html=True)

        with c3:
            st.markdown(f"""
            <div class="metric-card">
                <div class="label">Windows Observed</div>
                <div class="value value-blue">{total_obs:,}</div>
                <div class="sub">{total_obs * 5 / 60:.1f} hours</div>
            </div>
            """, unsafe_allow_html=True)

        with c4:
            st.markdown(f"""
            <div class="metric-card">
                <div class="label">Signals Fired</div>
                <div class="value value-gold">{total_signals}</div>
                <div class="sub">{signal_rate:.1f}% rate</div>
            </div>
            """, unsafe_allow_html=True)

        with c5:
            avg_edge = float(resolved["edge"].mean()) * 100 if not resolved.empty and "edge" in resolved.columns else 0
            st.markdown(f"""
            <div class="metric-card">
                <div class="label">Avg Edge</div>
                <div class="value value-white">{avg_edge:.1f}%</div>
                <div class="sub">on taken trades</div>
            </div>
            """, unsafe_allow_html=True)

        with c6:
            avg_drift = float(resolved["drift_pct"].abs().mean()) * 100 if not resolved.empty and "drift_pct" in resolved.columns else 0
            st.markdown(f"""
            <div class="metric-card">
                <div class="label">Avg Drift</div>
                <div class="value value-white">{avg_drift:.2f}%</div>
                <div class="sub">at entry</div>
            </div>
            """, unsafe_allow_html=True)

        st.markdown("---")

        # ─── Cumulative P&L Chart ────────────────────────────────────────
        if not resolved.empty:
            st.subheader("📈 Cumulative P&L")
            resolved_sorted = resolved.sort_values("timestamp")
            resolved_sorted["cum_pnl"] = resolved_sorted["pnl"].cumsum()
            resolved_sorted["trade_num"] = range(1, len(resolved_sorted) + 1)

            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=resolved_sorted["trade_num"],
                y=resolved_sorted["cum_pnl"],
                mode="lines+markers",
                line=dict(color="#e2b714", width=2),
                marker=dict(
                    size=8,
                    color=["#00d26a" if w == 1 else "#ff4757"
                           for w in resolved_sorted["won"]],
                    line=dict(color="#1a1a2e", width=1),
                ),
                hovertemplate=(
                    "Trade #%{x}<br>"
                    "P&L: $%{customdata[0]:+.2f}<br>"
                    "Cumulative: $%{y:+.2f}<br>"
                    "Side: %{customdata[1]}<br>"
                    "Edge: %{customdata[2]:.1f}%"
                ),
                customdata=list(zip(
                    resolved_sorted["pnl"],
                    resolved_sorted["side"],
                    resolved_sorted["edge"] * 100,
                )),
            ))
            fig.update_layout(
                template="plotly_dark",
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(26,26,46,0.8)",
                xaxis_title="Trade #",
                yaxis_title="Cumulative P&L ($)",
                height=350,
                margin=dict(l=40, r=20, t=20, b=40),
            )
            # Zero line
            fig.add_hline(y=0, line=dict(color="#8892b0", width=1, dash="dash"))
            st.plotly_chart(fig, use_container_width=True)

        # ─── Win/Loss by Side ────────────────────────────────────────────
        if not resolved.empty:
            col1, col2 = st.columns(2)
            with col1:
                st.subheader("🟢🔴 Outcomes by Side")
                side_stats = resolved.groupby(["side", "won"]).size().reset_index(name="count")
                side_stats["result"] = side_stats["won"].map({1: "Win", 0: "Loss"})
                fig_side = px.bar(
                    side_stats, x="side", y="count", color="result",
                    color_discrete_map={"Win": "#00d26a", "Loss": "#ff4757"},
                    barmode="group",
                    template="plotly_dark",
                )
                fig_side.update_layout(
                    paper_bgcolor="rgba(0,0,0,0)",
                    plot_bgcolor="rgba(26,26,46,0.8)",
                    height=300,
                    margin=dict(l=40, r=20, t=20, b=40),
                    showlegend=True,
                    legend=dict(orientation="h", y=-0.15),
                )
                st.plotly_chart(fig_side, use_container_width=True)

            with col2:
                st.subheader("⏰ Trades by Hour (PST)")
                if "timestamp" in resolved.columns:
                    resolved_h = resolved.copy()
                    resolved_h["hour"] = resolved_h["timestamp"].dt.hour
                    hourly = resolved_h.groupby("hour").agg(
                        trades=("id", "count"),
                        wins=("won", "sum"),
                    ).reset_index()
                    hourly["wr"] = hourly["wins"] / hourly["trades"] * 100
                    fig_hour = go.Figure()
                    fig_hour.add_trace(go.Bar(
                        x=hourly["hour"], y=hourly["trades"],
                        marker_color="#45aaf2", name="Trades",
                    ))
                    fig_hour.add_trace(go.Scatter(
                        x=hourly["hour"], y=hourly["wr"],
                        mode="lines+markers",
                        line=dict(color="#e2b714", width=2),
                        marker=dict(size=6),
                        name="Win Rate %",
                        yaxis="y2",
                    ))
                    fig_hour.update_layout(
                        template="plotly_dark",
                        paper_bgcolor="rgba(0,0,0,0)",
                        plot_bgcolor="rgba(26,26,46,0.8)",
                        height=300,
                        margin=dict(l=40, r=40, t=20, b=40),
                        yaxis=dict(title="Trades"),
                        yaxis2=dict(title="Win Rate %", overlaying="y",
                                    side="right", range=[0, 100]),
                        showlegend=True,
                        legend=dict(orientation="h", y=-0.15),
                    )
                    st.plotly_chart(fig_hour, use_container_width=True)


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 2: SIGNAL ANALYSIS
# ═══════════════════════════════════════════════════════════════════════════════

with tab_signals:
    if obs_df.empty:
        st.info("No observations yet. Start shadow mode to collect data.")
    else:
        st.subheader("📡 Signal Distribution")

        col1, col2 = st.columns(2)

        with col1:
            # Drift distribution
            st.markdown("#### Drift at Observation Time")
            if "drift_pct" in obs_df.columns:
                drift_vals = obs_df["drift_pct"].dropna() * 100
                fig_drift = go.Figure()
                fig_drift.add_trace(go.Histogram(
                    x=drift_vals,
                    nbinsx=50,
                    marker_color="#45aaf2",
                    opacity=0.8,
                ))
                # Mark the threshold
                fig_drift.add_vline(x=0.15, line=dict(color="#e2b714", dash="dash"),
                                    annotation_text="Min drift (0.15%)")
                fig_drift.add_vline(x=-0.15, line=dict(color="#e2b714", dash="dash"))
                fig_drift.update_layout(
                    template="plotly_dark",
                    paper_bgcolor="rgba(0,0,0,0)",
                    plot_bgcolor="rgba(26,26,46,0.8)",
                    xaxis_title="Drift %",
                    yaxis_title="Count",
                    height=350,
                    margin=dict(l=40, r=20, t=20, b=40),
                )
                st.plotly_chart(fig_drift, use_container_width=True)

        with col2:
            # Edge distribution (signals only)
            signals = obs_df[obs_df["signal_fired"] == 1] if "signal_fired" in obs_df.columns else pd.DataFrame()
            st.markdown("#### Edge on Fired Signals")
            if not signals.empty and "edge" in signals.columns:
                edge_vals = signals["edge"].dropna() * 100
                fig_edge = go.Figure()
                fig_edge.add_trace(go.Histogram(
                    x=edge_vals,
                    nbinsx=30,
                    marker_color="#e2b714",
                    opacity=0.8,
                ))
                fig_edge.add_vline(x=7, line=dict(color="#ff4757", dash="dash"),
                                   annotation_text="Min edge (7%)")
                fig_edge.update_layout(
                    template="plotly_dark",
                    paper_bgcolor="rgba(0,0,0,0)",
                    plot_bgcolor="rgba(26,26,46,0.8)",
                    xaxis_title="Edge %",
                    yaxis_title="Count",
                    height=350,
                    margin=dict(l=40, r=20, t=20, b=40),
                )
                st.plotly_chart(fig_edge, use_container_width=True)
            else:
                st.info("No signals fired yet")

        # ─── Rejection reasons ───────────────────────────────────────────
        st.markdown("---")
        st.subheader("❌ Rejection Reasons")
        if "reject_reason" in obs_df.columns:
            rejects = obs_df[obs_df["reject_reason"].notna()]
            if not rejects.empty:
                # Parse rejection categories
                def categorize_reject(reason):
                    if not reason:
                        return "Unknown"
                    reason = str(reason).lower()
                    if "drift" in reason:
                        return "Insufficient Drift"
                    elif "ask" in reason or "price" in reason:
                        return "Price Too High"
                    elif "edge" in reason:
                        return "Insufficient Edge"
                    elif "depth" in reason:
                        return "Low Depth"
                    elif "fill" in reason:
                        return "Fill Too Small"
                    return "Other"

                rejects = rejects.copy()
                rejects["category"] = rejects["reject_reason"].apply(categorize_reject)
                cat_counts = rejects["category"].value_counts().reset_index()
                cat_counts.columns = ["Reason", "Count"]

                fig_rej = px.pie(
                    cat_counts, values="Count", names="Reason",
                    color_discrete_sequence=["#ff4757", "#ffa502", "#45aaf2", "#2ed573", "#8892b0"],
                    hole=0.4,
                )
                fig_rej.update_layout(
                    template="plotly_dark",
                    paper_bgcolor="rgba(0,0,0,0)",
                    height=350,
                )
                st.plotly_chart(fig_rej, use_container_width=True)
            else:
                st.success("No rejected signals — all observations either passed or didn't meet drift minimum")

        # ─── Drift vs Edge scatter ───────────────────────────────────────
        st.markdown("---")
        st.subheader("🎯 Drift vs Edge (All Observations)")
        if "drift_pct" in obs_df.columns and "edge" in obs_df.columns:
            scatter_df = obs_df[obs_df["edge"].notna()].copy()
            if not scatter_df.empty:
                scatter_df["abs_drift_pct"] = scatter_df["drift_pct"].abs() * 100
                scatter_df["edge_pct"] = scatter_df["edge"] * 100
                scatter_df["signal"] = scatter_df["signal_fired"].map(
                    {1: "Signal Fired", 0: "No Signal"}
                )

                fig_scatter = px.scatter(
                    scatter_df,
                    x="abs_drift_pct", y="edge_pct",
                    color="signal",
                    color_discrete_map={"Signal Fired": "#e2b714", "No Signal": "#8892b0"},
                    opacity=0.6,
                    hover_data=["window_ts", "favored_side"],
                )
                # Threshold lines
                fig_scatter.add_hline(y=7, line=dict(color="#ff4757", dash="dash"),
                                      annotation_text="Min edge")
                fig_scatter.add_vline(x=0.15, line=dict(color="#ff4757", dash="dash"),
                                      annotation_text="Min drift")
                fig_scatter.update_layout(
                    template="plotly_dark",
                    paper_bgcolor="rgba(0,0,0,0)",
                    plot_bgcolor="rgba(26,26,46,0.8)",
                    xaxis_title="Absolute Drift %",
                    yaxis_title="Edge %",
                    height=400,
                    margin=dict(l=40, r=20, t=20, b=40),
                )
                st.plotly_chart(fig_scatter, use_container_width=True)


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 3: FILTER EFFECTIVENESS
# ═══════════════════════════════════════════════════════════════════════════════

with tab_filters:
    if obs_df.empty:
        st.info("No data yet. Run shadow mode to collect observations.")
    else:
        st.subheader("🔬 Filter Analysis")

        # ─── Volatility filter ───────────────────────────────────────────
        col1, col2 = st.columns(2)

        with col1:
            st.markdown("#### 🌊 Volatility Factor Over Time")
            if "vol_factor" in obs_df.columns and "timestamp" in obs_df.columns:
                vol_data = obs_df[obs_df["vol_factor"].notna()].copy()
                if not vol_data.empty:
                    fig_vol = go.Figure()
                    fig_vol.add_trace(go.Scatter(
                        x=vol_data["timestamp"],
                        y=vol_data["vol_factor"],
                        mode="lines",
                        line=dict(color="#45aaf2", width=1.5),
                        name="Vol Factor",
                    ))
                    fig_vol.add_hline(y=1.0, line=dict(color="#8892b0", dash="dash"),
                                      annotation_text="Baseline")
                    fig_vol.update_layout(
                        template="plotly_dark",
                        paper_bgcolor="rgba(0,0,0,0)",
                        plot_bgcolor="rgba(26,26,46,0.8)",
                        yaxis_title="Vol Factor (current/avg)",
                        height=300,
                        margin=dict(l=40, r=20, t=20, b=40),
                    )
                    st.plotly_chart(fig_vol, use_container_width=True)
                else:
                    st.info("Volatility data not yet available (needs ~3 windows)")

        with col2:
            st.markdown("#### 🔄 Streak History")
            state = load_state()
            streak_hist = state.get("streak_history", [])
            if streak_hist:
                # Show last 20 outcomes as colored blocks
                blocks = ""
                for s in streak_hist[-20:]:
                    color = "#00d26a" if s == "up" else "#ff4757"
                    label = "↑" if s == "up" else "↓"
                    blocks += f'<span style="color:{color};font-size:1.5rem;margin:0 2px;">{label}</span>'
                st.markdown(f"**Last {min(20, len(streak_hist))} outcomes:**", unsafe_allow_html=True)
                st.markdown(blocks, unsafe_allow_html=True)

                # Streak stats
                from itertools import groupby
                streaks = [(k, len(list(g))) for k, g in groupby(streak_hist)]
                if streaks:
                    max_streak = max(s[1] for s in streaks)
                    avg_streak = statistics.mean(s[1] for s in streaks)
                    st.markdown(f"**Max streak:** {max_streak} | **Avg streak:** {avg_streak:.1f}")
            else:
                st.info("No outcome data yet")

        st.markdown("---")

        # ─── Oracle lag detection ────────────────────────────────────────
        col3, col4 = st.columns(2)

        with col3:
            st.markdown("#### ⚡ Oracle Lag Detection")
            if "oracle_lag_detected" in obs_df.columns:
                lag_count = int(obs_df["oracle_lag_detected"].sum())
                total = len(obs_df)
                lag_pct = lag_count / total * 100 if total > 0 else 0
                st.markdown(f"""
                <div class="filter-card">
                    <h4>Oracle Lag Events</h4>
                    <p style="color: #e6f1ff; font-size: 1.5rem;">{lag_count} / {total} ({lag_pct:.1f}%)</p>
                    <p style="color: #8892b0;">Windows where Chainlink lag was detected</p>
                </div>
                """, unsafe_allow_html=True)

                # WR with lag vs without
                if not resolved.empty and "oracle_lag" in resolved.columns:
                    with_lag = resolved[resolved["oracle_lag"] == 1]
                    without_lag = resolved[resolved["oracle_lag"] == 0]
                    lag_wr = with_lag["won"].mean() * 100 if len(with_lag) > 0 else 0
                    no_lag_wr = without_lag["won"].mean() * 100 if len(without_lag) > 0 else 0
                    st.markdown(f"WR with lag: **{lag_wr:.1f}%** ({len(with_lag)} trades)")
                    st.markdown(f"WR without: **{no_lag_wr:.1f}%** ({len(without_lag)} trades)")

        with col4:
            st.markdown("#### 📊 Price Spread (Cross-Exchange)")
            if "price_spread_pct" in obs_df.columns:
                spread_data = obs_df["price_spread_pct"].dropna()
                if not spread_data.empty:
                    fig_spread = go.Figure()
                    fig_spread.add_trace(go.Histogram(
                        x=spread_data,
                        nbinsx=40,
                        marker_color="#e2b714",
                        opacity=0.8,
                    ))
                    fig_spread.add_vline(
                        x=0.01, line=dict(color="#ff4757", dash="dash"),
                        annotation_text="Lag threshold",
                    )
                    fig_spread.update_layout(
                        template="plotly_dark",
                        paper_bgcolor="rgba(0,0,0,0)",
                        plot_bgcolor="rgba(26,26,46,0.8)",
                        xaxis_title="Spread %",
                        yaxis_title="Count",
                        height=300,
                        margin=dict(l=40, r=20, t=20, b=40),
                    )
                    st.plotly_chart(fig_spread, use_container_width=True)

        # ─── Edge vs Win Rate buckets ────────────────────────────────────
        st.markdown("---")
        st.subheader("📐 Edge Buckets vs Win Rate")
        if not resolved.empty and "edge" in resolved.columns:
            resolved_c = resolved.copy()
            resolved_c["edge_pct"] = resolved_c["edge"] * 100
            bins = [0, 5, 7, 10, 15, 20, 50]
            labels = ["0-5%", "5-7%", "7-10%", "10-15%", "15-20%", "20%+"]
            resolved_c["edge_bucket"] = pd.cut(
                resolved_c["edge_pct"], bins=bins, labels=labels, include_lowest=True
            )
            bucket_stats = resolved_c.groupby("edge_bucket", observed=True).agg(
                trades=("id", "count"),
                wins=("won", "sum"),
                avg_pnl=("pnl", "mean"),
            ).reset_index()
            bucket_stats["wr"] = bucket_stats["wins"] / bucket_stats["trades"] * 100

            fig_buckets = make_subplots(specs=[[{"secondary_y": True}]])
            fig_buckets.add_trace(
                go.Bar(
                    x=bucket_stats["edge_bucket"].astype(str),
                    y=bucket_stats["trades"],
                    marker_color="#45aaf2",
                    name="Trades",
                ),
                secondary_y=False,
            )
            fig_buckets.add_trace(
                go.Scatter(
                    x=bucket_stats["edge_bucket"].astype(str),
                    y=bucket_stats["wr"],
                    mode="lines+markers",
                    line=dict(color="#e2b714", width=2),
                    marker=dict(size=8),
                    name="Win Rate %",
                ),
                secondary_y=True,
            )
            fig_buckets.update_layout(
                template="plotly_dark",
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(26,26,46,0.8)",
                height=350,
                margin=dict(l=40, r=40, t=20, b=40),
            )
            fig_buckets.update_yaxes(title_text="Trades", secondary_y=False)
            fig_buckets.update_yaxes(title_text="Win Rate %", range=[0, 100], secondary_y=True)
            st.plotly_chart(fig_buckets, use_container_width=True)


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 4: TRADE LOG
# ═══════════════════════════════════════════════════════════════════════════════

with tab_trades:
    if trades_df.empty:
        st.info("No trades yet. Run paper or live mode to see trades here.")
    else:
        st.subheader("💰 Trade History")

        # Summary row
        col1, col2, col3, col4 = st.columns(4)
        pending = trades_df[trades_df["resolved"] == 0] if "resolved" in trades_df.columns else pd.DataFrame()
        with col1:
            st.metric("Total Trades", len(trades_df))
        with col2:
            st.metric("Pending Resolution", len(pending))
        with col3:
            if not resolved.empty:
                avg_cost = resolved["cost_usdc"].mean()
                st.metric("Avg Trade Size", f"${avg_cost:.2f}")
        with col4:
            if not resolved.empty:
                best = resolved["pnl"].max()
                worst = resolved["pnl"].min()
                st.metric("Best / Worst", f"+${best:.2f} / -${abs(worst):.2f}")

        st.markdown("---")

        # Trade table
        display_cols = ["timestamp", "side", "drift_pct", "edge", "buy_price",
                        "shares", "cost_usdc", "won", "pnl", "resolved"]
        available_cols = [c for c in display_cols if c in trades_df.columns]

        display_df = trades_df[available_cols].copy()
        if "drift_pct" in display_df.columns:
            display_df["drift_pct"] = (display_df["drift_pct"] * 100).round(3).astype(str) + "%"
        if "edge" in display_df.columns:
            display_df["edge"] = (display_df["edge"] * 100).round(1).astype(str) + "%"
        if "buy_price" in display_df.columns:
            display_df["buy_price"] = display_df["buy_price"].round(3).apply(lambda x: f"${x}")
        if "cost_usdc" in display_df.columns:
            display_df["cost_usdc"] = display_df["cost_usdc"].round(2).apply(lambda x: f"${x}")
        if "pnl" in display_df.columns:
            display_df["pnl"] = display_df["pnl"].apply(
                lambda x: f"${x:+.2f}" if pd.notna(x) else "⏳"
            )
        if "won" in display_df.columns:
            display_df["won"] = display_df["won"].apply(
                lambda x: "✅" if x == 1 else ("❌" if x == 0 else "⏳")
            )
        if "resolved" in display_df.columns:
            display_df["resolved"] = display_df["resolved"].map({1: "✓", 0: "⏳"})

        # Rename for display
        rename_map = {
            "timestamp": "Time",
            "side": "Side",
            "drift_pct": "Drift",
            "edge": "Edge",
            "buy_price": "Price",
            "shares": "Shares",
            "cost_usdc": "Cost",
            "won": "Result",
            "pnl": "P&L",
            "resolved": "Status",
        }
        display_df = display_df.rename(columns={k: v for k, v in rename_map.items() if k in display_df.columns})

        st.dataframe(
            display_df,
            use_container_width=True,
            hide_index=True,
            height=500,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 5: LIVE LOGS
# ═══════════════════════════════════════════════════════════════════════════════

with tab_logs:
    st.subheader("📋 Recent Log Output")

    lines = load_recent_logs(100)
    if lines:
        log_text = "".join(lines)
        st.code(log_text, language="text")
    else:
        st.info("No log entries yet. Start the sniper to see output here.")

    # Raw observation data explorer
    st.markdown("---")
    st.subheader("🔍 Raw Observation Explorer")
    if not obs_df.empty:
        n_show = st.slider("Recent observations to show", 5, 100, 20)
        recent = obs_df.head(n_show)
        cols_to_show = [
            "timestamp", "window_ts", "time_left_sec", "drift_pct",
            "vol_factor", "favored_side", "favored_ask",
            "model_prob", "implied_prob", "edge",
            "streak_count", "oracle_lag_detected", "lag_bonus",
            "signal_fired", "reject_reason", "trade_taken",
        ]
        available = [c for c in cols_to_show if c in recent.columns]
        st.dataframe(recent[available], use_container_width=True, hide_index=True, height=400)

# ─── Footer ──────────────────────────────────────────────────────────────────
st.markdown("---")
st.markdown(
    '<p style="text-align:center;color:#8892b0;font-size:0.8rem;">'
    '🎯 The Outsiders — Late Drift Sniper v3 | '
    f'Data: {len(obs_df)} observations, {len(trades_df)} trades'
    '</p>',
    unsafe_allow_html=True,
)
