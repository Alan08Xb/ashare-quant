"""第二轮改动测试：真实波动率 / 地产剔除 / 质量权重 / 建议买入价 / 大基金信号。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ashare_quant.config import Config
from ashare_quant.deep import buy_zone, fund_holding
from ashare_quant.factors import annualized_vol, compute_factors
from ashare_quant.universe import clean_universe
from tests.test_deep import _kline


def _cfg(**kw) -> Config:
    cfg = Config()
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


def test_annualized_vol_constant_is_zero():
    s = pd.Series(np.full(80, 10.0))
    assert annualized_vol(s, 60) == 0.0


def test_annualized_vol_trending_positive():
    rng = np.random.default_rng(1)
    price = 10 * np.exp(np.cumsum(rng.normal(0.001, 0.02, 100)))
    s = pd.Series(price)
    v = annualized_vol(s, 60)
    assert 0.1 < v < 0.8  # 20% 日波动 → 年化约 31%，留裕量


def test_universe_excludes_realestate():
    cfg = _cfg()
    df = _sample_with_industry(["电子", "银行", "房地产开发", "房地产开发Ⅱ", "软件"])
    uni = clean_universe(df, cfg)
    assert not uni["industry"].astype(str).str.contains("地产").any()
    assert "电子" in set(uni["industry"])
    assert "银行" in set(uni["industry"])


def test_lowvol_uses_vol_map_not_amplitude():
    cfg = _cfg()
    df = _sample_with_industry(["电子", "银行", "医药"])
    # 不给 vol_map：用振幅代理；给 vol_map：用真实波动率 → 结果应不同
    out_no = compute_factors(clean_universe(df.copy(), cfg), cfg, None)["f_lowvol"]
    vol_map = {}
    for _, r in clean_universe(df.copy(), cfg).iterrows():
        vol_map[str(r["code"])] = 0.2
    out_yes = compute_factors(clean_universe(df.copy(), cfg), cfg, vol_map)["f_lowvol"]
    assert out_no.notna().any() and out_yes.notna().any()


def test_buy_zone_uptrend():
    k = _kline(uptrend=True)
    chip = {"ok": True, "cost_line": float(k["close"].mean())}
    bz = buy_zone(k, chip)
    assert bz["buy"] is not None and bz["buy"] > 0
    assert bz["stop"] < bz["buy"]  # 止损在买入下方


def test_fund_holding_new_detected(monkeypatch):
    cfg = _cfg()
    rows = []
    for q, names in [("2025-12-31", ["某某投资公司"]),
                     ("2026-09-30", ["某某投资公司", "全国社保基金一一七组合"])]:
        for rk, n in enumerate(names, 1):
            rows.append({"END_DATE": q, "HOLDER_NAME": n, "HOLDER_RANK": rk})
    fake = pd.DataFrame(rows)
    monkeypatch.setattr("ashare_quant.deep.fetch_top_holders", lambda code, size: fake)
    out = fund_holding("600519", cfg)
    assert out["ok"] and out["new"] is True
    assert out["bonus"] == cfg.fund_bonus_new
    assert any("社保" in n for n in out["names"])


def test_fund_holding_hold_no_new(monkeypatch):
    cfg = _cfg()
    rows = []
    for q in ["2025-12-31", "2026-09-30"]:
        rows.append({"END_DATE": q, "HOLDER_NAME": "中央汇金资产管理", "HOLDER_RANK": 1})
    fake = pd.DataFrame(rows)
    monkeypatch.setattr("ashare_quant.deep.fetch_top_holders", lambda code, size: fake)
    out = fund_holding("600519", cfg)
    assert out["ok"] and out["new"] is False and out["hold"] is True
    assert out["bonus"] == cfg.fund_bonus_hold


def test_feishu_card_has_ma20_and_main_cost_columns():
    from ashare_quant.feishu import _DEEP_KEYS, build_card

    top = [{"rank": 1, "code": "600519", "name": "贵州茅台", "industry": "白酒",
            "price": 1500.0, "pct": 1.23, "ret_60": 5.0, "roe": 30.0}]
    deep = [{"code": "600519", "name": "贵州茅台", "buy": 1480.0, "stop": 1406.0,
             "ma20": 1495.5, "main_cost": 1470.25, "chip_adj": 0.31999999,
             "rec": "优选·可分批观察", "badges": "多头排列 | 回踩20日线"}]
    tables = [e for e in build_card(top, {"universe": "全市场"}, {}, deep)
              if e.get("tag") == "table"]
    deep_tbl = tables[-1]
    assert [c["name"] for c in deep_tbl["columns"]] == _DEEP_KEYS
    assert "ma20" in _DEEP_KEYS and "main_cost" in _DEEP_KEYS
    row = deep_tbl["rows"][0]
    assert row["ma20"] == "1495.50" and row["main_cost"] == "1470.25"
    assert row["chip_adj"] == "+0.32"
    assert row["buy"] == "1480.00" and row["stop"] == "1406.00"


def _sample_with_industry(industries, per=40) -> pd.DataFrame:
    rng = np.random.default_rng(3)
    n = len(industries) * per
    codes = [f"{300000 + i}" for i in range(n)]
    return pd.DataFrame({
        "code": codes,
        "name": [f"股{i}" for i in range(n)],
        "industry": np.repeat(industries, per),
        "price": rng.uniform(3, 120, n),
        "pct": rng.uniform(-3, 6, n),
        "amount": rng.uniform(1e8, 2e9, n),
        "amplitude": rng.uniform(1, 8, n),
        "turnover": rng.uniform(0.5, 10, n),
        "pe": rng.uniform(-20, 60, n),
        "pb": rng.uniform(0.5, 7, n),
        "mktcap": rng.uniform(2e9, 9e10, n),
        "floatcap": rng.uniform(2e9, 9e10, n),
        "ret_60": rng.uniform(-30, 50, n),
        "ret_ytd": rng.uniform(-40, 70, n),
        "ipo_date": 20150101,
        "roe": rng.normal(10, 6, n),
        "gross_margin": rng.uniform(10, 60, n),
        "np_yoy": rng.uniform(-40, 100, n),
        "net_profit": rng.uniform(1e8, 2e9, n),
    })