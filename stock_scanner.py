# -*- coding: utf-8 -*-
"""
台股籌碼 / 基本面 加權評分工具
================================
依據「動能減速 + 高估值 + 籌碼鬆動」分析框架，對個股計算 0~100 綜合參考分數，
並輸出趨勢圖，協助在事前辨識警訊（非投資建議，僅供參考）。

資料來源：FinMind 免費 API (https://finmindtrade.com)
  - 免註冊可用，但有流量限制；建議註冊取得 token 後放入環境變數 FINMIND_TOKEN
    或以 --token 參數傳入。

使用方式：
  單一個股：   python stock_scanner.py 2360
  匯入清單：   python stock_scanner.py --list mylist.csv     (CSV/TXT，每列一個代號，或含 stock_id 欄)
  離線示範：   python stock_scanner.py --demo                (不連網，用合成資料看輸出長相)
  自訂權重：   python stock_scanner.py 2360 --weights weights.json

輸出：
  output/<代號>_report.png   四合一趨勢圖（營收動能 / 法人籌碼 / 融資與價格 / 分數雷達）
  output/summary.csv         多檔個股的分數總表（掃描清單時）
"""

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
import pandas as pd
import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.font_manager import fontManager

# ---------------------------------------------------------------------------
# 中文字型（依作業系統自動挑選，找不到就退回英文標籤不至於亂碼）
# ---------------------------------------------------------------------------
_CJK_CANDIDATES = ["Microsoft JhengHei", "PingFang TC", "Noto Sans CJK TC",
                   "Noto Sans TC", "WenQuanYi Zen Hei", "SimHei", "Arial Unicode MS"]
_available = {f.name for f in fontManager.ttflist}
for _f in _CJK_CANDIDATES:
    if _f in _available:
        plt.rcParams["font.family"] = _f
        break
plt.rcParams["axes.unicode_minus"] = False

API_URL = "https://api.finmindtrade.com/api/v4/data"

# ---------------------------------------------------------------------------
# 預設權重（可用 --weights 覆蓋）— 對應先前討論的分析框架
# ---------------------------------------------------------------------------
DEFAULT_WEIGHTS = {
    "revenue_momentum": 0.25,   # 營收動能：YoY 水準 + YoY 加速度(二階導)
    "foreign_flow":     0.20,   # 外資：近20日累計買賣超趨勢 + 持股比變化
    "trust_flow":       0.15,   # 投信：連買連賣 + 近20日累計
    "margin_health":    0.10,   # 融資：下跌中融資增 = 籌碼變髒扣分
    "short_pressure":   0.10,   # 借券/融券：空方布局壓力
    "technical":        0.10,   # 技術面：月線乖離、趨勢位置
    "liquidity":        0.10,   # 流動性：量能是否足以承接法人調節
}


# ---------------------------------------------------------------------------
# 資料抓取
# ---------------------------------------------------------------------------
class FinMindClient:
    def __init__(self, token: str = ""):
        self.token = token or os.environ.get("FINMIND_TOKEN", "")

    def get(self, dataset: str, stock_id: str, start: str) -> pd.DataFrame:
        params = {"dataset": dataset, "data_id": stock_id, "start_date": start}
        if self.token:
            params["token"] = self.token
        for attempt in range(3):
            try:
                r = requests.get(API_URL, params=params, timeout=30)
                j = r.json()
                if j.get("status") == 402:  # 流量限制
                    print("  [!] API 流量限制，等待 60 秒後重試 ...")
                    time.sleep(60)
                    continue
                return pd.DataFrame(j.get("data", []))
            except Exception as e:
                if attempt == 2:
                    print(f"  [!] {dataset} 抓取失敗: {e}")
                    return pd.DataFrame()
                time.sleep(3)
        return pd.DataFrame()


@dataclass
class StockData:
    stock_id: str
    market: str = "twse"                                              # twse=上市 / tpex=上櫃
    price: pd.DataFrame = field(default_factory=pd.DataFrame)        # 日K
    revenue: pd.DataFrame = field(default_factory=pd.DataFrame)      # 月營收
    institutional: pd.DataFrame = field(default_factory=pd.DataFrame)# 三大法人
    margin: pd.DataFrame = field(default_factory=pd.DataFrame)       # 融資融券
    shareholding: pd.DataFrame = field(default_factory=pd.DataFrame) # 外資持股比


