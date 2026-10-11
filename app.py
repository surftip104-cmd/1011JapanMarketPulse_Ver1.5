import streamlit as st
import requests, pandas as pd, numpy as np
from datetime import datetime, timezone, timedelta
import json, os, time, re, html
import scoring_engine as scoring
import japan_kabuka_flow as jkf
import jpx_margin as mg   # JPX 銘柄別信用取引残高（信用買残・売残）
import final_judgment as fjm   # 最終総合判断カード

st.set_page_config(page_title="日本株 Market Pulse", page_icon="🇯🇵", layout="wide")

YF_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{}"

# 日本株コード: 4桁数字、または3桁数字+英字1文字（例: 285A）
JP_CODE_RE = re.compile(r"^(?:\d{4}|\d{3}[A-Za-z])$")

def normalize_jp_code(raw):
    """ユーザー入力をYahoo Finance用の日本株シンボルへ変換。"""
    code = str(raw or "").strip().upper()
    if code.endswith(".T"):
        code = code[:-2]
    if not JP_CODE_RE.fullmatch(code):
        return None
    return code + ".T"

def parse_stock_codes(text):
    """空白・カンマ・改行区切りで最大10銘柄を受け付ける。"""
    tokens = [x for x in re.split(r"[\s,、;；]+", str(text or "").strip()) if x]
    valid, invalid = [], []
    for token in tokens:
        sym = normalize_jp_code(token)
        if sym and sym not in valid:
            valid.append(sym)
        elif not sym:
            invalid.append(token)
    return valid[:10], invalid

@st.cache_data(ttl=300, show_spinner=False)
def yahoo_meta(symbol):
    """Yahoo chart APIのmetaから銘柄名等を取得。"""
    try:
        p = {"range":"5d", "interval":"1d", "includePrePost":"false", "events":"div,splits"}
        r = requests.get(YF_CHART.format(symbol), params=p, timeout=10, headers={"User-Agent":"Mozilla/5.0"})
        r.raise_for_status()
        result = r.json().get("chart", {}).get("result")
        if not result:
            return {}
        meta = result[0].get("meta", {})
        return {
            "shortName": meta.get("shortName") or meta.get("longName") or symbol,
            "exchange": meta.get("exchangeName", ""),
            "currency": meta.get("currency", "JPY"),
        }
    except Exception:
        return {}


# -----------------------------
# Yahoo Finance public chart reader
# -----------------------------
@st.cache_data(ttl=45, show_spinner=False)
def yahoo_chart(symbol, range_="5d", interval="5m"):
    try:
        p = {"range": range_, "interval": interval, "includePrePost": "false", "events": "div,splits"}
        r = requests.get(YF_CHART.format(symbol), params=p, timeout=10,
                         headers={"User-Agent":"Mozilla/5.0"})
        r.raise_for_status()
        j = r.json()["chart"]["result"][0]
        ts = j.get("timestamp", [])
        q = j["indicators"]["quote"][0]
        df = pd.DataFrame({
            "time": pd.to_datetime(ts, unit="s", utc=True),
            "open": q.get("open", [None]*len(ts)),
            "high": q.get("high", [None]*len(ts)),
            "low": q.get("low", [None]*len(ts)),
            "close": q.get("close", [None]*len(ts)),
            "volume": q.get("volume", [None]*len(ts)),
        }).dropna(subset=["close"])
        if df.empty:
            return df
        df["time_jst"] = df["time"].dt.tz_convert("Asia/Tokyo")
        return df
    except Exception:
        return pd.DataFrame()

@st.cache_data(ttl=300, show_spinner=False)
def get_daily(symbol, days=30):
    return yahoo_chart(symbol, "3mo", "1d")

def latest_stats(symbol):
    df = get_daily(symbol)
    if df.empty or len(df) < 2:
        return None
    x = df.iloc[-1]; prev = df.iloc[-2]
    chg = (x.close / prev.close - 1) * 100
    return {"close":float(x.close), "prev":float(prev.close), "change":float(chg),
            "volume":float(x.volume) if pd.notna(x.volume) else 0,
            "date":x.time_jst.strftime("%Y-%m-%d")}

def intraday_stats(symbol):
    df = yahoo_chart(symbol, "5d", "5m")
    if df.empty:
        return None
    # latest Japan session, approximate by JST calendar day
    today = df["time_jst"].dt.date.max()
    d = df[df["time_jst"].dt.date == today].copy()
    if d.empty: return None
    prev_days = df[df["time_jst"].dt.date < today]
    if prev_days.empty: return None
    prev_close = float(prev_days.iloc[-1].close)
    open_px = float(d.iloc[0].open)
    last_px = float(d.iloc[-1].close)
    high_px = float(d.high.max())
    low_px = float(d.low.min())
    gap = (open_px/prev_close-1)*100
    from_open = (last_px/open_px-1)*100
    from_high = (last_px/high_px-1)*100
    return {"open":open_px,"last":last_px,"prev":prev_close,"high":high_px,"low":low_px,
            "gap":gap,"from_open":from_open,"from_high":from_high,"df":d}

