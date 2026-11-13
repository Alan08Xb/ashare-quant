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

import pandas as pd

from .ai import ai_interpret, save_json_field, write_ai_report
from .backtest import run_backtest
from .config import Config
from .deep import deep_check
from .factors import annualized_vol, compute_factors
from .feishu import push_latest
from .fetch import fetch_kline, fetch_kline_close, fetch_market_snapshot, fetch_pool_closes
from .history import list_available_days, save_snapshot
from .report import write_reports
from .universe import clean_universe, industry_stats


def _run(print_top_only: bool = False) -> pd.DataFrame:
    cfg = Config()
    day = dt.date.today().strftime("%Y%m%d")
    print(f"[1/6] 抓取全市场快照（{dt.datetime.now().strftime('%H:%M:%S')}）...")
    snap, source = fetch_market_snapshot(cfg.market_fs(), cfg.snapshot_fields())
    if snap.empty:
        print("!! 未取到数据，可能非交易日或接口异常。", file=sys.stderr)
        sys.exit(2)

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

    print("[2.5/6] 抓历史收盘价算 60 日真实波动率（低波动因子）...")
    beg = (dt.date.today() - dt.timedelta(days=200)).strftime("%Y%m%d")
    end = dt.date.today().strftime("%Y%m%d")
    closes = fetch_pool_closes(uni["code"].astype(str).tolist(), beg, end, cfg.vol_concurrency)
    vol_map = {c: annualized_vol(s, cfg.vol_days, cfg.vol_annualize) for c, s in closes.items()}
    print(f"      波动率覆盖 {len(vol_map)}/{len(uni)} 只")

    if source == "sina":
        # ===== 新浪快照降级路径：两段式筛选 =====
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
                return float((s.iloc[-1] / base - 1) * 100)
            coarse["ret_60"] = coarse["code"].astype(str).map(_ret60)
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
    try:
        deep = deep_check(cand.head(cfg.deep_top), cfg)
        print(f"      深度校验完成（{len(deep)} 只）")
    except Exception as e:
        # 东财 K线/F10 被机房 IP 限流时深度校验可能整体失败：降级为只有 Top 榜单，
        # 避免整日 run 崩掉。宁可出榜单，不强求深检。
        print(f"      !! 深度校验失败（{type(e).__name__}: {e}），本日仅输出 Top 榜单"
              f"（无深度信号）。", file=sys.stderr)
        deep = pd.DataFrame()

    # 深度加成：基金新增 + 筹码位置（追高/机会），合入综合分，重新排名后再取 Top N
    if not deep.empty:
        deep = deep.set_index("code")
        cand = cand.copy()
        cand["fund_bonus"] = cand["code"].astype(str).map(
            lambda c: deep.loc[c, "fund_bonus"] if c in deep.index else 0.0)
        cand["chip_adj"] = cand["code"].astype(str).map(
            lambda c: deep.loc[c, "chip_adj"] if c in deep.index else 0.0)
        cand["score"] = cand["score"] + cand["fund_bonus"] + cand["chip_adj"]
        cand["rank"] = cand["score"].rank(ascending=False, method="first")
        cand = cand.sort_values("rank")
        top = cand.head(cfg.top_n)
        deep_disp = deep.loc[top["code"].astype(str)].reset_index()
    else:
        cand["fund_bonus"] = 0.0
        deep_disp = deep

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
        print(f"自我检验：{bt['valid_days']} 天 | 分组差 {bt.get('spread_mean'):.2%} | "
              f"RankIC {bt.get('ic_mean'):.3f} | ICIR {bt.get('icir'):.2f} | 多头占优 {bt.get('hit_rate'):.0%}")
    _print_top(top)
    return top


def _print_top(top: pd.DataFrame) -> None:
    print("\n=== Top 组合 ===")
    cols = [c for c in ["rank", "code", "name", "industry", "price", "pct",
                        "roe", "np_yoy", "pe", "pb", "floatcap", "score"] if c in top.columns]
    disp = top[cols].copy()
    disp["floatcap"] = pd.to_numeric(disp["floatcap"], errors="coerce") / 1e8
    disp = disp.round({"floatcap": 0, "roe": 1, "pe": 1, "pb": 2, "score": 2, "pct": 2})
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
    sub.add_parser("top", help="只打印今日Top")
    sub.add_parser("history", help="列出历史天数")
    sub.add_parser("backtest", help="只跑自我检验")
    sub.add_parser("ai", help="对已有 latest.json 重新生成 AI 解读")
    sub.add_parser("feishu", help="把已有 latest.json 结果推送到飞书机器人")
    sp = sub.add_parser("nav", help="单股近一年净值")
    sp.add_argument("code")
    args = p.parse_args()

    cfg = Config()
    if args.cmd == "run":
        _run(print_top_only=False)
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
            print(bt)
        else:
            print(f"历史不足（{bt.get('days',0)} 天），继续每日运行即可积累。")
    elif args.cmd == "nav":
        _nav(args.code)


if __name__ == "__main__":
    main()