def fetch_stock(client: FinMindClient, stock_id: str) -> StockData:
    today = date.today()
    d150 = (today - timedelta(days=150)).isoformat()   # 需 60 交易日算控盤，抓寬一點
    d3y = (today - timedelta(days=3 * 365)).isoformat()
    print(f"[{stock_id}] 下載資料中 ...")
    sd = StockData(stock_id=stock_id)
    info = client.get("TaiwanStockInfo", stock_id, "2000-01-01")
    if not info.empty and "type" in info.columns:
        row = info[info["stock_id"] == stock_id]
        if not row.empty:
            sd.market = str(row.iloc[0]["type"]).lower()  # 'twse' / 'tpex'
    sd.price = client.get("TaiwanStockPrice", stock_id, d150)
    sd.revenue = client.get("TaiwanStockMonthRevenue", stock_id, d3y)
    sd.institutional = client.get("TaiwanStockInstitutionalInvestorsBuySell", stock_id, d150)
    sd.margin = client.get("TaiwanStockMarginPurchaseShortSale", stock_id, d150)
    sd.shareholding = client.get("TaiwanStockShareholding", stock_id, d150)
    return sd


# ---------------------------------------------------------------------------
# 離線示範資料（模擬「高檔動能減速、籌碼鬆動」情境，方便先看輸出長相）
# ---------------------------------------------------------------------------
def demo_stock(stock_id: str = "2360", market: str = "twse") -> StockData:
    rng = np.random.default_rng(42)
    today = pd.Timestamp.today().normalize()

    # 月營收：兩年，YoY 由 130% 逐步降到 70%
    months = pd.date_range(end=today.replace(day=1), periods=30, freq="MS")
    base = np.linspace(18e8, 48e8, 30) * (1 + rng.normal(0, 0.04, 30))
    base[-1] *= 0.91  # 最後一個月 月減
    rev = pd.DataFrame({"date": months.strftime("%Y-%m-%d"),
                        "revenue": base.astype(int),
                        "revenue_month": months.month,
                        "revenue_year": months.year})

    # 日K：先漲後跌
    days = pd.bdate_range(end=today, periods=90)
    trend = np.concatenate([np.linspace(1800, 2795, 55), np.linspace(2795, 1990, 35)])
    close = trend * (1 + rng.normal(0, 0.008, 90))
    price = pd.DataFrame({"date": days.strftime("%Y-%m-%d"), "close": close,
                          "Trading_Volume": (rng.uniform(1200, 2600, 90) * 1000).astype(int)})

    # 三大法人：外資大量但方向雜訊高；投信「張數小、卻精準賣在大跌日」→ 測試控盤偵測
    daily_ret = pd.Series(close).pct_change().fillna(0).values
    inst_rows = []
    for i, d in enumerate(days):
        bias = 150 if i < 55 else -180
        for name in ["Foreign_Investor", "Investment_Trust", "Dealer_self"]:
            if name == "Investment_Trust":
                # 小量（數十張）但方向與當日漲跌高度同向
                direction = np.sign(daily_ret[i]) if rng.random() < 0.85 else -np.sign(daily_ret[i])
                net = direction * rng.uniform(20, 80) * 1000
            else:
                scale = {"Foreign_Investor": 1.0, "Dealer_self": 0.15}[name]
                net = (bias + rng.normal(0, 200)) * scale * 1000
            buy = max(net, 0) + rng.uniform(2e5, 8e5)
            inst_rows.append({"date": d.strftime("%Y-%m-%d"), "name": name,
                              "buy": int(buy), "sell": int(buy - net)})
    inst = pd.DataFrame(inst_rows)

    # 融資：下跌段融資反增（警訊情境）
    margin_balance = np.concatenate([np.linspace(800, 700, 55), np.linspace(700, 1050, 35)])
    margin = pd.DataFrame({"date": days.strftime("%Y-%m-%d"),
                           "MarginPurchaseTodayBalance": (margin_balance * 1000).astype(int),
                           "ShortSaleTodayBalance": (np.linspace(50, 160, 90) * 1000).astype(int)})

    # 外資持股比：59.9% -> 59.4%
    sh = pd.DataFrame({"date": days.strftime("%Y-%m-%d"),
                       "ForeignInvestmentSharesRatio": np.linspace(59.9, 59.4, 90)})

    sd = StockData(stock_id=stock_id, market=market, price=price, revenue=rev,
                   institutional=inst, margin=margin, shareholding=sh)
    return sd


