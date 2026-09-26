"""Superhouse Chatbot - Streamlit front-end."""
import base64
import html
import logging
import os
import re

import pandas as pd
import streamlit as st

import chatbot

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

st.set_page_config(page_title="Superhouse AI Assistant", page_icon="👞", layout="wide",
                   initial_sidebar_state="expanded")

SUGGESTIONS = {
    "Materials": [
        "Which leather items need to be ordered? Show top 10 by quantity",
        "Total excess stock value by leather group",
        "Sole items with overdue requirement",
    ],
    "Sale orders": [
        "Orders shipping in the next 30 days by buyer",
        "Delayed sale orders (ex-factory date passed)",
        "Order quantity by destination country",
    ],
    "Samples": [
        "Open samples that are past delivery date, by merchandiser",
        "Average sample turnaround days by sample type",
        "Tanmay ke kitne samples pending hai?",
    ],
    "Production (IO)": [
        "IO progress % for every internal order",
        "Which internal orders have not started cutting?",
        "Walmart orders with their IO balance",
    ],
}
HERO_CARDS = [
    ("📦", "Material shortfall", "Which leather items need to be ordered? Show top 10 by quantity"),
    ("🚢", "Shipping soon", "Orders shipping in the next 30 days by buyer"),
    ("🧪", "Late samples", "Open samples that are past delivery date, by merchandiser"),
    ("🏭", "Production status", "IO progress % for every internal order"),
    ("💰", "Excess stock", "Total excess stock value by leather group"),
    ("🌍", "Destinations", "Order quantity by destination country"),
]

