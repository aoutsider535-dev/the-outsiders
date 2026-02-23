"""
🏞 The Outsiders — Trading Dashboard v4
Dual-tab: LIVE (bright modern) + Paper (dark classic)
"""
import streamlit as st
import plotly.graph_objects as go
import pandas as pd
import sys
import os
import json
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from src.database import get_connection, init_db, get_trades

PST = timezone(timedelta(hours=-8))
LIVE_STARTING_BALANCE = 105.16  # Actual starting deposit on Polymarket
PAPER_STARTING_BALANCE = 1000.0


def get_live_balance():
    """Fetch real USDC balance from Polymarket CLOB API."""
    try:
        from dotenv import dotenv_values
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import BalanceAllowanceParams
        env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
        config = dotenv_values(env_path)
        pk = config.get("POLYGON_PRIVATE_KEY", "")
        addr = config.get("POLYGON_WALLET_ADDRESS", "")
        if not pk or not addr:
            return None
        host = "https://clob.polymarket.com"
        client = ClobClient(host, key=pk, chain_id=137)
        creds = client.create_or_derive_api_creds()
        client = ClobClient(host, key=pk, chain_id=137, creds=creds,
                           signature_type=1, funder=addr)
        bal = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type="COLLATERAL")
        )
        raw = int(bal.get("balance", 0))
        return raw / 1e6
    except Exception:
        return None

STRATEGY_COLORS = {
    "btc_5min_momentum_LIVE": "#00ff88",
    "btc_5min_meanrev_LIVE": "#ffb347",
    "btc_5min_ob_imbalance_LIVE": "#a29bfe",
    "btc_5min_smart_money_LIVE": "#fd79a8",
    "btc_5min_momentum": "#00d4aa",
    "btc_5min_v3_paper": "#00d4aa",
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
    "btc_5min_v3_paper": "⚡ Momentum (v1)",
    "btc_5min_meanrev": "🔄 Mean Reversion",
    "btc_5min_ob_imbalance": "📊 OB Imbalance",
    "btc_5min_smart_money": "🧠 Smart Money",
}

st.set_page_config(
    page_title="🏞 The Outsiders",
    page_icon="🏞",
    layout="wide",
    initial_sidebar_state="collapsed"
)

init_db()


def to_pst(ts):
    if pd.isna(ts) or ts is None:
        return ""
    try:
        dt = datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(PST)
        return dt.strftime("%b %d, %I:%M:%S %p")
    except:
        return str(ts)


def strategy_label(name):
    return STRATEGY_LABELS.get(name, name)


def strategy_color(name):
    return STRATEGY_COLORS.get(name, "#8892b0")


def check_trader_running(name="paper_trader"):
    try:
        import subprocess
        result = subprocess.run(["pgrep", "-f", name], capture_output=True, text=True)
        return result.returncode == 0
    except:
        return False


