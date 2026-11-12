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

# ---- 选股结果表 ----
# 排名 | 代码 | 名称 | 行业 | 现价 | 今涨跌 | 60日涨幅 | ROE% | 净利同比 | 毛利率 | PE | PB | 流通市值 | 换手 | 综合分
_SEL_COLUMNS = [
    {"name": "rank", "display_name": "排名", "width": "auto", "data_type": "text"},
    {"name": "code", "display_name": "代码", "width": "auto", "data_type": "text"},
    {"name": "name", "display_name": "名称", "width": "auto", "data_type": "text"},
    {"name": "industry", "display_name": "行业", "width": "auto", "data_type": "text"},
    {"name": "price", "display_name": "现价", "width": "auto", "data_type": "text"},
    {"name": "pct", "display_name": "今涨跌", "width": "auto", "data_type": "text"},
    {"name": "ret_60", "display_name": "60日涨幅", "width": "auto", "data_type": "text"},
    {"name": "roe", "display_name": "ROE%", "width": "auto", "data_type": "text"},
    {"name": "np_yoy", "display_name": "净利同比", "width": "auto", "data_type": "text"},
    {"name": "gross_margin", "display_name": "毛利率", "width": "auto", "data_type": "text"},
    {"name": "pe", "display_name": "PE", "width": "auto", "data_type": "text"},
    {"name": "pb", "display_name": "PB", "width": "auto", "data_type": "text"},
    {"name": "floatcap", "display_name": "流通市值", "width": "auto", "data_type": "text"},
    {"name": "turnover", "display_name": "换手", "width": "auto", "data_type": "text"},
    {"name": "score", "display_name": "综合分", "width": "auto", "data_type": "text"},
]
_SEL_KEYS = ["rank", "code", "name", "industry", "price", "pct", "ret_60", "roe",
             "np_yoy", "gross_margin", "pe", "pb", "floatcap", "turnover", "score"]

# ---- 深度校验表 ----
# 代码 | 名称 | 建议买入 | 止损参考 | 20日线 | 30日主力成本 | 筹码调整 | 规则建议 | 信号徽章
_DEEP_COLUMNS = [
    {"name": "code", "display_name": "代码", "width": "auto", "data_type": "text"},
    {"name": "name", "display_name": "名称", "width": "auto", "data_type": "text"},
    {"name": "buy", "display_name": "建议买入", "width": "auto", "data_type": "text"},
    {"name": "stop", "display_name": "止损参考", "width": "auto", "data_type": "text"},
    {"name": "ma20", "display_name": "20日线", "width": "auto", "data_type": "text"},
    {"name": "main_cost", "display_name": "30日主力成本", "width": "auto", "data_type": "text"},
    {"name": "chip_adj", "display_name": "筹码调整", "width": "auto", "data_type": "text"},
    {"name": "rec", "display_name": "规则建议", "width": "auto", "data_type": "text"},
    {"name": "badges", "display_name": "信号徽章", "width": "auto", "data_type": "text"},
]
_DEEP_KEYS = ["code", "name", "buy", "stop", "ma20", "main_cost", "chip_adj", "rec", "badges"]


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


def _fmt_sel(t: dict, key: str) -> str:
    """选股结果表单元格格式化。"""
    if key == "rank":
        return _num(t.get("rank"), "{:.0f}")
    if key in ("pct", "ret_60", "np_yoy"):
        return _fmt_pct(t.get(key))
    if key == "price":
        return _num(t.get(key), "{:.2f}")
    if key == "pb":
        return _num(t.get(key), "{:.2f}")
    if key in ("roe", "gross_margin", "turnover"):
        return _num(t.get(key), "{:.1f}")
    if key == "floatcap":
        fc = t.get("floatcap")
        return _num((fc / 1e8) if fc else None, "{:.0f}亿")
    if key in ("pe", "score"):
        return _num(t.get(key), "{:.2f}" if key == "score" else "{:.1f}")
    return str(t.get(key, "") or "")


def _badges_short(badges: str, max_len: int = 72) -> str:
    """精简信号徽章：保留头 2 个 + 尾 2 个，中间用 … 概括（徽章多且长，飞书窄屏易换行）。"""
    if not badges:
        return "—"
    parts = [p.strip() for p in badges.split("|") if p.strip()]
    if not parts:
        return "—"
    if len(parts) <= 3:
        return "、".join(parts)
    head, tail = parts[:2], parts[-2:]
    joined = "、".join(head + ["…"] + tail)
    return joined if len(joined) <= max_len else "、".join(head + tail)


def _fmt_deep(d: dict) -> dict:
    """深度校验表单元格格式化。"""
    return {
        "code": str(d.get("code", "")),
        "name": str(d.get("name", "")),
        "buy": _num(d.get("buy"), "{:.2f}"),
        "stop": _num(d.get("stop"), "{:.2f}"),
        "ma20": _num(d.get("ma20"), "{:.2f}"),
        "main_cost": _num(d.get("main_cost"), "{:.2f}"),
        "chip_adj": _num(d.get("chip_adj"), "{:+.2f}"),
        "rec": str(d.get("rec", "")),
        "badges": _badges_short(str(d.get("badges", ""))),
    }


def build_card(top: list[dict], meta: dict, backtest: dict, deep: list[dict],
               ai_text: str = "", page_url: str = "", n: int = 30) -> list:
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

    # ---- 选股结果表 ----
    sel_rows = [{k: _fmt_sel(t, k) for k in _SEL_KEYS} for t in top[:n]]
    elements.append({
        "tag": "table",
        "page_size": 10,
        "row_height": "low",
        "columns": _SEL_COLUMNS,
        "rows": sel_rows,
    })

    # ---- 深度校验表 ----
    deep_map = {str(d.get("code")): d for d in deep if d} if deep else {}
    deep_rows = []
    for t in top[:n]:
        d = deep_map.get(str(t.get("code")))
        if d:
            deep_rows.append(_fmt_deep(d))
    if deep_rows:
        elements.append({"tag": "markdown",
                         "content": "**深度校验（买入 / 止损 / 筹码 / 建议 / 信号）**"})
        elements.append({
            "tag": "table",
            "page_size": 10,
            "row_height": "low",
            "columns": _DEEP_COLUMNS,
            "rows": deep_rows,
        })

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
    n = int(os.environ.get("FEISHU_TOP", "30"))

    elements = build_card(top, meta, data.get("backtest", {}), deep, ai_text, page_url, n)
    return send_webhook(webhook, elements)


def run_cli(cfg: Config) -> None:
    sys.exit(push_latest(cfg))