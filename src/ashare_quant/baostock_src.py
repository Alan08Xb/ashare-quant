"""baostock 备用数据源：东财被机房 IP 屏蔽时，补全质量/成长/动量基本面。

baostock（证券宝）是国内免费源，独立于东财/新浪服务器，免注册免 token。
本模块只做降级兜底——东财快照或东财 F10 失败时调用，绝不让 baostock 故障拖垮整个 run。

实测能力（0.9.40）：
- 质量  query_profit_data   -> roeAvg(ROE) / gpMargin(毛利率) / npMargin(净利率) / netProfit(净利)
- 成长  query_growth_data   -> YOYNI(净利同比) / YOYPNI(归母净利同比)
- 动量  query_history_k_data_plus 前复权日线 -> 算 60 交易日涨幅
- 上市日 query_stock_basic  -> ipoDate
- 行业  无（query_stock_basic 无 industry 字段）→ 行业因子在降级路径归 0

约定：代码统一 6 位数字（如 "600519"），内部加市场前缀 sh./sz.。
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

_bs = None          # 懒连接单例
_cache: dict[str, dict] = {}   # code -> {roe, gross_margin, np_yoy, net_profit, ipo_date}
_login_failed = None  # 登录熔断：失败一次后本次进程内不再重试（避免 100 只逐只磨超时）


def _session():
    """懒登录 baostock（进程内只登一次）。失败抛 RuntimeError，由调用方降级。

    带熔断：一旦登录失败，本次进程内后续调用立即抛同样错误，不再重复打网络——
    否则 Actions 机房 IP 被屏蔽时，粗筛 100 只每只都会卡一次登录超时。
    """
    global _bs, _login_failed
    if _bs is not None:
        return _bs
    if _login_failed is not None:
        raise RuntimeError(f"baostock 登录已失败（熔断）: {_login_failed}")
    import baostock as bs
    lg = bs.login()
    if lg.error_code != "0":
        _login_failed = lg.error_msg
        raise RuntimeError(f"baostock 登录失败: {lg.error_msg}")
    _bs = bs
    return _bs


def _iter_rows(rs) -> list[dict]:
    out = []
    while (rs.error_code == "0") and rs.next():
        out.append(dict(zip(rs.fields, rs.get_row_data())))
    return out


def _sym(code: str) -> str:
    """6 位代码加交易所前缀：6 开头=沪(sh.)，其余=深(sz.)。北交所已被新浪侧滤除。"""
    code = str(code).zfill(6)
    return ("sh." if code.startswith("6") else "sz.") + code


def _to_float(v):
    try:
        f = float(v)
        return f if np.isfinite(f) else np.nan
    except (TypeError, ValueError):
        return np.nan


def _report_candidates(today: dt.date) -> list[tuple[int, int]]:
    """按日期推算"最新已披露报告期"候选（最新在前）。A股披露时滞：
    一季报≤4/30，中报≤8/31，三季报≤10/31，年报≤次年4/30。
    """
    y, q = today.year, (today.month - 1) // 3 + 1
    if q == 1:      # 1-3月：最新是三季报(上年Q3)；年报要4月底才出
        return [(y - 1, 3), (y - 1, 4), (y - 1, 2)]
    if q == 2:      # 4-6月：一季报已出(4/30)
        return [(y, 1), (y - 1, 4), (y - 1, 3)]
    if q == 3:      # 7-9月：一季报在手；中报8月底起
        return [(y, 1), (y, 2), (y - 1, 4)]
    return [(y, 3), (y, 2), (y, 1)]   # 10-12月：三季报已出(10/31)


def _latest_report(bs, query_fn, today: dt.date) -> dict | None:
    """按候选报告期顺序取最新一条非空记录。"""
    for year, quarter in _report_candidates(today):
        rs = query_fn(year, quarter)
        rows = _iter_rows(rs)
        if rows:
            return max(rows, key=lambda x: x.get("pubDate", ""))
    return None


def fetch_quality(code: str) -> dict:
    """拉一只股票的质量/成长/上市日。失败或空返回 {}。结果按 code 缓存。"""
    code = str(code).zfill(6)
    if code in _cache:
        return _cache[code]
    sym = _sym(code)
    today = dt.date.today()
    r: dict = {}
    try:
        bs = _session()   # 登录失败也算"本只失败"→返回 {}，绝不外抛
        prof = _latest_report(bs, lambda y, q: bs.query_profit_data(sym, year=y, quarter=q), today)
        if prof:
            r["roe"] = _to_float(prof.get("roeAvg")) * 100.0            # 小数→%（与东财ROEJQ同单位）
            r["gross_margin"] = _to_float(prof.get("gpMargin")) * 100.0   # 小数→%
            r["net_profit"] = _to_float(prof.get("netProfit"))            # 元
        grow = _latest_report(bs, lambda y, q: bs.query_growth_data(sym, year=y, quarter=q), today)
        if grow:
            r["np_yoy"] = _to_float(grow.get("YOYNI")) * 100.0            # 小数→%
        basic = _iter_rows(bs.query_stock_basic(sym))
        if basic:
            r["ipo_date"] = basic[0].get("ipoDate", "")
    except Exception:
        pass
    if r:
        _cache[code] = r
    return r


def fetch_ret60(code: str) -> float:
    """前复权日线算 60 交易日涨幅（%）。K线不足 60 行返回 NaN。"""
    sym = _sym(code)
    end = dt.date.today()
    beg = end - dt.timedelta(days=200)
    try:
        bs = _session()   # 登录失败→返回 NaN，绝不外抛
        rs = bs.query_history_k_data_plus(
            sym, "date,close",
            start_date=beg.strftime("%Y-%m-%d"), end_date=end.strftime("%Y-%m-%d"),
            frequency="d", adjustflag="2")
        rows = _iter_rows(rs)
        if len(rows) < 61:
            return float("nan")
        close = pd.to_numeric(pd.Series([x["close"] for x in rows]), errors="coerce").dropna()
        if len(close) < 61:
            return float("nan")
        close = close.tail(61)
        return float((close.iloc[-1] / close.iloc[0] - 1) * 100.0)
    except Exception:
        return float("nan")


def enrich(codes: list[str], fill_ret60: bool = True) -> pd.DataFrame:
    """对一批股票批量补全基本面，返回 index=code 的 DataFrame。

    列：roe / gross_margin / np_yoy / net_profit / ipo_date（可缺），ret_60 可选（默认补）。
    任何股票失败则该行对应列为 NaN——绝不抛异常拖垮流程。
    """
    rows = {}
    for code in codes:
        code = str(code).zfill(6)
        r = fetch_quality(code)
        if not r:
            r = {}
        if fill_ret60:
            r["ret_60"] = fetch_ret60(code)
        rows[code] = r
    out = pd.DataFrame.from_dict(rows, orient="index")
    out.index.name = "code"
    return out