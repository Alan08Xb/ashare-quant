"""股票池清洗：剔除无法真实买入 / 质量存疑 / 流动性不足的标的。"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from .config import Config


def clean_universe(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """对快照做清洗，返回“可买入股票池”。"""
    d = df.copy()

    # 数值化
    for c in ["price", "pct", "amount", "amplitude", "turnover", "pe", "pb",
              "mktcap", "floatcap", "ret_60", "ret_ytd", "roe", "np_yoy",
              "gross_margin", "net_profit"]:
        if c in d.columns:
            d[c] = pd.to_numeric(d[c], errors="coerce")

    n_before = len(d)

    # 1) 基本可交易性 + 去重
    d = d.dropna(subset=["code", "price"])
    d = d.drop_duplicates(subset=["code"])
    d = d[d["price"] > 0]
    d = d[d["amount"] > 0]                       # 排除停牌（成交额为 0 或缺失）

    # 2) 排除 ST / *ST / 退市
    if cfg.exclude_st:
        mask = ~d["name"].astype(str).str.contains("ST|退", na=False, case=False)
        d = d[mask]

    # 3) 排除次新（上市不足 N 天）
    ipo = pd.to_numeric(d["ipo_date"], errors="coerce")
    today = int(dt.date.today().strftime("%Y%m%d"))
    days_listed = (today - ipo)
    d["days_listed"] = days_listed
    d = d[(days_listed.isna()) | (days_listed >= cfg.min_listing_days)]

    # 4) 流动性门槛：成交额 / 流通市值
    d = d[d["amount"] >= cfg.min_amount]
    if cfg.min_float_cap > 0:
        d = d[d["floatcap"] >= cfg.min_float_cap]

    # 5) 排除当日涨停（涨跌幅贴着对应板块涨跌停价，无法真实成交）
    if cfg.exclude_limit_up:
        d["limit_up"] = _is_limit_up(d)
        d = d[~d["limit_up"]]

    # 5b) 按行业关键词剔除整块板块（默认剔除地产）
    if cfg.exclude_industry_kw:
        m = d["industry"].astype(str).str
        drop = pd.Series(False, index=d.index)
        for kw in cfg.exclude_industry_kw:
            drop = drop | m.contains(kw, na=False)
        d = d[~drop]

    # 6) 市值必须为正
    d = d[d["mktcap"] > 0]

    # 7) 净资产为负（pb<=0，资不抵债）：其 ROE/PE/账面指标无意义，默认排除
    if cfg.exclude_negative_equity:
        d = d[d["pb"] > 0]

    d = d.drop(columns=["days_listed"], errors="ignore")
    d = d.drop(columns=["limit_up"], errors="ignore")
    d.attrs["n_before"] = n_before
    d.attrs["n_after"] = len(d)
    return d


def _is_limit_up(d: pd.DataFrame) -> pd.Series:
    """按板块判断是否涨停。深主板/沪主板 10%，创业板/科创板 20%。"""
    pct = d["pct"].clip(lower=0)
    code = d["code"].astype(str)
    is_gem_star = code.str.startswith(("300", "301", "688", "689"))
    lim = np.where(is_gem_star, 19.5, 9.8)
    # 留 0.5% 容差：实际离涨停价差在 0.3% 以内视为涨停附近
    return (lim - pct) <= 0.3


def industry_stats(d: pd.DataFrame) -> str:
    return (f"{d.attrs.get('n_before', '?')} → {d.attrs.get('n_after', '?')} 只 | "
            f"行业数 {d['industry'].nunique()} | "
            f"市值中位数 {d['floatcap'].median()/1e8:.0f} 亿")