# ---------------------------------------------------------------------------
# 各子指標評分（每項回傳 0~100 分 + 說明文字）
# ---------------------------------------------------------------------------
def _clip(x, lo=0, hi=100):
    return float(np.clip(x, lo, hi))


def score_revenue(rev: pd.DataFrame):
    """YoY 水準 + YoY 加速度（二階導）。加速度轉負即使 YoY 仍高也扣分。"""
    if rev.empty or len(rev) < 14:
        return 50.0, "營收資料不足", pd.DataFrame()
    r = rev.copy()
    r["date"] = pd.to_datetime(r["date"])
    r = r.sort_values("date").reset_index(drop=True)
    r["yoy"] = r["revenue"].pct_change(12) * 100
    r = r.dropna(subset=["yoy"]).reset_index(drop=True)
    if len(r) < 4:
        return 50.0, "YoY 樣本不足", r
    yoy_now = r["yoy"].iloc[-1]
    yoy_3m_avg_prev = r["yoy"].iloc[-4:-1].mean()
    accel = yoy_now - yoy_3m_avg_prev          # 二階導
    mom = (r["revenue"].iloc[-1] / r["revenue"].iloc[-2] - 1) * 100

    level_score = _clip(50 + yoy_now * 0.6)     # YoY 0% = 50 分
    accel_score = _clip(50 + accel * 0.8)       # 減速直接反映
    score = _clip(level_score * 0.5 + accel_score * 0.5)
    note = f"YoY {yoy_now:+.1f}%｜較前3月均{accel:+.1f}pp｜MoM {mom:+.1f}%"
    return score, note, r


def _inst_net(inst: pd.DataFrame, name: str) -> pd.Series:
    if inst.empty:
        return pd.Series(dtype=float)
    d = inst[inst["name"].str.contains(name, case=False, na=False)].copy()
    if d.empty:
        return pd.Series(dtype=float)
    d["net"] = (d["buy"] - d["sell"]) / 1000.0  # 張
    d["date"] = pd.to_datetime(d["date"])
    return d.groupby("date")["net"].sum().sort_index()


def _daily_return(price: pd.DataFrame) -> pd.Series:
    if price.empty:
        return pd.Series(dtype=float)
    p = price.copy()
    p["date"] = pd.to_datetime(p["date"])
    p = p.sort_values("date").set_index("date")["close"].astype(float)
    return p.pct_change().dropna()


def influence_metrics(net: pd.Series, ret: pd.Series, window: int = 60):
    """
    衡量某法人對股價的「控盤力」，完全不使用張數大小：
      1. 同向率 control_rate：收黑日該法人站賣方、收紅日站買方的比例（排除平盤/零買賣日）
      2. Spearman 等級相關 corr：日報酬 vs 當日買賣超的「排序」相關（等級化後張數大小不影響）
    influence = 0.5 * max(2*(control_rate-0.5), 0) + 0.5 * max(corr, 0)   → 0~1
    """
    if net.empty or ret.empty:
        return {"influence": 0.0, "control_rate": np.nan, "corr": np.nan, "n": 0}
    df = pd.concat([net.rename("net"), ret.rename("ret")], axis=1).dropna().tail(window)
    df = df[(df["net"] != 0) & (df["ret"] != 0)]
    n = len(df)
    if n < 15:
        return {"influence": 0.0, "control_rate": np.nan, "corr": np.nan, "n": n}
    control_rate = float((np.sign(df["net"]) == np.sign(df["ret"])).mean())
    corr = float(df["net"].rank().corr(df["ret"].rank()))  # Spearman
    influence = 0.5 * max(2 * (control_rate - 0.5), 0.0) + 0.5 * max(corr, 0.0)
    return {"influence": round(influence, 3), "control_rate": round(control_rate, 3),
            "corr": round(corr, 3), "n": n}


