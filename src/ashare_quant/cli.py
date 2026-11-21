"""命令行入口。

用法：
  python -m ashare_quant run            # 抓数据→清洗→打分→深度校验→报告
  python -m ashare_quant top            # 只打印今日 Top 表格
  python -m ashare_quant history        # 列出已积累的历史天数
  python -m ashare_quant backtest       # 只跑自我检验回测
  python -m ashare_quant ai             # 对已有 latest.json 重新生成 AI 解读
  python -m ashare_quant nav --code 600519   # 单只股票近一年净值（参考）
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys

import numpy as np
import pandas as pd

from .ai import ai_interpret, save_json_field, write_ai_report
from .backtest import run_backtest
from .config import Config
from .core import apply_deep_adj, deep_with_retry
from . import dailydb
from . import pull
from .deep import deep_check
from .deep_bs import deep_check_from_baostock
from .factors import annualized_vol, compute_factors
from .feishu import push_latest
from .fetch import fetch_kline, fetch_kline_close, fetch_market_snapshot, fetch_pool_closes
from .history import list_available_days, load_deep_history, load_history, save_deep_snapshot, save_snapshot
from .report import write_reports
from .staticdb import load_static, merge_static, save_static
from .universe import clean_universe, industry_stats


def _run(print_top_only: bool = False) -> pd.DataFrame:
    cfg = Config()
    day = dt.date.today().strftime("%Y%m%d")

    # ===== 当日数据仓库优先 =====
    # 东财拉取任务(NAS)已在 10:00 落库当日东财数据 → 100% 从库拉取，纯本地、零网络、秒级。
    # 库不新鲜(拉取失败/非交易日) → 走下方现有降级路径(新浪+baostock)。
    if dailydb.is_fresh(cfg, day):
        print(f"[run] 当日数据仓库已更新（data/dailydb/{day}/），100% 从静态库拉取…")
        return _run_from_db(day, cfg, print_top_only)

    print(f"[1/6] 抓取全市场快照（{dt.datetime.now().strftime('%H:%M:%S')}）...")
    snap, source = fetch_market_snapshot(cfg.market_fs(), cfg.snapshot_fields())
    if snap.empty:
        print("!! 未取到数据，可能非交易日或接口异常。", file=sys.stderr)
        sys.exit(2)

    # ---- 静态基本面库：东财通时全市场落盘；东财挂(新浪源)时回填慢变因子 ----
    # 行业/ROE/毛利率/净利/上市日 是季度级慢变数据。东财通时把全市场这些列存盘，
    # 东财故障日(新浪源)读回补全，让 quality 因子与 industry_bonus 在降级路径照样
    # 生效，避免退化成"低 PB 银行/高速刷屏"。static 为空=东财不通且静态库也冷启动。
    static: dict = {}
    if source == "eastmoney":
        try:
            if save_static(snap, cfg):
                print(f"      已更新静态基本面库（{dt.date.today().isoformat()}）",
                      file=sys.stderr)
        except Exception as e:
            print(f"      !! 静态库写入失败（{type(e).__name__}: {e}），忽略",
                  file=sys.stderr)
    else:
        static = load_static(cfg)
        if static:
            snap = merge_static(snap, static)
            print(f"      已从静态库回填慢变因子（{len(static)} 只：行业/ROE/毛利/净利/上市日）",
                  file=sys.stderr)
        else:
            print("      新浪源且静态库为空（首次冷启动？），走两段式 baostock 补齐降级路径",
                  file=sys.stderr)

    # 盘前/休市保护：A股 09:30 前快照的“最新价 f2”字段多为空("-")，会导致清洗后 0 只，
    # 误以为是选股失败。此时明确提示等待开盘，而不是静默产出空结果。
    prices = pd.to_numeric(snap["price"], errors="coerce")
    if prices.notna().sum() == 0:
        print(f"!! 快照 {len(snap)} 只的实时价全为空（现在可能在北京时间 09:30 开盘前，"
              f"或已休市）。请等到开盘后（约 09:35 起）再运行。", file=sys.stderr)
        sys.exit(2)

    print(f"[2/6] 清洗股票池（原始 {len(snap)} 只）...")
    uni = clean_universe(snap, cfg)
    print(f"      {industry_stats(uni)}")
    if uni.empty:
        # 盘前/休市/接口异常都会导致清洗后 0 只：若静默继续，会把空结果提交成报告，
        # 覆盖昨天的好数据。直接报错退出，让 CI 失败，避免污染历史。
        print("!! 清洗后股票池为 0 只。可能原因：当前在北京时间 09:30 开盘前/已休市 "
              "(实时价或成交额字段为空)，或数据接口异常。请开盘后再跑。", file=sys.stderr)
        sys.exit(2)

    print("[2.5/6] 抓历史收盘价算 60 日真实波动率（低波动因子，可熔断）...")
    beg = (dt.date.today() - dt.timedelta(days=200)).strftime("%Y%m%d")
    end = dt.date.today().strftime("%Y%m%d")
    closes = fetch_pool_closes(uni["code"].astype(str).tolist(), beg, end,
                               cfg.vol_concurrency, cfg.vol_kline_deadline)
    vol_map = {c: annualized_vol(s, cfg.vol_days, cfg.vol_annualize) for c, s in closes.items()}
    print(f"      波动率覆盖 {len(vol_map)}/{len(uni)} 只")

    # 新浪源若已回填静态库(行业/ROE/毛利/净利/上市日)，quality 与 industry_bonus 已生效，
    # 走和东财一样的单遍打分，不再两段式(那段只为补缺失基本面)。仅当新浪源且静态库也为空
    # (首次冷启动，只能靠 baostock 补)才走两段式，并加绝对质量门槛防低价垃圾股刷屏。
    use_two_stage = (source == "sina" and not static)
    if use_two_stage:
        # ===== 新浪快照降级 + 静态库冷启动：两段式筛选 =====
        # 第一段：新浪只有估值/流动性字段（质量/成长/行业缺列自动归 0）→ 粗筛 top coarse_top
        print(f"[3a/6] 粗筛（仅估值/流动性因子）→ top {cfg.coarse_top} ...")
        first_pass = compute_factors(uni, cfg, vol_map)          # 全市场因子表（回测归档用）
        coarse = first_pass.nlargest(cfg.coarse_top, "score").copy()

        # 第二段：baostock 补齐粗筛池的质量/成长，K线补 60 日动量，再精筛出 top_n
        print(f"[3b/6] baostock 补齐 {len(coarse)} 只基本面（质量/成长/动量）...")
        fund = None
        try:
            from .baostock_src import enrich as bs_enrich
            fund = bs_enrich([str(c).zfill(6) for c in coarse["code"]], fill_ret60=False)
            fund.index = fund.index.astype(str)
            for col in ("roe", "gross_margin", "np_yoy", "net_profit"):
                if col in fund.columns:
                    coarse[col] = coarse["code"].astype(str).map(fund[col])
            # 动量：优先用已抓的 K 线收盘算 60 日涨幅（免额外请求）
            def _ret60(c: str) -> float:
                s = closes.get(str(c).zfill(6))
                if s is None or len(s) < 2:
                    return float("nan")
                base = s.iloc[-61] if len(s) >= 61 else s.iloc[0]
                if not base or base != base or base <= 0:   # 防 0/NaN 收盘 → 除零崩整段补齐
                    return float("nan")
                return float((s.iloc[-1] / base - 1) * 100)
            coarse["ret_60"] = coarse["code"].astype(str).map(_ret60)
            # 动量兜底：东财 K线覆盖不足时（closes 缺失）用 baostock 补 60 日涨幅，
            # 避免降级路径动量因子整体失效、分数结构残缺。单只 0.2s，仅补缺失的。
            missing = coarse["code"].astype(str)[coarse["ret_60"].isna()].tolist()
            if missing:
                # baostock socket 长连接非线程安全，必须串行（并发会乱码/卡死）。
                from .baostock_src import fetch_ret60 as bs_ret60
                import time as _t
                cov0 = coarse["ret_60"].notna().sum()
                _t0 = _t.time()
                _budget = 120.0   # 动量兜底整体熔断：baostock 抽风时不磨太久，用已补到的继续
                added = 0
                for c in missing:
                    if (_t.time() - _t0) > _budget:
                        print(f"      动量兜底达 {_budget:.0f}s 熔断，补 {added} 只后停止…",
                              file=sys.stderr)
                        break
                    v = bs_ret60(str(c).zfill(6))
                    if pd.notna(v):
                        hit = coarse.index[coarse["code"].astype(str) == c]
                        if len(hit) > 0:
                            coarse.loc[hit[0], "ret_60"] = v
                            added += 1
                print(f"      动量兜底: 东财K线覆盖 {cov0}/{len(coarse)}, "
                      f"baostock 补 {added} 只", file=sys.stderr)
            # 次新复核：新浪无上市日，用 baostock 上市日回填后排除不足 N 天的
            if "ipo_date" in fund.columns:
                coarse["ipo_date"] = coarse["code"].astype(str).map(fund["ipo_date"])
                ipo = pd.to_numeric(coarse["ipo_date"], errors="coerce")
                days = int(dt.date.today().strftime("%Y%m%d")) - ipo
                coarse = coarse[days.isna() | (days >= cfg.min_listing_days)]
        except Exception as e:
            print(f"      !! baostock 补齐失败（{type(e).__name__}: {e}），"
                  f"退化为仅估值/流动性因子", file=sys.stderr)
            fund = None
        if fund is not None:
            _cov = lambda c: f"{c} {int(fund[c].notna().sum())}/{len(fund)}" if c in fund.columns else f"{c} 缺列"
            print(f"      baostock 补齐: {_cov('roe')} | {_cov('np_yoy')} | "
                  f"ret_60 {int(coarse['ret_60'].notna().sum())}/{len(coarse)}")
        # 冷启动质量门槛：两段式只能用 baostock 补 ROE/净利，粗筛池偏小(100只)、z-score
        # 横截面窄，易被"低 PB 稳定股(银行/高速)"主导。加绝对门槛把 ROE<阈值 或 净利同比
        # <阈值 的低价垃圾股直接剔掉(缺失值不拦，避免误杀 baostock 没补到的)。
        _mask = pd.Series(True, index=coarse.index)
        if "roe" in coarse.columns:
            _roe = pd.to_numeric(coarse["roe"], errors="coerce")
            _mask &= (_roe.isna() | (_roe >= cfg.degraded_roe_min))
        if "np_yoy" in coarse.columns:
            _npy = pd.to_numeric(coarse["np_yoy"], errors="coerce")
            _mask &= (_npy.isna() | (_npy >= cfg.degraded_np_yoy_min))
        _before = len(coarse)
        coarse = coarse[_mask]
        if len(coarse) < _before:
            print(f"      降级质量门槛: {_before} → {len(coarse)} 只 "
                  f"(ROE≥{cfg.degraded_roe_min:.0f}%或缺失 & 净利同比≥{cfg.degraded_np_yoy_min:.0f}%或缺失)",
                  file=sys.stderr)
        print("[3c/6] 二次精筛（质量/成长/动量因子已生效）...")
        scored = compute_factors(coarse, cfg, vol_map)
    else:
        print("[3/6] 计算因子并打分...")
        first_pass = compute_factors(uni, cfg, vol_map)
        scored = first_pass

    cand = scored.sort_values("rank").head(max(cfg.top_n, cfg.deep_top))
    top = cand.head(cfg.top_n)

    if print_top_only:
        _print_top(top)
        return top

    print("[4/6] 归档当日快照（用于自我检验回测）...")
    # 归档"第一阶段"全市场因子表（非 baostock 二次精筛的 100 只小表），
    # 保证回测分组口径稳定：每天都是全市场同一套一阶段因子
    save_snapshot(first_pass, cfg, day)

    print(f"[4.5/6] 深度校验 Top {cfg.deep_top}（基本面/技术面/雷区/大基金）...")
    deep = deep_with_retry(cand.head(cfg.deep_top), cfg)
    if deep.empty:
        # 东财深检失败（机房 IP 屏蔽）→ 用 baostock 补简化深检（技术面/筹码/形态，基本面受限）
        print(f"      东财深检为空，改用 baostock 补深检（{cfg.deep_top} 只）…")
        deep = deep_check_from_baostock(cand.head(cfg.deep_top), cfg)
    print(f"      深度校验完成（{len(deep)} 只）")
    # 深检信号单独存档：供日后对“深检规则(筹码/形态/买点)历史命中率”回测
    if not deep.empty:
        save_deep_snapshot(deep, cfg, day)

    # 深度加成 + 行业加成：合入精调分 score_adj，重新排名后再取 Top N。
    # score 保留为纯因子分（价值/质量/动量/低波动），归档回测口径一致、可归因；
    # score_adj = 纯因子分 + 行业政策加成(f_industry) + 基金/筹码/形态深检加成。
    top, deep_disp = apply_deep_adj(cand, deep, cfg)

    print("[5/6] 回测 + 生成报告...")
    bt = run_backtest(cfg)
    meta = {
        "source": source,                 # 当日数据源：eastmoney=字段齐全 / sina=baostock 二次筛选补齐
        "universe": industry_stats(uni),
        "w_value": cfg.w_value, "w_quality": cfg.w_quality,
        "w_momentum": cfg.w_momentum, "w_lowvol": cfg.w_lowvol,
        "size_neutral": cfg.size_neutral, "topn": cfg.top_n,
        "vol_days": cfg.vol_days, "fund_bonus_new": cfg.fund_bonus_new,
        "exclude_industry": ",".join(cfg.exclude_industry_kw),
        # 数据降级提示：东财接口故障时 source=sina 且关键字段缺失，分数不具可比性
        "degraded": source != "eastmoney",
    }
    ai_text = ""
    if cfg.ai_api_key:
        print("[6/6] AI 解读...")
        ai_text = ai_interpret(cfg, top.to_dict(orient="records"),
                               deep_disp.to_dict(orient="records") if not deep_disp.empty else [], meta)
        if ai_text:
            write_ai_report(cfg, ai_text)
    paths = write_reports(top, meta, bt, cfg, deep_disp, ai_text)
    print("      报告已写入：")
    for k, p in paths.items():
        print(f"        {k:5s}  {p}")
    if bt.get("ok"):
        hz = bt.get("horizons", {})
        print(f"自我检验（{bt['days']} 交易日 · 主周期 {bt['main_horizon']}日）：")
        for n in sorted(hz, key=int):
            p = hz[n]
            print(f"  {n:>2}日 分组差 {p['spread_mean']:+.2%} | RankIC {p['ic_mean']:+.3f} "
                  f"| ICIR {p['icir']:+.2f} | 多头占优 {p['hit_rate']:.0%} | 样本 {p['valid_days']}天")
    _print_top(top)
    return top


def _run_from_db(day: str, cfg: Config, print_top_only: bool) -> pd.DataFrame:
    """当日数据仓库已更新 → 100% 从库拉取，纯本地渲染（零网络、秒级）。"""
    data = dailydb.load_day(cfg, day)
    top, deep, meta, bt = data["top"], data["deep"], data["meta"], data["backtest"]
    if top.empty:
        print("!! 数据仓库 manifest=success 但 top 为空（落盘不完整），无法出报告。",
              file=sys.stderr)
        return pd.DataFrame()
    if print_top_only:
        _print_top(top)
        return top
    # 深检兜底：库快照新鲜但深检为空（拉取时东财+baostock 深检都失败）→ 按规格补深检。
    # Actions 东财被屏蔽，直接 baostock（快）；补到则用库 snapshot 重建 cand 重排。
    if deep.empty:
        print("[run] 库深检为空（拉取时失败），用 baostock 补深检…")
        snap = dailydb.load_snapshot(cfg, day)
        if not snap.empty:
            cand = snap.sort_values("rank").head(max(cfg.top_n, cfg.deep_top))
            deep = deep_check_from_baostock(cand.head(cfg.deep_top), cfg)
            if not deep.empty:
                top, deep = apply_deep_adj(cand, deep, cfg)
    # AI 解读（可选）：从库里的 top/deep 重新生成，不重跑抓数
    ai_text = ""
    if cfg.ai_api_key:
        print("[run] AI 解读（读库数据）...")
        ai_text = ai_interpret(cfg, top.to_dict(orient="records"),
                               deep.to_dict(orient="records") if not deep.empty else [], meta)
        if ai_text:
            write_ai_report(cfg, ai_text)
    paths = write_reports(top, meta, bt, cfg, deep, ai_text)
    print("      报告已写入：")
    for k, p in paths.items():
        print(f"        {k:5s}  {p}")
    if bt.get("ok"):
        hz = bt.get("horizons", {})
        print(f"自我检验（{bt['days']} 交易日 · 主周期 {bt['main_horizon']}日）：")
        for n in sorted(hz, key=int):
            p = hz[n]
            print(f"  {n:>2}日 分组差 {p['spread_mean']:+.2%} | RankIC {p['ic_mean']:+.3f} "
                  f"| ICIR {p['icir']:+.2f} | 多头占优 {p['hit_rate']:.0%} | 样本 {p['valid_days']}天")
    _print_top(top)
    return top


def _deep_backtest(cfg: Config) -> None:
    """对已积累的深检信号(deep_*.csv)回测：验证"结构形态/筹码集中/追高/形态买点"的历史命中率。

    用历史快照(含price)给每个深检信号算后 20/60 日的区间收益，分组看：
      形态命中(yearly/platform) vs 未命中；
      筹码高度集中(conc90<阈值) vs 未集中；
      追高风险(chip_adj<=-0.4) vs 贴成本(chip_adj>=0.4)。
    这是把"深检规则"本身变成可回测的信号，而非只信规则的主观直觉。
    """
    deep = load_deep_history(cfg)
    if deep.empty:
        print("尚未积累任何深检信号（deep_*.csv），每日 run 会自动存档。", file=sys.stderr)
        return
    snap = load_history(cfg)
    if snap.empty:
        print("无历史快照，无法计算后续收益。", file=sys.stderr)
        return
    snap = snap[["date", "code", "price"]].copy()
    # 用"该 code 的完整快照序列"对齐后 N 个交易日收益，而不是稀疏的深检记录序列。
    # 深检只覆盖 Top50，同一 code 在 deep 序列里隔几天才出现一次，shift(-N) 会跨更多自然日；
    # 改按全市场快照逐 code 排日序，取第 N 个交易日后的收盘，口径与主回测一致。
    snap = snap.sort_values(["code", "date"])
    snap["fwd20_price"] = snap.groupby("code")["price"].shift(-20)
    snap["fwd60_price"] = snap.groupby("code")["price"].shift(-60)
    deep = deep.merge(snap[["date", "code", "price", "fwd20_price", "fwd60_price"]],
                      on=["date", "code"], how="left")
    deep["fwd_20"] = deep["fwd20_price"] / deep["price"] - 1.0
    deep["fwd_60"] = deep["fwd60_price"] / deep["price"] - 1.0
    fwd = "fwd_20"

    def _stat(cond):
        s = deep.loc[cond, fwd]
        return (len(s), s.mean(), (s > 0).mean()) if len(s) else (0, np.nan, np.nan)

    print(f"深检规则回测（{fwd}日区间，样本=信号命中组）：")
    print(f"  累计 {deep['date'].nunique()} 个交易日，{len(deep)} 条深检记录\n")

    def _line(name, cond):
        n, mean, hit = _stat(cond)
        if n:
            print(f"  {name:<22s}  n={n:3d}  平均{fwd}日 {mean:+.1%}  胜率 {hit:.0%}")

    _line("全量深检基线", deep[fwd].notna())
    _line("  形态命中(回踩/平台)", deep.get("pattern_yearly", False).fillna(False) | deep.get("pattern_platform", False).fillna(False))
    _line("  回踩年线", deep.get("pattern_yearly", False).fillna(False))
    _line("  平台突破", deep.get("pattern_platform", False).fillna(False))
    _line("  筹码集中(conc90<0.15)", (deep.get("conc90") < 0.15).fillna(False))
    _line("  贴成本(主力浮盈小)", (deep.get("chip_adj", 0) >= 0.4))
    _line("  追高风险(chip_adj<=-0.4)", (deep.get("chip_adj", 0) <= -0.4))
    print("\n提示：样本量小(n<20)时胜率不稳定；持续 run 会逐步积累有效统计。")


def _print_top(top: pd.DataFrame) -> None:
    print("\n=== Top 组合（精调分 score_adj = 纯因子分 + 行业/基金/筹码/形态加成） ===")
    cols = [c for c in ["rank", "code", "name", "industry", "price", "pct",
                        "roe", "np_yoy", "pe", "pb", "floatcap", "score_adj", "score"] if c in top.columns]
    disp = top[cols].copy()
    disp["floatcap"] = pd.to_numeric(disp["floatcap"], errors="coerce") / 1e8
    disp = disp.round({"floatcap": 0, "roe": 1, "pe": 1, "pb": 2, "score_adj": 2, "score": 2, "pct": 2})
    print(disp.to_string(index=False))


def _nav(code: str) -> None:
    end = dt.date.today()
    beg = end - dt.timedelta(days=365)
    k = fetch_kline(code, beg.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
    if k.empty:
        print(f"未取到 {code} 的 K 线。", file=sys.stderr)
        sys.exit(2)
    nav = k[["date", "close"]].copy()
    nav["close"] = nav["close"] / nav["close"].iloc[0]
    print(nav.tail(260).to_string(index=False))


def _ai_rerun(cfg: Config) -> None:
    """用已有的 latest.json（top + deep）重新生成 AI 解读，不重跑抓数。"""
    if not cfg.ai_api_key:
        print("未配置 AI_API_KEY。设置环境变量 AI_API_KEY（可选 AI_BASE_URL/AI_MODEL）后重试。",
              file=sys.stderr)
        sys.exit(3)
    path = "reports/latest.json"
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        print("未找到 reports/latest.json，请先运行 python -m ashare_quant run。", file=sys.stderr)
        sys.exit(3)
    top = data.get("top", [])
    deep = data.get("deep", [])
    meta = data.get("meta", {})
    if not top:
        print("latest.json 里没有 top 数据。", file=sys.stderr)
        sys.exit(3)
    print(f"正在生成 AI 解读（{len(top)} 只）...")
    text = ai_interpret(cfg, top, deep, meta)
    print("AI 解读完成，写入 reports/ai.md 并更新 latest.json")
    write_ai_report(cfg, text)
    save_json_field(cfg, text)


def main() -> None:
    p = argparse.ArgumentParser(prog="ashare-quant", description="A股多因子选股")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="完整运行")
    sub.add_parser("pull", help="东财拉取任务(NAS 10:00)：抓东财落库 data/dailydb")
    sub.add_parser("top", help="只打印今日Top")
    sub.add_parser("history", help="列出历史天数")
    sub.add_parser("backtest", help="只跑自我检验")
    sub.add_parser("deepbt", help="回测深检规则(筹码/形态/买点)历史命中率")
    sub.add_parser("ai", help="对已有 latest.json 重新生成 AI 解读")
    sub.add_parser("feishu", help="把已有 latest.json 结果推送到飞书机器人")
    sp = sub.add_parser("nav", help="单股近一年净值")
    sp.add_argument("code")
    args = p.parse_args()

    cfg = Config()
    if args.cmd == "run":
        _run(print_top_only=False)
    elif args.cmd == "pull":
        sys.exit(pull.pull(cfg))
    elif args.cmd == "top":
        _run(print_top_only=True)
    elif args.cmd == "ai":
        _ai_rerun(cfg)
    elif args.cmd == "feishu":
        sys.exit(push_latest(cfg))
    elif args.cmd == "history":
        days = list_available_days(cfg)
        print(f"已积累 {len(days)} 个交易日的快照：{', '.join(days[-10:]) if days else '无'}")
    elif args.cmd == "backtest":
        bt = run_backtest(cfg)
        if bt.get("ok"):
            hz = bt.get("horizons", {})
            print(f"自我检验（{bt['days']} 交易日 · 主周期 {bt['main_horizon']}日）：")
            for n in sorted(hz, key=int):
                p = hz[n]
                print(f"  {n:>2}日 分组差 {p['spread_mean']:+.2%} | RankIC {p['ic_mean']:+.3f} "
                      f"| ICIR {p['icir']:+.2f} | 多头占优 {p['hit_rate']:.0%} | 样本 {p['valid_days']}天")
        else:
            print(f"历史不足（{bt.get('days',0)} 天），继续每日运行即可积累。")
    elif args.cmd == "deepbt":
        _deep_backtest(cfg)
    elif args.cmd == "nav":
        _nav(args.code)


if __name__ == "__main__":
    main()