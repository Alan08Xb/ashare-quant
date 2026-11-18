"""深度校验逻辑测试（合成数据，不联网）。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ashare_quant.config import Config
from ashare_quant.deep import (_badges, chip_distribution, pattern_platform_breakout,
                               pattern_yearly_pullback, technical)


def _kline(n=250, uptrend=True) -> pd.DataFrame:
    dates = pd.bdate_range("2025-01-01", periods=n)
    rng = np.random.default_rng(0)
    if uptrend:
        base = np.linspace(10, 20, n)
    else:
        base = np.linspace(20, 10, n)
    close = base * (1 + rng.normal(0, 0.005, n))
    vol = 1e6 + rng.uniform(-2e5, 2e5, n)
    turn = np.full(n, 1.5)
    return pd.DataFrame({
        "date": dates, "open": close * 0.99, "close": close,
        "high": close * 1.01, "low": close * 0.99,
        "volume": vol, "amount": vol * close, "turnover": turn,
    })


def test_technical_uptrend_bull_ma():
    t = technical(_kline(uptrend=True))
    assert t["ok"] and t["ma_state"] == "多头"
    assert 0 <= t["price_perc"] <= 1


def test_chip_profit_in_uptrend():
    c = chip_distribution(_kline(uptrend=True))
    assert c["ok"]
    assert c["profit"] > 0.5          # 上涨趋势末端，大部分筹码在下方获利
    assert c["cost_line"] > 0
    assert c["peak_state"] in ("单峰", "双峰", "多峰", "发散")


def test_chip_main_cost_uses_only_last_30_days():
    """30 日主力成本均线只受最近 30 根 K 线影响：篡改更早的价格/换手不应改变它。"""
    k = _kline(uptrend=True)
    base = chip_distribution(k)
    cut = len(k) - 30
    k2 = k.copy()
    k2.loc[:cut - 1, "close"] *= 5          # 老 K 线价格失真
    k2.loc[:cut - 1, "turnover"] = 0.01     # 老 K 线换手失真
    alt = chip_distribution(k2)
    assert base["main_cost"] == pytest.approx(alt["main_cost"])
    assert base["cost_line"] != pytest.approx(alt["cost_line"])


def test_badges_flags_risk_and_recommendation():
    cfg = Config()
    d = {"tech_ok": True, "ma_state": "多头", "close_vs_ma20": 0.02,
         "vol_state": "缩量", "vol_ratio": 0.6, "chip_ok": True,
         "peak_state": "单峰", "profit": 0.7, "close_vs_cost": 0.05,
         "fund_ok": True, "roe_n": 3, "roe_years": 3, "cash_ratio": 1.4,
         "gross_margin": 50.0, "np_yoy": 45.0, "goodwill_ratio": 0.05,
         "debt": 40.0, "cd_double": False, "ar_ratio": 0.2, "pledge": 0.1,
         "price_perc": 0.5}
    badges, rec = _badges(d, cfg)
    assert any("ROE≥15%：3/3年" in b for b in badges)
    assert "现金流" in " ".join(badges)
    assert rec == "优选·可分批观察"


def test_badges_high_goodwill_triggers_avoid():
    cfg = Config()
    d = {"tech_ok": True, "ma_state": "多头", "close_vs_ma20": 0.02,
         "vol_state": "平量", "vol_ratio": 1.0, "chip_ok": False,
         "fund_ok": True, "roe_n": 1, "roe_years": 3, "cash_ratio": 0.5,
         "gross_margin": 20.0, "np_yoy": -10.0, "goodwill_ratio": 0.6,
         "debt": 45.0, "cd_double": False, "ar_ratio": 0.3, "pledge": 0.1,
         "price_perc": 0.5}
    badges, rec = _badges(d, cfg)
    assert any("商誉" in b and "⚠" in b for b in badges)
    assert rec == "回避·雷区"


def test_chip_concentration_output():
    c = chip_distribution(_kline(uptrend=True))
    assert c["ok"]
    assert "conc90" in c and "conc70" in c
    assert 0 < c["conc90"] < 1 and 0 < c["conc70"] < 1
    assert c["conc70"] <= c["conc90"] + 1e-9    # 70% 价带更窄


def test_chip_concentration_high_after_consolidation():
    """长期横盘后，筹码高度集中（conc90 明显小于趋势股）。"""
    dates = pd.bdate_range("2025-01-01", periods=200)
    close = np.full(200, 15.0) + np.random.default_rng(1).normal(0, 0.1, 200)
    turn = np.full(200, 2.0)
    k = pd.DataFrame({"date": dates, "open": close * 0.995, "close": close,
                      "high": close * 1.005, "low": close * 0.995,
                      "volume": np.full(200, 1e6), "amount": np.full(200, 1.5e7),
                      "turnover": turn})
    c = chip_distribution(k)
    assert c["ok"] and c["conc90"] < 0.15    # 集中


def test_chip_conc_badge_flagged():
    cfg = Config()
    d = {"tech_ok": True, "ma_state": "多头", "close_vs_ma20": 0.02,
         "vol_state": "平量", "vol_ratio": 1.0, "chip_ok": True,
         "peak_state": "单峰", "profit": 0.5, "close_vs_cost": 0.03,
         "conc90": 0.08, "fund_ok": True, "roe_n": 3, "roe_years": 3,
         "cash_ratio": 1.4, "gross_margin": 40.0, "np_yoy": 20.0,
         "goodwill_ratio": 0.03, "debt": 30.0, "cd_double": False,
         "ar_ratio": 0.15, "pledge": 0.05, "price_perc": 0.5}
    badges, _ = _badges(d, cfg)
    assert any("筹码集中✓" in b for b in badges)


def test_pattern_yearly_pullback_uptrend_then_dip():
    """构造：年线(250日)约10 → 最近60日内先跌破年线→上穿→冲高→缩量回踩不破年线。应命中。"""
    # 250 日横盘 10 → 年线≈10；随后 60 日走完"跌破→上穿→冲高→回踩"
    seg1 = np.full(250, 10.0)
    dip = np.linspace(10, 9.5, 5)          # 跌破年线
    rise = np.linspace(9.5, 17.0, 25)      # 上穿年线
    spike = np.array([18.5])               # 冲高(窗口内最高收盘)
    pull = np.linspace(18.5, 12.5, 30)     # 缩量回踩(仍高于年线≈10)
    close = np.concatenate([seg1, dip, rise, spike, pull])
    n = len(close)
    dates = pd.bdate_range("2023-01-01", periods=n)
    vol = np.concatenate([np.full(250 + 5 + 25, 1e6), np.full(1, 5e6), np.full(30, 2e5)])
    k = pd.DataFrame({"date": dates, "open": close * 0.99, "close": close,
                      "high": close * 1.01, "low": close * 0.99,
                      "volume": vol, "amount": vol * close, "turnover": np.full(n, 1.0)})
    assert pattern_yearly_pullback(k)


def test_pattern_yearly_pullback_no_rebound_returns_false():
    n = 320
    dates = pd.bdate_range("2024-01-01", periods=n)
    close = np.concatenate([
        np.linspace(10, 18, 250),
        np.linspace(18, 15, 70),        # 持续下行跌破年线
    ])
    vol = np.full(n, 1e6)
    k = pd.DataFrame({"date": dates, "open": close * 0.99, "close": close,
                      "high": close * 1.01, "low": close * 0.99,
                      "volume": vol, "amount": vol * close, "turnover": np.full(n, 1.0)})
    assert not pattern_yearly_pullback(k)


def test_pattern_platform_breakout():
    """构造：长期平台(贴近60日线) → 放量长阳突破60日线。应命中。"""
    n = 130
    dates = pd.bdate_range("2024-01-01", periods=n)
    close = np.full(n, 20.0)
    # 最后一天放量突破：close > ma60(=20) > open（开盘低于 60 日线）
    close[-1] = 21.5
    openp = np.full(n, 19.9); openp[-1] = 19.9
    vol = np.full(n, 1e6); vol[-1] = 2e7   # 量/5日均量=20，成交额≈4.3亿≥2亿
    k = pd.DataFrame({"date": dates, "open": openp, "close": close,
                      "high": np.maximum(close, openp) * 1.01, "low": np.minimum(close, openp) * 0.99,
                      "volume": vol, "amount": vol * close, "turnover": np.full(n, 2.0)})
    assert pattern_platform_breakout(k)


def test_pattern_platform_breakout_no_volume_returns_false():
    n = 130
    dates = pd.bdate_range("2024-01-01", periods=n)
    close = np.full(n, 20.0); close[-1] = 21.5
    openp = np.full(n, 19.9)
    vol = np.full(n, 1e6)          # 未放量
    k = pd.DataFrame({"date": dates, "open": openp, "close": close,
                      "high": np.maximum(close, openp) * 1.01, "low": np.minimum(close, openp) * 0.99,
                      "volume": vol, "amount": vol * close, "turnover": np.full(n, 2.0)})
    assert not pattern_platform_breakout(k)