def _stance_score(net: pd.Series, ret: pd.Series, window: int = 20):
    """
    法人近期態度分（不看張數）：
      stance = Σ sign(當日買賣超) × |當日漲跌幅| ÷ Σ|當日漲跌幅|   ∈ [-1, 1]
    → 在大跌日站賣方會被重扣；在大漲日站買方加分。小量賣但每次都賣在重挫日 = 重扣。
    另計連買/連賣天數作輔助。
    """
    if net.empty:
        return 50.0, 0
    streak = 0
    for v in net.iloc[::-1]:
        if v > 0 and streak >= 0:
            streak += 1
        elif v < 0 and streak <= 0:
            streak -= 1
        else:
            break
    df = pd.concat([net.rename("net"), ret.rename("ret")], axis=1).dropna().tail(window)
    df = df[df["net"] != 0]
    if df.empty or df["ret"].abs().sum() == 0:
        return _clip(50 + streak * 4), streak
    stance = float((np.sign(df["net"]) * df["ret"].abs()).sum() / df["ret"].abs().sum())
    return _clip(50 + stance * 45 + streak * 3), streak


def score_foreign(inst: pd.DataFrame, shareholding: pd.DataFrame, ret: pd.Series):
    net = _inst_net(inst, "Foreign")
    if net.empty:
        return 50.0, "無外資資料", net
    score, streak = _stance_score(net, ret)
    ratio_note = ""
    if not shareholding.empty and "ForeignInvestmentSharesRatio" in shareholding.columns:
        s = shareholding.sort_values("date")["ForeignInvestmentSharesRatio"]
        chg = s.iloc[-1] - s.iloc[max(-20, -len(s))]
        score = _clip(score + chg * 25)
        ratio_note = f"｜持股比20日{chg:+.2f}pp"
    note = f"{'連買' if streak>0 else '連賣'}{abs(streak)}日｜大跌日方向加權態度分{ratio_note}"
    return score, note, net


def score_trust(inst: pd.DataFrame, ret: pd.Series):
    net = _inst_net(inst, "Investment_Trust")
    if net.empty:
        return 50.0, "無投信資料", net
    score, streak = _stance_score(net, ret)
    note = f"{'連買' if streak>0 else '連賣'}{abs(streak)}日｜大跌日方向加權態度分"
    return score, note, net


def dynamic_flow_weights(market: str, infl_f: dict, infl_t: dict, base: dict):
    """
    法人權重動態分配：
      - 法人總桶 = base 中 foreign_flow + trust_flow（預設 0.35）
      - 先驗：上市 外資65%/投信35%；上櫃 外資35%/投信65%
      - 實測：兩者 influence 佔比（皆無效時退回先驗）
      - 最終 = 0.5×先驗 + 0.5×實測
    """
    bucket = base.get("foreign_flow", 0.20) + base.get("trust_flow", 0.15)
    prior_f = 0.65 if market == "twse" else 0.35
    fi, ti = infl_f.get("influence", 0.0), infl_t.get("influence", 0.0)
    if fi + ti > 0.05:
        data_f = fi / (fi + ti)
    else:
        data_f = prior_f
    share_f = 0.5 * prior_f + 0.5 * data_f
    w = dict(base)
    w["foreign_flow"] = round(bucket * share_f, 4)
    w["trust_flow"] = round(bucket * (1 - share_f), 4)
    return w, share_f


def score_margin(margin: pd.DataFrame, price: pd.DataFrame):
    """下跌過程融資增加 = 散戶接刀，重扣；下跌融資減 = 籌碼沉澱，加分。"""
    if margin.empty or price.empty:
        return 50.0, "無融資資料", pd.Series(dtype=float)
    m = margin.copy()
    m["date"] = pd.to_datetime(m["date"])
    m = m.sort_values("date")
    bal = m.set_index("date")["MarginPurchaseTodayBalance"] / 1000.0
    p = price.copy()
    p["date"] = pd.to_datetime(p["date"])
    p = p.sort_values("date").set_index("date")["close"]
    n = min(len(bal), 20)
    if n < 5:
        return 50.0, "融資樣本不足", bal
    m_chg = (bal.iloc[-1] / bal.iloc[-n] - 1) * 100
    p_chg = (p.iloc[-1] / p.iloc[-min(len(p), 20)] - 1) * 100
    score = 50.0
    if p_chg < -3 and m_chg > 5:
        score = _clip(50 - m_chg * 1.5 + p_chg)      # 下跌+融資增：重扣
    elif p_chg < -3 and m_chg < 0:
        score = _clip(60 - m_chg * 0.8)              # 下跌+融資減：加分
    else:
        score = _clip(50 - m_chg * 0.5)
    note = f"20日融資{m_chg:+.1f}%、股價{p_chg:+.1f}%"
    return score, note, bal


