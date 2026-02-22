"""
🏞 The Outsiders — Trading Dashboard v2
Modern dark theme, PST timezone, real-time paper trading view.
"""
import streamlit as st
import plotly.graph_objects as go
import plotly.express as px
import pandas as pd
import sys
import os
import json
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from src.database import get_connection, init_db, get_trades, get_performance_summary

PST = timezone(timedelta(hours=-8))

st.set_page_config(
    page_title="🏞 The Outsiders",
    page_icon="🏞",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# Custom CSS for modern dark look
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');
    
    .stApp {
        font-family: 'Inter', sans-serif;
    }
    
    /* Dark card style */
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
        font-weight: 500 !important;
        text-transform: uppercase;
        letter-spacing: 0.05em;
    }
    
    div[data-testid="stMetric"] [data-testid="stMetricValue"] {
        font-size: 1.8rem !important;
        font-weight: 700 !important;
    }
    
    /* Header styling */
    h1 {
        font-weight: 700 !important;
        letter-spacing: -0.02em !important;
    }
    
    /* Sidebar */
    section[data-testid="stSidebar"] {
        background: #0a0a1a;
        border-right: 1px solid rgba(255,255,255,0.05);
    }
    
    /* Tables */
    .stDataFrame {
        border-radius: 12px;
        overflow: hidden;
    }
    
    /* Status pill */
    .status-live {
        display: inline-block;
        background: #00d4aa;
        color: #000;
        padding: 4px 12px;
        border-radius: 20px;
        font-size: 0.75rem;
        font-weight: 600;
        letter-spacing: 0.05em;
        animation: pulse 2s infinite;
    }
    
    @keyframes pulse {
        0%, 100% { opacity: 1; }
        50% { opacity: 0.7; }
    }
    
    .status-offline {
        display: inline-block;
        background: #ff6b6b;
        color: #fff;
        padding: 4px 12px;
        border-radius: 20px;
        font-size: 0.75rem;
        font-weight: 600;
    }
    
    /* Divider */
    hr {
        border-color: rgba(255,255,255,0.06) !important;
        margin: 1.5rem 0 !important;
    }
    
    .outsiders-tagline {
        color: #8892b0;
        font-size: 1rem;
        font-weight: 300;
        font-style: italic;
        margin-top: -10px;
    }
    
    .trade-ticker {
        font-family: 'SF Mono', 'Fira Code', monospace;
        font-size: 0.85rem;
        color: #ccd6f6;
        background: rgba(255,255,255,0.03);
        padding: 8px 14px;
        border-radius: 8px;
        border-left: 3px solid #00d4aa;
        margin: 4px 0;
    }
    
    .trade-ticker.loss {
        border-left-color: #ff6b6b;
    }
