"""新架构测试：当日数据仓库(dailydb) / 精调分合并(apply_deep_adj) / baostock 深检兜底(deep_bs)。"""

from __future__ import annotations

import datetime as dt
import os

import numpy as np
import pandas as pd
import pytest

from ashare_quant import dailydb
from ashare_quant.config import Config
from ashare_quant.core import apply_deep_adj
from ashare_quant.deep_bs import _deep_one_bs, deep_check_from_baostock
from tests.test_deep import _kline


def _cfg(tmp) -> Config:
    cfg = Config()
    cfg.dailydb_dir = os.path.join(tmp, "dailydb")
    cfg.history_dir = os.path.join(tmp, "history")
    cfg.report_dir = os.path.join(tmp, "reports")
    cfg.docs_dir = os.path.join(tmp, "docs")
    return cfg


def test_dailydb_roundtrip_fresh(tmp_path):
    cfg = _cfg(str(tmp_path))
    day = dt.date.today().strftime("%Y%m%d")
    snap = pd.DataFrame({"code": ["600519"], "score": [1.0]})
    top = pd.DataFrame({"code": ["600519"], "rank": [1], "score_adj": [1.2]})
    deep = pd.DataFrame({"code": ["600519"], "chip_adj": [0.4]})
    meta = {"source": "eastmoney", "universe": "x", "degraded": False}
    bt = {"ok": True, "days": 5}
    dailydb.save_day(cfg, day, snapshot=snap, top=top, deep=deep, meta=meta, backtest=bt)
    assert dailydb.is_fresh(cfg, day) is True
    out = dailydb.load_day(cfg, day)
    assert list(out["top"]["code"]) == ["600519"]
    assert out["deep"]["chip_adj"].iloc[0] == 0.4
    assert out["meta"]["source"] == "eastmoney"
    assert out["backtest"]["days"] == 5
    # 读回 top/deep 时 code 保持字符串（含前导零）
    assert str(out["top"]["code"].iloc[0]) == "600519"


def test_dailydb_not_fresh_when_failed_or_wrong_source(tmp_path):
    cfg = _cfg(str(tmp_path))
    day = dt.date.today().strftime("%Y%m%d")
    dailydb.mark_status(cfg, day, "failed", "eastmoney", {}, "")
    assert dailydb.is_fresh(cfg, day) is False
    dailydb.mark_status(cfg, day, "success", "sina", {}, "")
    assert dailydb.is_fresh(cfg, day) is False   # 非东财来源不算新鲜


def test_apply_deep_adj_merges_and_ranks(tmp_path):
    cfg = _cfg(str(tmp_path))
    cfg.top_n = 2
    cand = pd.DataFrame({
        "code": ["600519", "000001", "300750"],
        "score": [3.0, 2.0, 1.0],
        "f_industry": [0.5, 0.0, 0.0],
    })
    deep = pd.DataFrame({
        "code": ["600519", "300750"],
        "fund_bonus": [0.5, 0.0], "chip_adj": [0.4, -0.2], "pattern_adj": [0.3, 0.0],
    })
    top, deep_disp = apply_deep_adj(cand, deep, cfg)
    # 600519: score3 + 行业0.5 + 基金0.5 + 筹码0.4 + 形态0.3 = 4.7
    # 300750: 1.0 + 0 + 0 -0.2 + 0 = 0.8
    # 000001(缺深检): 2.0 + 0 + 0 = 2.0 → 排第2
    assert top.iloc[0]["code"] == "600519"
    assert top.iloc[0]["score_adj"] == pytest.approx(4.7)
    assert top.iloc[1]["code"] == "000001"          # 缺深检 code 加成归 0，未崩
    assert set(top["code"]) == {"600519", "000001"}  # top_n=2
    assert len(deep_disp) == 1 and deep_disp.iloc[0]["code"] == "600519"


def test_deep_bs_one_builds_signals(monkeypatch):
    cfg = Config()
    cfg.dailydb_dir = "unused"
    k = _kline(n=320, uptrend=True)
    monkeypatch.setattr("ashare_quant.deep_bs.bs.fetch_kline", lambda code, days=520: k)
    monkeypatch.setattr("ashare_quant.deep_bs.bs.fetch_quality",
                        lambda code: {"roe": 15.0, "gross_margin": 40.0, "np_yoy": 20.0})
    d = _deep_one_bs("600519", "贵州茅台", cfg)
    assert d is not None
    assert d["code"] == "600519" and d["name"] == "贵州茅台"
    assert d["tech_ok"] is True and d["chip_ok"] is True
    assert d["fund_ok"] is True
    assert "pattern_yearly" in d and "pattern_platform" in d
    assert "pattern_adj" in d and "chip_adj" in d
    assert "buy" in d and "stop" in d and d["stop"] < d["buy"]
    assert d["badges"] and d["rec"] in ("优选·形态买点✓", "优选·可分批观察", "趋势向上·等回踩", "一般·观望", "质地好·等买点")
    # baostock 无经营现金/净利 → cash_ratio None → quality_ok False → 不强推"优选·形态"
    assert d["cash_ratio"] is None


def test_deep_bs_serial_deadline(tmp_path, monkeypatch):
    cfg = Config()
    cfg.dailydb_dir = os.path.join(str(tmp_path), "dailydb")
    k = _kline(n=320, uptrend=True)
    monkeypatch.setattr("ashare_quant.deep_bs.bs.fetch_kline", lambda code, days=520: k)
    monkeypatch.setattr("ashare_quant.deep_bs.bs.fetch_quality",
                        lambda code: {"roe": 15.0, "gross_margin": 40.0, "np_yoy": 20.0})
    cand = pd.DataFrame({"code": ["600519", "000001", "300750"], "name": ["a", "b", "c"]})
    out = deep_check_from_baostock(cand, cfg, deadline=300.0)
    assert len(out) == 3              # 全部成功（mock 即时返回）
    assert list(out["code"]) == ["600519", "000001", "300750"]