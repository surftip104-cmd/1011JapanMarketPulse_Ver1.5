# -*- coding: utf-8 -*-
"""
最終総合判断カード。
市場スコア（指数・海外・為替）に、日経の時間軸別モメンタム／失速／寄り天、資金流入セクター、
入力銘柄（個別スコア・信用需給・寄り天）を重ねて、1枚のカードにまとめる。
参考情報であり売買を推奨するものではない。
"""
import html
import math
from datetime import timezone, timedelta

import numpy as np

JST = timezone(timedelta(hours=9))
TONE_COLOR = {"good": "#20d89a", "warn": "#ffc857", "bad": "#ff6574", "info": "#6cb6ff"}


def session_info(now):
    """JST の now から相場の時間帯ラベルと、出来高の経過割合(0-1, 不明はNone)を返す。"""
    if now.weekday() >= 5:
        return {"emoji": "🔴", "text": "休場日（直近営業日のデータ）", "frac": None, "state": "closed"}
    t = now.hour * 60 + now.minute
    total = 330.0  # 9:00-11:30(150分) + 12:30-15:30(180分)
    if t < 9 * 60:
        return {"emoji": "🔵", "text": "寄り付き前（〜9:00）", "frac": 0.0, "state": "pre"}
    if t < 11 * 60 + 30:
        return {"emoji": "🟢", "text": "場中前場（〜11:30）", "frac": (t - 540) / total, "state": "open"}
    if t < 12 * 60 + 30:
        return {"emoji": "🟡", "text": "昼休み（〜12:30）", "frac": 150 / total, "state": "open"}
    if t < 15 * 60 + 30:
        return {"emoji": "🟢", "text": "場中後場（〜15:30）", "frac": (150 + t - 750) / total, "state": "open"}
    return {"emoji": "⚪", "text": "大引け後", "frac": 1.0, "state": "closed"}


def _ret(close, n):
    if close is None or len(close) <= n:
        return None
    a, b = float(close.iloc[-1 - n]), float(close.iloc[-1])
    return (b / a - 1) * 100 if a else None


def _short(name, n=10):
    name = str(name or "")
    return name if len(name) <= n else name[:n] + "…"