def fmt_pct(v):
    if v is None: return "—"
    return f"{v:+.2f}%"

def fmt_shares_delta(v):
    if v is None: return None
    return f"{v:+,.0f}株"

# -----------------------------
# 信用残（JPX 銘柄別信用取引残高）
# -----------------------------
@st.cache_data(ttl=900, show_spinner=False)
def cached_margin_auto():
    return mg.load_latest()

@st.cache_data(show_spinner=False)
def cached_margin_upload(data):
    return mg.parse_pdf_bytes(data)

@st.cache_data(ttl=900, show_spinner=False)
def cached_japan_kabuka_flow(code):
    """機関投資家の空売り・個人信用増減を15分キャッシュで取得。"""
    return jkf.fetch_flow(code, timeout=6)

# -----------------------------
# Symbols
# -----------------------------
MARKETS = {
    "日経225": "^N225",
    "TOPIX": "^TOPX",
    "SOX": "^SOX",
    "NASDAQ": "^IXIC",
    "S&P500": "^GSPC",
    "NYダウ": "^DJI",
    "KOSPI": "^KS11",
    "ドル円": "JPY=X",
}

SECTORS = {
    "食品": "1617.T", "エネルギー資源":"1618.T", "建設・資材":"1619.T",
    "素材・化学":"1620.T", "医薬品":"1621.T", "自動車・輸送":"1622.T",
    "鉄鋼・非鉄":"1623.T", "機械":"1624.T", "電機・精密":"1625.T",
    "情報通信・サービス":"1626.T", "電力・ガス":"1627.T", "運輸・物流":"1628.T",
    "商社・卸売":"1629.T", "小売":"1630.T", "銀行":"1631.T",
    "金融（除く銀行）":"1632.T", "不動産":"1633.T"
}

THEMES = {
    "メモリ": ["285A.T","SNDK","MU","000660.KS","005930.KS"],
    "AI・半導体": ["6857.T","8035.T","6146.T","6920.T","6526.T","285A.T"],
    "自動車": ["7203.T","7267.T","7269.T","6902.T"],
    "銀行": ["8306.T","8316.T","8411.T"],
    "防衛": ["7011.T","7012.T","7013.T","6208.T"],
    "商社": ["8001.T","8002.T","8031.T","8053.T","8058.T"],
}

# -----------------------------
# Theme / market calculations
# -----------------------------
@st.cache_data(ttl=60, show_spinner=False)
def basket_stats(symbols):
    rows=[]
    for s in symbols:
        z=latest_stats(s)
        if z: rows.append({"symbol":s, **z})
    if not rows: return pd.DataFrame()
    return pd.DataFrame(rows)

def theme_score(df):
    if df.empty: return None
    # blend average return, breadth, and max concentration
    avg=df.change.mean()
    breadth=(df.change>0).mean()*100
    score=5 + avg*1.8 + (breadth-50)/20
    return float(np.clip(score,0,10))

def market_score(m):
    vals=[]
    for k in ["日経225","TOPIX","SOX","NASDAQ","KOSPI"]:
        if m.get(k): vals.append(m[k]["change"])
    base=np.mean(vals) if vals else 0
    yen = m.get("ドル円",{}).get("change",0)
    # Weak yen is mildly positive for Japanese equities, but not dominant.
    score=5 + base*1.25 + yen*0.25
    return float(np.clip(score,0,10))

def label_score(s):
    if s >= 7.0: return "🟢 強気"
    if s >= 5.5: return "🟡 やや強気"
    if s >= 4.5: return "🟡 様子見"
    if s >= 3.0: return "🟠 やや弱気"
    return "🔴 弱気"

def pull_sector_df():
    rows=[]
    for name,sym in SECTORS.items():
        z=latest_stats(sym)
        if z: rows.append({"セクター":name, "変化率":z["change"], "終値":z["close"]})
    df=pd.DataFrame(rows)
    if not df.empty:
        df["方向"]=np.where(df["変化率"]>=0,"流入","流出")
        df=df.sort_values("変化率",ascending=False)
    return df