def score_short(margin: pd.DataFrame):
    if margin.empty or "ShortSaleTodayBalance" not in margin.columns:
        return 50.0, "無券資料", pd.Series(dtype=float)
    m = margin.copy()
    m["date"] = pd.to_datetime(m["date"])
    s = m.sort_values("date").set_index("date")["ShortSaleTodayBalance"] / 1000.0
    n = min(len(s), 20)
    if n < 5 or s.iloc[-n] == 0:
        return 50.0, "券樣本不足", s
    chg = (s.iloc[-1] / s.iloc[-n] - 1) * 100
    score = _clip(50 - chg * 0.6)   # 融券/空方餘額增加 = 壓力
    note = f"20日融券餘額{chg:+.1f}%"
    return score, note, s


def score_technical(price: pd.DataFrame):
    if price.empty or len(price) < 20:
        return 50.0, "價格資料不足", pd.DataFrame()
    p = price.copy()
    p["date"] = pd.to_datetime(p["date"])
    p = p.sort_values("date").reset_index(drop=True)
    p["ma20"] = p["close"].rolling(20).mean()
    p["ma60"] = p["close"].rolling(60).mean()
    last = p.iloc[-1]
    bias = (last["close"] / last["ma20"] - 1) * 100 if pd.notna(last["ma20"]) else 0
    above60 = pd.notna(last["ma60"]) and last["close"] > last["ma60"]
    # 貼近月線最健康；乖離過大（正負皆然）都扣分，跌破季線再扣
    score = _clip(70 - abs(bias) * 2 + (10 if above60 else -15))
    note = f"月線乖離{bias:+.1f}%｜{'站上' if above60 else '跌破'}季線"
    return score, note, p


def score_liquidity(price: pd.DataFrame, inst: pd.DataFrame):
    if price.empty:
        return 50.0, "無量能資料", None
    p = price.copy()
    vol = p["Trading_Volume"].astype(float) / 1000.0  # 張
    avg20 = vol.tail(20).mean()
    net_f = _inst_net(inst, "Foreign")
    ratio = abs(net_f.tail(5).sum()) / max(avg20 * 5, 1) if not net_f.empty else 0
    # 法人5日調節量占總量比重越高，流動性風險越大
    score = _clip(80 - ratio * 200)
    note = f"20日均量{avg20:,.0f}張｜外資5日調節佔比{ratio*100:.1f}%"
    return score, note, None


# ---------------------------------------------------------------------------
# 綜合評分 + 繪圖
# ---------------------------------------------------------------------------
LABELS = {
    "revenue_momentum": "營收動能",
    "foreign_flow": "外資動向",
    "trust_flow": "投信動向",
    "margin_health": "融資健康",
    "short_pressure": "空方壓力",
    "technical": "技術位置",
    "liquidity": "流動性",
}


