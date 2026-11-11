"""推送每日选股结果到飞书（自定义机器人 Webhook）。

用法：
  1. 在飞书群 设置→群机器人→添加机器人→自定义机器人，复制 Webhook 地址
  2. 把 Webhook 放进环境变量 FEISHU_WEBHOOK（GitHub Actions 里放 Secret）
  3. python -m ashare_quant feishu

依赖：requests（项目已有）。飞书自定义机器人单条卡片消息上限 150KB。
"""

from __future__ import annotations

import json
import os
import sys

import requests

from .config import Config


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


def build_text(top: list[dict], meta: dict, backtest: dict, deep: list[dict],
               ai_text: str = "", page_url: str = "", n: int = 20) -> str:
    """拼一段飞书 markdown 文本（表格 + 摘要 + AI 解读）。"""
    lines = [f"# A股多因子选股 · {meta.get('generated_date', '')}",
             "",
             f"> 股票池：{meta.get('universe', '—')}"]

    bt = backtest or {}
    if bt.get("ok"):
        lines.append("> 自我检验：分组差 "
                     f"{_num(bt.get('spread_mean'), '{:.2%}')} | RankIC "
                     f"{_num(bt.get('ic_mean'), '{:.3f}')} | ICIR "
                     f"{_num(bt.get('icir'), '{:.2f}')} | 多头占优 "
                     f"{_num(bt.get('hit_rate'), '{:.0%}')}")
    elif bt.get("days"):
        lines.append(f"> 自我检验：历史快照 {int(bt['days'])} 个交易日，继续积累中…")

    lines += ["",
              "| 排名 | 代码 | 名称 | 行业 | 现价 | 今涨跌 | PE | PB | ROE | 综合分 |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]

    deep_map = {d.get("code"): d for d in deep if d} if deep else {}
    for t in top[:n]:
        code = t.get("code", "")
        d = deep_map.get(str(code), {})
        rec = d.get("rec", "")
        name = t.get("name", "")
        # 雷区类加红标提示，质地好加绿标
        mark = ""
        if "雷区" in rec:
            mark = "❌"
        elif "质地好" in rec or "优选" in rec:
            mark = "✅"
        lines.append(
            f"| {t.get('rank','')} | {code} | {name}{mark} | {t.get('industry','')} "
            f"| {_num(t.get('price'), '{:.2f}')} | {_fmt_pct(t.get('pct'))} "
            f"| {_num(t.get('pe'), '{:.1f}')} | {_num(t.get('pb'), '{:.2f}')} "
            f"| {_num(t.get('roe'), '{:.1f}')} | {_num(t.get('score'), '{:.2f}')} |")

    if deep_map:
        lines += ["", "**深度校验**（建议买入 / 止损参考）："]
        rows = []
        for t in top[:n]:
            d = deep_map.get(str(t.get("code")))
            if not d or not d.get("buy"):
                continue
            rows.append(f"{t.get('name')}({t.get('code')}) 买{_num(d.get('buy'),'{:.2f}')}/"
                        f"损{_num(d.get('stop'),'{:.2f}')} · {d.get('rec','')}")
        if rows:
            lines += ["- " + r for r in rows]

    if ai_text:
        lines += ["", "---", "## AI 解读", "", ai_text.strip()]

    if page_url:
        lines += ["", f"📄 完整网页报告：{page_url}"]

    lines += ["", "_仅供自用研究参考，不构成投资建议。_"]
    return "\n".join(lines)


def send_webhook(webhook: str, text: str) -> int:
    """POST 到飞书机器人 webhook。成功返回 0，失败返回非 0。"""
    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "A股多因子选股 · 每日推送"},
                "template": "blue",
            },
            "elements": [
                {"tag": "markdown", "content": text},
                {"tag": "note", "elements": [{"tag": "plain_text",
                                              "content": "数据来自东方财富公开接口，仅供研究参考，不构成投资建议。"}]},
            ],
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

    text = build_text(top, meta, data.get("backtest", {}), deep, ai_text, page_url, n)
    return send_webhook(webhook, text)


def run_cli(cfg: Config) -> None:
    sys.exit(push_latest(cfg))