"""滚动回测 / 因子有效性检验。

基于每天存下的历史快照，用“后 N 个快照日收盘/当日收盘-1”近似 N 周期收益，
对综合分做分位组合 + RankIC 检验。这是工具自我验证的核心：
分数越高，后续收益是否真越高，数据会自己说话。

升级（多周期回看）：不再只看次日，而是同时输出
1/5/20/60 个快照日的分位差、RankIC、ICIR、多头占优占比，帮助判断：
  短期(1日)：验证是否只是隔夜反转；
  中期(20日)：选股工具真正关心的持仓周期；
并保留旧字段 (spread_mean/ic_mean/icir/hit_rate) 给报告/飞书用，主周期取 20 日。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .config import Config
from .history import load_history

# 回测周期（单位：快照日，即历史快照序列上的位置差）
HORIZONS = (1, 5, 20, 60)


def _forward_returns(h: pd.DataFrame) -> pd.DataFrame:
    """为每个 (date, code) 计算到未来第 N 个快照日的区间收益。

    对同一 code 用快照序列 shift(-N) 取后 N 个快照日的收盘，做“后 N 日/当日-1”；
    跨多日折算以快照数计，不依赖自然日（长假/停牌会自动顺延）。
    """
    h = h.sort_values(["code", "date"]).copy()
    g = h.groupby("code", group_keys=False)
    for n in HORIZONS:
        h[f"fwd_{n}"] = g["price"].shift(-n) / h["price"] - 1.0
    return h


def run_backtest(cfg: Config) -> dict:
    """对已积累的历史快照做多周期分位 + IC 检验。历史不足时返回空报告。"""
    h = load_history(cfg)
    if h.empty or h["date"].nunique() < 3:
        return {"ok": False, "days": 0}

    h = _forward_returns(h)
    per = {}
    for n in HORIZONS:
        fwd = f"fwd_{n}"
        out_rows, ic_rows = [], []
        for day, grp in h.groupby("date"):
            g = grp.dropna(subset=["score", fwd])
            if len(g) < 50:
                continue
            g = g.copy()
            g["q5"] = pd.qcut(g["score"].rank(method="first"), 5, labels=[1, 2, 3, 4, 5])
            q = g.groupby("q5", observed=True)[fwd].mean()
            top, bot = q.get(5, np.nan), q.get(1, np.nan)
            out_rows.append({"top": top, "bottom": bot,
                             "spread": (top - bot) if (pd.notna(top) and pd.notna(bot)) else np.nan})
            if g["score"].nunique() > 3:
                ic_rows.append(g["score"].corr(g[fwd], method="spearman"))
        res = pd.DataFrame(out_rows)
        if not res.empty:
            ic = pd.Series(ic_rows, dtype=float)
            per[n] = {
                "valid_days": int(len(res)),
                "top_mean": res["top"].mean(),
                "bottom_mean": res["bottom"].mean(),
                "spread_mean": res["spread"].mean(),
                "ic_mean": ic.mean(),
                "icir": (ic.mean() / ic.std(ddof=0)) if len(ic) > 2 and ic.std(ddof=0) > 0 else float("nan"),
                "hit_rate": (res["spread"] > 0).mean(),
            }

    if not per:
        return {"ok": False, "days": int(h["date"].nunique())}

    # 主指标：优先 20 日周期（持仓周期），次选 5 日、60 日、1 日
    main_n = next((n for n in (20, 5, 60, 1) if n in per), 20)
    main = per[main_n]
    return {
        "ok": True,
        "days": int(h["date"].nunique()),
        "horizons": {str(k): v for k, v in per.items()},
        "main_horizon": main_n,
        # 兼容旧字段：报告/飞书读这些
        "valid_days": main["valid_days"],
        "n_cross": int(len(h) / h["date"].nunique()),
        "top_mean": main["top_mean"],
        "bottom_mean": main["bottom_mean"],
        "spread_mean": main["spread_mean"],
        "ic_mean": main["ic_mean"],
        "icir": main["icir"],
        "hit_rate": main["hit_rate"],
    }