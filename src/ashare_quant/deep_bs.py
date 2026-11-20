"""baostock 深检兜底：东财深检失败时的降级深度分析。

东财被机房 IP 屏蔽（Actions 上常见）时，deep_check 熔断返回空表。此时用 baostock
补一份简化版深检：技术面(均线/量能) + 筹码分布(三角) + 结构形态(回踩年线/平台突破)
+ 买点/止损 + 基本面(baostock 的 ROE/毛利/净利同比)。

baostock 缺 经营现金/商誉/负债/十大股东/质押 → 对应雷区与 quality_ok 判定自动缺省
（badges 不显示这些项，建议大多回落"一般·观望"，诚实反映数据受限）。

baostock 是 socket 长连接非线程安全 → 必须串行，加整体 deadline 熔断。
"""

from __future__ import annotations

import time

import numpy as np
import pandas as pd

from . import baostock_src as bs
from .config import Config
from .deep import (_badges, buy_zone, chip_adjust, chip_distribution,
                   pattern_platform_breakout, pattern_yearly_pullback, technical)


def _deep_one_bs(code: str, name: str | None, cfg: Config) -> dict | None:
    """单只 baostock 深检。失败/数据不足返回 None（不中断整体）。"""
    k = bs.fetch_kline(code, 520)
    if k.empty or len(k) < 310:      # 回踩年线需 250 日线 + lookback 60
        return None
    tech = technical(k)
    chip = chip_distribution(k)
    pat_yearly = pattern_yearly_pullback(k, cfg.pattern_yearly_days) if cfg.pattern_enable else False
    pat_platform = pattern_platform_breakout(k, cfg.pattern_platform_days,
                                             float(cfg.pattern_min_amount)) if cfg.pattern_enable else False
    f = bs.fetch_quality(code)       # roe / gross_margin / np_yoy / ipo_date（无现金/负债/股东）
    bz = buy_zone(k, chip)

    d = dict(tech)
    d["tech_ok"] = d.pop("ok", False)
    if chip.get("ok"):
        for kk, vv in chip.items():
            if kk != "ok":
                d[kk] = vv
    d["chip_ok"] = chip.get("ok", False)
    d["fund_ok"] = bool(f)
    if f:
        # baostock 只有单期 ROE/毛利/净利同比，无法算"连续 N 年"；用最近一期近似
        d["roe_n"] = 1
        d["roe_years"] = 1
        d["roe_recent"] = float(f["roe"]) if f.get("roe") is not None else np.nan
        d["gross_margin"] = float(f["gross_margin"]) if f.get("gross_margin") is not None else np.nan
        d["np_yoy"] = float(f["np_yoy"]) if f.get("np_yoy") is not None else np.nan
    d["cash_ratio"] = None           # baostock 无经营现金/净利 → quality_ok False
    d["pledge"] = None
    d["fund_bonus"] = 0.0
    d["fund_state"] = "无"
    d["buy"], d["stop"] = bz.get("buy"), bz.get("stop")
    d["ma20"] = bz.get("ma20")
    d["cost_line"] = bz.get("cost_line")
    d["main_cost"] = bz.get("main_cost")
    d["code"], d["name"] = code, name
    d["pattern_yearly"] = pat_yearly
    d["pattern_platform"] = pat_platform
    d["pattern_adj"] = float(cfg.pattern_bonus if (pat_yearly or pat_platform) else 0.0)
    d["chip_adj"] = chip_adjust(d, cfg)
    badges, rec = _badges(d, cfg)
    d["badges"] = " | ".join(badges) if badges else "数据不足"
    d["rec"] = rec
    return d


def deep_check_from_baostock(candidates: pd.DataFrame, cfg: Config,
                             deadline: float | None = 300.0) -> pd.DataFrame:
    """对候选用 baostock 做简化深检。串行(baostock socket 非线程安全)，整体熔断。"""
    t0 = time.time()
    rows = []
    codes = [str(c).zfill(6) for c in candidates["code"].tolist()]
    names = {str(s["code"]).zfill(6): s.get("name") for _, s in candidates.iterrows()}
    n = len(codes)
    for i, code in enumerate(codes):
        if deadline is not None and (time.time() - t0) > deadline:
            print(f"      [baostock深检] 达 {deadline:.0f}s 熔断，完成 {len(rows)}/{n} 只…",
                  file=__import__("sys").stderr)
            break
        try:
            d = _deep_one_bs(code, names.get(code), cfg)
            if d:
                rows.append(d)
        except Exception:
            continue
        if (i + 1) % 10 == 0:
            print(f"      [baostock深检] {i+1}/{n} 只…")
    return pd.DataFrame(rows)