# ---------------------------------------------------------------------------
# Styling
# ---------------------------------------------------------------------------
st.html("""
<style>@import url("https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=DM+Serif+Display&display=swap");</style>
<style>
:root{
  --espresso:#1E130D; --espresso-2:#2E1D14; --cognac:#B5652B; --cognac-2:#8E4A1C;
  --gold:#D4A24C; --gold-soft:#F3D48A; --cream:#FAF7F2; --paper:#FFFFFF;
  --ink:#2A2420; --muted:#7A6E66; --line:#EAE1D6;
}
html, body, [class*="css"], .stMarkdown, .stButton button, .stChatInput textarea{font-family:'Inter',sans-serif;}
.stApp{background:radial-gradient(1200px 500px at 70% -10%, #F6EBDD 0%, var(--cream) 55%) fixed;}
#MainMenu, footer, [data-testid="stDecoration"]{visibility:hidden;}
.block-container{padding-top:1.4rem; padding-bottom:7rem; max-width:1100px;}

/* ---------- Sidebar ---------- */
[data-testid="stSidebar"]{background:linear-gradient(180deg,var(--espresso-2) 0%,var(--espresso) 100%);
  border-right:1px solid rgba(212,162,76,.25);}
[data-testid="stSidebar"] *{color:#EFE6DA;}
[data-testid="stSidebar"] .stButton button{background:rgba(255,255,255,.04); border:1px solid rgba(212,162,76,.22);
  color:#EFE6DA; text-align:left; justify-content:flex-start; border-radius:10px; font-size:.82rem;
  padding:.45rem .7rem; transition:all .15s ease; width:100%;}
[data-testid="stSidebar"] .stButton button:hover{background:rgba(212,162,76,.14); border-color:var(--gold); color:#fff;}
[data-testid="stSidebar"] .stButton button p{color:inherit !important; font-size:.82rem;}
.brand{display:flex; gap:.8rem; align-items:center; padding:.3rem 0 1.1rem; border-bottom:1px solid rgba(212,162,76,.22); margin-bottom:1rem;}
.brand img{width:54px; height:54px; object-fit:contain; filter:drop-shadow(0 4px 10px rgba(0,0,0,.35));}
.brand .name{font-family:'DM Serif Display',serif; font-size:1.35rem; line-height:1.1; color:var(--gold-soft) !important;}
.brand .sub{font-size:.7rem; letter-spacing:.18em; text-transform:uppercase; color:#BFAE9C !important; margin-top:.2rem;}
[data-testid="stSidebar"] [data-testid="stExpander"]{background:transparent; border:1px solid rgba(212,162,76,.18) !important; border-radius:10px !important; margin-bottom:.4rem;}
[data-testid="stSidebar"] [data-testid="stExpander"] summary{padding:.45rem .7rem;}
[data-testid="stSidebar"] [data-testid="stExpander"] summary p{font-size:.84rem; font-weight:600; color:#EFE6DA !important;}
[data-testid="stSidebar"] details{background:transparent !important;}
.side-h{font-size:.68rem; letter-spacing:.16em; text-transform:uppercase; color:var(--gold) !important; margin:1.1rem 0 .5rem; font-weight:600;}
.kpis{display:grid; grid-template-columns:1fr 1fr; gap:.5rem;}
.kpi{background:rgba(255,255,255,.05); border:1px solid rgba(212,162,76,.18); border-radius:12px; padding:.55rem .65rem;}
.kpi .v{font-size:1.05rem; font-weight:700; color:#fff !important;}
.kpi .l{font-size:.66rem; color:#BFAE9C !important; text-transform:uppercase; letter-spacing:.06em;}
.kpi.wide{grid-column:1 / span 2;}
.status{display:flex; align-items:center; gap:.45rem; font-size:.75rem; color:#BFAE9C !important; margin-top:1rem;}
.dot{width:8px; height:8px; border-radius:50%; background:#3FB97A; box-shadow:0 0 0 3px rgba(63,185,122,.2);}
.dot.off{background:#E0694B; box-shadow:0 0 0 3px rgba(224,105,75,.2);}

/* ---------- Header ---------- */
.topbar{display:flex; justify-content:space-between; align-items:center; padding:1rem 1.3rem; margin-bottom:1.2rem;
  background:linear-gradient(120deg,var(--espresso) 0%,#3B2518 60%,var(--cognac-2) 100%); border-radius:18px;
  box-shadow:0 12px 30px -12px rgba(30,19,13,.55); border:1px solid rgba(212,162,76,.35);}
.topbar h1{font-family:'DM Serif Display',serif; font-weight:400; color:var(--gold-soft); font-size:1.7rem; margin:0; padding:0;}
.topbar p{color:#E6D8C6; margin:.15rem 0 0; font-size:.86rem;}
.pill{background:rgba(212,162,76,.16); border:1px solid rgba(212,162,76,.5); color:var(--gold-soft);
  padding:.3rem .75rem; border-radius:999px; font-size:.72rem; letter-spacing:.08em; text-transform:uppercase; white-space:nowrap;}

/* ---------- Welcome ---------- */
.hello{text-align:center; margin:1.4rem 0 1.2rem;}
.hello h2{font-family:'DM Serif Display',serif; font-weight:400; color:var(--espresso); font-size:2rem; margin-bottom:.2rem;}
.hello p{color:var(--muted);}
.main .stButton button, [data-testid="stMain"] .stButton button{background:var(--paper); border:1px solid var(--line); border-radius:14px;
  padding:.85rem 1rem; text-align:left; justify-content:flex-start; min-height:74px; color:var(--ink);
  box-shadow:0 2px 6px rgba(42,36,32,.04); transition:all .18s ease; width:100%;}
[data-testid="stMain"] .stButton button:hover{border-color:var(--cognac); box-shadow:0 10px 24px -12px rgba(181,101,43,.55); transform:translateY(-2px); color:var(--ink);}

/* ---------- Chat bubbles ---------- */
.row{display:flex; gap:.7rem; margin:.9rem 0; align-items:flex-end; animation:fade .25s ease;}
.row.user{justify-content:flex-end;}
.avatar{flex:0 0 36px; height:36px; border-radius:50%; display:flex; align-items:center; justify-content:center;
  font-size:.78rem; font-weight:700;}
.avatar.bot{background:linear-gradient(135deg,var(--espresso-2),var(--espresso)); color:var(--gold-soft); border:1.5px solid var(--gold);}
.avatar.me{background:#EFE3D3; color:var(--cognac-2); border:1.5px solid #E2CDB3;}
.bubble{max-width:78%; padding:.8rem 1.05rem; border-radius:18px; line-height:1.55; font-size:.94rem; word-wrap:break-word;}
.bubble.user{background:linear-gradient(135deg,var(--cognac) 0%,var(--cognac-2) 100%); color:#fff; border-bottom-right-radius:5px;
  box-shadow:0 8px 20px -10px rgba(142,74,28,.7);}
.bubble.bot{background:var(--paper); color:var(--ink); border:1px solid var(--line); border-bottom-left-radius:5px;
  box-shadow:0 6px 18px -12px rgba(42,36,32,.35);}
.bubble.bot.err{border-color:#EFC9BC; background:#FFF8F5;}
.bubble ul{margin:.35rem 0 0 1.1rem; padding:0;} .bubble li{margin:.15rem 0;}
.bubble code{background:#F4ECE1; padding:.05rem .35rem; border-radius:5px; font-size:.85em;}
.bubble b, .bubble strong{color:inherit;}
.bubble.bot b{color:var(--cognac-2);}
.meta{font-size:.72rem; color:var(--muted); margin:.1rem 0 0 3.2rem; display:flex; gap:.5rem; flex-wrap:wrap;}
.chip{background:#F3EADF; border:1px solid var(--line); padding:.1rem .5rem; border-radius:999px;}
.typing{display:inline-flex; gap:4px; padding:.2rem 0;}
.typing span{width:7px; height:7px; border-radius:50%; background:var(--cognac); opacity:.35; animation:blink 1.2s infinite;}
.typing span:nth-child(2){animation-delay:.2s} .typing span:nth-child(3){animation-delay:.4s}
@keyframes blink{0%,80%,100%{opacity:.25; transform:translateY(0)} 40%{opacity:1; transform:translateY(-3px)}}
@keyframes fade{from{opacity:0; transform:translateY(4px)} to{opacity:1; transform:none}}
[data-testid="stMain"] [data-testid="stExpander"]{margin-left:3.2rem; border:1px solid var(--line) !important; border-radius:12px !important; background:var(--paper);}
[data-testid="stMain"] [data-testid="stExpander"] summary p{font-size:.84rem; color:var(--cognac-2); font-weight:600;}
[data-testid="stChatInput"]{border-radius:16px; border:1.5px solid #E2CDB3 !important; box-shadow:0 10px 30px -14px rgba(42,36,32,.4);}
[data-testid="stChatInput"]:focus-within{border-color:var(--cognac) !important;}
@media (max-width:700px){ .bubble{max-width:88%;} .topbar{flex-direction:column; align-items:flex-start; gap:.6rem;}
  [data-testid="stMain"] [data-testid="stExpander"], .meta{margin-left:0;} }
</style>
""")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
@st.cache_data(show_spinner=False)
def logo_data_uri() -> str:
    for path, mime in ((os.path.join(BASE_DIR, "logo.png"), "image/png"),
                       (os.path.join(BASE_DIR, "assets", "logo.png"), "image/png"),
                       (os.path.join(BASE_DIR, "assets", "logo.svg"), "image/svg+xml")):
        if os.path.exists(path):
            with open(path, "rb") as f:
                return f"data:{mime};base64," + base64.b64encode(f.read()).decode()
    return ""


