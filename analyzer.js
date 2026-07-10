/* analyzer.js — 台股籌碼/基本面加權評分引擎（JS 版）
 * 與 stock_scanner.py 的評分邏輯一一對應，供瀏覽器端即時分析使用。
 * 可在 Node 環境 require()，也可在瀏覽器以 <script> 直接載入（掛在 window.Analyzer）。
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.Analyzer = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const DEFAULT_WEIGHTS = {
    revenue_momentum: 0.25,
    foreign_flow: 0.20,
    trust_flow: 0.15,
    margin_health: 0.10,
    short_pressure: 0.10,
    technical: 0.10,
    liquidity: 0.10,
  };

  const LABELS = {
    revenue_momentum: "營收動能",
    foreign_flow: "外資動向",
    trust_flow: "投信動向",
    margin_health: "融資健康",
    short_pressure: "空方壓力",
    technical: "技術位置",
    liquidity: "流動性",
  };

  function clip(x, lo = 0, hi = 100) {
    return Math.max(lo, Math.min(hi, x));
  }

  function mean(arr) {
    if (!arr.length) return 0;
    return arr.reduce((a, b) => a + b, 0) / arr.length;
  }

  function toDateKey(d) {
    // 接受 'YYYY-MM-DD' 或 Date，統一轉成 'YYYY-MM-DD' 字串當 key
    if (d instanceof Date) return d.toISOString().slice(0, 10);
    return String(d).slice(0, 10);
  }

  // ---------------------------------------------------------------
  // 日報酬率：輸入依日期排序的價格陣列 [{date, close}]，回傳 Map(date -> pctChange)
  // ---------------------------------------------------------------
  function dailyReturns(priceRows) {
    const rows = [...priceRows].sort((a, b) => (a.date < b.date ? -1 : 1));
    const ret = new Map();
    for (let i = 1; i < rows.length; i++) {
      const prev = Number(rows[i - 1].close);
      const cur = Number(rows[i].close);
      if (prev > 0) ret.set(toDateKey(rows[i].date), (cur - prev) / prev);
    }
    return ret;
  }

  // ---------------------------------------------------------------
  // 三大法人淨買賣超：instRows [{date, name, buy, sell}] -> Map(date -> net股數/1000=張)
  // ---------------------------------------------------------------
  function instNet(instRows, nameSubstr) {
    const map = new Map();
    for (const r of instRows) {
      if (!r.name || r.name.toLowerCase().indexOf(nameSubstr.toLowerCase()) === -1) continue;
      const key = toDateKey(r.date);
      const net = (Number(r.buy) - Number(r.sell)) / 1000.0;
      map.set(key, (map.get(key) || 0) + net);
    }
    return map;
  }

  // ---------------------------------------------------------------
  // 控盤影響力：同向率 + Spearman 等級相關（完全不使用張數大小）
  // ---------------------------------------------------------------
  function rankArray(arr) {
    const idx = arr.map((v, i) => i).sort((a, b) => arr[a] - arr[b]);
    const ranks = new Array(arr.length);
    idx.forEach((originalIdx, rank) => { ranks[originalIdx] = rank; });
    return ranks;
  }

  function spearman(a, b) {
    if (a.length < 2) return 0;
    const ra = rankArray(a), rb = rankArray(b);
    const ma = mean(ra), mb = mean(rb);
    let num = 0, da = 0, db = 0;
    for (let i = 0; i < ra.length; i++) {
      num += (ra[i] - ma) * (rb[i] - mb);
      da += (ra[i] - ma) ** 2;
      db += (rb[i] - mb) ** 2;
    }
    if (da === 0 || db === 0) return 0;
    return num / Math.sqrt(da * db);
  }

  function influenceMetrics(net, ret, window = 60) {
    const dates = [...net.keys()].filter(d => ret.has(d) && net.get(d) !== 0 && ret.get(d) !== 0)
      .sort().slice(-window);
    const n = dates.length;
    if (n < 15) return { influence: 0, control_rate: NaN, corr: NaN, n };
    const netArr = dates.map(d => net.get(d));
    const retArr = dates.map(d => ret.get(d));
    const sameSign = netArr.filter((v, i) => Math.sign(v) === Math.sign(retArr[i])).length;
    const controlRate = sameSign / n;
    const corr = spearman(netArr, retArr);
    const influence = 0.5 * Math.max(2 * (controlRate - 0.5), 0) + 0.5 * Math.max(corr, 0);
    return { influence: round(influence, 3), control_rate: round(controlRate, 3), corr: round(corr, 3), n };
  }

  function round(x, d) { const f = 10 ** d; return Math.round(x * f) / f; }

  // ---------------------------------------------------------------
  // 法人態度分：不看張數，看「當日方向 × |漲跌幅|」的加權平均
  // ---------------------------------------------------------------
  function stanceScore(net, ret, window = 20) {
    if (net.size === 0) return { score: 50, streak: 0 };
    const sortedDates = [...net.keys()].sort();
    let streak = 0;
    for (let i = sortedDates.length - 1; i >= 0; i--) {
      const v = net.get(sortedDates[i]);
      if (v > 0 && streak >= 0) streak++;
      else if (v < 0 && streak <= 0) streak--;
      else break;
    }
    const dates = sortedDates.filter(d => ret.has(d) && net.get(d) !== 0).slice(-window);
    if (dates.length === 0) return { score: clip(50 + streak * 4), streak };
    let num = 0, den = 0;
    for (const d of dates) {
      const n = net.get(d), r = Math.abs(ret.get(d));
      num += Math.sign(n) * r;
      den += r;
    }
    if (den === 0) return { score: clip(50 + streak * 4), streak };
    const stance = num / den;
    return { score: clip(50 + stance * 45 + streak * 3), streak };
  }

  // ---------------------------------------------------------------
  // 營收動能：YoY 水準 + 二階導（加速度）
  // ---------------------------------------------------------------
  function revenueScore(revenueRows) {
    if (!revenueRows || revenueRows.length < 14) return { score: 50, note: "營收資料不足" };
    const rows = [...revenueRows].sort((a, b) => (a.date < b.date ? -1 : 1));
    const yoy = [];
    for (let i = 12; i < rows.length; i++) {
      const prev = Number(rows[i - 12].revenue);
      if (prev > 0) yoy.push({ date: rows[i].date, revenue: Number(rows[i].revenue), yoy: (Number(rows[i].revenue) / prev - 1) * 100 });
    }
    if (yoy.length < 4) return { score: 50, note: "YoY 樣本不足" };
    const yoyNow = yoy[yoy.length - 1].yoy;
    const prev3 = yoy.slice(-4, -1).map(x => x.yoy);
    const accel = yoyNow - mean(prev3);
    const mom = (yoy[yoy.length - 1].revenue / yoy[yoy.length - 2].revenue - 1) * 100;
    const levelScore = clip(50 + yoyNow * 0.6);
    const accelScore = clip(50 + accel * 0.8);
    const score = clip(levelScore * 0.5 + accelScore * 0.5);
    const note = `YoY ${sign(yoyNow)}%｜較前3月均${sign(accel)}pp｜MoM ${sign(mom)}%`;
    return { score, note, series: yoy };
  }

  function sign(x) { return (x >= 0 ? "+" : "") + x.toFixed(1); }

  // ---------------------------------------------------------------
  // 外資 / 投信 評分
  // ---------------------------------------------------------------
  function foreignScore(instRows, shareholdingRows, ret) {
    const net = instNet(instRows, "Foreign");
    if (net.size === 0) return { score: 50, note: "無外資資料", net };
    let { score, streak } = stanceScore(net, ret);
    let ratioNote = "";
    if (shareholdingRows && shareholdingRows.length > 0) {
      const s = [...shareholdingRows].sort((a, b) => (a.date < b.date ? -1 : 1));
      const last = Number(s[s.length - 1].ForeignInvestmentSharesRatio);
      const idx = Math.max(0, s.length - 20);
      const before = Number(s[idx].ForeignInvestmentSharesRatio);
      const chg = last - before;
      score = clip(score + chg * 25);
      ratioNote = `｜持股比20日${sign(chg)}pp`;
    }
    const note = `${streak > 0 ? "連買" : "連賣"}${Math.abs(streak)}日｜大跌日方向加權態度分${ratioNote}`;
    return { score, note, net };
  }

  function trustScore(instRows, ret) {
    const net = instNet(instRows, "Investment_Trust");
    if (net.size === 0) return { score: 50, note: "無投信資料", net };
    const { score, streak } = stanceScore(net, ret);
    const note = `${streak > 0 ? "連買" : "連賣"}${Math.abs(streak)}日｜大跌日方向加權態度分`;
    return { score, note, net };
  }

  // ---------------------------------------------------------------
  // 融資健康：下跌+融資增=重扣；下跌+融資減=加分
  // ---------------------------------------------------------------
  function marginScore(marginRows, priceRows) {
    if (!marginRows || !marginRows.length || !priceRows || !priceRows.length)
      return { score: 50, note: "無融資資料" };
    const m = [...marginRows].sort((a, b) => (a.date < b.date ? -1 : 1));
    const p = [...priceRows].sort((a, b) => (a.date < b.date ? -1 : 1));
    const n = Math.min(m.length, 20);
    if (n < 5) return { score: 50, note: "融資樣本不足" };
    const balNow = Number(m[m.length - 1].MarginPurchaseTodayBalance);
    const balBefore = Number(m[m.length - n].MarginPurchaseTodayBalance);
    const mChg = balBefore > 0 ? (balNow / balBefore - 1) * 100 : 0;
    const pn = Math.min(p.length, 20);
    const priceNow = Number(p[p.length - 1].close);
    const priceBefore = Number(p[p.length - pn].close);
    const pChg = priceBefore > 0 ? (priceNow / priceBefore - 1) * 100 : 0;
    let score;
    if (pChg < -3 && mChg > 5) score = clip(50 - mChg * 1.5 + pChg);
    else if (pChg < -3 && mChg < 0) score = clip(60 - mChg * 0.8);
    else score = clip(50 - mChg * 0.5);
    return { score, note: `20日融資${sign(mChg)}%、股價${sign(pChg)}%` };
  }

  function shortScore(marginRows) {
    if (!marginRows || !marginRows.length) return { score: 50, note: "無券資料" };
    const s = [...marginRows].sort((a, b) => (a.date < b.date ? -1 : 1));
    const n = Math.min(s.length, 20);
    if (n < 5) return { score: 50, note: "券樣本不足" };
    const now = Number(s[s.length - 1].ShortSaleTodayBalance);
    const before = Number(s[s.length - n].ShortSaleTodayBalance);
    if (!before) return { score: 50, note: "券樣本不足" };
    const chg = (now / before - 1) * 100;
    return { score: clip(50 - chg * 0.6), note: `20日融券餘額${sign(chg)}%` };
  }

  // ---------------------------------------------------------------
  // 技術位置：月線乖離 + 季線
  // ---------------------------------------------------------------
  function sma(values, n, endIdx) {
    if (endIdx + 1 < n) return null;
    let sum = 0;
    for (let i = endIdx - n + 1; i <= endIdx; i++) sum += values[i];
    return sum / n;
  }

  function technicalScore(priceRows) {
    if (!priceRows || priceRows.length < 20) return { score: 50, note: "價格資料不足" };
    const p = [...priceRows].sort((a, b) => (a.date < b.date ? -1 : 1));
    const closes = p.map(r => Number(r.close));
    const last = closes.length - 1;
    const ma20 = sma(closes, 20, last);
    const ma60 = sma(closes, 60, last);
    const bias = ma20 ? (closes[last] / ma20 - 1) * 100 : 0;
    const above60 = ma60 !== null && closes[last] > ma60;
    const score = clip(70 - Math.abs(bias) * 2 + (above60 ? 10 : -15));
    return { score, note: `月線乖離${sign(bias)}%｜${above60 ? "站上" : "跌破"}季線`, ma20, ma60 };
  }

  function liquidityScore(priceRows, instRows) {
    if (!priceRows || !priceRows.length) return { score: 50, note: "無量能資料" };
    const p = [...priceRows].sort((a, b) => (a.date < b.date ? -1 : 1));
    const vols = p.map(r => Number(r.Trading_Volume) / 1000.0);
    const avg20 = mean(vols.slice(-20));
    const net = instNet(instRows, "Foreign");
    const dates = [...net.keys()].sort().slice(-5);
    const sum5 = dates.reduce((a, d) => a + Math.abs(net.get(d)), 0);
    const ratio = avg20 > 0 ? sum5 / (avg20 * 5) : 0;
    const score = clip(80 - ratio * 200);
    return { score, note: `20日均量${Math.round(avg20).toLocaleString()}張｜外資5日調節佔比${(ratio * 100).toFixed(1)}%` };
  }

  // ---------------------------------------------------------------
  // 技術指標：EMA / KDJ / MACD / OBV / 月線扣抵
  // priceRows 需含 date, close，KDJ另需 max(高) / min(低)
  // ---------------------------------------------------------------
  function ema(values, period) {
    const k = 2 / (period + 1);
    const out = new Array(values.length).fill(null);
    let prev = null;
    for (let i = 0; i < values.length; i++) {
      if (values[i] == null) continue;
      if (prev === null) { prev = values[i]; out[i] = prev; continue; }
      prev = values[i] * k + prev * (1 - k);
      out[i] = prev;
    }
    return out;
  }

  function smaSeries(values, period) {
    const out = new Array(values.length).fill(null);
    let sum = 0;
    for (let i = 0; i < values.length; i++) {
      sum += values[i];
      if (i >= period) sum -= values[i - period];
      if (i >= period - 1) out[i] = sum / period;
    }
    return out;
  }

  function kdj(priceRows, period = 9) {
    const p = [...priceRows].sort((a, b) => (a.date < b.date ? -1 : 1));
    const close = p.map(r => Number(r.close));
    const high = p.map(r => Number(r.max != null ? r.max : r.close));
    const low = p.map(r => Number(r.min != null ? r.min : r.close));
    const K = [], D = [], J = [];
    let prevK = 50, prevD = 50;
    for (let i = 0; i < close.length; i++) {
      const start = Math.max(0, i - period + 1);
      const hh = Math.max(...high.slice(start, i + 1));
      const ll = Math.min(...low.slice(start, i + 1));
      const rsv = hh === ll ? 50 : ((close[i] - ll) / (hh - ll)) * 100;
      const k = (prevK * 2 + rsv) / 3;
      const d = (prevD * 2 + k) / 3;
      const j = 3 * k - 2 * d;
      K.push(k); D.push(d); J.push(j);
      prevK = k; prevD = d;
    }
    return { dates: p.map(r => r.date), K, D, J };
  }

  function macd(priceRows, fast = 12, slow = 26, signal = 9) {
    const p = [...priceRows].sort((a, b) => (a.date < b.date ? -1 : 1));
    const close = p.map(r => Number(r.close));
    const emaFast = ema(close, fast);
    const emaSlow = ema(close, slow);
    const dif = close.map((_, i) => (emaFast[i] != null && emaSlow[i] != null) ? emaFast[i] - emaSlow[i] : null);
    const validDif = dif.map(v => v == null ? 0 : v);
    const deaRaw = ema(validDif, signal);
    const dea = dif.map((v, i) => v == null ? null : deaRaw[i]);
    const hist = dif.map((v, i) => (v == null || dea[i] == null) ? null : v - dea[i]);
    return { dates: p.map(r => r.date), dif, dea, hist };
  }

  function obv(priceRows, maPeriod = 30) {
    const p = [...priceRows].sort((a, b) => (a.date < b.date ? -1 : 1));
    const close = p.map(r => Number(r.close));
    const vol = p.map(r => Number(r.Trading_Volume || 0));
    const out = [0];
    for (let i = 1; i < close.length; i++) {
      if (close[i] > close[i - 1]) out.push(out[i - 1] + vol[i]);
      else if (close[i] < close[i - 1]) out.push(out[i - 1] - vol[i]);
      else out.push(out[i - 1]);
    }
    const ma = smaSeries(out, maPeriod);
    return { dates: p.map(r => r.date), obv: out, ma };
  }

  // 月線扣抵：比較目前收盤價與「n個交易日前」即將被扣掉的舊值，判斷月線未來易漲或易跌
  function monthlyDeduction(priceRows, period = 20) {
    const p = [...priceRows].sort((a, b) => (a.date < b.date ? -1 : 1));
    if (p.length <= period) return { favorable: null, note: "資料不足判斷月線扣抵" };
    const close = p.map(r => Number(r.close));
    const now = close[close.length - 1];
    const deductValue = close[close.length - 1 - period];
    const favorable = now > deductValue;
    const diffPct = ((now - deductValue) / deductValue) * 100;
    const note = favorable
      ? `月線扣抵值${deductValue.toFixed(1)}偏低（現價高出${diffPct.toFixed(1)}%），扣抵後有利月線續揚`
      : `月線扣抵值${deductValue.toFixed(1)}偏高（現價低了${Math.abs(diffPct).toFixed(1)}%），扣抵後月線恐走平或彎頭向下`;
    return { favorable, deductValue, diffPct, note };
  }

  function emaStatus(priceRows, period = 21) {
    const p = [...priceRows].sort((a, b) => (a.date < b.date ? -1 : 1));
    const close = p.map(r => Number(r.close));
    const emaSeries = ema(close, period);
    const now = close[close.length - 1];
    const emaNow = emaSeries[emaSeries.length - 1];
    if (emaNow == null) return { above: null, note: `資料不足計算EMA${period}` };
    const above = now > emaNow;
    const diffPct = ((now - emaNow) / emaNow) * 100;
    const note = above
      ? `股價${now.toFixed(1)}站上EMA${period}(${emaNow.toFixed(1)})，高出${diffPct.toFixed(1)}%，短線偏多`
      : `股價${now.toFixed(1)}跌破EMA${period}(${emaNow.toFixed(1)})，低了${Math.abs(diffPct).toFixed(1)}%，短線偏空`;
    return { above, emaNow, diffPct, note };
  }

  // ---------------------------------------------------------------
  // 籌碼文字解讀：控盤者 + 外資投信是同買同賣還是對作
  // ---------------------------------------------------------------
  function chipNarrative(netF, netT, controllerText, window = 5) {
    const datesF = [...netF.keys()].sort();
    const datesT = [...netT.keys()].sort();
    const recentF = datesF.slice(-window).reduce((s, d) => s + netF.get(d), 0);
    const recentT = datesT.slice(-window).reduce((s, d) => s + netT.get(d), 0);
    const fDir = recentF > 0 ? "買超" : recentF < 0 ? "賣超" : "持平";
    const tDir = recentT > 0 ? "買超" : recentT < 0 ? "賣超" : "持平";
    let relation;
    if (recentF === 0 && recentT === 0) relation = "外資與投信近期買賣皆平淡，無明顯動作";
    else if (Math.sign(recentF) === Math.sign(recentT) && recentF !== 0) relation = `外資與投信近${window}日同步${fDir}，方向一致，力道疊加`;
    else relation = `外資近${window}日${fDir}、投信${tDir}，兩者對作（方向不一致），需觀察誰的影響力較大來判斷實際走勢`;
    return `${controllerText}。${relation}`;
  }

  function buildNarrative(result, priceRows) {
    const lines = [];
    lines.push(chipNarrative(result.series.foreignNet, result.series.trustNet, result.controller));
    const ema21 = emaStatus(priceRows, 21);
    if (ema21.note) lines.push(ema21.note);
    const md = monthlyDeduction(priceRows, 20);
    if (md.note) lines.push(md.note);
    return lines;
  }

  // ---------------------------------------------------------------
  // 動態法人權重：上市/上櫃先驗 + 實測影響力 各半
  // ---------------------------------------------------------------
  function dynamicFlowWeights(market, inflF, inflT, base) {
    const bucket = (base.foreign_flow || 0.20) + (base.trust_flow || 0.15);
    const priorF = market === "twse" ? 0.65 : 0.35;
    const fi = inflF.influence || 0, ti = inflT.influence || 0;
    const dataF = (fi + ti > 0.05) ? fi / (fi + ti) : priorF;
    const shareF = 0.5 * priorF + 0.5 * dataF;
    const w = Object.assign({}, base);
    w.foreign_flow = round(bucket * shareF, 4);
    w.trust_flow = round(bucket * (1 - shareF), 4);
    return w;
  }

  // ---------------------------------------------------------------
  // 綜合分析入口
  // stockData: { stockId, market('twse'|'tpex'), price, revenue, institutional, margin, shareholding }
  //   price: [{date, close, Trading_Volume}]
  //   revenue: [{date, revenue}]
  //   institutional: [{date, name, buy, sell}]  name含 'Foreign'/'Investment_Trust'
  //   margin: [{date, MarginPurchaseTodayBalance, ShortSaleTodayBalance}]
  //   shareholding: [{date, ForeignInvestmentSharesRatio}]
  // ---------------------------------------------------------------
  function analyze(stockData, weightsInput) {
    const weights0 = Object.assign({}, DEFAULT_WEIGHTS, weightsInput || {});
    const ret = dailyReturns(stockData.price || []);

    const netFAll = instNet(stockData.institutional || [], "Foreign");
    const netTAll = instNet(stockData.institutional || [], "Investment_Trust");
    const inflF = influenceMetrics(netFAll, ret);
    const inflT = influenceMetrics(netTAll, ret);
    const weights = dynamicFlowWeights(stockData.market, inflF, inflT, weights0);

    let controller;
    if (Math.max(inflF.influence, inflT.influence) < 0.15) {
      controller = "無明顯控盤者（散戶/其他資金主導）";
    } else if (inflF.influence >= inflT.influence) {
      controller = `外資控盤（同向率${(inflF.control_rate * 100).toFixed(0)}%、等級相關${sign(inflF.corr)}）`;
    } else {
      controller = `投信控盤（同向率${(inflT.control_rate * 100).toFixed(0)}%、等級相關${sign(inflT.corr)}）`;
    }

    const rev = revenueScore(stockData.revenue || []);
    const fs = foreignScore(stockData.institutional || [], stockData.shareholding || [], ret);
    const ts = trustScore(stockData.institutional || [], ret);
    const ms = marginScore(stockData.margin || [], stockData.price || []);
    const ss = shortScore(stockData.margin || []);
    const tech = technicalScore(stockData.price || []);
    const liq = liquidityScore(stockData.price || [], stockData.institutional || []);

    const scores = {
      revenue_momentum: rev.score, foreign_flow: fs.score, trust_flow: ts.score,
      margin_health: ms.score, short_pressure: ss.score, technical: tech.score, liquidity: liq.score,
    };
    const notes = {
      revenue_momentum: rev.note, foreign_flow: fs.note, trust_flow: ts.note,
      margin_health: ms.note, short_pressure: ss.note, technical: tech.note, liquidity: liq.note,
    };
    const wSum = Object.values(weights).reduce((a, b) => a + b, 0) || 1;
    let total = 0;
    for (const k of Object.keys(scores)) total += scores[k] * (weights[k] || 0);
    total = round(total / wSum, 1);

    let verdict;
    if (total >= 70) verdict = "偏多：動能與籌碼同向，仍需追蹤持續性";
    else if (total >= 55) verdict = "中性偏多：留意個別警訊項目";
    else if (total >= 45) verdict = "中性：多空拉鋸，等訊號明朗";
    else if (total >= 30) verdict = "偏空警戒：籌碼鬆動或動能減速中";
    else verdict = "高風險：多項警訊同時出現";

    const result = {
      stockId: stockData.stockId, market: stockData.market === "twse" ? "上市" : "上櫃",
      total, verdict, controller, scores, notes,
      flowWeights: { 外資: weights.foreign_flow, 投信: weights.trust_flow },
      influence: { 外資: inflF, 投信: inflT },
      series: { revenue: rev.series, price: stockData.price, foreignNet: netFAll, trustNet: netTAll, margin: stockData.margin },
    };
    result.narrative = buildNarrative(result, stockData.price || []);
    return result;
  }

  return {
    DEFAULT_WEIGHTS, LABELS, analyze,
    technical: { ema, smaSeries, kdj, macd, obv, monthlyDeduction, emaStatus },
    // 個別函式也匯出，方便單元測試
    _internal: {
      dailyReturns, instNet, influenceMetrics, stanceScore, revenueScore,
      foreignScore, trustScore, marginScore, shortScore, technicalScore,
      liquidityScore, dynamicFlowWeights, spearman, clip, round,
      ema, kdj, macd, obv, monthlyDeduction, emaStatus, chipNarrative, buildNarrative,
    },
  };
});