def analyze(sd: StockData, weights: dict):
    ret = _daily_return(sd.price)

    # --- 控盤偵測（不看張數，只看方向與排序） ---
    net_f_all = _inst_net(sd.institutional, "Foreign")
    net_t_all = _inst_net(sd.institutional, "Investment_Trust")
    infl_f = influence_metrics(net_f_all, ret)
    infl_t = influence_metrics(net_t_all, ret)
    weights, share_f = dynamic_flow_weights(sd.market, infl_f, infl_t, weights)

    if max(infl_f["influence"], infl_t["influence"]) < 0.15:
        controller = "無明顯控盤者（散戶/其他資金主導）"
    elif infl_f["influence"] >= infl_t["influence"]:
        controller = f"外資控盤（同向率{infl_f['control_rate']:.0%}、等級相關{infl_f['corr']:+.2f}）"
    else:
        controller = f"投信控盤（同向率{infl_t['control_rate']:.0%}、等級相關{infl_t['corr']:+.2f}）"

    s_rev, n_rev, rev_df = score_revenue(sd.revenue)
    s_for, n_for, for_net = score_foreign(sd.institutional, sd.shareholding, ret)
    s_tru, n_tru, tru_net = score_trust(sd.institutional, ret)
    s_mar, n_mar, mar_bal = score_margin(sd.margin, sd.price)
    s_sho, n_sho, sho_bal = score_short(sd.margin)
    s_tec, n_tec, p_df = score_technical(sd.price)
    s_liq, n_liq, _ = score_liquidity(sd.price, sd.institutional)

    scores = {"revenue_momentum": s_rev, "foreign_flow": s_for, "trust_flow": s_tru,
              "margin_health": s_mar, "short_pressure": s_sho,
              "technical": s_tec, "liquidity": s_liq}
    notes = {"revenue_momentum": n_rev, "foreign_flow": n_for, "trust_flow": n_tru,
             "margin_health": n_mar, "short_pressure": n_sho,
             "technical": n_tec, "liquidity": n_liq}
    total = sum(scores[k] * weights.get(k, 0) for k in scores)
    total = round(total / max(sum(weights.values()), 1e-9), 1)

    if total >= 70:
        verdict = "偏多：動能與籌碼同向，仍需追蹤持續性"
    elif total >= 55:
        verdict = "中性偏多：留意個別警訊項目"
    elif total >= 45:
        verdict = "中性：多空拉鋸，等訊號明朗"
    elif total >= 30:
        verdict = "偏空警戒：籌碼鬆動或動能減速中"
    else:
        verdict = "高風險：多項警訊同時出現"

    return {"total": total, "verdict": verdict, "scores": scores, "notes": notes,
            "market": "上市" if sd.market == "twse" else "上櫃",
            "controller": controller,
            "flow_weights": {"外資": weights["foreign_flow"], "投信": weights["trust_flow"]},
            "influence": {"外資": infl_f, "投信": infl_t},
            "frames": {"revenue": rev_df, "foreign": for_net, "trust": tru_net,
                       "margin": mar_bal, "price": p_df}}


def plot_report(sd: StockData, result: dict, outdir: str):
    frames = result["frames"]
    fig, axes = plt.subplots(2, 2, figsize=(15, 10))
    fig.suptitle(f"{sd.stock_id}（{result['market']}）分數 {result['total']}/100 — {result['verdict']}\n"
                 f"{result['controller']}｜法人權重：外資{result['flow_weights']['外資']:.0%}/投信{result['flow_weights']['投信']:.0%}",
                 fontsize=13, fontweight="bold")

    # (1) 營收 + YoY
    ax = axes[0][0]
    rev = frames["revenue"]
    if isinstance(rev, pd.DataFrame) and not rev.empty and "yoy" in rev.columns:
        r = rev.tail(18)
        ax.bar(r["date"], r["revenue"] / 1e8, width=20, color="#4C78A8", alpha=0.7, label="月營收(億)")
        ax2 = ax.twinx()
        ax2.plot(r["date"], r["yoy"], color="#E45756", marker="o", label="YoY %")
        ax2.axhline(0, color="gray", lw=0.5)
        ax.set_title("營收動能（柱=營收、線=YoY）")
        ax.legend(loc="upper left"); ax2.legend(loc="upper right")
    else:
        ax.set_title("營收動能（無資料）")

    # (2) 法人累計買賣超 vs 股價
    ax = axes[0][1]
    p = frames["price"]
    if isinstance(p, pd.DataFrame) and not p.empty:
        ax.plot(p["date"], p["close"], color="black", lw=1.2, label="收盤價")
        if "ma20" in p.columns:
            ax.plot(p["date"], p["ma20"], color="orange", lw=1, label="20MA")
        ax2 = ax.twinx()
        for key, color, lab in [("foreign", "#4C78A8", "外資累計"), ("trust", "#72B7B2", "投信累計")]:
            net = frames[key]
            if isinstance(net, pd.Series) and not net.empty:
                ax2.plot(net.index, net.cumsum(), color=color, lw=1.2, label=lab)
        ax2.axhline(0, color="gray", lw=0.5)
        ax.set_title("股價 vs 法人累計買賣超(張)")
        ax.legend(loc="upper left"); ax2.legend(loc="lower left")
    else:
        ax.set_title("股價 vs 法人（無資料）")

    # (3) 融資餘額 vs 股價
    ax = axes[1][0]
    mar = frames["margin"]
    if isinstance(mar, pd.Series) and not mar.empty and isinstance(p, pd.DataFrame) and not p.empty:
        ax.plot(mar.index, mar.values, color="#E45756", label="融資餘額(張)")
        ax2 = ax.twinx()
        ax2.plot(p["date"], p["close"], color="black", lw=1, alpha=0.6, label="收盤價")
        ax.set_title("融資餘額 vs 股價（下跌+融資增=警訊）")
        ax.legend(loc="upper left"); ax2.legend(loc="upper right")
    else:
        ax.set_title("融資餘額（無資料）")

    # (4) 分數雷達圖
    ax = axes[1][1]
    ax.remove()
    ax = fig.add_subplot(2, 2, 4, projection="polar")
    keys = list(LABELS.keys())
    vals = [result["scores"][k] for k in keys] + [result["scores"][keys[0]]]
    ang = np.linspace(0, 2 * np.pi, len(keys), endpoint=False).tolist()
    ang += ang[:1]
    ax.plot(ang, vals, color="#4C78A8"); ax.fill(ang, vals, color="#4C78A8", alpha=0.25)
    ax.set_xticks(ang[:-1]); ax.set_xticklabels([LABELS[k] for k in keys], fontsize=10)
    ax.set_ylim(0, 100); ax.set_title("七大構面分數")

    plt.tight_layout(rect=[0, 0, 1, 0.95])
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, f"{sd.stock_id}_report.png")
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path


