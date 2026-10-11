# -*- coding: utf-8 -*-
"""5軸デイトレ判定エンジン。未取得値は推測で補完しない。"""
import numpy as np
import pandas as pd

AXIS_META = {
    "capital": ("💰", "資金流入スコア"),
    "supply": ("⚖️", "需給スコア"),
    "large": ("🏦", "大口動向スコア"),
    "trend": ("📈", "トレンドスコア"),
    "yoriten": ("⚠️", "寄り天リスク"),
}


def _clip(v, lo=0, hi=100):
    return float(np.clip(v, lo, hi))


def _axis(score, status, detail, **extra):
    return {"score": None if score is None else _clip(score), "status": status, "detail": detail, **extra}


def calculate_scores(daily, intra, margin=None, yoriten=None, large_flow=None,
                     volume_ratio=None, avg_daily_volume=None):
    axes = {}
    # 1) 資金流入: daily change, daily volume ratio, intraday turnover approximation.
    change = daily.get("change") if daily else None
    turnover = None
    if intra is not None and isinstance(intra.get("df"), pd.DataFrame) and not intra["df"].empty:
        d = intra["df"]
        vol = pd.to_numeric(d.get("volume"), errors="coerce").fillna(0)
        close = pd.to_numeric(d.get("close"), errors="coerce")
        if len(vol) and float(vol.sum()) > 0:
            turnover = float((vol * close).sum())
    if change is not None:
        score = 50 + _clip(float(change), -5, 5) * 7
        details = [f"前日比 {float(change):+.2f}%"]
        available_parts = 1
        if volume_ratio is not None:
            score += float(np.clip((float(volume_ratio) - 1.0) * 12, -12, 18))
            details.append(f"出来高倍率 {float(volume_ratio):.2f}倍")
            available_parts += 1
        else:
            details.append("出来高倍率 未取得")
        if turnover is not None:
            score += 5 if turnover > 0 else 0
            if turnover >= 1e8:
                turnover_text = f"{turnover/1e8:.2f}億円"
            elif turnover >= 1e4:
                turnover_text = f"{turnover/1e4:.1f}万円"
            else:
                turnover_text = f"{turnover:,.0f}円"
            details.append(f"当日売買代金概算 {turnover_text}")
            available_parts += 1
        else:
            details.append("売買代金概算 未取得")
        axes["capital"] = _axis(score, "取得済み" if available_parts == 3 else "一部取得", "／".join(details), turnover=turnover)
    else:
        axes["capital"] = _axis(None, "未取得", "価格変化率を取得できません")

    # 2) Supply/demand: official JPX margin balances and ratio. OU ratio is not available.
    if margin:
        ratio = margin.get("ratio")
        buy_chg, sell_chg = margin.get("buy_chg"), margin.get("sell_chg")
        score = 50.0
        notes = []
        used = 0
        if ratio is not None:
            score += float(np.clip((2.0 - float(ratio)) * 8, -20, 20))
            notes.append(f"JPX信用倍率 {float(ratio):.2f}倍")
            used += 1
        if buy_chg is not None:
            notes.append(f"信用買残増減 {float(buy_chg):+,.0f}株")
            score += float(np.clip(-np.sign(float(buy_chg)) * min(abs(float(buy_chg)) / max(float(avg_daily_volume or 1), 1) * 10, 8), -8, 8))
            used += 1
        if sell_chg is not None:
            notes.append(f"信用売残増減 {float(sell_chg):+,.0f}株")
            score += float(np.clip(np.sign(float(sell_chg)) * min(abs(float(sell_chg)) / max(float(avg_daily_volume or 1), 1) * 10, 8), -8, 8))
            used += 1
        notes.append("OU比率 未取得")
        axes["supply"] = _axis(score, "取得済み" if used >= 2 else "一部取得", "／".join(notes) if notes else "信用残データあり。倍率・増減は一部未取得")
    else:
        axes["supply"] = _axis(None, "未取得", "JPX信用買残・売残・信用倍率が未取得。OU比率も未連携")

    # 3) Large-flow proxy: publicly disclosed institutional short delta + personal credit changes.
    # This is not real-time time-and-sales; no trade tape is fabricated.
    flow = large_flow or {}
    short_delta = flow.get("short_delta")
    credit_sell = flow.get("credit_sell_delta")
    credit_buy = flow.get("credit_buy_delta")
    flow_values = [v for v in (short_delta, credit_sell, credit_buy) if v is not None]
    if flow.get("status") == "取得済み" and flow_values and avg_daily_volume is not None and float(avg_daily_volume) > 0:
        score = 50.0
        notes = []
        denom = float(avg_daily_volume)
        if short_delta is not None:
            # More institutional shorting is bearish; short covering is bullish.
            impact = float(np.clip(float(short_delta) / denom * 100, -20, 20))
            score -= impact
            notes.append(f"機関空売り増減 {float(short_delta):+,.0f}株（増加は弱気）")
        if credit_buy is not None:
            impact = float(np.clip(float(credit_buy) / denom * 35, -10, 10))
            score -= impact
            notes.append(f"個人信用買増減 {float(credit_buy):+,.0f}株（増加は上値負担寄り）")
        if credit_sell is not None:
            impact = float(np.clip(float(credit_sell) / denom * 25, -8, 8))
            score += impact
            notes.append(f"個人信用売増減 {float(credit_sell):+,.0f}株（増加は買戻し余地）")
        axes["large"] = _axis(score, "取得済み", "／".join(notes), source=flow.get("url"), date=flow.get("latest_date"))
    elif flow.get("status") == "取得済み" and flow_values:
        axes["large"] = _axis(None, "一部取得", "公開増減値は取得済みですが、比較基準となる平均出来高が未取得のためスコア未算出", source=flow.get("url"), date=flow.get("latest_date"))
    else:
        axes["large"] = _axis(None, "未取得", flow.get("reason", "機関空売り・個人信用増減が未取得"), source=flow.get("url"))

    # 4) Trend: current intraday VWAP position and higher highs/higher lows.
    if intra is not None and isinstance(intra.get("df"), pd.DataFrame) and len(intra["df"]) >= 3:
        d = intra["df"].copy().tail(12)
        vol = pd.to_numeric(d["volume"], errors="coerce").fillna(0)
        typical = (pd.to_numeric(d["high"], errors="coerce") + pd.to_numeric(d["low"], errors="coerce") + pd.to_numeric(d["close"], errors="coerce")) / 3
        vwap = float((typical * vol).sum() / vol.sum()) if vol.sum() > 0 else float(d.iloc[-1]["close"])
        last = float(d.iloc[-1]["close"])
        highs = pd.to_numeric(d["high"], errors="coerce").dropna().to_numpy()
        lows = pd.to_numeric(d["low"], errors="coerce").dropna().to_numpy()
        higher = len(highs) >= 4 and highs[-1] > highs[-3] and lows[-1] > lows[-3]
        score = 50 + (18 if last >= vwap else -18) + (18 if higher else -8)
        axes["trend"] = _axis(score, "取得済み", f"直近12本の終値はVWAP{'上' if last >= vwap else '下'}／高値・安値切り上げ {'あり' if higher else '未確認'}", vwap=vwap, last=last)
    else:
        axes["trend"] = _axis(None, "未取得", "当日の5分足が不足。VWAP・高値安値推移を計算できません")

    # 5) Existing individual opening reversal risk. Higher risk means more bearish.
    if yoriten is not None and yoriten.get("risk") is not None:
        risk = float(yoriten["risk"])
        axes["yoriten"] = _axis(risk, "取得済み", f"寄り天リスク {risk:.0f}%／{yoriten.get('label', '')}")
    else:
        axes["yoriten"] = _axis(None, "未取得", "当日の5分足不足のため算出できません")

    # Only valid axes contribute; missing values are not treated as zero.
    available = [k for k, v in axes.items() if v["score"] is not None]
    directional = []
    for key in available:
        s = axes[key]["score"]
        directional.append((100 - s, s) if key == "yoriten" else (s, 100 - s))
    if directional:
        buy = sum(x for x, _ in directional) / len(directional)
        sell = sum(y for _, y in directional) / len(directional)
    else:
        buy = sell = 0.0
    risk = axes["yoriten"]["score"]
    buy_blocked = risk is not None and risk >= 65
    if len(available) < 2:
        decision = "様子見"
    elif buy_blocked:
        decision = "売り候補" if sell >= 60 and sell >= buy + 10 else "様子見"
    elif buy >= 65 and buy >= sell + 10:
        decision = "買い候補"
    elif sell >= 65 and sell >= buy + 10:
        decision = "売り候補"
    else:
        decision = "様子見"
    return {"axes": axes, "buy_score": _clip(buy), "sell_score": _clip(sell),
            "available_count": len(available), "decision": decision,
            "buy_blocked": buy_blocked}
