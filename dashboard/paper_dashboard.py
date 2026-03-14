"""
Paper Trader v4 Dashboard
=========================
Separate dashboard for paper trading simulation results.
Run: streamlit run dashboard/paper_dashboard.py --server.port 8502
"""

import streamlit as st
import sqlite3
import pandas as pd
import os
from datetime import datetime, timezone, timedelta

PST = timezone(timedelta(hours=-7))
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "paper_v4.db")
LOG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "paper_v4.log")

st.set_page_config(page_title="Paper Trader v4", page_icon="📋", layout="wide")


def get_conn():
    if not os.path.exists(DB_PATH):
        st.warning("No paper_v4.db found. Start the paper trader first.")
        st.stop()
    return sqlite3.connect(DB_PATH)


def load_trades():
    conn = get_conn()
    df = pd.read_sql("SELECT * FROM paper_trades WHERE status='closed' ORDER BY exit_timestamp", conn)
    conn.close()
    return df


def load_snapshots():
    conn = get_conn()
    df = pd.read_sql("SELECT * FROM book_snapshots ORDER BY timestamp DESC LIMIT 1000", conn)
    conn.close()
    return df


# ═══════════════════════════════════════════════════════════
# HEADER
# ═══════════════════════════════════════════════════════════

st.title("📋 Paper Trader v4 — Realistic Simulation")
st.caption("Real CLOB book depth · Slippage model · Multiple configs · Zero risk")

tab1, tab2, tab3, tab4 = st.tabs(["🏆 Config Comparison", "📊 Trade Log", "📸 Book Snapshots", "📜 Live Log"])


# ═══════════════════════════════════════════════════════════
# TAB 1: CONFIG COMPARISON
# ═══════════════════════════════════════════════════════════

with tab1:
    df = load_trades()
    
    if df.empty:
        st.info("No closed trades yet. Paper trader is collecting data...")
    else:
        configs = df["config"].unique()
        
        # Summary cards
        cols = st.columns(len(configs))
        best_pnl = -999
        best_cfg = ""
        
        summary_data = []
        for cfg in sorted(configs):
            cdf = df[df["config"] == cfg]
            wins = len(cdf[cdf["pnl"] > 0])
            losses = len(cdf[cdf["pnl"] <= 0])
            total = wins + losses
            wr = wins / total * 100 if total else 0
            net_pnl = cdf["pnl"].sum()
            avg_win = cdf[cdf["pnl"] > 0]["pnl"].mean() if wins else 0
            avg_loss = cdf[cdf["pnl"] <= 0]["pnl"].mean() if losses else 0
            
            if net_pnl > best_pnl:
                best_pnl = net_pnl
                best_cfg = cfg
            
            summary_data.append({
                "Config": cfg,
                "Trades": total,
                "Win Rate": f"{wr:.0f}%",
                "Net PnL": f"${net_pnl:+.2f}",
                "Avg Win": f"${avg_win:+.2f}",
                "Avg Loss": f"${avg_loss:+.2f}",
                "Profit Factor": f"{abs(avg_win/avg_loss):.2f}" if avg_loss != 0 else "∞",
                "Balance": f"${1000 + net_pnl:.2f}",
            })
        
        st.subheader("📊 Config Performance Comparison")
        summary_df = pd.DataFrame(summary_data)
        
        # Highlight best config
        st.dataframe(summary_df, use_container_width=True, hide_index=True)
        st.success(f"🏆 Best config: **{best_cfg}** with ${best_pnl:+.2f}")
        
        # Cumulative PnL chart
        st.subheader("📈 Cumulative PnL by Config")
        chart_data = {}
        for cfg in sorted(configs):
            cdf = df[df["config"] == cfg].copy()
            cdf["cum_pnl"] = cdf["pnl"].cumsum()
            chart_data[cfg] = cdf["cum_pnl"].values
        
        max_len = max(len(v) for v in chart_data.values())
        chart_df = pd.DataFrame({
            cfg: list(vals) + [vals[-1]] * (max_len - len(vals))
            for cfg, vals in chart_data.items()
        })
        st.line_chart(chart_df)
        
        # Exit reason breakdown
        st.subheader("🚪 Exit Reasons by Config")
        for cfg in sorted(configs):
            cdf = df[df["config"] == cfg]
            reasons = cdf.groupby("exit_reason").agg(
                count=("pnl", "count"),
                total_pnl=("pnl", "sum"),
                avg_pnl=("pnl", "mean"),
            ).sort_values("count", ascending=False)
            
            with st.expander(f"{cfg} — Exit Breakdown"):
                st.dataframe(reasons, use_container_width=True)
        
        # Win rate by asset
        st.subheader("🪙 Performance by Asset")
        asset_data = []
        for asset in df["asset"].unique():
            for cfg in sorted(configs):
                adf = df[(df["asset"] == asset) & (df["config"] == cfg)]
                if adf.empty:
                    continue
                wins = len(adf[adf["pnl"] > 0])
                total = len(adf)
                asset_data.append({
                    "Asset": asset.upper(),
                    "Config": cfg,
                    "Trades": total,
                    "WR": f"{wins/total*100:.0f}%",
                    "PnL": f"${adf['pnl'].sum():+.2f}",
                })
        if asset_data:
            st.dataframe(pd.DataFrame(asset_data), use_container_width=True, hide_index=True)


