# -*- coding: utf-8 -*-
"""japan-kabuka.com の公開空売り・信用増減を取得する補助モジュール。
取得不能/未掲載は None とし、0株に置き換えない。
"""
from __future__ import annotations
import re
from datetime import datetime
from io import StringIO
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

BASE = "https://japan-kabuka.com"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; JapanMarketPulse/1.0)"}


def _number(text):
    s = str(text or "").replace("\u3000", " ").replace(",", "").strip()
    if not s or s in {"-", "—", "–", "－", "未取得", "None"}:
        return None
    # Keep the first signed/unsigned number and understand Japanese 万 units.
    m = re.search(r"([+-]?\s*\d+(?:\.\d+)?)\s*(万)?", s)
    if not m:
        return None
    try:
        v = float(m.group(1).replace(" ", ""))
        if m.group(2):
            v *= 10000
        return v
    except ValueError:
        return None


def _norm(s):
    return re.sub(r"\s+", "", str(s or "")).replace("\u3000", "")


def _table_rows(html):
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")
    parsed = []
    for table in tables:
        rows = []
        for tr in table.find_all("tr"):
            cells = tr.find_all(["th", "td"])
            vals = [_norm(c.get_text(" ", strip=True)) for c in cells]
            if vals:
                rows.append(vals)
        if len(rows) >= 2:
            parsed.append(rows)
    return parsed


def _find_delta_table(html):
    """Find the chart table with date, institution short delta, and personal credit deltas."""
    for rows in _table_rows(html):
        all_text = " ".join(" ".join(r) for r in rows[:4])
        norm = _norm(all_text)
        if "信用売増減" in norm and "信用買増減" in norm:
            header_i = None
            header = None
            for i, row in enumerate(rows[:5]):
                joined = _norm(" ".join(row))
                if "信用売増減" in joined and "信用買増減" in joined:
                    header_i, header = i, row
                    break
            if header is None:
                continue
            # Headers are normally one row. If multi-row, choose the row with most cells.
            cols = [_norm(x) for x in header]
            idx_short = next((i for i,x in enumerate(cols) if "空売増減" in x or "機関空売増減" in x), None)
            idx_sell = next((i for i,x in enumerate(cols) if "信用売増減" in x), None)
            idx_buy = next((i for i,x in enumerate(cols) if "信用買増減" in x), None)
            idx_date = next((i for i,x in enumerate(cols) if x.lower() == "date" or "日付" in x), 0)
            idx_change = next((i for i,x in enumerate(cols) if "前日比" in x), None)
            if idx_sell is None or idx_buy is None:
                continue
            data = []
            for row in rows[header_i+1:]:
                if len(row) <= max(idx_sell, idx_buy):
                    continue
                date = row[idx_date] if idx_date < len(row) else ""
                # Ignore footer rows and repeated headers.
                if not re.search(r"\d{1,2}/\d{1,2}|\d{4}[-/]\d{1,2}[-/]\d{1,2}", date):
                    continue
                def cell(i): return _number(row[i]) if i is not None and i < len(row) else None
                data.append({"date": date, "price_change_pct": cell(idx_change),
                             "short_delta": cell(idx_short), "credit_sell_delta": cell(idx_sell),
                             "credit_buy_delta": cell(idx_buy)})
            if data:
                return data
    return []


def fetch_flow(code, timeout=10):
    """Return recent disclosed changes from the given code's public detail/chart pages."""
    code = re.sub(r"\D", "", str(code or "")) if str(code or "").isdigit() else str(code or "").upper().strip()
    if not code:
        return {"status": "未取得", "reason": "銘柄コードが空です", "url": None}
    detail_url = f"{BASE}/chart_detail/?{urlencode({'id': code})}"
    chart_url = f"{BASE}/chart/?{urlencode({'id': code})}"
    errors = []
    # The requested detail page is always checked first; the chart page is used as a
    # structured fallback because its table exposes the three aggregate delta columns.
    for url in (detail_url, chart_url):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=timeout)
            resp.raise_for_status()
            rows = _find_delta_table(resp.text)
            if rows:
                # Sum up to the latest five rows with actually published numeric values.
                recent = rows[:5]
                out = {}
                for key in ("short_delta", "credit_sell_delta", "credit_buy_delta"):
                    values = [r[key] for r in recent if r.get(key) is not None]
                    out[key] = float(sum(values)) if values else None
                any_value = any(out[k] is not None for k in out)
                latest_date = next((r["date"] for r in recent if r.get("date")), None)
                return {
                    **out,
                    "status": "取得済み" if any_value else "未取得",
                    "reason": "直近5行の公表増減を合算" if any_value else "表に数値が見つかりません",
                    "latest_date": latest_date,
                    "rows_used": len(recent),
                    "url": detail_url,
                    "data_url": url,
                }
            errors.append(f"{url}: 対象の増減表を検出できません")
        except Exception as exc:
            errors.append(f"{url}: {type(exc).__name__}: {exc}")
    return {"short_delta": None, "credit_sell_delta": None, "credit_buy_delta": None,
            "status": "未取得", "reason": "公開ページから自動取得できませんでした。" + " / ".join(errors),
            "latest_date": None, "rows_used": 0, "url": detail_url, "data_url": None}
