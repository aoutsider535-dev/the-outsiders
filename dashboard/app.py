"""
Polymarket Trading Bot — Dashboard
Real-time performance overview, trade history, and strategy metrics.
"""
import streamlit as st
import plotly.graph_objects as go
import plotly.express as px
import pandas as pd
import sys
import os
import json
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from src.database import get_connection, init_db, get_trades, get_performance_summary

st.set_page_config(
    page_title="🕶️ The Outsiders",
    page_icon="🕶️",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Initialize DB
init_db()


def load_trades(limit=500):
    trades = get_trades(limit=limit)
    if not trades:
        return pd.DataFrame()
    df = pd.DataFrame(trades)
    if "timestamp" in df.columns:
        df["time"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
    if "signal_data" in df.columns:
        df["signal_data"] = df["signal_data"].apply(
            lambda x: json.loads(x) if isinstance(x, str) and x else {}
        )
    return df


def load_equity_curve():
    conn = get_connection()
    rows = conn.execute("""
        SELECT timestamp, 
               SUM(pnl) OVER (ORDER BY timestamp) as cumulative_pnl
        FROM trades WHERE status='closed'
        ORDER BY timestamp
    """).fetchall()
    conn.close()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([dict(r) for r in rows])
    df["time"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
    df["balance"] = 1000 + df["cumulative_pnl"]
    return df


# ─── SIDEBAR ───
st.sidebar.title("🕶️ The Outsiders")
st.sidebar.markdown("---")

strategy_filter = st.sidebar.selectbox("Strategy", ["All", "btc_5min"])
trade_type = st.sidebar.selectbox("Trade Type", ["All", "Simulated", "Live"])
refresh = st.sidebar.button("🔄 Refresh Data")

st.sidebar.markdown("---")
st.sidebar.markdown("**Status:** 🟢 Online" if True else "**Status:** 🔴 Offline")
st.sidebar.markdown(f"**Last Update:** {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}")

# ─── MAIN ───
st.title("🕶️ The Outsiders — Trading Dashboard")
st.markdown("*Not insiders. Just smarter.*")

# Load data
df = load_trades()

if df.empty:
    st.info("📭 No trades yet. Run a backtest first!\n\n"
            "```bash\ncd polymarket-bot && python -m src.backtester\n```")
    st.stop()

closed = df[df["status"] == "closed"] if "status" in df.columns else df

# ─── KPI ROW ───
col1, col2, col3, col4, col5 = st.columns(5)

total_trades = len(closed)
wins = len(closed[closed["pnl"] > 0]) if "pnl" in closed.columns else 0
win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
total_pnl = closed["pnl"].sum() if "pnl" in closed.columns else 0
avg_edge = closed["edge_pct"].mean() if "edge_pct" in closed.columns and not closed["edge_pct"].isna().all() else 0

col1.metric("Total Trades", total_trades)
col2.metric("Win Rate", f"{win_rate:.1f}%")
col3.metric("Total P&L", f"${total_pnl:+,.2f}")
col4.metric("Avg Edge", f"{avg_edge:.1f}%")
col5.metric("Balance", f"${1000 + total_pnl:,.2f}")

st.markdown("---")

# ─── EQUITY CURVE ───
equity = load_equity_curve()
if not equity.empty:
    fig_equity = go.Figure()
    fig_equity.add_trace(go.Scatter(
        x=equity["time"], y=equity["balance"],
        mode="lines", name="Balance",
        line=dict(color="#00d4aa", width=2),
        fill="tozeroy", fillcolor="rgba(0,212,170,0.1)"
    ))
    fig_equity.update_layout(
        title="📈 Equity Curve",
        xaxis_title="Time", yaxis_title="Balance ($)",
        template="plotly_dark", height=400,
        yaxis=dict(tickprefix="$")
    )
    st.plotly_chart(fig_equity, use_container_width=True)

# ─── TRADE DISTRIBUTION ───
col_left, col_right = st.columns(2)

with col_left:
    if "pnl_pct" in closed.columns:
        fig_dist = px.histogram(
            closed, x="pnl_pct", nbins=30,
            title="📊 P&L Distribution (%)",
            color_discrete_sequence=["#00d4aa"]
        )
        fig_dist.update_layout(template="plotly_dark", height=350)
        st.plotly_chart(fig_dist, use_container_width=True)

with col_right:
    if "direction" in closed.columns:
        direction_counts = closed["direction"].value_counts()
        fig_dir = px.pie(
            values=direction_counts.values, names=direction_counts.index,
            title="🎯 Direction Split",
            color_discrete_sequence=["#00d4aa", "#ff6b6b"]
        )
        fig_dir.update_layout(template="plotly_dark", height=350)
        st.plotly_chart(fig_dir, use_container_width=True)

# ─── RECENT TRADES TABLE ───
st.markdown("### 📋 Recent Trades")
if not closed.empty:
    display_cols = ["time", "direction", "entry_price", "exit_price", "pnl", "pnl_pct",
                    "edge_pct", "confidence", "exit_reason", "is_simulated"]
    available_cols = [c for c in display_cols if c in closed.columns]
    display_df = closed[available_cols].head(50).copy()

    if "pnl" in display_df.columns:
        display_df["pnl"] = display_df["pnl"].apply(lambda x: f"${x:+.2f}" if pd.notna(x) else "")
    if "pnl_pct" in display_df.columns:
        display_df["pnl_pct"] = display_df["pnl_pct"].apply(lambda x: f"{x:+.1f}%" if pd.notna(x) else "")
    if "confidence" in display_df.columns:
        display_df["confidence"] = display_df["confidence"].apply(lambda x: f"{x:.1%}" if pd.notna(x) else "")
    if "edge_pct" in display_df.columns:
        display_df["edge_pct"] = display_df["edge_pct"].apply(lambda x: f"{x:.1f}%" if pd.notna(x) else "")
    if "is_simulated" in display_df.columns:
        display_df["is_simulated"] = display_df["is_simulated"].apply(lambda x: "🧪 Sim" if x else "💰 Live")

    st.dataframe(display_df, use_container_width=True, hide_index=True)

# ─── STRATEGY PERFORMANCE ───
st.markdown("### ⚙️ Strategy Parameters")
conn = get_connection()
params_rows = conn.execute(
    "SELECT * FROM strategy_params ORDER BY created_at DESC LIMIT 5"
).fetchall()
conn.close()

if params_rows:
    params_df = pd.DataFrame([dict(r) for r in params_rows])
    display_params = params_df[["strategy", "backtest_trades", "backtest_pnl",
                                 "backtest_win_rate", "performance_score", "created_at"]].copy()
    display_params.columns = ["Strategy", "Trades", "P&L", "Win Rate %", "Sharpe", "Date"]
    st.dataframe(display_params, use_container_width=True, hide_index=True)

st.markdown("---")
st.markdown("*🕶️ The Outsiders v0.1 — Jakob & Austin*")
