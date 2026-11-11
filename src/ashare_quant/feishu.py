"""推送每日选股结果到飞书（自定义机器人 Webhook）。

飞书卡片 markdown 元素**不支持表格语法**（`| a | b |` 会按普通文本错行）。
所以这里用飞书卡片的原生结构化组件排版：
  - `table`       渲染 Top 股票表格（列对齐、不换行、可横向滚动）
  - `column_set`  把深度校验买点排成多列网格
  - AI 解读等自由文本用 `markdown` 组件
  - 注意：标题/表格各字段内容里**不要带换行**，否则仍会错行。

用法：
  1. 飞书群 设置→群机器人→自定义机器人，复制 Webhook
  2. 设环境变量 FEISHU_WEBHOOK（GitHub Actions 里放 Secret）
  3. python -m ashare_quant feishu
"""

from __future__ import annotations

import json
import os
import sys

import requests

from .config import Config

# 表格列定义（顺序即显示顺序）；宽屏下用 wide_screen_mode 可左右滚动
_TABLE_COLUMNS = [
    {"name": "排名", "display_name": "排名", "width": "auto"},
    {"name": "代码", "display_name": "代码", "width": "auto"},
    {"name": "名称", "display_name": "名称", "width": "auto"},
    {"name": "行业", "display_name": "行业", "width": "auto"},
    {"name": "现价", "display_name": "现价", "width": "auto"},
    {"name": "今涨跌", "display_name": "今涨跌", "width": "auto"},
    {"name": "PE", "display_name": "PE", "width": "auto"},
    {"name": "PB", "display_name": "PB", "width": "auto"},
    {"name": "ROE", "display_name": "ROE", "width": "auto"},
    {"name": "综合分", "display_name": "综合分", "width": "auto"},
]

_STOCK_KEYS = ["rank", "code", "name", "industry", "price", "pct", "pe", "pb", "roe", "score"]


def _num(v, fmt="{:.1f}"):
    """把值格式化成字符串；None/NaN → '—'。"""
    if v is None:
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "—"
    if f != f:  # NaN
        return "—"
    return fmt.format(f)


def _fmt_pct(v):
    """涨幅格式：正负号 + 保留两位。"""
    if v is None:
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "—"
    if f != f:
        return "—"
    return f"{f:+.2f}%"


def _fmt_stock(t: dict, key: str) -> str:
    """按列 key 格式化单只股票字段。"""
    if key == "rank":
        return _num(t.get("rank"), "{:.0f}")
    if key == "pct":
        return _fmt_pct(t.get("pct"))
    if key == "price":
        return _num(t.get(key), "{:.2f}")
    if key == "pb":
        return _num(t.get(key), "{:.2f}")
    if key in ("roe", "pe", "score"):
        return _num(t.get(key), "{:.2f}" if key == "score" else "{:.1f}")
    return str(t.get(key, "") or "")


def _rec_style(rec: str) -> dict:
    """规则建议用 lark_md + 内联颜色标签着色（质地好=绿，雷区=红，一般=灰）。

    注意：div 的 fields 里 text 只接受 lark_md/plain_text 文本对象；
    text_tag 是独立元素，不能直接当字段值，否则飞书报 11246/11310。
    """
    if "雷区" in rec:
        color = "red"
    elif "质地好" in rec or "优选" in rec:
        color = "green"
    else:
        color = "grey"
    return {"tag": "lark_md", "content": f"<font color='{color}'>{rec}</font>"}


def _cell(text: str) -> dict:
    return {"tag": "text", "text": {"tag": "plain_text", "content": text}}


