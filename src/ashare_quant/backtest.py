"""滚动回测 / 因子有效性检验。

基于每天存下的历史快照，用“后一天收盘/当天收盘-1”近似次日收益，
对综合分做分位组合 + RankIC 检验。这是工具自我验证的核心：
分数越高，次日/后续收益是否真越高，数据会自己说话。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .config import Config
from .history import load_history


def _forward_returns(h: pd.DataFrame) -> pd.DataFrame:
    """为每个 (date, code) 计算到下一个快照日的区间收益。"""
    h = h.sort_values(["code", "date"]).copy()
    h["next_close"] = h.groupby("code")["price"].shift(-1)
    h["next_date"] = h.groupby("code")["date"].shift(-1)
    # 跨多日时折算为“每交易日”近似（用天数校准）
    h["gap_days"] = (h["next_date"] - h["date"]).dt.days
    h["fwd_ret"] = h["next_close"] / h["price"] - 1.0
    h.loc[h["gap_days"] > 5, "fwd_ret"] = np.nan  # 间隔过长（如长假+休市）剔除
    return h.dropna(subset=["fwd_ret"])


def run_backtest(cfg: Config) -> dict:
    """对已积累的历史快照做分位 + IC 检验。历史不足时返回空报告。"""
    h = load_history(cfg)
    if h.empty or h["date"].nunique() < 3:
        return {"ok": False, "days": 0}

    h = _forward_returns(h)
    # 每个截面日都做分位
    quant_col = "q5"
    out_rows = []
    ic_rows = []
    for day, grp in h.groupby("date"):
        grp = grp.dropna(subset=["score"])
        if len(grp) < 50:
            continue
        grp = grp.copy()
        grp[quant_col] = pd.qcut(grp["score"].rank(method="first"), 5, labels=[1, 2, 3, 4, 5])
        q = grp.groupby(quant_col, observed=True)["fwd_ret"].mean()
        top = q.get(5, np.nan)
        bot = q.get(1, np.nan)
        out_rows.append({
            "date": day, "top": top, "bottom": bot,
            "spread": (top - bot) if (pd.notna(top) and pd.notna(bot)) else np.nan,
        })
        # RankIC：综合分与次日收益的秩相关
        if grp["score"].nunique() > 3:
            ic = grp["score"].corr(grp["fwd_ret"], method="spearman")
            ic_rows.append({"date": day, "ic": ic})

    res = pd.DataFrame(out_rows)
    icf = pd.DataFrame(ic_rows)
    if res.empty:
        return {"ok": False, "days": int(h["date"].nunique())}

    def _stat(s):
        s = pd.to_numeric(s, errors="coerce").dropna()
        return float(s.mean()) if len(s) else float("nan")

    return {
        "ok": True,
        "days": int(h["date"].nunique()),
        "valid_days": int(len(res)),
        "n_cross": int(len(h) / h["date"].nunique()),
        "top_mean": _stat(res["top"]),
        "bottom_mean": _stat(res["bottom"]),
        "spread_mean": _stat(res["spread"]),
        "ic_mean": _stat(icf["ic"]),
        "icir": (float(icf["ic"].mean() / icf["ic"].std(ddof=0))
                 if len(icf) > 2 and icf["ic"].std(ddof=0) > 0 else float("nan")),
        "hit_rate": (float((res["spread"] > 0).mean()) if len(res) else float("nan")),
    }