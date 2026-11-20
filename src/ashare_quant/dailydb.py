"""当日数据仓库：东财拉取任务(NAS)每日落盘，分析任务(GitHub Actions)读它出报告。

架构：把"脆弱的东财抓取"从分析流程里彻底剥离。NAS 在 10:00 交易日跑 `pull`，
把当日全市场快照 + Top 深检信号 + meta/backtest 落盘到 data/dailydb/<date>/，
并写 manifest.json 标注"当日是否已更新"。分析任务先查 manifest：当日是东财快照
来源且 status==success → 100% 从库拉取（纯本地、秒级、零网络）；否则降级走
新浪+baostock 路径。

目录约定：
  data/dailydb/<date>/
    snapshot.csv    当日全市场一阶段因子表（与 history 归档同格式，供回测口径一致）
    top.json        Top N 最终结果（score_adj 精调分，含行业/基金/筹码/形态加成）
    deep.csv        当日 Top 深检信号
    meta.json       数据源/权重/股票池等元信息
    backtest.json   回测结果（在 NAS 侧用本地积累的 history 计算后落盘）
    manifest.json   {"date","status","source","updated_at","universe"}  新鲜度标志
"""

from __future__ import annotations

import json
import os

import pandas as pd


def day_dir(cfg, date: str) -> str:
    return os.path.join(cfg.dailydb_dir, date)


def manifest_path(cfg, date: str) -> str:
    return os.path.join(day_dir(cfg, date), "manifest.json")


def load_manifest(cfg, date: str) -> dict:
    """读当日 manifest；文件不存在/损坏返回 {}（不报错）。"""
    p = manifest_path(cfg, date)
    try:
        with open(p, encoding="utf-8") as f:
            m = json.load(f)
        return m if isinstance(m, dict) else {}
    except Exception:
        return {}


def is_fresh(cfg, date: str) -> bool:
    """当日数据仓库是否新鲜可用：东财快照来源且 status==success。"""
    m = load_manifest(cfg, date)
    return bool(m) and m.get("date") == date \
        and m.get("status") == "success" and m.get("source") == "eastmoney"


def mark_status(cfg, date: str, status: str, source: str, meta: dict, universe: str) -> None:
    """写/更新当日 manifest（pull 拉取成功/失败时调用）。"""
    os.makedirs(day_dir(cfg, date), exist_ok=True)
    m = {
        "date": date,
        "status": status,
        "source": source,
        "updated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "universe": universe,
    }
    with open(manifest_path(cfg, date), "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=2)


def save_day(cfg, date: str, *, snapshot: pd.DataFrame,
             top: pd.DataFrame, deep: pd.DataFrame | None,
             meta: dict, backtest: dict) -> None:
    """拉取任务把当日结果全部落盘（含 manifest status=success）。"""
    d = day_dir(cfg, date)
    os.makedirs(d, exist_ok=True)
    snapshot.to_csv(os.path.join(d, "snapshot.csv"), index=False, encoding="utf-8-sig")
    top.to_csv(os.path.join(d, "top.csv"), index=False, encoding="utf-8-sig")
    if deep is not None and not deep.empty:
        deep.to_csv(os.path.join(d, "deep.csv"), index=False, encoding="utf-8-sig")
    with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    with open(os.path.join(d, "backtest.json"), "w", encoding="utf-8") as f:
        json.dump(backtest, f, ensure_ascii=False, indent=2)
    mark_status(cfg, date, "success", "eastmoney", meta, meta.get("universe", ""))


def load_day(cfg, date: str) -> dict:
    """分析任务读当日仓库。缺文件返回对应空值（不报错）。"""
    d = day_dir(cfg, date)
    top = pd.DataFrame()
    deep = pd.DataFrame()
    meta, backtest = {}, {}
    p_top = os.path.join(d, "top.csv")
    p_deep = os.path.join(d, "deep.csv")
    p_meta = os.path.join(d, "meta.json")
    p_bt = os.path.join(d, "backtest.json")
    try:
        if os.path.exists(p_top):
            top = pd.read_csv(p_top, dtype={"code": str})
    except Exception:
        top = pd.DataFrame()
    try:
        if os.path.exists(p_deep):
            deep = pd.read_csv(p_deep, dtype={"code": str})
    except Exception:
        deep = pd.DataFrame()
    for p, target in ((p_meta, meta), (p_bt, backtest)):
        try:
            if os.path.exists(p):
                with open(p, encoding="utf-8") as f:
                    target.update(json.load(f))
        except Exception:
            pass
    return {"top": top, "deep": deep, "meta": meta, "backtest": backtest}