def build_final_judgment(market, market_score, sdf, nk_intra, nk_yoriten, nk_daily, results, now):
    sess = session_info(now)
    score = float(market_score)
    tags, sents = [], []          # tags: (text, tone) / sents: 先読み文の各文

    nk = (market or {}).get("日経225") or {}
    day_chg = nk.get("change")

    # --- 時間軸別モメンタム（直近1時間・当日・5日・20日）---
    frames = []
    if nk_intra and nk_intra.get("df") is not None and len(nk_intra["df"]) >= 13:
        r1 = _ret(nk_intra["df"].close, 12)
        if r1 is not None:
            frames.append(("直近1時間", r1, 0.1))
    if nk_intra:
        frames.append(("当日", float(nk_intra["from_open"]), 0.1))
    if nk_daily is not None and len(nk_daily) > 0:
        for label, n in (("5日", 5), ("20日", 20)):
            r = _ret(nk_daily.close, n)
            if r is not None:
                frames.append((label, r, 0.2))
    ups = sum(1 for _, r, t in frames if r > t)
    downs = sum(1 for _, r, t in frames if r < -t)
    need = max(2, math.ceil(len(frames) * 0.75)) if len(frames) >= 2 else 99
    trend = "down" if downs >= need else ("up" if ups >= need else None)

    # --- 失速の兆し（上昇していても勢いが落ちている）---
    fade = False
    if nk_intra:
        h1 = frames[0][1] if frames and frames[0][0] == "直近1時間" else None
        if day_chg is not None and day_chg > 0.3 and (nk_intra["from_high"] <= -0.4 or (h1 is not None and h1 < -0.1)):
            fade = True
        if nk_intra["gap"] > 0.5 and nk_intra["from_open"] < 0:
            fade = True
    if fade:
        score -= 0.6
        tags.append(("⚠️失速の兆し", "warn"))
        sents.append("上昇の勢いが失速（早期警戒）。")
    if trend == "down":
        score -= 0.8
        tags.append(("下落優勢", "bad"))
        sents.append("複数の時間軸で下落基調。")
    elif trend == "up":
        score += 0.4
        tags.append(("上昇優勢", "good"))
        sents.append("複数の時間軸で上昇基調。")

    # --- 日経の大きな動き ---
    if day_chg is not None and day_chg >= 1.5:
        tags.append(("日経大幅高", "good"))
        sents.append("日経平均が大幅高で買い意欲旺盛。")
    elif day_chg is not None and day_chg <= -1.5:
        tags.append(("日経大幅安", "bad"))
        sents.append("日経平均が大幅安で売り圧力が強い。")

    # --- 寄り天リスク（日経225自身の5分足）---
    if nk_yoriten:
        risk = nk_yoriten["risk"]
        if risk >= 65:
            score -= 0.8
            tags.append(("寄り天注意", "warn"))
            sents.append("寄り天リスクが高く高値掴みに警戒。")
        elif risk >= 35:
            score -= 0.3
        else:
            score += 0.2

    # --- 資金流入セクター ---
    sectors = []
    if sdf is not None and len(sdf) > 0:
        top = sdf.sort_values("変化率", ascending=False).head(3)
        sectors = [(str(r["セクター"]), float(r["変化率"])) for _, r in top.iterrows()]
        avg = float(np.mean([p for _, p in sectors]))
        if avg >= 1.0 and sum(1 for _, p in sectors if p > 0) >= 2:
            score += 0.4
            tags.append(("セクター好調", "good"))
            sents.append("・".join(n for n, _ in sectors) + "など複数セクターが強く物色対象が豊富。")
        elif avg < 0:
            score -= 0.4
            tags.append(("セクター不調", "bad"))
            sents.append("資金流入セクターが乏しく物色対象が限られる。")

    # --- 入力銘柄（個別スコア・信用需給・寄り天）---
    results = results or []
    if results:
        mean_ind = float(np.mean([z["score"] for z in results]))
        score += float(np.clip((mean_ind - 5.0) * 0.3, -0.9, 0.9))

        vrs = [z["volume_ratio"] for z in results if z.get("volume_ratio")]
        f = sess["frac"]
        if vrs and f is not None and f >= 0.25:
            vr = float(np.mean(vrs)) / f if f < 1.0 else float(np.mean(vrs))
            if vr < 0.7:
                tags.append(("出来高少", "info"))
                sents.append("入力銘柄の出来高が普段よりかなり少なく様子見ムード。")
            elif vr < 0.85:
                sents.append("入力銘柄の出来高は普段よりやや少なめ。")
            elif vr > 1.5:
                tags.append(("出来高多", "info"))
                sents.append("入力銘柄の出来高が膨らみ参加者が多い。")

        best = max(results, key=lambda z: z["score"])
        worst = min(results, key=lambda z: z["score"])
        if len(results) == 1:
            sents.append(f"入力銘柄 {best['code']} {_short(best['name'])} は{best['action'][2:]}（{best['score']:.1f}/10）。")
        else:
            sents.append(f"入力銘柄では {best['code']} {_short(best['name'])} が最も強く（{best['score']:.1f}）、"
                         f"{worst['code']} {_short(worst['name'])} が最も弱い（{worst['score']:.1f}）。")
        heavy = [z["code"] for z in results if z.get("margin") and z["margin"]["score"] <= 3.5]
        light = [z["code"] for z in results if z.get("margin") and z["margin"]["score"] >= 6.5]
        hot = [z["code"] for z in results if z.get("yoriten") and z["yoriten"]["risk"] >= 65]
        if heavy:
            tags.append(("信用買残が重い", "warn"))
            sents.append("、".join(heavy) + " は信用買残が重く戻り売りに警戒。")
        if light:
            sents.append("、".join(light) + " は信用需給が軽く上値余地。")
        if hot:
            sents.append("、".join(hot) + " は個別の寄り天リスクが高い。")

    score = float(np.clip(score, 0, 10))

    # --- 判定 ---
    if score >= 7.0:
        emoji, title, sub, tone = "🚀", "積極エントリー可", "強い流れに乗る", "good"
    elif score >= 5.6:
        emoji, title, sub, tone = "🟢", "やや強気", "押し目中心で選別", "good"
    elif score >= 3.6:
        emoji, title, sub, tone = "🤔", "様子見推奨", "慎重な見極めを", "warn"
    elif score >= 2.5:
        emoji, title, sub, tone = "🟠", "守り重視", "リバウンド狙いは小さく", "warn"
    else:
        emoji, title, sub, tone = "🔴", "見送り推奨", "ポジションは軽く", "bad"

    st_ = sess["state"]
    look = {"pre": "寄り付きの動きを確認し", "open": "次の値動きを確認し"}.get(st_, "翌営業日の寄り付きの動きを確認し")
    if score >= 7.0:
        closing = "強いテーマの押し目を優先し、高値追いは避けたい。"
    elif score >= 5.6:
        closing = "強い銘柄に絞り、押し目を待って入りたい。"
    elif score >= 3.6:
        closing = look + "、無理なエントリーは避けたい。"
    elif score >= 2.5:
        closing = "ポジションは軽めにし、反転確認までは見送り中心で。"
    else:
        closing = "新規は見送りを基本に、戻りの弱さを確認したい。"
    outlook = " ".join(sents + [closing])

    return {
        "score": score, "emoji": emoji, "title": title, "subtitle": sub, "tone": tone,
        "tags": tags[:6], "sectors": sectors, "outlook": outlook,
        "time": now.strftime("%H:%M:%S"), "session": f"{sess['emoji']}{sess['text']}",
        "data_date": nk.get("date"), "today": now.strftime("%Y-%m-%d"),
        "frames": frames,
    }


