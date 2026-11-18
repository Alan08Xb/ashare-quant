"""多周期回测（1/5/20/60）测试：用临时 history 目录合成快照序列。"""

from __future__ import annotations

import os
import tempfile

import numpy as np
import pandas as pd
import pytest

from ashare_quant.backtest import _forward_returns, run_backtest
from ashare_quant.config import Config


def _make_cfg(history_dir: str) -> Config:
    cfg = Config()
    cfg.history_dir = history_dir
    cfg.report_dir = os.path.join(history_dir, "reports")
    cfg.docs_dir = os.path.join(history_dir, "docs")
    return cfg


def _snap(dates: list[str], prices: list[float], scores: list[float], codes=("600519", "000001", "000002", "000003", "000004")):
    """构造一个横截面：同一批 code 在多个日期，价格/分数按给定列表演进。"""
    rows = []
    for d in dates:
        for i, c in enumerate(codes):
            rows.append({"date": d, "code": c, "price": prices[i], "score": scores[i]})
    return pd.DataFrame(rows)


def test_forward_returns_multihorizon():
    """同一 code 价格单调上涨，且每天 snap → fwd_1/fwd_5 均>0，跨周期顺延。"""
    h = pd.DataFrame({
        "code": ["600519"] * 30,
        "date": pd.bdate_range("2025-01-01", periods=30),
        "price": np.linspace(10, 13, 30),      # 缓涨
        "score": np.linspace(1, 2, 30),
    })
    h = _forward_returns(h)
    assert "fwd_1" in h.columns and "fwd_5" in h.columns and "fwd_20" in h.columns
    # 非末尾的 fwd_5 应基本 >0（价格在涨）
    mid = h.iloc[10]
    assert mid["fwd_5"] > 0
    assert h.iloc[-1]["fwd_5"] != h.iloc[-1]["fwd_5"]   # 末尾 fwd_5 是 NaN


def test_run_backtest_multihorizon(tmp_path):
    cfg = _make_cfg(str(tmp_path))
    os.makedirs(cfg.history_dir, exist_ok=True)
    # 6 个交易日，60 只股票，每只价格随日期上行；分数与未来价格正相关（高分→更涨）
    dates = [f"2025-01-0{i}" for i in range(1, 7)]
    codes = [f"{600000 + i:06d}" for i in range(60)]
    frames = []
    for i, d in enumerate(dates):
        for j, c in enumerate(codes):
            # price: 每日递增，且高分股(大j)每日涨幅更大 → fwd 与 score 正相关
            price = 10 + (i + 1) * (0.2 + j * 0.01)
            score = j + i * 0.01
            frames.append({"date": d, "code": c, "price": price, "score": score})
    h = pd.DataFrame(frames)
    # 把每天的截面写成独立 csv（按 date 归档）
    for d, grp in h.groupby("date"):
        fn = str(d).replace("-", "") + ".csv"
        grp.to_csv(os.path.join(cfg.history_dir, fn), index=False)
    bt = run_backtest(cfg)
    assert bt["ok"]
    assert "horizons" in bt
    # 主周期应落在 20（不足20天则退到1/5）
    assert bt["main_horizon"] in (1, 5)
    # 高分股在未来日涨更多 → 分组差为正（不依赖 scipy 的 IC 校验）
    assert bt["spread_mean"] > 0