# ═══════════════════════════════════════════════════════════
# TAB 2: TRADE LOG
# ═══════════════════════════════════════════════════════════

with tab2:
    df = load_trades()
    
    if df.empty:
        st.info("No trades yet.")
    else:
        # Filters
        col1, col2, col3 = st.columns(3)
        with col1:
            cfg_filter = st.selectbox("Config", ["All"] + sorted(df["config"].unique().tolist()))
        with col2:
            asset_filter = st.selectbox("Asset", ["All"] + sorted(df["asset"].unique().tolist()))
        with col3:
            reason_filter = st.selectbox("Exit Reason", ["All"] + sorted(df["exit_reason"].dropna().unique().tolist()))
        
        filtered = df.copy()
        if cfg_filter != "All":
            filtered = filtered[filtered["config"] == cfg_filter]
        if asset_filter != "All":
            filtered = filtered[filtered["asset"] == asset_filter]
        if reason_filter != "All":
            filtered = filtered[filtered["exit_reason"] == reason_filter]
        
        # Display
        display_cols = ["config", "asset", "direction", "strategy", "entry_price", 
                       "simulated_fill", "exit_price", "simulated_exit", "pnl",
                       "entry_slippage", "exit_slippage", "exit_reason", "timestamp"]
        available = [c for c in display_cols if c in filtered.columns]
        
        st.dataframe(filtered[available].sort_values("timestamp", ascending=False),
                     use_container_width=True, hide_index=True)
        
        # Slippage analysis
        st.subheader("💸 Slippage Analysis")
        if "entry_slippage" in filtered.columns:
            col1, col2 = st.columns(2)
            with col1:
                avg_entry_slip = filtered["entry_slippage"].mean()
                st.metric("Avg Entry Slippage", f"${avg_entry_slip:.4f}")
            with col2:
                avg_exit_slip = filtered["exit_slippage"].mean()
                st.metric("Avg Exit Slippage", f"${avg_exit_slip:.4f}")


# ═══════════════════════════════════════════════════════════
# TAB 3: BOOK SNAPSHOTS
# ═══════════════════════════════════════════════════════════

with tab3:
    snapshots = load_snapshots()
    
    if snapshots.empty:
        st.info("No book snapshots yet.")
    else:
        st.metric("Total Snapshots", len(snapshots))
        
        # Spread analysis
        st.subheader("📊 Spread Distribution")
        if "spread" in snapshots.columns:
            col1, col2, col3 = st.columns(3)
            with col1:
                st.metric("Avg Spread", f"${snapshots['spread'].mean():.4f}")
            with col2:
                st.metric("Median Spread", f"${snapshots['spread'].median():.4f}")
            with col3:
                st.metric("Max Spread", f"${snapshots['spread'].max():.4f}")
        
        # Depth analysis
        st.subheader("💧 Liquidity Depth (within 5% of best)")
        if "bid_depth_5pct" in snapshots.columns:
            col1, col2 = st.columns(2)
            with col1:
                st.metric("Avg Bid Depth", f"${snapshots['bid_depth_5pct'].mean():.2f}")
            with col2:
                st.metric("Avg Ask Depth", f"${snapshots['ask_depth_5pct'].mean():.2f}")
        
        # Recent snapshots
        st.subheader("📸 Recent Snapshots")
        display_cols = ["asset", "direction", "timestamp", "best_bid", "best_ask", 
                       "spread", "bid_depth_5pct", "ask_depth_5pct"]
        available = [c for c in display_cols if c in snapshots.columns]
        st.dataframe(snapshots[available].head(50), use_container_width=True, hide_index=True)


# ═══════════════════════════════════════════════════════════
# TAB 4: LIVE LOG
# ═══════════════════════════════════════════════════════════

with tab4:
    if os.path.exists(LOG_PATH):
        with open(LOG_PATH, "r") as f:
            lines = f.readlines()
        
        st.text_area("📜 Paper Trader Log (last 100 lines)", 
                     "".join(lines[-100:]), height=500)
        st.caption(f"Total lines: {len(lines)}")
    else:
        st.info("No log file yet. Start the paper trader first.")

# Auto-refresh
st.markdown("---")
col1, col2 = st.columns([1, 3])
with col1:
    if st.button("🔄 Refresh"):
        st.rerun()
with col2:
    auto = st.checkbox("Auto-refresh (30s)")
    if auto:
        import time
        time.sleep(30)
        st.rerun()
