"""因子计算：去极值 + 标准化 + 市值中性化。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .config import Config


def annualized_vol(close: pd.Series, days: int = 60, annualize: float = 252.0) -> float:
    """历史 N 日真实波动率（年化）：对数收益率的日标准差 × sqrt(年化)。"""
    r = np.log(close / close.shift(1)).dropna()
    if len(r) < 5:
        return float("nan")
    r = r.tail(days)
    return float(r.std(ddof=0) * np.sqrt(annualize))


def winsorize(s: pd.Series, lo: float = 0.01, hi: float = 0.99) -> pd.Series:
    """分位数去极值（缩尾）。"""
    s = s.astype(float)
    ql, qh = s.quantile(lo), s.quantile(hi)
    return s.clip(ql, qh)


def zscore(s: pd.Series) -> pd.Series:
    """截面标准化。"""
    s = s.astype(float)
    m, sd = s.mean(), s.std(ddof=0)
    if sd == 0 or pd.isna(sd):
        return s.mul(0.0)
    return (s - m) / sd


def _raw_value(df: pd.DataFrame) -> pd.Series:
    """价值因子：账面市值比 + 盈利收益率，二者取可用者的平均。"""
    idx = df.index
    btob = np.where(df["pb"] > 0, 1.0 / df["pb"], np.nan)
    etop = np.where((df["net_profit"] > 0) & (df["mktcap"] > 0),
                    df["net_profit"] / df["mktcap"], np.nan)
    zv = pd.Series(btob, index=idx)
    ze = pd.Series(etop, index=idx)
    zv = zscore(winsorize(zv)).replace([np.inf, -np.inf], np.nan)
    ze = zscore(winsorize(ze)).replace([np.inf, -np.inf], np.nan)
    val = pd.concat([zv, ze], axis=1).mean(axis=1, skipna=True)
    return val.fillna(0.0)


def _raw_quality(df: pd.DataFrame) -> pd.Series:
    """质量因子：ROE + 毛利率 + 净利同比，可用者平均。"""
    parts = []
    for c in ["roe", "gross_margin", "np_yoy"]:
        if c in df.columns:
            z = zscore(winsorize(df[c].astype(float)))
            parts.append(z)
    if not parts:
        return pd.Series(0.0, index=df.index)
    return pd.concat(parts, axis=1).mean(axis=1, skipna=True).fillna(0.0)


def _raw_momentum(df: pd.DataFrame) -> pd.Series:
    """动量因子：60 日涨跌幅（回看趋势，反向排除短期过热见 score）。"""
    return zscore(winsorize(df["ret_60"].astype(float)))


def _raw_lowvol(df: pd.DataFrame, vol_map: dict | None = None) -> pd.Series:
    """低波动：优先用历史真实波动率（年化），缺失时回退到当日振幅代理。"""
    if vol_map:
        vols = df["code"].astype(str).map(
            lambda c: (vol_map.get(c) if c in (vol_map or {}) else None))
        if vols.notna().any():
            v = pd.to_numeric(vols, errors="coerce")
            return -zscore(winsorize(v)).fillna(0.0)
    if "amplitude" not in df.columns:
        return pd.Series(0.0, index=df.index)
    return -zscore(winsorize(df["amplitude"].astype(float)))


def size_neutralize(df: pd.DataFrame, factor: pd.Series, cfg: Config) -> pd.Series:
    """市值中性化：按市值分位桶内去均值，消除大小盘对因子的污染。

    不做中性化的因子可能只是因为“偏小市值”而显得有效。
    """
    if not cfg.size_neutral or factor.isna().all():
        return factor
    buckets = pd.qcut(df["mktcap"].rank(method="first"), cfg.size_buckets, labels=False)
    out = factor.groupby(buckets).transform(lambda x: x - x.mean())
    return out


def compute_factors(df: pd.DataFrame, cfg: Config, vol_map: dict | None = None) -> pd.DataFrame:
    """输入清洗后的股票池，输出带各因子原始分与综合分的 DataFrame。vol_map: {code: close_Series}。"""
    d = df.copy()

    d["f_value"] = _raw_value(d)
    d["f_quality"] = _raw_quality(d)
    d["f_momentum"] = _raw_momentum(d)
    d["f_lowvol"] = _raw_lowvol(d, vol_map)

    # 市值中性化（可选）
    for c in ["f_value", "f_quality", "f_momentum", "f_lowvol"]:
        d[c] = size_neutralize(d, d[c], cfg)

    # 综合分
    d["score"] = (
        cfg.w_value * d["f_value"]
        + cfg.w_quality * d["f_quality"]
        + cfg.w_momentum * d["f_momentum"]
        + cfg.w_lowvol * d["f_lowvol"]
    )
    d["rank"] = d["score"].rank(ascending=False, method="first")
    return d