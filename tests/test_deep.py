"""深度校验逻辑测试（合成数据，不联网）。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ashare_quant.config import Config
from ashare_quant.deep import _badges, chip_distribution, technical


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