FJ_CSS = (
    "<style>"
    ".fj{border-radius:22px;padding:24px 28px;margin:6px 0 16px;background:linear-gradient(145deg,#171108,#101318);}"
    ".fj-top{display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px;color:#9aa2ad;font-weight:700;letter-spacing:.04em;}"
    ".fj-top .t{font-family:ui-monospace,Menlo,Consolas,monospace;font-weight:500;}"
    ".fj-main{display:flex;align-items:center;gap:20px;margin:14px 0 6px;}"
    ".fj-emoji{font-size:3.4rem;line-height:1;}"
    ".fj-title{font-size:2.3rem;font-weight:800;line-height:1.15;}"
    ".fj-sub{color:#9aa2ad;font-size:1.05rem;margin-top:4px;}"
    ".fj-sub b{color:#c9ced6;}"
    ".fj-tags{display:flex;flex-wrap:wrap;gap:10px;margin:14px 0 6px;}"
    ".fj-tag,.fj-sec{border-radius:10px;padding:7px 14px;font-weight:700;font-size:.95rem;border:1px solid;}"
    ".fj-good{color:#20d89a;border-color:#1f7a5c;background:rgba(32,216,154,.10);}"
    ".fj-warn{color:#ffc857;border-color:#8a6a1f;background:rgba(255,200,87,.10);}"
    ".fj-bad{color:#ff6574;border-color:#8a3340;background:rgba(255,101,116,.10);}"
    ".fj-info{color:#6cb6ff;border-color:#2c5a8a;background:rgba(108,182,255,.10);}"
    ".fj-sec{color:#20d89a;border-color:#1f7a5c;background:rgba(32,216,154,.10);font-size:1.05rem;padding:9px 16px;}"
    ".fj-h{color:#9aa2ad;margin:18px 0 8px;}"
    ".fj-out{background:rgba(255,255,255,.04);border-radius:16px;padding:18px 20px;margin-top:18px;color:#d5d9e0;line-height:1.75;font-size:1.02rem;}"
    ".fj-note{color:#6f7782;font-size:.78rem;margin-top:10px;}"
    "</style>"
)


def render_html(fj):
    e = html.escape
    color = TONE_COLOR[fj["tone"]]
    right = f"{e(fj['time'])} ・ {e(fj['session'])}"
    if fj.get("data_date") and fj["data_date"] != fj["today"]:
        right += f" ・ データ基準日 {e(fj['data_date'])}"
    tags = "".join(f'<span class="fj-tag fj-{t}">{e(x)}</span>' for x, t in fj["tags"])
    secs = "".join(f'<span class="fj-sec">{e(n)} {p:+.1f}%</span>' for n, p in fj["sectors"])
    parts = [
        FJ_CSS,
        f'<div class="fj" style="border:2px solid {color}99;">',
        f'<div class="fj-top"><span>総合相場判断</span><span class="t">{right}</span></div>',
        '<div class="fj-main">',
        f'<div class="fj-emoji">{fj["emoji"]}</div>',
        f'<div><div class="fj-title" style="color:{color};">{e(fj["title"])}</div>',
        f'<div class="fj-sub">{e(fj["subtitle"])} ｜ <b>{fj["score"]:.1f}/10</b></div></div>',
        '</div>',
    ]
    if tags:
        parts.append(f'<div class="fj-tags">{tags}</div>')
    if secs:
        parts.append(f'<div class="fj-h">💰 資金流入セクター（騰落率上位）</div><div class="fj-tags">{secs}</div>')
    parts.append(f'<div class="fj-out">🔮 先読み: {e(fj["outlook"])}</div>')
    parts.append('<div class="fj-note">指数・時間軸別モメンタム・寄り天・セクター・入力銘柄（信用需給含む）を合成した参考判断です。売買を推奨するものではありません。</div>')
    parts.append('</div>')
    return "".join(parts)
