# -*- coding: utf-8 -*-
"""
JPX「銘柄別信用取引残高」(日々16:00目安で公表) の取得・解析・需給評価。

- 公表ページ: https://www.jpx.co.jp/markets/statistics-equities/margin/01.html
- ファイル名規則: YYYYMMDD_mtall.pdf (YYYYMMDD = 申込み現在日。公表は翌営業日16:00目安)
- 単位: 株数(1株単位)
"""
import os
import re
import glob
from urllib.parse import urljoin

import numpy as np
import pandas as pd
import requests

JPX_PAGE = "https://www.jpx.co.jp/markets/statistics-equities/margin/01.html"
UA = {"User-Agent": "Mozilla/5.0"}
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "margin_cache")
MIN_ROWS = 1000          # 全銘柄PDFなら約4,000行。これ未満は解析失敗とみなす
ADJ_WEIGHT = 0.2         # 信用需給スコア(0-10)の総合スコアへの影響度 (±1.0)

# app.py の信用需給ガイド表示と互換性を保つための判定基準
# 方向は「株価・買残・売残」の順。変化率が基準を超えた場合に up/down と判定。
DIR_TH = 3.0            # 買残・売残の増減率しきい値(%)
PRICE_TH = 1.0          # 株価変化率しきい値(%)
CRITERIA = [
    ("信用倍率", "買残 ÷ 売残。倍率が高いほど買い長で、将来の戻り売り圧力に注意"),
    ("買残の増減", "買残増加は将来の売り圧力になりやすく、減少は整理進行の可能性"),
    ("売残の増減", "売残増加は買い戻し需要の余地、減少は買い戻し一巡の可能性"),
    ("買残／平均出来高", "買残を平均出来高で割った目安。日数が大きいほど需給が重い傾向"),
]
COMBO = {
    ("up", "up", "up"): ("上昇・買残増・売残増", "株価上昇に信用買いと売りの双方が増加。売買が拮抗している可能性"),
    ("up", "up", "down"): ("上昇・買残増・売残減", "買い主導の上昇。買残の積み上がりによる上値の重さに注意"),
    ("up", "down", "up"): ("上昇・買残減・売残増", "買残整理と売り増加を伴う上昇。踏み上げの可能性もある"),
    ("up", "down", "down"): ("上昇・買残減・売残減", "信用残が整理されながら上昇。需給改善の可能性"),
    ("down", "up", "up"): ("下落・買残増・売残増", "下落中に信用残が増加。需給悪化に警戒"),
    ("down", "up", "down"): ("下落・買残増・売残減", "買い下がりが増え、売り方が減少。戻り売り圧力に注意"),
    ("down", "down", "up"): ("下落・買残減・売残増", "買残整理と売り増加を伴う下落。下落継続・買い戻し双方に注意"),
    ("down", "down", "down"): ("下落・買残減・売残減", "信用残が整理されながら下落。投げ売り一巡かは価格推移も確認"),
}

COLS = ["code", "sell", "sell_chg", "sell_ratio", "buy", "buy_chg", "buy_ratio",
        "sell_neg", "sell_neg_chg", "sell_std", "sell_std_chg",
        "buy_neg", "buy_neg_chg", "buy_std", "buy_std_chg"]

# 例: "... 貸 13010 JP3257200000 株数 Shs. 9,400 600 0.1% 160,100 2,400 1.3% 0 0 9,400 600 38,100 600 122,000 1,800"
#   売残(計,前日比,上場比) 買残(計,前日比,上場比) 売:一般(残,前日比) 売:制度(残,前日比) 買:一般(残,前日比) 買:制度(残,前日比)
# 新証券コード(英字入り)は 130A0 → 130A、285A0 → 285A。ETFの上場比は "*"、減少は "▲"。
_ISIN = r"[A-Z]{2}[A-Z0-9]{10}"
_NUM = r"(?:▲\s*)?(?:[\d,]+(?:\.\d+)?%?|\*)"
ROW_RE = re.compile(
    r"(?<![0-9A-Z])(\d{3}[0-9A-Z])0\s+" + _ISIN + r"\s+株数\s*Shs\.\s*"
    r"((?:" + _NUM + r"(?=\s|$)\s*){14})"
)
TOK_RE = re.compile(r"(▲\s*)?([\d,]+(?:\.\d+)?%?|\*)")
ASOF_RE = re.compile(r"(\d{4})/(\d{1,2})/(\d{1,2})\s*申込み現在")