def print_report(sd: StockData, result: dict):
    print("=" * 62)
    print(f"  {sd.stock_id}（{result['market']}）  綜合參考分數：{result['total']} / 100")
    print(f"  判讀：{result['verdict']}")
    print(f"  控盤判定：{result['controller']}")
    fw = result["flow_weights"]
    print(f"  動態法人權重：外資 {fw['外資']:.1%}／投信 {fw['投信']:.1%}"
          f"（先驗依{result['market']}＋近60日實測影響力各半）")
    print("-" * 62)
    for k in LABELS:
        print(f"  {LABELS[k]:<6}{result['scores'][k]:6.1f} 分  {result['notes'][k]}")
    print("=" * 62)


# ---------------------------------------------------------------------------
# 主程式
# ---------------------------------------------------------------------------
def generate_index_html(rows: list, outdir: str):
    """產生一頁簡單的總覽網頁，列出所有個股分數與對應圖表，方便 GitHub Pages 直接顯示。"""
    ts = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M")
    rows_sorted = sorted(rows, key=lambda r: r["total"], reverse=True)

    def color(total):
        if total >= 70: return "#2E7D32"
        if total >= 55: return "#66BB6A"
        if total >= 45: return "#BDBDBD"
        if total >= 30: return "#EF9A9A"
        return "#C62828"

    cards = []
    for r in rows_sorted:
        cards.append(f"""
        <div class="card">
          <div class="card-head">
            <span class="sid">{r['stock_id']}</span>
            <span class="market">{r.get('market','')}</span>
            <span class="score" style="background:{color(r['total'])}">{r['total']}</span>
          </div>
          <div class="verdict">{r.get('verdict','')}</div>
          <div class="controller">{r.get('controller','')}</div>
          <a href="{r['stock_id']}_report.png" target="_blank">
            <img src="{r['stock_id']}_report.png" alt="{r['stock_id']} 報告圖" loading="lazy">
          </a>
        </div>""")

    html = f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>台股籌碼評分總覽</title>