@st.cache_data(ttl=300, show_spinner=False)
def overview() -> dict:
    return chatbot.db_overview()


def md_to_html(text: str) -> str:
    """Tiny, safe markdown subset (escape first): bold, italic, code, bullet lists, line breaks."""
    out, in_list = [], False
    for line in html.escape(text or "").splitlines():
        s = line.strip()
        m = re.match(r"^([-*•]|\d+[.)])\s+(.*)$", s)
        if m:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{m.group(2)}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        out.append(s + "<br>" if s else "<br>")
    if in_list:
        out.append("</ul>")
    h = "".join(out)
    h = re.sub(r"(<br>){3,}", "<br><br>", h)
    h = re.sub(r"^(<br>)+|(<br>)+$", "", h)
    h = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", h)
    h = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", h)
    h = re.sub(r"`([^`]+)`", r"<code>\1</code>", h)
    return h


def fmt_num(v) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "–"
    if abs(v) >= 1e7:
        return f"{v / 1e7:,.2f} Cr"
    if abs(v) >= 1e5:
        return f"{v / 1e5:,.2f} L"
    return f"{v:,.0f}"


def user_bubble(text: str):
    st.markdown(f'<div class="row user"><div class="bubble user">{html.escape(text)}</div>'
                f'<div class="avatar me">YOU</div></div>', unsafe_allow_html=True)


def bot_bubble(msg: dict, idx: int):
    err = " err" if msg.get("type") == "error" else ""
    st.markdown(f'<div class="row"><div class="avatar bot">SH</div>'
                f'<div class="bubble bot{err}">{md_to_html(msg.get("content", ""))}</div></div>',
                unsafe_allow_html=True)
    rows, cols = msg.get("rows") or [], msg.get("columns") or []
    chips = []
    if msg.get("title"):
        chips.append(html.escape(msg["title"]))
    if rows:
        chips.append(f"{len(rows):,}{'+' if msg.get('truncated') else ''} rows")
    if msg.get("attempts", 0) > 1:
        chips.append(f"auto-corrected ×{msg['attempts'] - 1}")
    if chips:
        st.markdown('<div class="meta">' + "".join(f'<span class="chip">{c}</span>' for c in chips) + "</div>",
                    unsafe_allow_html=True)
    if rows and cols:
        df = pd.DataFrame(rows, columns=cols)
        if not (len(df) == 1 and len(cols) <= 2):
            with st.expander(f"📊 View data ({len(df):,} rows)", expanded=len(df) <= 15):
                pretty = df.rename(columns=lambda c: c.replace("_", " ").title())
                st.dataframe(pretty, width="stretch", hide_index=True)
                st.download_button("⬇ Download CSV", df.to_csv(index=False).encode("utf-8"),
                                   file_name=f"superhouse_result_{idx}.csv", mime="text/csv", key=f"dl_{idx}")
    if msg.get("sql"):
        with st.expander("🧠 SQL used"):
            st.code(msg["sql"], language="sql")