def _conv(sign, tok):
    if tok == "*":
        return np.nan
    if tok.endswith("%"):
        return float(tok[:-1])
    v = float(tok.replace(",", ""))
    return -v if sign else v


def parse_margin_text(text):
    """PDFから得たテキストを解析し、code索引のDataFrameを返す。"""
    text = str(text).replace("　", " ").replace("\xa0", " ")
    rows, seen = [], set()
    for m in ROW_RE.finditer(text):
        code = m.group(1)
        if code in seen:
            continue
        vals = [_conv(s, t) for s, t in TOK_RE.findall(m.group(2))][:14]
        if len(vals) < 14:
            continue
        seen.add(code)
        rows.append([code] + vals)
    if not rows:
        return pd.DataFrame(columns=COLS[1:])
    return pd.DataFrame(rows, columns=COLS).set_index("code")


def _rows_from_words(words, tol=2.0):
    """座標付き単語を同一行にまとめ直す(表のセルが1行ずつ分離される抽出対策)。"""
    ws = sorted(words, key=lambda w: (w[1] + w[3]) / 2)
    lines, cur, cy = [], [], None
    for w in ws:
        yc = (w[1] + w[3]) / 2
        if cy is None or abs(yc - cy) <= tol:
            cur.append(w)
            cy = yc if cy is None else (cy * (len(cur) - 1) + yc) / len(cur)
        else:
            lines.append(cur)
            cur, cy = [w], yc
    if cur:
        lines.append(cur)
    return [" ".join(x[4] for x in sorted(l, key=lambda w: w[0])) for l in lines]


def _candidate_texts(data):
    """抽出方法を順に試すためのジェネレータ。"""
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(stream=data, filetype="pdf")
        yield "pymupdf-rows", "\n".join(
            "\n".join(_rows_from_words(p.get_text("words"))) for p in doc)
        yield "pymupdf-text", "\n".join(p.get_text("text") for p in doc)
    except ImportError:
        pass
    except Exception:
        pass
    try:
        import io
        from pypdf import PdfReader
        rd = PdfReader(io.BytesIO(data))
        yield "pypdf", "\n".join((p.extract_text() or "") for p in rd.pages)
    except Exception:
        pass


def parse_pdf_bytes(data):
    """PDFバイト列 → (DataFrame, asof 'YYYYMMDD' or None, 使用した抽出方法)。"""
    best, best_method, best_text = pd.DataFrame(columns=COLS[1:]), None, ""
    for method, text in _candidate_texts(data):
        df = parse_margin_text(text)
        if len(df) > len(best):
            best, best_method, best_text = df, method, text
        if len(df) >= MIN_ROWS:
            break
    asof = None
    m = ASOF_RE.search(best_text.replace("　", " ")) if best_text else None
    if m:
        asof = f"{int(m.group(1)):04d}{int(m.group(2)):02d}{int(m.group(3)):02d}"
    return best, asof, best_method


def find_latest_pdf(timeout=15):
    """JPX公表ページから最新の YYYYMMDD_mtall.pdf を探す。"""
    r = requests.get(JPX_PAGE, headers=UA, timeout=timeout)
    r.raise_for_status()
    hits = re.findall(r'''["'(]([^"'()\s]*?(\d{8})_mtall\.pdf)''', r.text)
    if not hits:
        return None
    url, d = max(hits, key=lambda x: x[1])
    return d, urljoin(JPX_PAGE, url)


def fmt_asof(d):
    return f"{d[:4]}/{d[4:6]}/{d[6:8]}" if d and len(d) == 8 else "不明"


def _csv_path(d):
    return os.path.join(CACHE_DIR, f"{d}_margin.csv")


def _read_cache(path):
    return pd.read_csv(path, dtype={"code": str}).set_index("code")