# -----------------------------
# Individual stock analysis
# -----------------------------
def volume_ratio(symbol, days=20):
    df = get_daily(symbol)
    if df.empty or len(df) < days + 1:
        return None
    recent = float(df.iloc[-1].volume or 0)
    base = pd.to_numeric(df.iloc[-days-1:-1].volume, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    if base.empty or base.mean() == 0:
        return None
    return recent / float(base.mean())

def vwap_stats(intra_df):
    """当日5分足からVWAP・前場初動などを算出。"""
    if intra_df is None or intra_df.empty:
        return None
    d = intra_df.copy()
    vol = pd.to_numeric(d["volume"], errors="coerce").fillna(0)
    typical = (pd.to_numeric(d["high"], errors="coerce") +
               pd.to_numeric(d["low"], errors="coerce") +
               pd.to_numeric(d["close"], errors="coerce")) / 3.0
    total_vol = float(vol.sum())
    vwap = float((typical * vol).sum() / total_vol) if total_vol > 0 else float(d.iloc[-1].close)
    first3 = d.head(3)
    first15_high = float(first3.high.max()) if not first3.empty else float(d.high.max())
    first15_low = float(first3.low.min()) if not first3.empty else float(d.low.min())
    return {
        "vwap": vwap,
        "first15_high": first15_high,
        "first15_low": first15_low,
        "last_above_vwap": float(d.iloc[-1].close) >= vwap,
    }

def individual_yoriten_risk(symbol, intra):
    """個別銘柄の寄り天リスク。指数ではなく、その銘柄自身の5分足を評価する。"""
    if not intra or intra.get("df") is None or intra["df"].empty:
        return None
    d = intra["df"]
    v = vwap_stats(d)
    if not v:
        return None
    risk = 15.0
    # 上窓が大きいほど「寄りで買われた後の反落」リスクを加点
    if intra["gap"] > 2.0: risk += 30
    elif intra["gap"] > 1.0: risk += 20
    elif intra["gap"] > 0.5: risk += 10
    # 寄り後に失速しているか
    if intra["from_open"] < 0:
        risk += min(30, abs(intra["from_open"]) * 14)
    elif intra["from_open"] < 0.4:
        risk += 5
    # 高値からの押し
    if intra["from_high"] < -1.5: risk += 20
    elif intra["from_high"] < -0.7: risk += 10
    # VWAP割れは寄り後の買い勢力低下として加点
    if intra["last"] < v["vwap"]:
        risk += 12
    # 初動高値を更新できず、その後に下げている
    if intra["last"] < v["first15_high"] * 0.995 and intra["from_open"] < 0:
        risk += 8
    risk = int(np.clip(risk, 5, 95))
    label = "🟢 低リスク" if risk < 35 else ("🟡 中リスク" if risk < 65 else "🔴 高リスク")
    return {**v, "risk": risk, "label": label}

# 個別銘柄→関連テーマ。主要銘柄は明示し、それ以外は名称キーワードで推定する。
CODE_THEME = {
    "285A": "メモリ", "7203": "自動車", "7267": "自動車", "7269": "自動車", "6902": "自動車",
    "5706": "非鉄・素材", "5711": "非鉄・素材", "5801": "非鉄・素材", "5802": "非鉄・素材",
    "6857": "AI・半導体", "8035": "AI・半導体", "6146": "AI・半導体", "6920": "AI・半導体", "6526": "AI・半導体",
    "8306": "銀行", "8316": "銀行", "8411": "銀行",
    "7011": "防衛", "7012": "防衛", "7013": "防衛", "6208": "防衛",
    "8001": "商社", "8002": "商社", "8031": "商社", "8053": "商社", "8058": "商社",
}

RELATED_BASKETS = {
    "メモリ": ["285A.T", "SNDK", "MU", "000660.KS", "005930.KS"],
    "AI・半導体": ["6857.T", "8035.T", "6146.T", "6920.T", "6526.T", "285A.T"],
    "自動車": ["7203.T", "7267.T", "7269.T", "6902.T"],
    "非鉄・素材": ["5706.T", "5711.T", "5801.T", "5802.T", "5713.T"],
    "銀行": ["8306.T", "8316.T", "8411.T"],
    "防衛": ["7011.T", "7012.T", "7013.T", "6208.T"],
    "商社": ["8001.T", "8002.T", "8031.T", "8053.T", "8058.T"],
}

def infer_related_theme(code, name):
    code = code.upper().replace(".T", "")
    if code in CODE_THEME:
        return CODE_THEME[code]
    n = str(name or "")
    keyword_map = [
        ("メモリ", "メモリ"), ("半導体", "AI・半導体"), ("自動車", "自動車"),
        ("銀行", "銀行"), ("防衛", "防衛"), ("商事", "商社"), ("商船", "商社"),
        ("金属", "非鉄・素材"), ("鉱業", "非鉄・素材"),
    ]
    for kw, theme in keyword_map:
        if kw in n:
            return theme
    return "市場全体"

def individual_related_analysis(z):
    theme = infer_related_theme(z["code"], z["name"])
    symbols = RELATED_BASKETS.get(theme, ["^N225", "^TOPX", "^SOX"])
    df = basket_stats(symbols)
    sc = theme_score(df)
    return {"theme": theme, "symbols": symbols, "df": df, "score": sc}

def price_chg_on(dfd, asof):
    """信用残の基準日(YYYYMMDD)における株価の前日比(%)。日付が見つからなければNone。"""
    if dfd is None or dfd.empty or not asof or len(asof) != 8:
        return None
    key = f"{asof[:4]}-{asof[4:6]}-{asof[6:8]}"
    dates = dfd["time_jst"].dt.strftime("%Y-%m-%d").tolist()
    if key not in dates:
        return None
    i = dates.index(key)
    if i == 0:
        return None
    prev, cur = float(dfd.iloc[i - 1].close), float(dfd.iloc[i].close)
    return (cur / prev - 1) * 100 if prev else None

def individual_score(symbol, mdf=None, asof=None):
    daily = latest_stats(symbol)
    intra = intraday_stats(symbol)
    if not daily:
        return None
    name = yahoo_meta(symbol).get("shortName", symbol.replace(".T", ""))
    market = latest_stats("^N225")
    rel = daily["change"] - (market["change"] if market else 0)
    vr = volume_ratio(symbol)

    score = 5.0
    score += np.clip(daily["change"] * 0.75, -2.0, 2.0)
    score += np.clip(rel * 0.55, -1.5, 1.5)
    if vr is not None:
        score += np.clip((vr - 1.0) * 0.8, -1.0, 1.5)
    if intra:
        score += np.clip(intra["from_open"] * 0.35, -1.0, 1.0)
        if intra["last"] < intra["open"] and intra["from_high"] < -1.5:
            score -= 0.5

    # 信用需給（買残・売残・倍率・回転日数）を ±1.0 の範囲で総合スコアへ反映
    margin = None
    if mdf is not None and len(mdf) > 0:
        try:
            dfd = get_daily(symbol)
            avg_vol = float(pd.to_numeric(dfd.volume.tail(20), errors="coerce").mean()) if not dfd.empty else None
        except Exception:
            avg_vol = None
        try:
            _pchg = price_chg_on(get_daily(symbol), asof)
        except Exception:
            _pchg = None
        margin = mg.analyze_margin(symbol.replace(".T", ""), mdf, avg_volume=avg_vol,
                                   price=daily["close"], price_chg=_pchg)
        if margin:
            score += margin["adj"]
    score = float(np.clip(score, 0, 10))

    if score >= 7.0:
        action = "🟢 買い優勢"
        strategy = "上昇トレンドを確認できるなら押し目買い候補。高値追いは出来高を確認。"
    elif score >= 5.8:
        action = "🟢 やや買い優勢"
        strategy = "強さはあるため監視継続。VWAP・前場高値などの上抜け確認後が無難。"
    elif score >= 4.5:
        action = "🟡 様子見"
        strategy = "方向感が弱い。無理に入らず、出来高増加と高値・安値更新を待つ。"
    elif score >= 3.2:
        action = "🟠 売り優勢"
        strategy = "戻り売りを優先。ただし急落後のリバウンドには注意。"
    else:
        action = "🔴 下落警戒"
        strategy = "逆張りは避け、戻りの弱さを確認。デイトレは見送りも選択肢。"

    if margin and margin["comment_short"]:
        strategy += "　【信用】" + margin["comment_short"]

    result = {
        "symbol": symbol, "code": symbol.replace(".T", ""), "name": name,
        "score": score, "action": action, "strategy": strategy,
        "daily": daily, "intraday": intra, "relative": rel, "volume_ratio": vr,
        "margin": margin
    }
    result["related"] = individual_related_analysis(result)
    result["yoriten"] = individual_yoriten_risk(symbol, intra)
    return result


def show_five_axis_scores(z):
    """追加5軸の色・アイコン付きスコアカードと総合判定を表示。"""
    pack = z.get("five_axis") or {}
    axes = pack.get("axes", {})
    st.markdown("#### 🧭 追加機能：5軸デイトレ・スコア")
    cards = []
    for key in ("capital", "supply", "large", "trend", "yoriten"):
        axis = axes.get(key, {"score": None, "status": "未取得", "detail": "未取得"})
        score_value = axis.get("score")
        if score_value is None:
            color, icon, score_text = "#8b929c", "⚪", "未取得"
        elif key == "yoriten":
            color, icon = (("#20d89a", "🟢") if score_value < 35 else (("#ffc857", "🟡") if score_value < 65 else ("#ff6574", "🔴")))
            score_text = f"{score_value:.0f}/100"
        else:
            color, icon = (("#20d89a", "🟢") if score_value >= 65 else (("#ffc857", "🟡") if score_value >= 45 else ("#ff6574", "🔴")))
            score_text = f"{score_value:.0f}/100"
        title = scoring.AXIS_META[key][1]
        detail = html.escape(str(axis.get("detail", "")))
        status = html.escape(str(axis.get("status", "未取得")))
        cards.append(
            f'<div style="height:100%;min-height:168px;border:1px solid {color}88;'
            f'border-radius:14px;padding:12px;background:linear-gradient(145deg,#11151b,#1a2028);">'
            f'<div style="font-size:0.9rem;color:#c6ccd4;">{scoring.AXIS_META[key][0]} {html.escape(title)}</div>'
            f'<div style="font-size:1.45rem;font-weight:800;color:{color};margin:8px 0;">{icon} {score_text}</div>'
            f'<div style="font-size:0.75rem;color:#aeb5bf;margin-bottom:7px;">{status}</div>'
            f'<div style="font-size:0.78rem;line-height:1.5;color:#d1d5db;">{detail}</div></div>'
        )
    cols = st.columns(5)
    for col, card in zip(cols, cards):
        with col:
            st.markdown(card, unsafe_allow_html=True)
    b1, b2, b3, b4 = st.columns(4)
    with b1:
        st.metric("🟢 買いスコア", f"{pack.get('buy_score', 0):.0f}/100")
    with b2:
        st.metric("🔴 売りスコア", f"{pack.get('sell_score', 0):.0f}/100")
    with b3:
        st.metric("📊 データ充足度", f"{pack.get('available_count', 0)}/5 軸")
    with b4:
        decision = pack.get("decision", "様子見")
        icon = {"買い候補": "🟢", "売り候補": "🔴", "様子見": "🟡"}.get(decision, "⚪")
        st.metric("🎯 総合判定", f"{icon} {decision}")
    if pack.get("buy_blocked"):
        st.warning("寄り天リスクが65%以上のため、買い候補判定を抑制しています。")
    flow = z.get("large_flow") or {}
    source_url = flow.get("url")
    if source_url:
        st.markdown(f"[🏦 機関空売り・信用増減の公開元を確認]({source_url})")
    st.caption("スコアは取得できた軸のみで計算。未取得は0点扱いにせず除外します。機関空売りの公表値はリアルタイム歩み値ではありません。")

# -----------------------------
# UI
# -----------------------------
st.markdown("""
<style>
.block-container {padding-top:1.2rem; padding-bottom:2rem; max-width:1450px;}
.card {border-radius:18px; padding:20px; margin-bottom:14px; background:linear-gradient(145deg,#101318,#171b21); border:1px solid #30343c;}
.small {color:#9aa2ad;font-size:0.9rem}
.big {font-size:2rem;font-weight:800}
.green {color:#20d89a}.red {color:#ff6574}.yellow {color:#ffc857}
</style>
""", unsafe_allow_html=True)

st.title("🇯🇵 日本株 Market Pulse — デイトレ地合い判定")
st.caption("Ver.1.4 5軸スコア追加版。Yahoo Financeと公開需給データを利用した個人向け試作版です。リアルタイム性・利用条件は各データ提供元をご確認ください。")

col1,col2,col3=st.columns([1,1,1])
with col1:
    if st.button("🔄 今すぐ更新", use_container_width=True):
        st.cache_data.clear()
        st.rerun()
with col2:
    st.metric("更新", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
with col3:
    st.info("日本時間 9:00–15:30 の市場判定を想定")

# Individual stock input
st.subheader("🎯 個別銘柄分析")
st.caption("銘柄コードを入力すると、その銘柄自身の『関連テーマ・メモリ等のテーマ強弱』と『個別銘柄の寄り天リスク』まで自動判定します。複数入力はカンマ/空白区切りで最大10銘柄。")
code_col, btn_col = st.columns([4, 1])
with code_col:
    stock_input = st.text_input(
        "銘柄コード", value="285A",
        placeholder="7203 / 5706 / 285A / 9984 / 130A",
        help="対応: 4桁数字、3桁数字+英字1文字。例: 7203、5706、285A、130A"
    )
with btn_col:
    st.write("")
    analyze_clicked = st.button("🔍 分析する", use_container_width=True, type="primary")

# 信用残（JPX）の読み込み：自動取得 → 失敗時は手動PDF／手入力で補完
with st.sidebar:
    st.header("💳 信用残データ")
    st.caption("JPX『銘柄別信用取引残高』を自動取得します（日々16:00目安で公表）。")
    with st.expander("📖 信用需給の見方（判断基準）"):
        st.markdown("**指標ごとの基準**")
        for _n, _t in mg.CRITERIA:
            st.markdown(f"- **{_n}**：{_t}")
        st.markdown("**前日比の組み合わせ（株価／買残／売残）**")
        _jp = {"up": "↑", "down": "↓"}
        st.dataframe(
            pd.DataFrame(
                [{"株価": _jp[k[0]], "買残": _jp[k[1]], "売残": _jp[k[2]], "判定": v[0], "解説": v[1]}
                 for k, v in mg.COMBO.items()]),
            hide_index=True, use_container_width=True)
        st.caption(f"増減の判定は 買残・売残 ±{mg.DIR_TH:.0f}%、株価 ±{mg.PRICE_TH:.1f}% を境にしています"
                   "（jpx_margin.py の DIR_TH / PRICE_TH で変更可）。基準は目安で、売買を推奨するものではありません。")
    margin_pdf = st.file_uploader("自動取得できない時：JPXのPDFを指定", type=["pdf"], key="margin_pdf")
    margin_manual = st.text_area(
        "手入力（1行1銘柄）", value="", height=90, key="margin_manual",
        placeholder="コード 買残 買残前日比 売残 売残前日比\n285A 1200000 -30000 150000 5000",
        help="スペース/タブ区切り。手入力した銘柄はPDF/自動取得の値より優先されます。")

margin_info = {"df": None, "asof": None, "source": None, "warn": None}
with st.spinner("信用残データを確認中…（初回はPDF解析に数十秒かかることがあります）"):
    if margin_pdf is not None:
        _df, _asof, _method = cached_margin_upload(margin_pdf.getvalue())
        margin_info.update(df=_df if len(_df) else None, asof=_asof, source=f"アップロードPDF（{_method}）")
        if not len(_df):
            margin_info["warn"] = "アップロードされたPDFから信用残を読み取れませんでした。"
    else:
        margin_info = dict(cached_margin_auto())
_man = mg.parse_manual(margin_manual)
if len(_man):
    base = margin_info["df"] if margin_info["df"] is not None else pd.DataFrame(columns=mg.COLS[1:])
    margin_info["df"] = pd.concat([base.drop(index=_man.index, errors="ignore"), _man])
    margin_info["source"] = (margin_info["source"] or "") + "＋手入力"
mdf = margin_info["df"]

if mdf is not None and len(mdf):
    st.caption(f"💳 信用残：{mg.fmt_asof(margin_info['asof'])} 申込み現在（{margin_info['source']}／{len(mdf):,}銘柄）。"
               "当日分は翌営業日16:00頃にJPXが公表します。")
if margin_info.get("warn"):
    st.warning(margin_info["warn"])
if mdf is None or not len(mdf):
    st.info("信用残データを取得できていません。サイドバーでJPXのPDFを指定するか、手入力してください。（信用需給なしで分析します）")

results = []   # 個別銘柄の分析結果（最終総合判断でも使用）
if analyze_clicked or stock_input:
    codes, invalid = parse_stock_codes(stock_input)
    if invalid:
        st.warning("認識できないコード: " + ", ".join(invalid) + "　→ 4桁数字 または 3桁数字+英字1文字で入力してください。")
    if codes:
        results=[]
        with st.spinner("個別銘柄データを取得・分析中…"):
            for sym in codes:
                z=individual_score(sym, mdf, margin_info.get("asof"))
                if z: results.append(z)
        if results:
            for z in results:
                d=z["daily"]; intra=z["intraday"]; rel=z["related"]; yr=z["yoriten"]
                # Public institutional short/credit delta data; unavailable values remain None.
                flow = cached_japan_kabuka_flow(z["code"])
                try:
                    _daily_df = get_daily(z["symbol"])
                    _avg_volume = float(pd.to_numeric(_daily_df.volume.tail(20), errors="coerce").dropna().mean()) if not _daily_df.empty else None
                    if _avg_volume is not None and not np.isfinite(_avg_volume):
                        _avg_volume = None
                except Exception:
                    _avg_volume = None
                z["large_flow"] = flow
                z["five_axis"] = scoring.calculate_scores(
                    daily=z.get("daily"), intra=z.get("intraday"), margin=z.get("margin"),
                    yoriten=z.get("yoriten"), large_flow=flow,
                    volume_ratio=z.get("volume_ratio"), avg_daily_volume=_avg_volume)
                st.markdown('<div class="card">', unsafe_allow_html=True)
                h1,h2,h3=st.columns([2.2,1,1])
                with h1:
                    st.markdown(f"### {z['code']}　{z['name']}")
                    st.markdown(f"**{z['action']}**　　**{z['score']:.1f} / 10**")
                with h2:
                    st.metric("前日比", fmt_pct(d["change"]))
                with h3:
                    vr=z["volume_ratio"]
                    st.metric("出来高倍率", "—" if vr is None else f"{vr:.1f}倍")

                a,b,c,dcol=st.columns(4)
                with a: st.metric("現在/終値", f"{d['close']:,.0f}円")
                with b: st.metric("日経比", fmt_pct(z["relative"]))
                with c: st.metric("高値", f"{(intra['high'] if intra else d['close']):,.0f}円")
                with dcol: st.metric("安値", f"{(intra['low'] if intra else d['close']):,.0f}円")

                if intra:
                    x,y,w=st.columns(3)
                    with x: st.metric("寄り付き", f"{intra['open']:,.0f}円", fmt_pct(intra['gap']))
                    with y: st.metric("始値から", fmt_pct(intra['from_open']))
                    with w: st.metric("高値から", fmt_pct(intra['from_high']))

                # 個別銘柄版：キオクシア固定を廃止し、入力銘柄に応じて関連テーマを自動選択
                st.markdown("#### 🚀 関連テーマ / 同業・関連株")
                if rel["score"] is not None:
                    tc1,tc2=st.columns([1,2])
                    with tc1:
                        st.markdown(f"### {rel['theme']}")
                        st.markdown(f"## {rel['score']:.1f} / 10")
                        st.caption("入力銘柄に応じて関連テーマを自動選択")
                    with tc2:
                        if not rel["df"].empty:
                            show=rel["df"].copy()
                            show["銘柄"] = show["symbol"].str.replace(r"\.T$", "", regex=True)
                            st.dataframe(show[["銘柄","change","close"]].rename(columns={"change":"前日比","close":"終値"}).style.format({"前日比":"{:+.2f}%","終値":"{:,.2f}"}), hide_index=True, use_container_width=True)
                else:
                    st.info(f"関連テーマ：{rel['theme']}（比較データを取得できませんでした）")

                # 個別銘柄版：日経225ではなく入力銘柄自身の寄り天リスク
                st.markdown("#### ⚠️ 個別銘柄の寄り天リスク")
                if yr:
                    rc1,rc2,rc3,rc4=st.columns(4)
                    with rc1: st.metric("寄り天リスク", f"{yr['risk']}%")
                    with rc2: st.metric("寄り付きギャップ", fmt_pct(intra["gap"]))
                    with rc3: st.metric("VWAP", f"{yr['vwap']:,.0f}円")
                    with rc4: st.metric("現在のVWAP比", "上" if yr["last_above_vwap"] else "下")
                    st.progress(yr["risk"]/100)
                    st.caption(f"{yr['label']}　個別銘柄のギャップ・始値比・高値比・VWAP・初動高値を合成した参考指標。")
                else:
                    st.info("当日の5分足を取得できないため、個別銘柄の寄り天リスクは計算できません。")

                # 信用需給：JPX公表の信用買残・売残から倍率・回転日数・内訳を表示
                st.markdown("#### 💳 信用需給（信用買残・売残）")
                mrg = z.get("margin")
                if mrg:
                    m1,m2,m3,m4=st.columns(4)
                    with m1: st.metric("信用買残", f"{mrg['buy']:,.0f}株", fmt_shares_delta(mrg["buy_chg"]), delta_color="off")
                    with m2: st.metric("信用売残", f"{mrg['sell']:,.0f}株", fmt_shares_delta(mrg["sell_chg"]), delta_color="off")
                    with m3: st.metric("信用倍率", "—（売残なし）" if mrg["ratio"] is None else f"{mrg['ratio']:.2f}倍")
                    with m4: st.metric("信用需給スコア", f"{mrg['score']:.1f} / 10", f"総合へ {mrg['adj']:+.1f}", delta_color="off")

                    n1,n2,n3,n4=st.columns(4)
                    with n1: st.metric("買残 / 上場株数", "—" if mrg["buy_ratio"] is None else f"{mrg['buy_ratio']:.1f}%")
                    with n2: st.metric("買残の回転日数", "—" if mrg["days"] is None else f"{mrg['days']:.1f}日分", help="信用買残 ÷ 直近20日平均出来高")
                    with n3: st.metric("買残金額", "—" if mrg["buy_value"] is None else f"{mrg['buy_value']/1e8:,.1f}億円", help="買残 × 現在値（概算）")
                    with n4: st.metric("買残の制度信用比率", "—" if mrg["std_share"] is None else f"{mrg['std_share']:.0f}%", help="制度信用は返済期日が6か月")

                    def _i(v): return "—" if v is None else f"{v:,.0f}"
                    def _d(v): return "—" if v is None else f"{v:+,.0f}"
                    brk = pd.DataFrame([
                        {"区分":"買残", "合計":_i(mrg["buy"]), "前日比":_d(mrg["buy_chg"]),
                         "一般信用":_i(mrg["buy_neg"]), "(前日比)":_d(mrg["buy_neg_chg"]),
                         "制度信用":_i(mrg["buy_std"]), "(前日比 )":_d(mrg["buy_std_chg"])},
                        {"区分":"売残", "合計":_i(mrg["sell"]), "前日比":_d(mrg["sell_chg"]),
                         "一般信用":_i(mrg["sell_neg"]), "(前日比)":_d(mrg["sell_neg_chg"]),
                         "制度信用":_i(mrg["sell_std"]), "(前日比 )":_d(mrg["sell_std_chg"])},
                    ])
                    st.dataframe(brk, hide_index=True, use_container_width=True)
                    st.caption(f"{mrg['label']}　{mrg['comment']}　{mg.fmt_asof(margin_info['asof'])} 申込み現在（単位：株）。参考指標であり売買を推奨するものではありません。")

                    # 判断基準コメント：各指標の判定と、株価×買残×売残の前日比の組み合わせ解釈
                    cb = mrg.get("combo")
                    if cb:
                        st.markdown(f"**📈 前日比の読み（{mg.fmt_asof(margin_info['asof'])} 基準）**　{cb['judge']}")
                        st.caption(f"{cb['title']}\n\n{cb['text']}")
                    with st.expander("📖 各指標の判定と判断基準", expanded=False):
                        st.dataframe(
                            pd.DataFrame(mrg["details"], columns=["指標", "値", "判定"]),
                            hide_index=True, use_container_width=True)
                        st.caption("基準の一覧と前日比の組み合わせ表はサイドバー『📖 信用需給の見方』にあります。")
                elif mdf is not None and len(mdf):
                    st.info("この銘柄は今回の信用残一覧に見つかりませんでした（信用取引の対象外、または新規上場等）。")
                else:
                    st.info("信用残データが未取得のため表示できません。")

                show_five_axis_scores(z)
                st.info("🎯 今日の戦略：" + z["strategy"])
                st.markdown('</div>', unsafe_allow_html=True)
        elif not invalid:
            st.error("銘柄データを取得できませんでした。コードが上場銘柄として有効か、ネットワーク接続を確認してください。")

# Market
market={}
for name,sym in MARKETS.items():
    z=latest_stats(sym)
    if z: market[name]=z

score=market_score(market)
if score >= 6.5:
    overall="🚀 リスクオン"
elif score >= 5.3:
    overall="🟢 やや強気"
elif score >= 4.5:
    overall="🤔 様子見"
else:
    overall="🔴 下落警戒"

st.subheader("🤔 総合相場判断")
c1,c2=st.columns([1,2])
with c1:
    st.markdown(f"### {overall}")
    st.markdown(f"## {score:.1f} / 10")
    st.caption("指数・海外市場・為替を合成した参考スコア")
with c2:
    cols=st.columns(4)
    for i,(name,z) in enumerate(market.items()):
        with cols[i%4]:
            st.metric(name, f"{z['close']:,.2f}", fmt_pct(z["change"]))

# Sectors
st.subheader("💰 資金流入・流出セクター")
sdf=pull_sector_df()
if sdf.empty:
    st.warning("セクターデータを取得できませんでした。ネットワークまたはデータ提供元を確認してください。")
else:
    a,b=st.columns(2)
    with a:
        st.markdown("#### 🟢 資金流入候補")
        st.dataframe(sdf.head(6)[["セクター","変化率"]].style.format({"変化率":"{:+.2f}%"}),hide_index=True,use_container_width=True)
    with b:
        st.markdown("#### 🔴 資金流出候補")
        st.dataframe(sdf.tail(6).sort_values("変化率")[["セクター","変化率"]].style.format({"変化率":"{:+.2f}%"}),hide_index=True,use_container_width=True)
    st.caption("Ver.1ではJPXのTOPIX-17系統に対応する17業種ETFをセクター代理として使用。JPXは33業種分類とTOPIX-17を定義しています。")

# Themes
st.subheader("🚀 テーマ強弱")
theme_rows=[]
for name,symbols in THEMES.items():
    df=basket_stats(symbols)
    sc=theme_score(df)
    if sc is not None:
        theme_rows.append({"テーマ":name,"スコア":sc,"平均騰落":df.change.mean(),"上昇率":(df.change>0).mean()*100})
tdf=pd.DataFrame(theme_rows).sort_values("スコア",ascending=False) if theme_rows else pd.DataFrame()
if not tdf.empty:
    st.dataframe(tdf.style.format({"スコア":"{:.1f}","平均騰落":"{:+.2f}%","上昇率":"{:.0f}%"}),hide_index=True,use_container_width=True)

# 個別銘柄分析へ統合済み
st.caption("💾 キオクシア / メモリ判定、⚠️ 寄り天リスク、💳 信用需給 は、上の『個別銘柄分析』で入力した銘柄ごとに自動表示します。")

# Strategy
st.subheader("🎯 今日の戦略")
if score >= 6.5:
    strategy="強いテーマの押し目を優先。高値追いよりVWAP・前場安値付近の反応を確認。"
elif score >= 5.3:
    strategy="強いセクターに絞り、寄り付き直後の追随を避けて押し目を待つ。"
elif score >= 4.5:
    strategy="無理なエントリーを避ける。指数より相対的に強いテーマだけ監視。"
else:
    strategy="売り優勢の可能性。リバウンド狙いは小さく、VWAP回復など明確な反転を確認。"
st.info(strategy)

# 最終総合判断：市場スコア＋日経の時間軸/失速/寄り天＋セクター＋入力銘柄（信用需給含む）
st.subheader("🧭 最終総合判断")
_now_jst = datetime.now(timezone(timedelta(hours=9)))
_nk_intra = intraday_stats("^N225")
_nk_yr = individual_yoriten_risk("^N225", _nk_intra) if _nk_intra else None
_fj = fjm.build_final_judgment(
    market=market, market_score=score, sdf=sdf,
    nk_intra=_nk_intra, nk_yoriten=_nk_yr, nk_daily=get_daily("^N225"),
    results=results, now=_now_jst,
)
st.markdown(fjm.render_html(_fj), unsafe_allow_html=True)

# Sidebar settings
with st.sidebar:
    st.header("⚙️ 設定")
    st.write("データ取得：Yahoo Finance 公開チャート")
    st.write("更新間隔：45秒（キャッシュ）")
    st.divider()
    st.subheader("テーマ編集")
    st.caption("app.py の THEMES を編集すると監視テーマを追加できます。")
    st.code("THEMES = {\\n  'メモリ': ['285A.T','SNDK','MU'],\\n  'AI・半導体': [...]\\n}", language="python")
    st.divider()
    st.warning("⚠️ データの利用条件・遅延・正確性は各提供元の規約に従ってください。本アプリは研究・個人利用向けの試作です。")
