"""选股核心编排：深检重试 + 精调分合并。被 cli(分析任务) 和 pull(拉取任务) 共用。"""

from __future__ import annotations

import datetime as dt
import sys

import pandas as pd

from .config import Config
from .deep import deep_check
from .fetch import fetch_kline


def eastmoney_alive(cfg: Config) -> bool:
    """探活东财：对一只股票拉 K 线，通即为恢复（成功秒级，失败 20s 超时）。"""
    import time
    try:
        end = dt.date.today()
        beg = end - dt.timedelta(days=40)
        k = fetch_kline("600519", beg.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
        return not k.empty
    except Exception:
        return False


def deep_with_retry(candidates: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """深度校验，失败/为空时自动重试补深检。

    东财 K线/F10 故障时 deep_check 会熔断返回空表——但那是单次调度，东财几分钟后恢复
    也不会回头补，导致当日深检永久缺失。这里在深检为空时按 interval×times 等待重试：
    每次先探活东财，恢复才重跑 deep_check（避免东财持续挂时整轮 600s 白跑）。
    """
    import time

    def _run_once() -> pd.DataFrame:
        try:
            return deep_check(candidates, cfg, cfg.deep_deadline)
        except Exception as e:
            print(f"      !! 深度校验失败（{type(e).__name__}: {e}），本日仅输出 Top 榜单"
                  f"（无深度信号）。", file=sys.stderr)
            return pd.DataFrame()

    deep = _run_once()
    if not deep.empty or cfg.deep_retry_times <= 0:
        return deep

    print(f"      深检为空（东财接口故障），每 {cfg.deep_retry_interval}s 重试 "
          f"{cfg.deep_retry_times} 次探活补深检…", file=sys.stderr)
    for i in range(cfg.deep_retry_times):
        time.sleep(cfg.deep_retry_interval)
        print(f"      → 重试 {i+1}/{cfg.deep_retry_times}：探活东财…")
        if not eastmoney_alive(cfg):
            print(f"         东财仍不可用，继续等待。")
            continue
        print(f"         东财已恢复，重跑深检…")
        deep = _run_once()
        if not deep.empty:
            print(f"         深检补成功（{len(deep)} 只）")
            break
    if deep.empty:
        print(f"      深检重试 {cfg.deep_retry_times} 次后仍失败，本日仅输出 Top 榜单"
              f"（无深度信号），东财恢复后次日自动正常。", file=sys.stderr)
    return deep


def apply_deep_adj(cand: pd.DataFrame, deep: pd.DataFrame, cfg: Config):
    """把深检加成(基金/筹码/形态) + 行业加成合入精调分 score_adj，重新排名出 Top。

    score 保留为纯因子分（价值/质量/动量/低波动），归档回测口径一致、可归因；
    score_adj = 纯因子分 + 行业政策加成(f_industry) + 基金/筹码/形态深检加成。
    返回 (top, deep_disp)。深检部分失败/熔断时 deep 缺行，缺失 code 加成归 0。
    """
    cand = cand.copy()
    if not deep.empty:
        deep = deep.set_index("code")
        cand["fund_bonus"] = cand["code"].astype(str).map(
            lambda c: deep.loc[c, "fund_bonus"] if c in deep.index else 0.0)
        cand["chip_adj"] = cand["code"].astype(str).map(
            lambda c: deep.loc[c, "chip_adj"] if c in deep.index else 0.0)
        cand["pattern_adj"] = cand["code"].astype(str).map(
            lambda c: deep.loc[c, "pattern_adj"] if c in deep.index else 0.0)
        cand["industry_adj"] = cand["f_industry"] if "f_industry" in cand.columns else 0.0
        cand["score_adj"] = cand["score"] + cand["industry_adj"] + cand["fund_bonus"] \
            + cand["chip_adj"] + cand["pattern_adj"]
    else:
        cand["fund_bonus"] = 0.0
        cand["score_adj"] = cand["score"] + (cand["f_industry"] if "f_industry" in cand.columns else 0.0)
    cand["rank"] = cand["score_adj"].rank(ascending=False, method="first")
    cand = cand.sort_values("rank")
    top = cand.head(cfg.top_n)
    if not deep.empty:
        # 只取 deep 里真实存在的 code：深检部分失败/熔断时 deep 缺行，直接 .loc 会 KeyError
        deep_codes = [c for c in top["code"].astype(str) if c in deep.index]
        deep_disp = deep.loc[deep_codes].reset_index() if deep_codes else deep
    else:
        deep_disp = deep
    return top, deep_disp