def load_latest():
    """最新の信用残を取得(ローカルキャッシュ優先)。失敗時は直近キャッシュにフォールバック。"""
    os.makedirs(CACHE_DIR, exist_ok=True)
    info = {"df": None, "asof": None, "source": None, "warn": None}
    latest = None
    try:
        latest = find_latest_pdf()
        if latest is None:
            info["warn"] = "JPXページから信用残PDFのリンクを見つけられませんでした。"
    except Exception as e:
        info["warn"] = f"JPXページに接続できませんでした（{type(e).__name__}）。"

    if latest:
        d, url = latest
        p = _csv_path(d)
        if os.path.exists(p):
            return {**info, "df": _read_cache(p), "asof": d, "source": "キャッシュ（JPX公表分）"}
        try:
            rr = requests.get(url, headers=UA, timeout=90)
            rr.raise_for_status()
            df, asof, method = parse_pdf_bytes(rr.content)
            if len(df) >= MIN_ROWS:
                df.reset_index().to_csv(p, index=False, encoding="utf-8")
                return {**info, "df": df, "asof": d, "source": f"JPXから取得（{method}）"}
            info["warn"] = (f"PDFは取得できましたが解析結果が{len(df)}行でした。"
                            "サイドバーからPDFを手動アップロードするか手入力を使ってください。")
        except Exception as e:
            info["warn"] = f"PDFの取得に失敗しました（{type(e).__name__}）。"

    files = sorted(glob.glob(os.path.join(CACHE_DIR, "*_margin.csv")))
    if files:
        f = files[-1]
        d = os.path.basename(f).split("_")[0]
        return {**info, "df": _read_cache(f), "asof": d, "source": "キャッシュ（最新取得に失敗・古い可能性）"}
    return info


def parse_manual(text):
    """手入力: 1行1銘柄 `コード,買残,買残前日比,売残,売残前日比` → DataFrame。"""
    rows = {}
    for line in str(text or "").splitlines():
        # 桁区切りのカンマ(1,200,000)だけ除去してから区切り文字で分割する
        line = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", line.strip())
        parts = [x for x in re.split(r"[\s,、\t]+", line) if x]
        if len(parts) < 5:
            continue
        code = parts[0].upper().replace(".T", "")
        try:
            buy, bchg, sell, schg = [float(x.replace("▲", "-")) for x in parts[1:5]]
        except ValueError:
            continue
        rows[code] = {c: np.nan for c in COLS[1:]}
        rows[code].update({"buy": buy, "buy_chg": bchg, "sell": sell, "sell_chg": schg})
    return pd.DataFrame.from_dict(rows, orient="index") if rows else pd.DataFrame()


def _f(x):
    try:
        v = float(x)
        return None if np.isnan(v) else v
    except (TypeError, ValueError):
        return None


