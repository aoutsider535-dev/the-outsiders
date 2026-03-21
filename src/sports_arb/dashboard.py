"""
Streamlit dashboard for the Sports Arbitrage Scanner.

Run with: streamlit run src/sports_arb/dashboard.py

Three tabs:
  1. Live Opportunities — current edges
  2. Historical — all logged opportunities
  3. Stats — edge frequency, avg size, time-of-day patterns
"""

import sys
from pathlib import Path

# Add project root to path so imports work when run via `streamlit run`
_project_root = Path(__file__).resolve().parents[2]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

import json
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from src.sports_arb.config import DB_PATH, MIN_EDGE_PCT, SPORT_LABELS
from src.sports_arb.database import ArbDatabase

# ── Page config ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Sports Arb Scanner",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Dark theme CSS
st.markdown("""
<style>
    .stApp { background-color: #0e1117; }
    .edge-green { color: #00ff88; font-weight: bold; }
    .edge-yellow { color: #ffcc00; font-weight: bold; }
    .edge-red { color: #ff4444; }
    .metric-card {
        background: #1a1a2e;
        border-radius: 10px;
        padding: 20px;
        text-align: center;
    }
</style>
""", unsafe_allow_html=True)


def color_edge(val: float) -> str:
    """Color-code edge percentages."""
    if isinstance(val, (int, float)):
        if val >= 0.05:
            return "color: #00ff88; font-weight: bold"
        elif val >= 0.04:
            return "color: #ffcc00; font-weight: bold"
        elif val > 0:
            return "color: #ff4444"
    return ""