</style>
""", unsafe_allow_html=True)

init_db()


def to_pst(ts):
    """Convert unix timestamp to PST datetime string."""
    if pd.isna(ts) or ts is None:
        return ""
    try:
        dt = datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(PST)
        return dt.strftime("%b %d, %I:%M:%S %p PST")
    except:
        return str(ts)


def to_pst_short(ts):
    """Short PST format for charts."""
    if pd.isna(ts) or ts is None:
        return ""
    try:
        dt = datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(PST)
        return dt.strftime("%I:%M %p")
    except:
        return str(ts)


def load_trades(limit=500):
    trades = get_trades(limit=limit)
    if not trades:
        return pd.DataFrame()
    df = pd.DataFrame(trades)
    if "timestamp" in df.columns:
        df["time_utc"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
        df["time_pst"] = df["time_utc"].dt.tz_convert("US/Pacific")
        df["time_display"] = df["timestamp"].apply(to_pst)
    if "signal_data" in df.columns:
        df["signal_data"] = df["signal_data"].apply(
            lambda x: json.loads(x) if isinstance(x, str) and x else {}
        )
    return df


def load_equity_curve():
    conn = get_connection()
    rows = conn.execute("""
        SELECT timestamp, pnl,
               SUM(pnl) OVER (ORDER BY timestamp) as cumulative_pnl
        FROM trades WHERE status='closed'
        ORDER BY timestamp
    """).fetchall()
    conn.close()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([dict(r) for r in rows])
    df["time_pst"] = pd.to_datetime(df["timestamp"], unit="s", utc=True).dt.tz_convert("US/Pacific")
    df["balance"] = 1000 + df["cumulative_pnl"]
    return df


def check_paper_trader_running():
    """Check if paper trader process is alive."""
    try:
        import subprocess
        result = subprocess.run(["pgrep", "-f", "paper_trader"], capture_output=True, text=True)
        return result.returncode == 0
    except:
        return False


# ─── HEADER ───
col_header, col_status = st.columns([4, 1])
with col_header:
    st.markdown("# 🏞 The Outsiders")
    st.markdown('<p class="outsiders-tagline">Not insiders. Just smarter.</p>', unsafe_allow_html=True)

with col_status:
    is_live = check_paper_trader_running()
    now_pst = datetime.now(timezone.utc).astimezone(PST).strftime("%I:%M %p PST")
    if is_live:
        st.markdown(f'<div style="text-align:right;padding-top:20px;">'
                    f'<span class="status-live">● LIVE</span><br>'
                    f'<span style="color:#8892b0;font-size:0.8rem;">{now_pst}</span>'
                    f'</div>', unsafe_allow_html=True)
    else:
        st.markdown(f'<div style="text-align:right;padding-top:20px;">'
                    f'<span class="status-offline">● OFFLINE</span><br>'
                    f'<span style="color:#8892b0;font-size:0.8rem;">{now_pst}</span>'
                    f'</div>', unsafe_allow_html=True)

st.markdown("---")

# Load data
df = load_trades()

if df.empty:
    st.markdown("""
    <div style="text-align:center;padding:60px 0;">
        <h2 style="color:#8892b0;">📭 No trades yet</h2>
        <p style="color:#495670;">Run the paper trader or backtester to see results here.</p>
        <code style="background:#1a1a2e;padding:10px 20px;border-radius:8px;color:#00d4aa;">
        python3 -m src.paper_trader
        </code>
    </div>
    """, unsafe_allow_html=True)
    st.stop()

closed = df[df["status"] == "closed"] if "status" in df.columns else df
open_trades = df[df["status"] == "open"] if "status" in df.columns else pd.DataFrame()

# ─── KPI ROW ───
total_trades = len(closed)
wins = len(closed[closed["pnl"] > 0]) if "pnl" in closed.columns and total_trades > 0 else 0
losses = total_trades - wins
win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
total_pnl = closed["pnl"].sum() if "pnl" in closed.columns and total_trades > 0 else 0
avg_edge = closed["edge_pct"].mean() if "edge_pct" in closed.columns and not closed["edge_pct"].isna().all() else 0
balance = 1000 + total_pnl

col1, col2, col3, col4, col5, col6 = st.columns(6)

col1.metric("Balance", f"${balance:,.2f}", f"{((balance-1000)/1000*100):+.1f}%")
col2.metric("Total P&L", f"${total_pnl:+,.2f}")
col3.metric("Trades", f"{total_trades}", f"{wins}W / {losses}L")
col4.metric("Win Rate", f"{win_rate:.1f}%")
col5.metric("Avg Edge", f"{avg_edge:.1f}%")

# Live trade indicator
if not open_trades.empty:
    latest = open_trades.iloc[0]
    direction = latest.get("direction", "?").upper()
    col6.metric("Open Trade", f"📍 {direction}", f"@ ${latest.get('entry_price', 0):.3f}")
else:
    col6.metric("Open Trade", "None", "Waiting for edge")

st.markdown("---")

# ─── EQUITY CURVE ───
equity = load_equity_curve()
if not equity.empty:
    fig_equity = go.Figure()
    
    # Add gradient area
    fig_equity.add_trace(go.Scatter(
        x=equity["time_pst"], y=equity["balance"],
        mode="lines",
        name="Balance",
        line=dict(color="#00d4aa", width=2.5),
        fill="tozeroy",
        fillcolor="rgba(0,212,170,0.08)",
    ))
    
    # Add starting balance reference line
    fig_equity.add_hline(y=1000, line_dash="dot", line_color="rgba(255,255,255,0.15)",
                         annotation_text="Starting $1,000", annotation_position="bottom right",
                         annotation_font_color="rgba(255,255,255,0.3)")
    
    fig_equity.update_layout(
        title=dict(text="Equity Curve", font=dict(size=18, color="#ccd6f6")),
        xaxis=dict(
            title="", gridcolor="rgba(255,255,255,0.03)",
            tickfont=dict(color="#8892b0"),
        ),
        yaxis=dict(
            title="Balance ($)", tickprefix="$", gridcolor="rgba(255,255,255,0.03)",
            tickfont=dict(color="#8892b0"), title_font=dict(color="#8892b0"),
        ),
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(10,10,26,0.5)",
        height=380,
        margin=dict(l=60, r=20, t=50, b=40),
        showlegend=False,
    )
    st.plotly_chart(fig_equity, use_container_width=True)

# ─── CHARTS ROW ───
col_left, col_mid, col_right = st.columns(3)

with col_left:
    if "pnl_pct" in closed.columns and total_trades > 0:
        colors = ["#00d4aa" if x > 0 else "#ff6b6b" for x in closed["pnl_pct"].fillna(0)]
        fig_pnl = go.Figure(go.Bar(
            x=list(range(len(closed))),
            y=closed["pnl_pct"].fillna(0),
            marker_color=colors,
            opacity=0.85,
        ))
        fig_pnl.update_layout(
            title=dict(text="Trade Returns (%)", font=dict(size=14, color="#ccd6f6")),
            xaxis=dict(title="Trade #", gridcolor="rgba(255,255,255,0.03)", tickfont=dict(color="#8892b0")),
            yaxis=dict(title="P&L %", gridcolor="rgba(255,255,255,0.03)", tickfont=dict(color="#8892b0"),
                      ticksuffix="%"),
            template="plotly_dark",
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(10,10,26,0.5)",
            height=280,
            margin=dict(l=50, r=10, t=40, b=40),
            showlegend=False,
        )
        st.plotly_chart(fig_pnl, use_container_width=True)

with col_mid:
    if "direction" in closed.columns and total_trades > 0:
        dir_counts = closed["direction"].value_counts()
        fig_dir = go.Figure(go.Pie(
            labels=dir_counts.index.str.upper(),
            values=dir_counts.values,
            hole=0.6,
            marker=dict(colors=["#00d4aa", "#ff6b6b"]),
            textfont=dict(color="#ccd6f6", size=13),
        ))
        fig_dir.update_layout(
            title=dict(text="Direction Split", font=dict(size=14, color="#ccd6f6")),
            template="plotly_dark",
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(10,10,26,0.5)",
            height=280,
            margin=dict(l=10, r=10, t=40, b=10),
            legend=dict(font=dict(color="#8892b0")),
        )
        st.plotly_chart(fig_dir, use_container_width=True)

with col_right:
    if total_trades > 0:
        fig_wr = go.Figure(go.Indicator(
            mode="gauge+number",
            value=win_rate,
            number=dict(suffix="%", font=dict(size=36, color="#ccd6f6")),
            gauge=dict(
                axis=dict(range=[0, 100], tickfont=dict(color="#8892b0")),
                bar=dict(color="#00d4aa"),
                bgcolor="rgba(255,255,255,0.03)",
                borderwidth=0,
                steps=[
                    dict(range=[0, 44], color="rgba(255,107,107,0.15)"),
                    dict(range=[44, 55], color="rgba(255,255,255,0.03)"),
                    dict(range=[55, 100], color="rgba(0,212,170,0.15)"),
                ],
                threshold=dict(line=dict(color="#ff6b6b", width=2), thickness=0.8, value=44),
            ),
        ))
        fig_wr.update_layout(
            title=dict(text="Win Rate", font=dict(size=14, color="#ccd6f6")),
            template="plotly_dark",
            paper_bgcolor="rgba(0,0,0,0)",
            height=280,
            margin=dict(l=30, r=30, t=50, b=10),
        )
        st.plotly_chart(fig_wr, use_container_width=True)

st.markdown("---")

# ─── RECENT TRADES ───
st.markdown("### 📋 Recent Trades")

if not closed.empty:
    display = closed.head(30).copy()
    
    # Build clean display dataframe
    trade_display = pd.DataFrame()
    
    if "time_display" in display.columns:
        trade_display["Time (PST)"] = display["time_display"]
    if "strategy" in display.columns:
        trade_display["Strategy"] = display["strategy"].apply(
            lambda x: "📡 Paper" if "paper" in str(x) else "🧪 Backtest" if "backtest" in str(x).lower() or "btc_5min" in str(x) else str(x)
        )
    if "direction" in display.columns:
        trade_display["Direction"] = display["direction"].apply(
            lambda x: f"🟢 {x.upper()}" if x == "up" else f"🔴 {x.upper()}"
        )
    if "entry_price" in display.columns:
        trade_display["Entry"] = display["entry_price"].apply(lambda x: f"${x:.3f}" if pd.notna(x) else "")
    if "pnl" in display.columns:
        trade_display["P&L"] = display["pnl"].apply(lambda x: f"${x:+.2f}" if pd.notna(x) else "")
    if "pnl_pct" in display.columns:
        trade_display["Return"] = display["pnl_pct"].apply(lambda x: f"{x:+.1f}%" if pd.notna(x) else "")
    if "edge_pct" in display.columns:
        trade_display["Edge"] = display["edge_pct"].apply(lambda x: f"{x:.1f}%" if pd.notna(x) else "")
    if "exit_reason" in display.columns:
        trade_display["Result"] = display["exit_reason"].apply(
            lambda x: "✅ Win" if "win" in str(x) else "❌ Loss" if "loss" in str(x) else str(x) if pd.notna(x) else ""
        )
    
    st.dataframe(trade_display, use_container_width=True, hide_index=True, height=400)

# ─── STRATEGY PARAMS ───
with st.expander("⚙️ Strategy Configuration"):
    params_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "best_params.json")
    if os.path.exists(params_path):
        with open(params_path) as f:
            config = json.load(f)
        params = config.get("params", {})
        
        p1, p2, p3, p4 = st.columns(4)
        p1.metric("Take Profit", f"{params.get('take_profit_pct', 0)}%")
        p2.metric("Stop Loss", f"{params.get('stop_loss_pct', 0)}%")
        p3.metric("Min Edge", f"{params.get('min_edge_pct', 0)}%")
        p4.metric("Risk/Trade", f"{params.get('risk_per_trade_pct', 0)}%")
        
        st.markdown("**Signal Weights:**")
        w1, w2, w3, w4, w5 = st.columns(5)
        w1.markdown(f"Momentum: **{params.get('w_momentum', 0):.2f}**")
        w2.markdown(f"Trend: **{params.get('w_trend', 0):.2f}**")
        w3.markdown(f"Last Candle: **{params.get('w_last_candle', 0):.2f}**")
        w4.markdown(f"Orderbook: **{params.get('w_orderbook', 0):.2f}**")
        w5.markdown(f"Volatility: **{params.get('w_volatility', 0):.2f}**")

# ─── FOOTER ───
st.markdown("---")
now_pst = datetime.now(timezone.utc).astimezone(PST).strftime("%b %d, %Y %I:%M %p PST")
st.markdown(
    f'<div style="text-align:center;color:#495670;font-size:0.8rem;padding:10px 0;">'
    f'🏞 The Outsiders v2 — Jakob & Austin | Last refresh: {now_pst}'
    f'</div>',
    unsafe_allow_html=True
)
