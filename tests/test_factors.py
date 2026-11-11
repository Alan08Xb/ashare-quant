"""因子逻辑单元测试（用合成数据，不联网）。"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from ashare_quant.config import Config
from ashare_quant.factors import compute_factors, winsorize, zscore
from ashare_quant.universe import clean_universe


def _cfg(**kw) -> Config:
    cfg = Config()
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


def _sample_df(n=400) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    codes = [f"{600000 + i}" for i in range(n)]
    ipo_old = 20150101
    ipo_recent = int((dt.date.today() - dt.timedelta(days=30)).strftime("%Y%m%d"))
    return pd.DataFrame({
        "code": codes,
        "name": [f"股票{i}" for i in range(n)],
        "industry": rng.choice(["电子", "医药", "机械", "食品"], n),
        "price": rng.uniform(3, 120, n),
        "pct": rng.uniform(-4, 8, n),
        "amount": rng.uniform(2e7, 2e9, n),
        "amplitude": rng.uniform(1, 9, n),
        "turnover": rng.uniform(0.3, 12, n),
        "pe": rng.uniform(-20, 60, n),
        "pb": rng.uniform(0.5, 8, n),
        "mktcap": rng.uniform(1e9, 8e10, n),
        "floatcap": rng.uniform(1e9, 8e10, n),
        "ret_60": rng.uniform(-30, 60, n),
        "ret_ytd": rng.uniform(-40, 80, n),
        "ipo_date": np.where(rng.random(n) < 0.9, ipo_old, ipo_recent),
        "roe": rng.normal(8, 6, n),
        "gross_margin": rng.uniform(5, 70, n),
        "np_yoy": rng.uniform(-60, 120, n),
        "net_profit": rng.uniform(1e7, 3e9, n),
    })


def test_winsorize_clips_outliers():
    s = pd.Series([1.0, 2.0, 3.0, 1000.0, -500.0, 4.0])
    w = winsorize(s)
    # 缩尾后，最值应等于原始数据对应分位数
    assert w.max() == s.quantile(0.99)
    assert w.min() == s.quantile(0.01)


def test_zscore_standardizes():
    z = zscore(pd.Series([1.0, 2.0, 3.0, 4.0, 5.0]))
    assert abs(z.mean()) < 1e-9 and abs(z.std(ddof=0) - 1) < 1e-9


def test_universe_excludes_st_and_new_and_zero_amount():
    cfg = _cfg()
    df = _sample_df(200)
    df.loc[0, "name"] = "*ST测试"
    df.loc[1, "amount"] = 0.0  # 停牌
    df.loc[2, "pb"] = -2.0     # 净资产为负（资不抵债），默认排除
    recent = int((dt.date.today() - dt.timedelta(days=3)).strftime("%Y%m%d"))
    df.loc[3, "ipo_date"] = recent  # 3 天前上市 → 次新，应被排除
    clean = clean_universe(df, cfg)
    assert not clean["name"].str.contains("ST").any()
    assert (clean["amount"] > 0).all()
    assert (clean["pb"] > 0).all()
    assert clean["code"].nunique() == len(clean)
    assert (clean["code"] != df.loc[3, "code"]).all()


def test_compute_factors_produces_score_and_rank():
    cfg = _cfg(size_neutral=True)
    df = _sample_df(400)
    clean = clean_universe(df, cfg)
    out = compute_factors(clean, cfg)
    assert {"score", "rank", "f_value", "f_quality", "f_momentum"}.issubset(out.columns)
    # rank 应恰好是 1..N 的排列，且 score 有限
    r = out["rank"].dropna().astype(int)
    assert set(r.tolist()) == set(range(1, len(out) + 1))
    assert out["score"].notna().all()
    assert len(out) == len(clean)


def test_high_quality_stocks_rank_higher():
    cfg = _cfg(size_neutral=False)
    df = _sample_df(300)
    # 让第一只股票在所有因子上都最好，综合分应排名第一
    df.loc[0, ["roe"]] = 45.0
    df.loc[0, ["gross_margin"]] = 80.0
    df.loc[0, ["np_yoy"]] = 150.0
    df.loc[0, ["ret_60"]] = 70.0
    df.loc[0, ["pb"]] = 0.5
    df.loc[0, ["net_profit"]] = 5e9
    df.loc[0, ["mktcap"]] = 2e9
    df.loc[0, ["amplitude"]] = 0.1
    out = compute_factors(clean_universe(df, cfg), cfg)
    best = out.sort_values("rank").iloc[0]
    assert best["code"] == df["code"].iloc[0]