def load_data() -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Load opportunities, snapshots, and stats from database."""
    db = ArbDatabase()

    opps = db.get_recent_opportunities(limit=500)
    snapshots = db.get_all_snapshots(limit=200)
    stats = db.get_stats()

    df_opps = pd.DataFrame(opps) if opps else pd.DataFrame()
    df_snaps = pd.DataFrame(snapshots) if snapshots else pd.DataFrame()

    return df_opps, df_snaps, stats


def render_live_tab(df: pd.DataFrame) -> None:
    """Tab 1: Live opportunities table."""
    st.header("🔴 Live Edge Opportunities")

    if df.empty:
        st.info(
            "No opportunities detected yet. Run the scanner first:\n\n"
            "```bash\npython -m src.sports_arb.scanner --once\n```"
        )
        return

    # Filters
    col1, col2, col3 = st.columns(3)
    with col1:
        sport_filter = st.selectbox(
            "Sport",
            ["All"] + sorted(df["sport"].unique().tolist()) if "sport" in df.columns else ["All"],
        )
    with col2:
        min_edge = st.slider("Min Edge %", 0.0, 15.0, MIN_EDGE_PCT * 100, 0.5)
    with col3:
        market_filter = st.selectbox(
            "Market Type",
            ["All"] + sorted(df["market_type"].unique().tolist()) if "market_type" in df.columns else ["All"],
        )

    # Apply filters
    filtered = df.copy()
    if sport_filter != "All":
        filtered = filtered[filtered["sport"] == sport_filter]
    if "edge_pct" in filtered.columns:
        filtered = filtered[filtered["edge_pct"] >= min_edge / 100]
    if market_filter != "All":
        filtered = filtered[filtered["market_type"] == market_filter]

    # Format for display
    if not filtered.empty and "edge_pct" in filtered.columns:
        display_df = filtered[[
            "sport", "game", "market_type", "line", "pm_side",
            "pm_price", "sharp_no_vig_prob", "edge_pct", "edge_after_costs",
            "pm_liquidity", "pm_volume", "match_confidence", "timestamp",
        ]].copy()

        display_df["edge_pct"] = display_df["edge_pct"].apply(lambda x: f"{x:.1%}")
        display_df["edge_after_costs"] = display_df["edge_after_costs"].apply(lambda x: f"{x:.1%}")
        display_df["pm_price"] = display_df["pm_price"].apply(lambda x: f"{x:.3f}")
        display_df["sharp_no_vig_prob"] = display_df["sharp_no_vig_prob"].apply(lambda x: f"{x:.3f}")
        display_df["pm_liquidity"] = display_df["pm_liquidity"].apply(lambda x: f"${x:,.0f}")
        display_df["pm_volume"] = display_df["pm_volume"].apply(lambda x: f"${x:,.0f}")

        display_df.columns = [
            "Sport", "Game", "Type", "Line", "PM Side",
            "PM Price", "Sharp Prob", "Edge %", "Net Edge",
            "Liquidity", "Volume", "Conf %", "Time",
        ]

        st.dataframe(display_df, use_container_width=True, hide_index=True)
        st.caption(f"{len(filtered)} opportunities shown")
    else:
        st.info("No opportunities match the current filters.")


def render_historical_tab(df: pd.DataFrame) -> None:
    """Tab 2: Historical opportunities with outcome tracking."""
    st.header("📜 Historical Opportunities")

    if df.empty:
        st.info("No historical data yet. Run the scanner to start collecting data.")
        return

    # Sort by timestamp descending
    if "timestamp" in df.columns:
        df_sorted = df.sort_values("timestamp", ascending=False)
    else:
        df_sorted = df

    st.dataframe(df_sorted, use_container_width=True, hide_index=True)
    st.caption(f"{len(df_sorted)} total opportunities logged")


def render_stats_tab(stats: dict, df: pd.DataFrame) -> None:
    """Tab 3: Stats and analytics."""
    st.header("📈 Scanner Statistics")

    # Top-level metrics
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Total Scans", f"{stats.get('total_scans', 0):,}")
    with col2:
        st.metric("Opportunities Found", f"{stats.get('total_opportunities', 0):,}")
    with col3:
        avg_edge = stats.get("avg_edge_pct", 0) or 0
        st.metric("Avg Edge", f"{avg_edge:.1%}")
    with col4:
        if not df.empty and "edge_pct" in df.columns:
            max_edge = df["edge_pct"].max()
            st.metric("Max Edge", f"{max_edge:.1%}")
        else:
            st.metric("Max Edge", "—")

    st.divider()

    # Edge frequency by sport
    if stats.get("by_sport"):
        st.subheader("Edges by Sport")
        sport_df = pd.DataFrame(stats["by_sport"])
        if not sport_df.empty:
            sport_df["avg_edge"] = sport_df["avg_edge"].apply(lambda x: f"{x:.1%}" if x else "—")
            sport_df.columns = ["Sport", "Count", "Avg Edge"]
            st.dataframe(sport_df, use_container_width=True, hide_index=True)

    # Time-of-day distribution
    if stats.get("by_hour"):
        st.subheader("Edge Frequency by Hour (UTC)")
        hour_df = pd.DataFrame(stats["by_hour"])
        if not hour_df.empty:
            st.bar_chart(hour_df.set_index("hour")["count"])

    # Liquidity distribution
    if not df.empty and "pm_liquidity" in df.columns:
        st.subheader("Liquidity Distribution")
        st.bar_chart(df["pm_liquidity"].dropna())

    # Edge distribution histogram
    if not df.empty and "edge_pct" in df.columns:
        st.subheader("Edge Size Distribution")
        st.bar_chart(df["edge_pct"].dropna())


def main() -> None:
    """Main dashboard entry point."""
    st.title("📊 Sports Arbitrage Scanner")
    st.caption("Phase 1 — Detection Only | The Outsiders")

    # Auto-refresh
    auto_refresh = st.sidebar.checkbox("Auto-refresh (30s)", value=False)
    if auto_refresh:
        st.sidebar.info("Dashboard refreshes every 30 seconds")
        # Streamlit doesn't have built-in auto-refresh, use st.rerun with fragment
        import time
        time.sleep(0.1)  # Avoid immediate rerun

    # Load data
    df_opps, df_snaps, stats = load_data()

    # Tabs
    tab1, tab2, tab3 = st.tabs(["🔴 Live Opportunities", "📜 Historical", "📈 Stats"])

    with tab1:
        render_live_tab(df_opps)

    with tab2:
        render_historical_tab(df_opps)

    with tab3:
        render_stats_tab(stats, df_opps)

    # Footer
    st.divider()
    st.caption(
        "⚠️ **Legal Disclaimer:** User must confirm WA state compliance before live trading. "
        "This tool is for informational purposes only. Not financial advice."
    )

    # Auto-refresh via rerun
    if auto_refresh:
        import time
        time.sleep(30)
        st.rerun()


if __name__ == "__main__":
    main()