def build_card(top: list[dict], meta: dict, backtest: dict, deep: list[dict],
               ai_text: str = "", page_url: str = "", n: int = 20) -> list:
    """构造飞书卡片 elements 列表。"""
    bt = backtest or {}

    # ---- 顶部：股票池 + 自我检验摘要 ----
    lines = [f"**股票池**：{meta.get('universe', '—')}"]
    if bt.get("ok"):
        lines.append(
            f"**自我检验**：分组差 {_num(bt.get('spread_mean'), '{:.2%}')} · "
            f"RankIC {_num(bt.get('ic_mean'), '{:.3f}')} · ICIR "
            f"{_num(bt.get('icir'), '{:.2f}')} · 多头占优 {_num(bt.get('hit_rate'), '{:.0%}')}")
    elif bt.get("days"):
        lines.append(f"**自我检验**：历史快照 {int(bt['days'])} 个交易日，继续积累中…")
    elements = [{"tag": "markdown", "content": "\n".join(lines)}]

    # ---- Top 表格：table 组件 ----
    deep_map = {str(d.get("code")): d for d in deep if d} if deep else {}
    table_rows = []
    for t in top[:n]:
        table_rows.append({
            "template": "blue",
            "cells": [_cell(_fmt_stock(t, k)) for k in _STOCK_KEYS],
        })
    elements.append({
        "tag": "table",
        "page_style": {"size": "lg", "page_split_mode": "default"},
        "column_count": len(_TABLE_COLUMNS),
        "columns": _TABLE_COLUMNS,
        "rows": table_rows,
    })

    # ---- 深度校验：column_set 多列网格 ----
    picks = []
    for t in top[:n]:
        d = deep_map.get(str(t.get("code")))
        if not d or not d.get("buy"):
            continue
        picks.append({
            "title": f"{t.get('name','')}({str(t.get('code',''))})",
            "buy": _num(d.get("buy"), "{:.2f}"),
            "stop": _num(d.get("stop"), "{:.2f}"),
            "rec": d.get("rec", ""),
        })
    if picks:
        elements.append({"tag": "markdown",
                         "content": "**深度校验（建议买入 / 止损参考）**"})
        for i in range(0, len(picks), 2):
            cols = []
            for p in picks[i:i + 2]:
                cols.append({
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "elements": [{
                        "tag": "div",
                        "fields": [
                            {"is_short": True, "text": {"tag": "lark_md",
                                                        "content": f"**{p['title']}**"}},
                            {"is_short": True, "text": {"tag": "lark_md",
                                                        "content": f"买 {p['buy']} / 损 {p['stop']}"}},
                            {"is_short": True, "text": _rec_style(p["rec"])},
                        ],
                    }],
                })
            if len(cols) == 1:
                cols.append({"tag": "column", "width": "weighted", "weight": 1,
                             "elements": []})
            elements.append({"tag": "column_set", "flex_mode": "none",
                             "background_style": "grey",
                             "columns": cols})

    # ---- AI 解读（自由文本，markdown 即可） ----
    if ai_text:
        elements.append({"tag": "markdown",
                         "content": f"**AI 解读**\n{ai_text.strip()}"})

    # ---- 底部：报告链接 + 免责 ----
    if page_url:
        elements.append({"tag": "markdown", "content": f"📄 完整网页报告：{page_url}"})
    elements.append({"tag": "note", "elements": [{"tag": "plain_text",
                    "content": "数据来自东方财富公开接口，仅供研究参考，不构成投资建议。"}]})
    return elements


def send_webhook(webhook: str, elements: list) -> int:
    """POST 到飞书机器人 webhook。成功返回 0，失败返回非 0。"""
    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "A股多因子选股 · 每日推送"},
                "template": "blue",
            },
            "elements": elements,
        },
    }
    r = requests.post(webhook, json=payload, timeout=30)
    data = r.json()
    if r.status_code == 200 and data.get("code") == 0:
        return 0
    print(f"飞书推送失败：HTTP {r.status_code} {data}", file=sys.stderr)
    return 1


def push_latest(cfg: Config) -> int:
    webhook = os.environ.get("FEISHU_WEBHOOK", "").strip()
    if not webhook:
        print("未配置 FEISHU_WEBHOOK。请把飞书机器人 Webhook 地址设为该环境变量。",
              file=sys.stderr)
        return 3
    json_path = os.path.join(cfg.report_dir, "latest.json")
    try:
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"未找到 {json_path}，请先运行 python -m ashare_quant run。", file=sys.stderr)
        return 3

    top = data.get("top", [])
    deep = data.get("deep", [])
    meta = dict(data.get("meta", {}))
    meta["generated_date"] = (data.get("generated") or "")[:10]
    ai_text = data.get("ai", "") or ""

    # 可选：GitHub Pages 报告地址。整段 URL 建议在 GitHub Secret/变量里配置，避免手贴被转义。
    page_url = os.environ.get("PAGE_URL", "").strip()
    n = int(os.environ.get("FEISHU_TOP", "20"))

    elements = build_card(top, meta, data.get("backtest", {}), deep, ai_text, page_url, n)
    return send_webhook(webhook, elements)


def run_cli(cfg: Config) -> None:
    sys.exit(push_latest(cfg))