<style>
  body {{ background:#0d1117; color:#e6edf3; font-family: -apple-system, "Microsoft JhengHei", sans-serif; margin:0; padding:24px; }}
  h1 {{ font-size:20px; }}
  .ts {{ color:#8b949e; font-size:13px; margin-bottom:20px; }}
  .grid {{ display:grid; grid-template-columns: repeat(auto-fill, minmax(320px,1fr)); gap:16px; }}
  .card {{ background:#161b22; border:1px solid #30363d; border-radius:10px; padding:14px; }}
  .card-head {{ display:flex; align-items:center; gap:8px; margin-bottom:6px; }}
  .sid {{ font-size:18px; font-weight:bold; }}
  .market {{ font-size:12px; color:#8b949e; border:1px solid #30363d; border-radius:6px; padding:1px 6px; }}
  .score {{ margin-left:auto; font-weight:bold; color:white; border-radius:8px; padding:2px 10px; }}
  .verdict {{ font-size:13px; color:#c9d1d9; margin-bottom:2px; }}
  .controller {{ font-size:12px; color:#8b949e; margin-bottom:8px; }}
  img {{ width:100%; border-radius:6px; border:1px solid #30363d; }}
  .disclaimer {{ margin-top:24px; font-size:12px; color:#8b949e; }}
</style>
</head>
<body>
  <h1>台股籌碼 / 基本面加權評分總覽</h1>
  <div class="ts">更新時間：{ts}（GitHub Actions 自動產生）</div>
  <div class="grid">
    {''.join(cards)}
  </div>
  <div class="disclaimer">※ 本頁為公開資料之量化整理，控盤判定為統計推論，不構成投資建議。</div>
</body>
</html>"""
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, "index.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path


def load_list(path: str):
    if path.lower().endswith(".csv"):
        df = pd.read_csv(path, dtype=str)
        col = "stock_id" if "stock_id" in df.columns else df.columns[0]
        return [s.strip() for s in df[col].dropna().tolist()]
    with open(path, encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]


def main():
    ap = argparse.ArgumentParser(description="台股籌碼/基本面加權評分工具")
    ap.add_argument("stock", nargs="?", help="個股代號，如 2360")
    ap.add_argument("--list", help="匯入清單檔（CSV 或 TXT，每列一個代號）；未指定且無 stock 時，預設讀取 watchlist.txt")
    ap.add_argument("--weights", help="自訂權重 JSON 檔")
    ap.add_argument("--token", default="", help="FinMind API token（可選）")
    ap.add_argument("--outdir", default="output", help="輸出資料夾")
    ap.add_argument("--demo", action="store_true", help="離線示範模式（合成資料）")
    ap.add_argument("--market", default="twse", choices=["twse", "tpex"],
                    help="示範模式的市場別（實際查詢時自動判別）")
    args = ap.parse_args()

    weights = dict(DEFAULT_WEIGHTS)
    if args.weights:
        with open(args.weights, encoding="utf-8") as f:
            weights.update(json.load(f))

    if args.demo:
        if args.list:
            targets = load_list(args.list)
        else:
            targets = [args.stock or "2360"]
    elif args.list:
        targets = load_list(args.list)
    elif args.stock:
        targets = [args.stock]
    elif os.path.exists("watchlist.txt"):
        print("[i] 未指定股票，讀取預設 watchlist.txt")
        targets = load_list("watchlist.txt")
    else:
        ap.print_help(); sys.exit(1)

    client = FinMindClient(args.token)
    rows = []
    for sid in targets:
        sd = demo_stock(sid, args.market) if args.demo else fetch_stock(client, sid)
        if sd.price.empty and not args.demo:
            print(f"[{sid}] 查無資料，略過"); continue
        result = analyze(sd, dict(weights))
        print_report(sd, result)
        path = plot_report(sd, result, args.outdir)
        print(f"  圖表已輸出：{path}\n")
        row = {"stock_id": sid, "market": result["market"], "total": result["total"],
               "controller": result["controller"], "verdict": result["verdict"]}
        row.update({LABELS[k]: round(v, 1) for k, v in result["scores"].items()})
        rows.append(row)
        if not args.demo and len(targets) > 1:
            time.sleep(1.5)  # 禮貌性間隔，避免撞流量限制

    if rows:
        summary = pd.DataFrame(rows).sort_values("total", ascending=False)
        os.makedirs(args.outdir, exist_ok=True)
        sp = os.path.join(args.outdir, "summary.csv")
        summary.to_csv(sp, index=False, encoding="utf-8-sig")
        if len(rows) > 1:
            print("\n===== 清單總表（依分數排序）=====")
            print(summary.to_string(index=False))
        print(f"\n總表已輸出：{sp}")
        idx_path = generate_index_html(rows, args.outdir)
        print(f"總覽網頁已輸出：{idx_path}")

    print("\n※ 本工具輸出僅為公開資料之量化整理，不構成投資建議。")


if __name__ == "__main__":
    main()
