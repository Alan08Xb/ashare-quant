"""深度校验：对综合分靠前的候选，逐只做基本面 / 成长 / 技术面 / 雷区扫描。

把用户的自选逻辑落地为可执行规则：
  1) 基本面硬指标：ROE 连续 N 年、经营现金流/净利、毛利率、负债率
  2) 成长加分：营收/净利同比
  3) 技术面择时：20 日均线 + 量能 + 筹码分布估算
  4) 雷区扫描：商誉、存贷双高、应收异常、大股东质押(可选)

输出每只股票的“信号徽章” + 一个透明的规则建议（不是荐股，是帮你归类）。
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from .config import Config
from .fetch import fetch_balance, fetch_kline, fetch_kline_close, fetch_main_finance, fetch_top_holders


# ---------------- 技术面 ----------------

def _ma_state(k: pd.DataFrame) -> str:
    """5/10/20/60 均线排列。"""
    if len(k) < 65:
        return "数据不足"
    c = k["close"]
    ma5, ma10, ma20, ma60 = (c.rolling(w).mean().iloc[-1] for w in (5, 10, 20, 60))
    if pd.isna(ma20) or pd.isna(ma60):
        return "数据不足"
    if ma5 > ma10 > ma20 > ma60:
        return "多头"
    if ma5 < ma10 < ma20 < ma60:
        return "空头"
    return "纠缠"


def technical(k: pd.DataFrame) -> dict:
    if k.empty or len(k) < 25:
        return {"ok": False}
    c = k["close"]
    last = c.iloc[-1]
    ma20 = c.rolling(20).mean().iloc[-1]
    dev20 = (last / ma20 - 1) if pd.notna(ma20) and ma20 > 0 else np.nan
    vol20 = k["volume"].rolling(20).mean().iloc[-1]
    vol_last = k["volume"].iloc[-1]
    vol_ratio = (vol_last / vol20) if (pd.notna(vol20) and vol20 > 0) else np.nan
    ret20 = (last / c.iloc[-21] - 1) if len(k) > 21 and c.iloc[-21] > 0 else np.nan
    lo, hi = c.min(), c.max()
    perc = (last - lo) / (hi - lo) if hi > lo else np.nan  # 近一年价格位置(估值位置近似)
    if vol_ratio is None or (pd.notna(vol_ratio) and np.isnan(vol_ratio)):
        vol_state = "平量"
    elif vol_ratio >= 1.5:
        vol_state = "放量"
    elif vol_ratio <= 0.7:
        vol_state = "缩量"
    else:
        vol_state = "平量"
    return {
        "ok": True,
        "ma_state": _ma_state(k),
        "close_vs_ma20": dev20,
        "vol_ratio": vol_ratio,
        "vol_state": vol_state,
        "ret20": ret20,
        "price_perc": perc,
    }


def chip_distribution(k: pd.DataFrame, window: int = 120) -> dict:
    """筹码分布估算（基于历史换手率衰减，非精确数据，勿当主力底牌）。

    算法：每一天的成交价按当日换手率作为“当日在现价换手”的权重，并乘以后续各日的
    (1-换手率) 存活衰减，得到该天成本仍被持有的比重，最后归一化为分布。
    """
    if len(k) < 60:
        return {"ok": False}
    k = k.tail(window).copy()
    close = k["close"].values
    turn = np.nan_to_num(k["turnover"].values, nan=0.0).clip(0, 3) / 100.0
    n = len(k)
    w = np.zeros(n)
    for i in range(n):
        f = turn[i]
        surv = 1.0
        for j in range(i + 1, n):
            surv *= max(1.0 - turn[j], 0.0)
        w[i] = f * surv
    if w.sum() <= 1e-9:
        w[:] = 1.0
    w = w / w.sum()
    last = close[-1]

    profit = float(w[close < last].sum())            # 获利盘比例
    cost_line = float((close * w).sum())             # 平均成本（筹码加权均价）
    dev_cost = last / cost_line - 1 if cost_line > 0 else np.nan

    # 60 日筹码成本均线：仅用最近 60 根 K 线重算，作为"主力近期建仓成本"近似
    k60 = k.tail(60).copy()
    c60 = k60["close"].values
    t60 = np.nan_to_num(k60["turnover"].values, nan=0.0).clip(0, 3) / 100.0
    m = len(k60)
    w60 = np.zeros(m)
    for i in range(m):
        f = t60[i]
        surv = 1.0
        for j in range(i + 1, m):
            surv *= max(1.0 - t60[j], 0.0)
        w60[i] = f * surv
    if w60.sum() > 1e-9:
        w60 = w60 / w60.sum()
        main_cost = float((c60 * w60).sum())
    else:
        main_cost = float(np.nan)

    # 峰数判定（在价格直方图上找局部峰）
    bins = 60
    edges = np.linspace(close.min(), close.max(), bins + 1)
    hist, _ = np.histogram(close, bins=edges, weights=w)
    centers = (edges[:-1] + edges[1:]) / 2
    sm = np.convolve(hist, np.ones(3) / 3, mode="same")
    peaks = []
    for i in range(1, bins - 1):
        if sm[i] > sm[i - 1] and sm[i] >= sm[i + 1] and sm[i] > 0.05:
            peaks.append(i)
    if len(peaks) == 0:
        peak_state = "发散"
    elif len(peaks) == 1:
        peak_state = "单峰"
    elif len(peaks) == 2:
        peak_state = "双峰"
    else:
        peak_state = "多峰"
    # 90% 筹码价位区间（下沿/上沿）
    order = np.argsort(close)
    cw = w[order].cumsum()
    lo9 = close[order][np.searchsorted(cw, 0.05)]
    hi9 = close[order][np.searchsorted(cw, 0.95)]
    return {
        "ok": True,
        "profit": profit,
        "cost_line": cost_line,
        "main_cost": main_cost,          # 60 日主力筹码成本均线
        "close_vs_cost": dev_cost,
        "peak_state": peak_state,
        "chip_lo": lo9,
        "chip_hi": hi9,
    }


# ---------------- 基本面 ----------------

def _annual(mf: pd.DataFrame) -> pd.DataFrame:
    """取出年报（12-31）行。"""
    if mf.empty:
        return mf
    d = pd.to_datetime(mf["REPORT_DATE"], errors="coerce")
    mf = mf.assign(_dt=d)
    return mf[mf["_dt"].dt.month == 12]


def _wsum(vals: list, w: tuple) -> float | None:
    """对最近 3 期做加权；权重归一化（0.6/0.2/0.2）。缺期则跳过该期并重新归一。"""
    import math
    pairs = [(a, b) for a, b in zip(vals, w)
             if a is not None and not (isinstance(a, float) and math.isnan(a))]
    if not pairs:
        return None
    tot = sum(p for _, p in pairs)
    if tot <= 0:
        return None
    return float(sum(v * p for v, p in pairs) / tot)


def fundamental(code: str, cfg: Config) -> dict:
    mf = fetch_main_finance(code)
    bl = fetch_balance(code)
    r: dict = {"ok": False}

    if not mf.empty:
        mf["_dt"] = pd.to_datetime(mf["REPORT_DATE"], errors="coerce")
        mf = mf.dropna(subset=["_dt"]).sort_values("_dt")
        latest = mf.iloc[-1]
        # 最近 3 个报告期（最新在前），用于 0.6/0.2/0.2 加权
        last3 = list(mf.tail(3).iloc[::-1].to_dict("records"))
        roe_w = _wsum([x.get("ROEJQ") for x in last3], cfg.fin_w)
        gm_w = _wsum([x.get("XSMLL") for x in last3], cfg.fin_w)
        debt_w = _wsum([x.get("ZCFZL") for x in last3], cfg.fin_w)
        cf_w = _wsum(
            [x.get("MGJYXJJE") / x.get("EPSJB") if x.get("EPSJB") and x.get("EPSJB") > 0
             else None for x in last3], cfg.fin_w)
        ann = _annual(mf).sort_values("_dt")
        # ROE 连续 N 年 ≥ 阈值（取最近最多 3 个年报）
        roe_vals = ann["ROEJQ"].dropna().tail(3).tolist()
        roe_n = int(sum(1 for v in roe_vals if v >= cfg.roe_min)) if roe_vals else 0
        roe_years = len(roe_vals)
        ann_latest = ann.iloc[-1] if not ann.empty else latest
        eps, cf = ann_latest.get("EPSJB", np.nan), ann_latest.get("MGJYXJJE", np.nan)
        cash_ratio = (cf / eps) if (pd.notna(eps) and pd.notna(cf) and eps > 0) else np.nan
        r.update({
            "ok": True,
            "roe_n": roe_n, "roe_years": roe_years,
            "roe_recent": float(ann_latest["ROEJQ"]) if pd.notna(ann_latest.get("ROEJQ")) else np.nan,
            "roe_w": roe_w,                # 三期加权 ROE
            "cash_ratio": cash_ratio,
            "cash_ratio_w": cf_w,          # 三期加权 经营现金/净利
            "gross_margin": gm_w,          # 三期加权毛利率
            "debt": debt_w,                # 三期加权负债率
            "np_yoy": float(latest["PARENTNETPROFITTZ"]) if pd.notna(latest.get("PARENTNETPROFITTZ")) else np.nan,
            "revenue_yoy": float(latest["TOTALOPERATEREVETZ"]) if pd.notna(latest.get("TOTALOPERATEREVETZ")) else np.nan,
            "ar_ratio": float(latest["YSZKYYSR"]) if pd.notna(latest.get("YSZKYYSR")) else np.nan,
        })

    if not bl.empty:
        bl["_dt"] = pd.to_datetime(bl["REPORT_DATE"], errors="coerce")
        b = bl.sort_values("_dt").iloc[-1]
        eq = b["TOTAL_ASSETS"] - b["TOTAL_LIABILITIES"]
        ta = b["TOTAL_ASSETS"]
        gw = (b["GOODWILL"] / eq) if (pd.notna(eq) and pd.notna(b.get("GOODWILL")) and eq > 0) else np.nan
        cd_double = (
            pd.notna(b.get("MONETARYFUNDS")) and pd.notna(b.get("SHORT_LOAN")) and pd.notna(ta) and ta > 0
            and (b["MONETARYFUNDS"] / ta) > 0.2 and (b["SHORT_LOAN"] / ta) > 0.1
        )
        r.update({
            "goodwill_ratio": gw,
            "cash_asset_ratio": float(b["MONETARYFUNDS"] / ta) if (pd.notna(b.get("MONETARYFUNDS")) and ta and ta > 0) else np.nan,
            "cd_double": bool(cd_double),
        })
    return r


def pledge_ratio(code: str) -> float | None:
    """大股东质押比例（可选）：依赖 akshare，未安装则返回 None（不报错）。"""
    try:
        import akshare as ak
        df = ak.stock_gpzy_pledge_ratio_em(symbol=code)
        if df is None or df.empty:
            return None
        col = [c for c in df.columns if "质押比例" in c]
        if not col:
            return None
        return float(pd.to_numeric(df[col[0]], errors="coerce").dropna().iloc[-1]) / 100.0
    except Exception:
        return None


def fund_holding(code: str, cfg: Config) -> dict:
    """看最新两个季报的十大股东里是否有大基金（国家队/社保/养老金/公募），判断是否新增。

    返回 {ok, new, hold, names, bonus}。hold=当前季在榜，new=当前季在榜但上一季不在。
    """
    out = {"ok": False, "new": False, "hold": False, "names": [], "bonus": 0.0}
    try:
        h = fetch_top_holders(code, size=30)
        if h.empty or "END_DATE" not in h.columns or "HOLDER_NAME" not in h.columns:
            return out
        h = h.assign(_d=pd.to_datetime(h["END_DATE"], errors="coerce")).dropna(subset=["_d"])
        h = h.sort_values("_d")
        dates = h["_d"].unique()
        if len(dates) < 1:
            return out
        def names_for(d):
            return set(h[h["_d"] == d]["HOLDER_NAME"].astype(str))
        latest, prev = names_for(dates[-1]), names_for(dates[-2]) if len(dates) > 1 else set()
        found = {n for n in latest if any(kw in n for kw in cfg.fund_keywords)}
        if not found:
            return out
        is_new = not (found & prev) if prev else True  # 无可比上季时按“新增”计(更谨慎标注)
        out.update({
            "ok": True, "new": bool(is_new), "hold": bool(not is_new),
            "names": sorted(found),
            "bonus": float(cfg.fund_bonus_new if is_new else cfg.fund_bonus_hold),
        })
        return out
    except Exception:
        return out


def buy_zone(k: pd.DataFrame, chip: dict) -> dict:
    """建议买入价 / 止损价：以 20 日线与 60 日筹码成本均线的较高者为支撑参考。

    参考列：
      - ma20      20 日均线
      - cost_line  筹码成本均线（全窗口加权）
      - main_cost  主力筹码成本均线（近 60 日加权）
    建议买入 = max(ma20, main_cost)；现价跌破 20 日线时以 20 日线为买点（等站上）。
    """
    if k.empty or len(k) < 25:
        return {"buy": None, "stop": None, "ma20": None, "cost_line": None, "main_cost": None}
    close = k["close"]
    ma20 = close.rolling(20).mean().iloc[-1]
    cost = chip.get("cost_line") if chip and chip.get("ok") else np.nan
    main_cost = chip.get("main_cost") if chip and chip.get("ok") else np.nan
    # 支撑参考：20 日线 与 主力(60日)筹码成本均线的较高者
    refs = [v for v in (ma20, main_cost) if pd.notna(v) and v > 0]
    support = max(refs) if refs else (float(ma20) if pd.notna(ma20) else float("nan"))
    last = close.iloc[-1]
    if last < ma20:                     # 空头/跌破：以 20 日线为买点(等站上)
        buy, stop = float(ma20), float(ma20) * 0.95
    else:
        buy, stop = float(support), float(support) * 0.95
    return {"buy": round(buy, 2), "stop": round(stop, 2),
            "ma20": round(float(ma20), 2) if pd.notna(ma20) else None,
            "cost_line": round(float(cost), 2) if pd.notna(cost) else None,
            "main_cost": round(float(main_cost), 2) if pd.notna(main_cost) else None}


def chip_adjust(d: dict, cfg: Config) -> float:
    """筹码位置对综合分的调整（应用“主力浮盈→接盘风险 / 主力浮盈小→机会”逻辑）。

    负数=追高/接盘风险(减分)，正数=贴近成本且基本面好的机会(加分)。
    """
    dev, profit = d.get("close_vs_cost"), d.get("profit")
    if not d.get("chip_ok") or pd.isna(dev) or pd.isna(profit):
        return 0.0
    pen = 0.0
    # 追高惩罚：价格远高于筹码成本线 → 大多数持仓者浮盈巨大 → 抛压/接盘风险
    if dev > cfg.chip_pen_dev:
        pen += min((dev - cfg.chip_pen_dev) * 2.0, 2.0)
    # 获利盘过高（>85% 浮盈）→ 主力随时兑现
    if profit > cfg.chip_pen_profit:
        pen += min((profit - cfg.chip_pen_profit) * 2.0, 1.0)
    # 低位机会：基本面好 且 价格贴近成本线(主力浮盈小) → 抛压小、机会大
    quality_ok = (d.get("roe_n", 0) >= 2
                  and d.get("cash_ratio") is not None
                  and not pd.isna(d.get("cash_ratio"))
                  and d.get("cash_ratio") >= cfg.cashflow_min)
    bonus = 0.0
    if quality_ok and -cfg.chip_bonus_dev <= dev <= cfg.chip_bonus_dev and 0.15 <= profit <= 0.75:
        bonus = cfg.chip_bonus
    elif quality_ok and dev < -cfg.chip_bonus_dev and profit <= 0.35:
        bonus = cfg.chip_bonus_dip   # 主力明显浮亏 + 基本面好 → 超跌机会
    return bonus - pen


# ---------------- 信号合成与建议 ----------------

def _badges(d: dict, cfg: Config) -> tuple[list[str], str]:
    """生成信号徽章 + 一个规则化建议标签。"""
    b: list[str] = []
    hard_risk = False

    # 技术面
    if d.get("tech_ok"):
        ms = d["ma_state"]
        b.append("多头排列" if ms == "多头" else ("空头排列" if ms == "空头" else "均线纠缠"))
        dv = d.get("close_vs_ma20")
        if pd.notna(dv):
            b.append("回踩20日线" if 0 <= dv <= 0.05 else ("站上20日线" if dv > 0.05 else "跌破20日线"))
        b.append(d.get("vol_state") + f"({d.get('vol_ratio', float('nan')):.2f})")
    if d.get("chip_ok"):
        b.append(f"筹码{d.get('peak_state')}·获利盘{d.get('profit', 0):.0%}")
        b.append(f"价/成本线{d.get('close_vs_cost', float('nan')):+.0%}")

    # 基本面
    if d.get("fund_ok"):
        roe_n, roe_years = d.get("roe_n", 0), d.get("roe_years", 0)
        b.append(f"ROE≥{cfg.roe_min:.0f}%：{roe_n}/{roe_years}年")
        cr = d.get("cash_ratio")
        if pd.notna(cr):
            b.append("现金流" + ("✓" if cr >= cfg.cashflow_min else "✗" + f"({cr:.2f})"))
        g = d.get("gross_margin")
        if pd.notna(g):
            b.append(f"毛利{g:.0f}%")
        ny = d.get("np_yoy")
        if pd.notna(ny):
            b.append(("成长·净利" + f"+{ny:.0f}%") if ny > 30 else f"净利{ny:+.0f}%")

    # 雷区
    gw = d.get("goodwill_ratio")
    if pd.notna(gw):
        if gw > cfg.goodwill_max:
            b.append(f"商誉⚠({gw:.0%})"); hard_risk = True
        elif gw > 0:
            b.append(f"商誉{gw:.0%}")
    db = d.get("debt")
    if pd.notna(db) and db > cfg.debt_max:
        b.append(f"高负债⚠({db:.0f}%)")
    if d.get("cd_double"):
        b.append("存贷双高❌"); hard_risk = True
    ar = d.get("ar_ratio")
    if pd.notna(ar) and ar > 0.5:
        b.append(f"应收高⚠({ar:.0%})")
    pl = d.get("pledge")
    if pl is not None:
        if pl > cfg.pledge_max:
            b.append(f"质押高⚠({pl:.0%})"); hard_risk = True
        else:
            b.append(f"质押{pl:.0%}")

    # 建议
    quality_ok = d.get("roe_n", 0) >= 2 and d.get("cash_ratio", 0) is not None and d.get("cash_ratio", 0) >= cfg.cashflow_min
    if hard_risk:
        rec = "回避·雷区"
    elif quality_ok and not (pd.notna(db) and db > cfg.debt_max):
        if d.get("ma_state") == "多头" and d.get("close_vs_ma20", 9) <= 0.05 and d.get("price_perc", 1) <= 0.9:
            rec = "优选·可分批观察"
        elif d.get("ma_state") == "多头":
            rec = "趋势向上·等回踩"
        else:
            rec = "质地好·等买点"
    else:
        rec = "一般·观望"
    return b, rec


def deep_check(candidates: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """对候选逐只做深度校验，返回含信号列与徽章的 DataFrame。"""
    rows = []
    for _, s in candidates.iterrows():
        code = str(s["code"]).zfill(6)
        k = fetch_kline(code, (dt.date.today() - dt.timedelta(days=400)).strftime("%Y%m%d"),
                        dt.date.today().strftime("%Y%m%d"))
        tech = technical(k)
        chip = chip_distribution(k)
        fund = fundamental(code, cfg)
        pl = pledge_ratio(code) if fund.get("ok") else None
        fh = fund_holding(code, cfg)
        bz = buy_zone(k, chip)
        d = dict(tech)
        d["tech_ok"] = d.pop("ok", False)
        if chip.get("ok"):
            for kk, vv in chip.items():
                if kk != "ok":
                    d[kk] = vv              # profit, cost_line, close_vs_cost, peak_state, chip_lo, chip_hi
        d["chip_ok"] = chip.get("ok", False)
        if fund.get("ok"):
            for kk, vv in fund.items():
                if kk != "ok":
                    d[kk] = vv              # roe_n, roe_years, cash_ratio, gross_margin, debt, np_yoy, goodwill_ratio, ...
        d["fund_ok"] = fund.get("ok", False)
        d["pledge"] = pl
        d["fund_bonus"] = fh.get("bonus", 0.0)
        d["fund_state"] = "新增" if fh.get("new") else ("持有" if fh.get("hold") else "无")
        d["buy"], d["stop"] = bz.get("buy"), bz.get("stop")
        d["ma20"] = bz.get("ma20")
        d["cost_line"] = bz.get("cost_line")
        d["main_cost"] = bz.get("main_cost")
        d["code"], d["name"] = code, s.get("name")
        d["chip_adj"] = chip_adjust(d, cfg)
        badges, rec = _badges(d, cfg)
        if fh.get("new"):
            badges.insert(0, "大基金新增✓")
        elif fh.get("hold"):
            badges.insert(0, "大基金持有")
        if d["chip_adj"] >= 0.4:
            badges.append("贴近成本·主力浮盈小✓")
        elif d["chip_adj"] <= -0.4:
            badges.append("追高·接盘风险⚠")
        d["badges"] = " | ".join(badges) if badges else "数据不足"
        d["rec"] = rec
        rows.append(d)
    return pd.DataFrame(rows)