def queue(question: str):
    st.session_state.pending = question


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
if "messages" not in st.session_state:
    st.session_state.messages = []

# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
with st.sidebar:
    logo = logo_data_uri()
    logo_html = f'<img src="{logo}" alt="Superhouse logo">' if logo else ""
    st.markdown(f'<div class="brand">{logo_html}<div><div class="name">Superhouse Limited</div>'
                f'<div class="sub">Shoe Division · AI Desk</div></div></div>', unsafe_allow_html=True)

    ov = overview()
    if ov:
        st.markdown('<div class="side-h">Live data snapshot</div>', unsafe_allow_html=True)
        st.markdown(f"""<div class="kpis">
            <div class="kpi"><div class="v">{ov['sale_orders']:,}</div><div class="l">Open orders</div></div>
            <div class="kpi"><div class="v">{fmt_num(ov['order_pairs'])}</div><div class="l">Pairs on order</div></div>
            <div class="kpi"><div class="v">{ov['open_samples']:,}</div><div class="l">Open samples</div></div>
            <div class="kpi"><div class="v">{fmt_num(ov['io_balance'])}</div><div class="l">IO pairs pending</div></div>
            <div class="kpi"><div class="v">{ov['leather_items'] + ov['sole_items']:,}</div><div class="l">Material items</div></div>
            <div class="kpi"><div class="v">₹{fmt_num(ov['excess_value'])}</div><div class="l">Excess stock</div></div>
        </div>""", unsafe_allow_html=True)

    st.markdown('<div class="side-h">Try asking</div>', unsafe_allow_html=True)
    for group, qs in SUGGESTIONS.items():
        with st.expander(group, expanded=False):
            for i, q in enumerate(qs):
                st.button(q, key=f"s_{group}_{i}", on_click=queue, args=(q,), width="stretch")

    st.markdown("<div style='height:.6rem'></div>", unsafe_allow_html=True)
    if st.button("＋  New chat", key="new_chat", width="stretch"):
        st.session_state.messages = []
        st.rerun()

    ready = chatbot.llm_ready()
    built = ov.get("built_at") if ov else None
    st.markdown(f'<div class="status"><span class="dot{"" if ready else " off"}"></span>'
                f'{"AI engine online · " + html.escape(chatbot.MODEL) if ready else "AI key not configured"}</div>'
                + (f'<div class="status" style="margin-top:.3rem">Data refreshed {html.escape(built)} UTC</div>' if built else ""),
                unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
st.markdown("""<div class="topbar"><div><h1>Superhouse AI Assistant</h1>
<p>Ask in plain English or Hinglish — stock, shortfalls, sale orders, samples &amp; production.</p></div>
<span class="pill">NL2SQL · Live data</span></div>""", unsafe_allow_html=True)

if not st.session_state.messages and "pending" not in st.session_state:
    st.markdown('<div class="hello"><h2>What would you like to know today?</h2>'
                '<p>Pick a starting point or type your own question below.</p></div>', unsafe_allow_html=True)
    cols = st.columns(3)
    for i, (icon, label, q) in enumerate(HERO_CARDS):
        with cols[i % 3]:
            st.button(f"{icon}  **{label}**  \n{q}", key=f"hero_{i}", on_click=queue, args=(q,),
                      width="stretch")

for i, m in enumerate(st.session_state.messages):
    if m["role"] == "user":
        user_bubble(m["content"])
    else:
        bot_bubble(m, i)

prompt = st.chat_input("Ask about stock, orders, samples or production…")
prompt = prompt or st.session_state.pop("pending", None)

if prompt:
    history = [dict(m) for m in st.session_state.messages]
    st.session_state.messages.append({"role": "user", "content": prompt})
    user_bubble(prompt)
    placeholder = st.empty()
    placeholder.markdown('<div class="row"><div class="avatar bot">SH</div><div class="bubble bot">'
                         '<span class="typing"><span></span><span></span><span></span></span></div></div>',
                         unsafe_allow_html=True)
    res = chatbot.ask(prompt, history)
    placeholder.empty()
    st.session_state.messages.append({
        "role": "assistant", "content": res["message"], "type": res["type"], "sql": res.get("sql"),
        "columns": res.get("columns"), "rows": res.get("rows"), "truncated": res.get("truncated"),
        "title": res.get("title"), "attempts": res.get("attempts", 0),
    })
    st.rerun()
