"""东财拉取任务：在 NAS（本地 IP，东财不封）每日 10:00 交易日运行。

把"脆弱的东财抓取 + 深检"从分析流程里剥离成独立任务：
  1. 东财快照（429/5xx 时停 5 分钟重试，最多 pull_retry_times 次）→ 清洗 → 打分
  2. 归档全市场一阶段因子表到本地 history（回测口径与积累，在 NAS 侧）
  3. 定 Top → 东财深检（K线/F10/股东）→ 精调分 score_adj 重新排名
  4. 全量落盘 data/dailydb/<date>/（快照/top/deep/meta/backtest/manifest=success）
  5. git push + 触发分析任务 dispatch（由外层 NAS cron 负责）

拉取失败(东财持续 429/502) → manifest 标 failed，分析任务自动降级新浪+baostock。
本任务绝不主动回退新浪——降级是分析任务的职责，保证职责单一、路径可预测。
"""

from __future__ import annotations

import datetime as dt
import sys

import pandas as pd

from . import dailydb
from .backtest import run_backtest
from .config import Config
from .factors import annualized_vol, compute_factors
from .fetch import fetch_pool_closes, fetch_snapshot
from .history import save_snapshot
from .staticdb import save_static
from .universe import clean_universe, industry_stats
from . import core


def _retryable(exc: BaseException) -> bool:
    """东财拉取是否可重试：429/5xx 或网络抖动都算（都是瞬时故障）。"""
    resp = getattr(exc, "response", None)
    if resp is not None and getattr(resp, "status_code", None):
        code = resp.status_code
        if code == 429 or 500 <= code <= 599:
            return True
        return False          # 其它 4xx（如字段错/参数错）是永久错误，不重试
    # 无 HTTP 响应（连接中断/超时/JSON 解析）→ 网络瞬时故障，可重试
    return True


def _eastmoney_snapshot(cfg: Config) -> pd.DataFrame:
    """东财全市场快照，带 429/5xx 重试（interval×times 兜底）。失败抛最后异常。"""
    import time
    for attempt in range(cfg.pull_retry_times + 1):
        try:
            snap = fetch_snapshot(cfg.market_fs(), cfg.snapshot_fields())
            if snap.empty:
                raise RuntimeError("东财快照为空（可能非交易日或接口异常）")
            return snap
        except Exception as e:
            if not _retryable(e):
                raise                     # 永久错误（参数/字段错等）：立即失败，不重试
            if attempt >= cfg.pull_retry_times:
                raise
            # 429/5xx 处理：停止，等 interval 秒重试（不要连续打东财）
            print(f"      !! 东财快照第 {attempt+1} 次失败（{type(e).__name__}: {e}），"
                  f"等 {cfg.pull_retry_interval}s 重试…", file=sys.stderr)
            time.sleep(cfg.pull_retry_interval)
    raise RuntimeError("unreachable")


def pull(cfg: Config | None = None) -> int:
    """东财拉取任务主入口。返回退出码：0=成功落库；2=拉取失败(分析侧将降级)。"""
    import time
    cfg = cfg or Config()
    day = dt.date.today().strftime("%Y%m%d")
    t0 = time.time()
    print(f"[pull] 东财拉取任务开始（{day}）…")

    # 1) 东财快照（带 429/5xx 重试）
    try:
        snap = _eastmoney_snapshot(cfg)
    except Exception as e:
        print(f"[pull] !! 东财快照 {cfg.pull_retry_times} 次重试后仍失败"
              f"（{type(e).__name__}: {e}）。今日数据仓库标记 failed，"
              f"分析任务将降级新浪+baostock。", file=sys.stderr)
        dailydb.mark_status(cfg, day, "failed", "eastmoney", {}, "")
        return 2

    # 2) 静态基本面库：东财通时全市场落盘慢变列（行业/ROE/毛利/净利/上市日）
    try:
        if save_static(snap, cfg):
            print(f"      已更新静态基本面库（{dt.date.today().isoformat()}）", file=sys.stderr)
    except Exception as e:
        print(f"      !! 静态库写入失败（{type(e).__name__}: {e}），忽略", file=sys.stderr)

    # 3) 盘前/休市保护
    prices = pd.to_numeric(snap["price"], errors="coerce")
    if prices.notna().sum() == 0:
        print(f"[pull] !! 快照 {len(snap)} 只实时价全空（现在可能在北京时间 10:00 开盘前或已休市）。"
              f"标记 failed，分析侧降级。", file=sys.stderr)
        dailydb.mark_status(cfg, day, "failed", "eastmoney", {}, "")
        return 2

    # 4) 清洗 + 波动率
    uni = clean_universe(snap, cfg)
    print(f"[pull] 清洗：{industry_stats(uni)}")
    if uni.empty:
        print("[pull] !! 清洗后 0 只，标记 failed（分析侧降级）。", file=sys.stderr)
        dailydb.mark_status(cfg, day, "failed", "eastmoney", {}, "")
        return 2
    beg = (dt.date.today() - dt.timedelta(days=200)).strftime("%Y%m%d")
    end = dt.date.today().strftime("%Y%m%d")
    closes = fetch_pool_closes(uni["code"].astype(str).tolist(), beg, end,
                               cfg.vol_concurrency, cfg.vol_kline_deadline)
    vol_map = {c: annualized_vol(s, cfg.vol_days, cfg.vol_annualize) for c, s in closes.items()}
    print(f"      波动率覆盖 {len(vol_map)}/{len(uni)} 只")

    # 5) 打分（全市场一阶段因子表，归档回测口径）
    first_pass = compute_factors(uni, cfg, vol_map)
    cand = first_pass.sort_values("rank").head(max(cfg.top_n, cfg.deep_top))

    # 6) 东财深检 Top（本地 IP，可连东财；失败自动重试补深检）
    print(f"[pull] 深检 Top {cfg.deep_top}…")
    deep = core.deep_with_retry(cand.head(cfg.deep_top), cfg)
    print(f"      深检完成（{len(deep)} 只）")
    top, deep_disp = core.apply_deep_adj(cand, deep, cfg)

    # 7) 归档当日全市场一阶段因子表（NAS 本地 history 积累，供回测）
    save_snapshot(first_pass, cfg, day)

    # 8) 回测（在 NAS 侧用本地积累的 history 计算）
    bt = run_backtest(cfg)

    # 9) 元信息
    meta = {
        "source": "eastmoney",
        "universe": industry_stats(uni),
        "w_value": cfg.w_value, "w_quality": cfg.w_quality,
        "w_momentum": cfg.w_momentum, "w_lowvol": cfg.w_lowvol,
        "size_neutral": cfg.size_neutral, "topn": cfg.top_n,
        "vol_days": cfg.vol_days, "fund_bonus_new": cfg.fund_bonus_new,
        "exclude_industry": ",".join(cfg.exclude_industry_kw),
        "degraded": False,
    }

    # 10) 全部落盘 + manifest=success
    dailydb.save_day(cfg, day, snapshot=first_pass, top=top,
                     deep=deep_disp, meta=meta, backtest=bt)
    print(f"[pull] ✅ 数据仓库已更新：data/dailydb/{day}/ "
          f"(top {len(top)} / deep {len(deep_disp)} / 用时 {time.time()-t0:.0f}s)")
    return 0


def run_pull_cli() -> None:
    sys.exit(pull())