def load_trades_filtered(is_live=False, limit=500):
    conn = get_connection()
    if is_live:
        query = "SELECT * FROM trades WHERE strategy LIKE '%_LIVE' ORDER BY timestamp DESC LIMIT ?"
    else:
        query = "SELECT * FROM trades WHERE strategy NOT LIKE '%_LIVE' ORDER BY timestamp DESC LIMIT ?"
    rows = conn.execute(query, (limit,)).fetchall()
    conn.close()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([dict(r) for r in rows])
    if "timestamp" in df.columns:
        df["time_utc"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
        df["time_pst"] = df["time_utc"].dt.tz_convert("US/Pacific")
        df["time_display"] = df["timestamp"].apply(to_pst)
    if "signal_data" in df.columns:
        df["signal_data"] = df["signal_data"].apply(
            lambda x: json.loads(x) if isinstance(x, str) and x else {}
        )
    return df


def load_equity(is_live=False, starting_balance=1000.0):
    conn = get_connection()
    if is_live:
        query = "SELECT timestamp, pnl, strategy, exit_reason FROM trades WHERE status='closed' AND strategy LIKE '%_LIVE' ORDER BY timestamp"
    else:
        query = "SELECT timestamp, pnl, strategy, exit_reason FROM trades WHERE status='closed' AND strategy NOT LIKE '%_LIVE' AND exit_reason LIKE '%_real' ORDER BY timestamp"
    rows = conn.execute(query).fetchall()
    conn.close()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([dict(r) for r in rows])
    df["cumulative_pnl"] = df["pnl"].cumsum()
    df["time_pst"] = pd.to_datetime(df["timestamp"], unit="s", utc=True).dt.tz_convert("US/Pacific")
    df["balance"] = starting_balance + df["cumulative_pnl"]
    df["strategy_label"] = df["strategy"].apply(strategy_label)
    return df


# ─── LIVE TAB CSS ───
LIVE_CSS = """
<style>
    @import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@300;400;500;600;700&display=swap');
    
    .live-header {
        background: linear-gradient(135deg, #0f0c29 0%, #302b63 50%, #24243e 100%);
        border-radius: 20px;
        padding: 30px;
        margin-bottom: 20px;
        border: 1px solid rgba(255,255,255,0.1);
        box-shadow: 0 8px 32px rgba(0,255,136,0.1);
    }
    
    .live-title {
        font-family: 'Space Grotesk', sans-serif;
        font-size: 2.5rem;
        font-weight: 700;
        background: linear-gradient(135deg, #00ff88 0%, #00d4aa 50%, #a29bfe 100%);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        letter-spacing: -0.02em;
    }
    
    .live-subtitle {
        font-family: 'Space Grotesk', sans-serif;
        color: #a0a0c0;
        font-size: 1.1rem;
        font-weight: 300;
        margin-top: -5px;
    }
    
    .live-badge {
        display: inline-block;
        background: linear-gradient(135deg, #00ff88, #00d4aa);
        color: #000;
        padding: 6px 16px;
        border-radius: 25px;
        font-size: 0.8rem;
        font-weight: 700;
        letter-spacing: 0.1em;
        text-transform: uppercase;
        animation: glow 2s ease-in-out infinite;
    }
    
    @keyframes glow {
        0%, 100% { box-shadow: 0 0 10px rgba(0,255,136,0.3); }
        50% { box-shadow: 0 0 25px rgba(0,255,136,0.6); }
    }
    
    .live-metric {
        background: linear-gradient(135deg, #1a1a3e 0%, #2d2b55 100%);
        border: 1px solid rgba(0,255,136,0.15);
        border-radius: 16px;
        padding: 20px;
        box-shadow: 0 4px 20px rgba(0,0,0,0.3);
    }
    
    .live-metric-value {
        font-family: 'Space Grotesk', sans-serif;
        font-size: 2rem;
        font-weight: 700;
        color: #fff;
    }
    
    .live-metric-label {
        font-family: 'Space Grotesk', sans-serif;
        color: #a0a0c0;
        font-size: 0.8rem;
        text-transform: uppercase;
        letter-spacing: 0.08em;
    }
    
    .win-text { color: #00ff88 !important; }
    .loss-text { color: #ff6b6b !important; }
    
    .strategy-card {
        background: linear-gradient(135deg, #1a1a3e 0%, #2d2b55 100%);
        border-radius: 16px;
        padding: 18px;
        border: 1px solid rgba(255,255,255,0.08);
        margin-bottom: 8px;
    }
    
    .trade-row {
        font-family: 'Space Grotesk', monospace;
        padding: 10px 14px;
        border-radius: 10px;
        margin: 4px 0;
        font-size: 0.9rem;
    }
    
    .trade-win {
        background: rgba(0,255,136,0.08);
        border-left: 3px solid #00ff88;
        color: #e0e0ff;
    }
    
    .trade-loss {
        background: rgba(255,107,107,0.08);
        border-left: 3px solid #ff6b6b;
        color: #e0e0ff;
    }
    
    .trade-open {
        background: rgba(162,155,254,0.08);
        border-left: 3px solid #a29bfe;
        color: #e0e0ff;
    }
</style>
"""

# ─── PAPER TAB CSS ───
PAPER_CSS = """
<style>
    div[data-testid="stMetric"] {
        background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
        border: 1px solid rgba(255,255,255,0.08);
        border-radius: 16px;
        padding: 20px;
        box-shadow: 0 4px 20px rgba(0,0,0,0.3);
    }
    div[data-testid="stMetric"] label {
        color: #8892b0 !important;
        font-size: 0.85rem !important;
        text-transform: uppercase;
        letter-spacing: 0.05em;
    }
    div[data-testid="stMetric"] [data-testid="stMetricValue"] {
        font-size: 1.8rem !important;
        font-weight: 700 !important;
    }
</style>
"""

# ─── MAIN LAYOUT ───
tab_live, tab_paper = st.tabs(["💰 LIVE TRADING", "📝 Paper Trading"])

# ════════════════════════════════════════════
# 💰 LIVE TAB
# ════════════════════════════════════════════
with tab_live:
    st.markdown(LIVE_CSS, unsafe_allow_html=True)
    
    # Header
    is_live = check_trader_running("live_trader")
    now_pst = datetime.now(timezone.utc).astimezone(PST).strftime("%I:%M %p PST")
    
    badge = '<span class="live-badge">● LIVE</span>' if is_live else '<span style="background:#ff6b6b;color:#fff;padding:6px 16px;border-radius:25px;font-size:0.8rem;font-weight:700;">● OFFLINE</span>'
    
    st.markdown(f"""
    <div class="live-header">
        <div style="display:flex;justify-content:space-between;align-items:center;">
            <div>
                <div class="live-title">🏞 The Outsiders</div>
                <div class="live-subtitle">Real money. Real edge. Not insiders — just smarter.</div>
            </div>
            <div style="text-align:right;">
                {badge}<br>
                <span style="color:#a0a0c0;font-size:0.8rem;">{now_pst}</span>
            </div>
        </div>
    </div>
    """, unsafe_allow_html=True)
    
    # Load live data
    df_live = load_trades_filtered(is_live=True)
    
    if df_live.empty:
        st.markdown("""
        <div style="text-align:center;padding:60px 0;">
            <h2 style="color:#a0a0c0;">🚀 No live trades yet</h2>
            <p style="color:#606080;">The live trader is warming up...</p>
        </div>
        """, unsafe_allow_html=True)
    else:
        closed_live = df_live[df_live["status"] == "closed"] if "status" in df_live.columns else df_live
        open_live = df_live[df_live["status"] == "open"] if "status" in df_live.columns else pd.DataFrame()
        
        total_trades = len(closed_live)
        wins = len(closed_live[closed_live["pnl"] > 0]) if "pnl" in closed_live.columns and total_trades > 0 else 0
        losses = total_trades - wins
        win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
        # Use real Polymarket balance if available, fall back to DB calculation
        real_balance = get_live_balance()
        if real_balance is not None:
            balance = real_balance
            total_pnl = balance - LIVE_STARTING_BALANCE
        else:
            total_pnl = closed_live["pnl"].sum() if "pnl" in closed_live.columns and total_trades > 0 else 0
            balance = LIVE_STARTING_BALANCE + total_pnl
        roi = ((balance - LIVE_STARTING_BALANCE) / LIVE_STARTING_BALANCE) * 100
        
        # KPI Row
        k1, k2, k3, k4, k5, k6 = st.columns(6)
        
        pnl_color = "win-text" if total_pnl >= 0 else "loss-text"
        
        k1.markdown(f"""
        <div class="live-metric">
            <div class="live-metric-label">Balance</div>
            <div class="live-metric-value {'win-text' if balance >= LIVE_STARTING_BALANCE else 'loss-text'}">${balance:,.2f}</div>
            <div style="color:#a0a0c0;font-size:0.8rem;">{roi:+.1f}% ROI</div>
        </div>
        """, unsafe_allow_html=True)
        
        k2.markdown(f"""
        <div class="live-metric">
            <div class="live-metric-label">Total P&L</div>
            <div class="live-metric-value {pnl_color}">${total_pnl:+,.2f}</div>
            <div style="color:#a0a0c0;font-size:0.8rem;">from ${LIVE_STARTING_BALANCE:.0f} start</div>
        </div>
        """, unsafe_allow_html=True)
        
        k3.markdown(f"""
        <div class="live-metric">
            <div class="live-metric-label">Record</div>
            <div class="live-metric-value">{wins}W / {losses}L</div>
            <div style="color:#a0a0c0;font-size:0.8rem;">{total_trades} total trades</div>
        </div>
        """, unsafe_allow_html=True)
        
        k4.markdown(f"""
        <div class="live-metric">
            <div class="live-metric-label">Win Rate</div>
            <div class="live-metric-value {'win-text' if win_rate >= 50 else 'loss-text'}">{win_rate:.1f}%</div>
            <div style="color:#a0a0c0;font-size:0.8rem;">target: 50%+</div>
        </div>
        """, unsafe_allow_html=True)
        
        k5.markdown(f"""
        <div class="live-metric">
            <div class="live-metric-label">Open Positions</div>
            <div class="live-metric-value" style="color:#a29bfe;">{len(open_live)}</div>
            <div style="color:#a0a0c0;font-size:0.8rem;">awaiting resolution</div>
        </div>
        """, unsafe_allow_html=True)
        
        active_strats = len(set(open_live["strategy"])) if not open_live.empty and "strategy" in open_live.columns else 0
        k6.markdown(f"""
        <div class="live-metric">
            <div class="live-metric-label">Active Strategies</div>
            <div class="live-metric-value" style="color:#ffb347;">{active_strats}/4</div>
            <div style="color:#a0a0c0;font-size:0.8rem;">trading now</div>
        </div>
        """, unsafe_allow_html=True)
        
        st.markdown("<br>", unsafe_allow_html=True)
        
        # Strategy Comparison
        if total_trades > 0:
            st.markdown("### 🏆 Strategy Performance")
            strat_names = closed_live["strategy"].unique()
            cols = st.columns(len(strat_names)) if len(strat_names) > 0 else []
            
            for i, sname in enumerate(strat_names):
                s_df = closed_live[closed_live["strategy"] == sname]
                s_trades = len(s_df)
                s_wins = len(s_df[s_df["pnl"] > 0])
                s_losses = s_trades - s_wins
                s_wr = (s_wins / s_trades * 100) if s_trades > 0 else 0
                s_pnl = s_df["pnl"].sum()
                color = strategy_color(sname)
                pnl_c = "#00ff88" if s_pnl >= 0 else "#ff6b6b"
                
                with cols[i]:
                    st.markdown(f"""
                    <div class="strategy-card" style="border-top: 3px solid {color};">
                        <div style="font-size:1.1rem;font-weight:600;color:#fff;margin-bottom:8px;">
                            {strategy_label(sname)}
                        </div>
                        <div style="color:#a0a0c0;font-size:0.85rem;line-height:2;">
                            Record: <b style="color:#fff">{s_wins}W/{s_losses}L</b><br>
                            Win Rate: <b style="color:{'#00ff88' if s_wr >= 50 else '#ff6b6b'}">{s_wr:.0f}%</b><br>
                            P&L: <b style="color:{pnl_c}">${s_pnl:+.2f}</b>
                        </div>
                    </div>
                    """, unsafe_allow_html=True)
        
        st.markdown("<br>", unsafe_allow_html=True)
        
        # Equity Curve
        equity = load_equity(is_live=True, starting_balance=LIVE_STARTING_BALANCE)
        if not equity.empty:
            fig = go.Figure()
            
            # Combined line
            fig.add_trace(go.Scatter(
                x=equity["time_pst"], y=equity["balance"],
                mode="lines+markers", name="Balance",
                line=dict(color="#00ff88", width=3),
                marker=dict(size=6, color=["#00ff88" if p > 0 else "#ff6b6b" for p in equity["pnl"]]),
                fill="tozeroy",
                fillcolor="rgba(0,255,136,0.06)",
            ))
            
            fig.add_hline(y=LIVE_STARTING_BALANCE, line_dash="dot", 
                         line_color="rgba(255,255,255,0.2)",
                         annotation_text=f"Start ${LIVE_STARTING_BALANCE:.0f}",
                         annotation_position="bottom right",
                         annotation_font_color="rgba(255,255,255,0.4)")
            
            fig.update_layout(
                title=dict(text="💰 Live Equity Curve", font=dict(size=20, color="#fff", family="Space Grotesk")),
                xaxis=dict(title="", gridcolor="rgba(255,255,255,0.03)", tickfont=dict(color="#a0a0c0")),
                yaxis=dict(title="Balance ($)", tickprefix="$", gridcolor="rgba(255,255,255,0.03)",
                          tickfont=dict(color="#a0a0c0"), title_font=dict(color="#a0a0c0")),
                template="plotly_dark",
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(15,12,41,0.5)",
                height=400, margin=dict(l=60, r=20, t=50, b=40),
                showlegend=False,
            )
            st.plotly_chart(fig, use_container_width=True)
        
        # Recent Trades
        st.markdown("### 📋 Live Trade History")
        
        all_live = df_live.head(50).copy()
        for _, trade in all_live.iterrows():
            direction = str(trade.get("direction", "?")).upper()
            entry = trade.get("entry_price", 0)
            pnl = trade.get("pnl")
            pnl_pct = trade.get("pnl_pct")
            status = trade.get("status", "")
            strat = trade.get("strategy", "")
            time_str = to_pst(trade.get("timestamp"))
            edge = trade.get("edge_pct", 0)
            
            emoji_dir = "🟢" if direction == "UP" else "🔴"
            color = strategy_color(strat)
            
            if status == "closed" and pnl is not None:
                if pnl > 0:
                    css_class = "trade-win"
                    result = f"✅ WON ${pnl:+.2f} ({pnl_pct:+.0f}%)"
                else:
                    css_class = "trade-loss"
                    result = f"❌ LOST ${pnl:.2f}"
            else:
                css_class = "trade-open"
                result = "⏳ Pending"
            
            st.markdown(f"""
            <div class="trade-row {css_class}">
                <span style="color:{color};font-weight:600;">{strategy_label(strat)}</span>
                &nbsp;{emoji_dir} {direction} @ ${entry:.3f}
                &nbsp;|&nbsp; Edge: {edge:.1f}%
                &nbsp;|&nbsp; {result}
                <span style="float:right;color:#a0a0c0;font-size:0.8rem;">{time_str}</span>
            </div>
            """, unsafe_allow_html=True)
    
    # Footer
    st.markdown("---")
    now_pst = datetime.now(timezone.utc).astimezone(PST).strftime("%b %d, %Y %I:%M %p PST")
    st.markdown(
        f'<div style="text-align:center;color:#606080;font-size:0.8rem;padding:10px 0;">'
        f'🏞 The Outsiders v4 — Jakob & Austin | LIVE | {now_pst}'
        f'</div>', unsafe_allow_html=True
    )


# ════════════════════════════════════════════
# 📝 PAPER TAB
# ════════════════════════════════════════════
with tab_paper:
    st.markdown(PAPER_CSS, unsafe_allow_html=True)
    
    col_h, col_s = st.columns([4, 1])
    with col_h:
        st.markdown("## 📝 Paper Trading")
        st.markdown('<p style="color:#8892b0;font-style:italic;margin-top:-10px;">Strategy testing ground — no real money</p>', unsafe_allow_html=True)
    with col_s:
        is_paper = check_trader_running("paper_trader")
        status_class = "color:#00d4aa;" if is_paper else "color:#ff6b6b;"
        status_text = "● RUNNING" if is_paper else "● OFFLINE"
        st.markdown(f'<div style="text-align:right;padding-top:10px;"><span style="{status_class}font-weight:600;">{status_text}</span></div>', unsafe_allow_html=True)
    
    st.markdown("---")
    
    # Filters
    f1, f2, f3 = st.columns([2, 2, 4])
    with f1:
        conn = get_connection()
        strats = conn.execute("SELECT DISTINCT strategy FROM trades WHERE strategy NOT LIKE '%_LIVE' ORDER BY strategy").fetchall()
        conn.close()
        strat_options = ["All Strategies"] + [strategy_label(r["strategy"]) for r in strats]
        selected_strat = st.selectbox("Strategy", strat_options, index=0, key="paper_strat")
    with f2:
        selected_res = st.selectbox("Resolution", ["All Trades", "Real Only", "Real Paper Only"], index=2, key="paper_res")
    
    df_paper = load_trades_filtered(is_live=False)
    
    if df_paper.empty:
        st.info("No paper trades yet. Run the paper trader to see results.")
    else:
        closed_paper = df_paper[df_paper["status"] == "closed"] if "status" in df_paper.columns else df_paper
        open_paper = df_paper[df_paper["status"] == "open"] if "status" in df_paper.columns else pd.DataFrame()
        
        # Apply filters
        if selected_strat != "All Strategies" and "strategy" in closed_paper.columns:
            name_map = {v: k for k, v in STRATEGY_LABELS.items()}
            sn = name_map.get(selected_strat, selected_strat)
            closed_paper = closed_paper[closed_paper["strategy"] == sn]
        if selected_res == "Real Only" and "exit_reason" in closed_paper.columns:
            closed_paper = closed_paper[closed_paper["exit_reason"].str.contains("_real|backtest_", na=False)]
        elif selected_res == "Real Paper Only" and "exit_reason" in closed_paper.columns:
            closed_paper = closed_paper[closed_paper["exit_reason"].str.contains("_real", na=False)]
        
        total_t = len(closed_paper)
        wins_p = len(closed_paper[closed_paper["pnl"] > 0]) if "pnl" in closed_paper.columns and total_t > 0 else 0
        losses_p = total_t - wins_p
        wr_p = (wins_p / total_t * 100) if total_t > 0 else 0
        pnl_p = closed_paper["pnl"].sum() if "pnl" in closed_paper.columns and total_t > 0 else 0
        bal_p = PAPER_STARTING_BALANCE + pnl_p
        
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Balance", f"${bal_p:,.2f}", f"{((bal_p-PAPER_STARTING_BALANCE)/PAPER_STARTING_BALANCE*100):+.1f}%")
        c2.metric("Total P&L", f"${pnl_p:+,.2f}")
        c3.metric("Trades", f"{total_t}", f"{wins_p}W / {losses_p}L")
        c4.metric("Win Rate", f"{wr_p:.1f}%")
        c5.metric("Open", f"{len(open_paper)}")
        
        st.markdown("---")
        
        # Strategy comparison
        if total_t > 0 and selected_strat == "All Strategies":
            st.markdown("### 🏆 Strategy Comparison")
            comp_cols = st.columns(min(len(closed_paper["strategy"].unique()), 5))
            
            for i, sname in enumerate(closed_paper["strategy"].unique()):
                s = closed_paper[closed_paper["strategy"] == sname]
                st_ = len(s)
                sw = len(s[s["pnl"] > 0])
                sl = st_ - sw
                swr = (sw / st_ * 100) if st_ > 0 else 0
                sp = s["pnl"].sum()
                color = strategy_color(sname)
                
                with comp_cols[i % len(comp_cols)]:
                    st.markdown(f"""
                    <div style="background:linear-gradient(135deg,#1a1a2e,#16213e);border:1px solid rgba(255,255,255,0.08);
                        border-top:3px solid {color};border-radius:12px;padding:16px;margin-bottom:10px;">
                        <b style="color:#ccd6f6;">{strategy_label(sname)}</b><br>
                        <span style="color:#8892b0;font-size:0.85rem;line-height:1.8;">
                        Record: <b style="color:#ccd6f6">{sw}W/{sl}L</b><br>
                        WR: <b style="color:{'#00d4aa' if swr>=50 else '#ff6b6b'}">{swr:.0f}%</b><br>
                        P&L: <b style="color:{'#00d4aa' if sp>=0 else '#ff6b6b'}">${sp:+.2f}</b>
                        </span>
                    </div>
                    """, unsafe_allow_html=True)
        
        # Equity curve
        equity_p = load_equity(is_live=False, starting_balance=PAPER_STARTING_BALANCE)
        if not equity_p.empty:
            fig_p = go.Figure()
            fig_p.add_trace(go.Scatter(
                x=equity_p["time_pst"], y=equity_p["balance"],
                mode="lines", name="Balance",
                line=dict(color="#00d4aa", width=2.5),
                fill="tozeroy", fillcolor="rgba(0,212,170,0.08)",
            ))
            fig_p.add_hline(y=PAPER_STARTING_BALANCE, line_dash="dot", line_color="rgba(255,255,255,0.15)")
            fig_p.update_layout(
                title=dict(text="Paper Equity Curve", font=dict(size=18, color="#ccd6f6")),
                xaxis=dict(title="", gridcolor="rgba(255,255,255,0.03)", tickfont=dict(color="#8892b0")),
                yaxis=dict(title="Balance ($)", tickprefix="$", gridcolor="rgba(255,255,255,0.03)",
                          tickfont=dict(color="#8892b0"), title_font=dict(color="#8892b0")),
                template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(10,10,26,0.5)", height=380,
                margin=dict(l=60, r=20, t=50, b=40), showlegend=False,
            )
            st.plotly_chart(fig_p, use_container_width=True)
        
        # Recent trades table
        st.markdown("### 📋 Recent Paper Trades")
        if not closed_paper.empty:
            display = closed_paper.head(30).copy()
            td = pd.DataFrame()
            if "time_display" in display.columns:
                td["Time (PST)"] = display["time_display"]
            if "strategy" in display.columns:
                td["Strategy"] = display["strategy"].apply(strategy_label)
            if "direction" in display.columns:
                td["Direction"] = display["direction"].apply(lambda x: f"🟢 {x.upper()}" if x == "up" else f"🔴 {x.upper()}")
            if "entry_price" in display.columns:
                td["Entry"] = display["entry_price"].apply(lambda x: f"${x:.3f}" if pd.notna(x) else "")
            if "pnl" in display.columns:
                td["P&L"] = display["pnl"].apply(lambda x: f"${x:+.2f}" if pd.notna(x) else "")
            if "exit_reason" in display.columns:
                td["Result"] = display["exit_reason"].apply(
                    lambda x: "✅ Win" if "win" in str(x) else "❌ Loss" if "loss" in str(x) else str(x) if pd.notna(x) else "")
            st.dataframe(td, use_container_width=True, hide_index=True, height=400)
    
    st.markdown("---")
    now_pst = datetime.now(timezone.utc).astimezone(PST).strftime("%b %d, %Y %I:%M %p PST")
    st.markdown(
        f'<div style="text-align:center;color:#495670;font-size:0.8rem;padding:10px 0;">'
        f'🏞 The Outsiders v4 — Paper Trading | {now_pst}'
        f'</div>', unsafe_allow_html=True
    )