def analyze_margin(code, mdf, avg_volume=None, price=None, price_chg=None):
    """1銘柄の信用需給評価。需給スコアは 0(悪化)〜10(良好) の参考値。"""
    if mdf is None or len(mdf) == 0 or code not in mdf.index:
        return None
    r = mdf.loc[code]
    buy, sell = _f(r.get("buy")), _f(r.get("sell"))
    if buy is None or sell is None:
        return None
    bchg, schg = _f(r.get("buy_chg")) or 0.0, _f(r.get("sell_chg")) or 0.0
    buy_ratio = _f(r.get("buy_ratio"))        # 買残/上場株数 (%)
    buy_std, buy_neg = _f(r.get("buy_std")), _f(r.get("buy_neg"))

    ratio = buy / sell if sell > 0 else None
    prev_buy = buy - bchg
    buy_chg_pct = bchg / prev_buy * 100 if prev_buy > 0 else None
    days = buy / avg_volume if avg_volume and avg_volume > 0 else None
    std_share = buy_std / buy * 100 if (buy_std is not None and buy > 0) else None
    buy_value = buy * price if price else None

    s, notes = 5.0, []
    if buy < 1000 and sell < 1000:
        notes.append("残高が僅少で需給指標の信頼性は低い")
    else:
        if ratio is None:
            notes.append("売残なし（信用倍率は算出不可・買い長）")
        elif ratio < 1:
            s += 1.5; notes.append(f"売り長（倍率{ratio:.2f}倍）で踏み上げ余地")
        elif ratio < 2:
            s += 0.8; notes.append(f"倍率{ratio:.2f}倍で需給は軽め")
        elif ratio < 5:
            notes.append(f"倍率{ratio:.2f}倍で中立")
        elif ratio < 10:
            s -= 0.8; notes.append(f"倍率{ratio:.1f}倍で買い長・上値は重め")
        else:
            s -= 1.5; notes.append(f"倍率{ratio:.1f}倍と買い長が顕著・戻り売り警戒")
        if buy_chg_pct is not None:
            if buy_chg_pct <= -3:
                s += 0.7; notes.append(f"買残が前日比{buy_chg_pct:+.1f}%で整理進行")
            elif buy_chg_pct >= 3:
                s -= 0.7; notes.append(f"買残が前日比{buy_chg_pct:+.1f}%で積み上がり")
        if days is not None:
            if days < 1:
                s += 0.5; notes.append(f"買残は平均出来高の{days:.1f}日分で軽い")
            elif days < 3:
                pass
            elif days < 6:
                s -= 0.5; notes.append(f"買残は平均出来高の{days:.1f}日分でやや重い")
            else:
                s -= 1.0; notes.append(f"買残は平均出来高の{days:.1f}日分で重い")
    s = float(np.clip(s, 0, 10))

    if s >= 6.5:
        label = "🟢 好需給（上値軽い）"
    elif s >= 4.5:
        label = "🟡 中立"
    elif s >= 3.2:
        label = "🟠 やや重い"
    else:
        label = "🔴 需給悪化（戻り売り警戒）"

    # --- 各指標の判定表（app.py の「各指標の判定と判断基準」用）---
    sell_prev = sell - schg
    sell_chg_pct = schg / sell_prev * 100 if sell_prev > 0 else None

    def _judge_ratio():
        if ratio is None: return "買い長（売残なし）"
        if ratio < 1: return "売り長・踏み上げ余地"
        if ratio < 2: return "軽め"
        if ratio < 5: return "中立"
        if ratio < 10: return "買い長・やや重い"
        return "買い長が顕著・戻り売り警戒"

    def _judge_chg(p, up_msg, down_msg):
        if p is None: return "判定不可"
        if p >= DIR_TH: return up_msg
        if p <= -DIR_TH: return down_msg
        return "横ばい"

    def _judge_days():
        if days is None: return "判定不可"
        if days < 1: return "軽い"
        if days < 3: return "標準"
        if days < 6: return "やや重い"
        return "重い"

    details = [
        ("信用倍率", "—" if ratio is None else f"{ratio:.2f}倍", _judge_ratio()),
        ("買残の増減", "—" if buy_chg_pct is None else f"{buy_chg_pct:+.1f}%",
         _judge_chg(buy_chg_pct, "積み上がり（将来の売り圧力）", "整理進行")),
        ("売残の増減", "—" if sell_chg_pct is None else f"{sell_chg_pct:+.1f}%",
         _judge_chg(sell_chg_pct, "買い戻し余地が増加", "買い戻し一巡の可能性")),
        ("買残／平均出来高", "—" if days is None else f"{days:.1f}日分", _judge_days()),
    ]

    # --- 株価×買残×売残の前日比の組み合わせ ---
    combo = None
    if price_chg is not None and buy_chg_pct is not None and sell_chg_pct is not None:
        def _dir(v, th):
            return "up" if v >= 0 else "down"
        key = (_dir(price_chg, PRICE_TH), _dir(buy_chg_pct, DIR_TH), _dir(sell_chg_pct, DIR_TH))
        if key in COMBO:
            title, text = COMBO[key]
            combo = {"judge": title, "title": title, "text": text}

    return {
        "details": details, "combo": combo,
        "buy": buy, "buy_chg": bchg, "sell": sell, "sell_chg": schg,
        "ratio": ratio, "buy_chg_pct": buy_chg_pct, "buy_ratio": buy_ratio,
        "days": days, "std_share": std_share, "buy_value": buy_value,
        "buy_neg": buy_neg, "buy_std": buy_std,
        "sell_neg": _f(r.get("sell_neg")), "sell_std": _f(r.get("sell_std")),
        "buy_neg_chg": _f(r.get("buy_neg_chg")), "buy_std_chg": _f(r.get("buy_std_chg")),
        "sell_neg_chg": _f(r.get("sell_neg_chg")), "sell_std_chg": _f(r.get("sell_std_chg")),
        "score": s, "label": label, "adj": (s - 5.0) * ADJ_WEIGHT,
        "comment": "。".join(notes) + "。" if notes else "",
        "comment_short": notes[0] + "。" if notes else "",
    }
