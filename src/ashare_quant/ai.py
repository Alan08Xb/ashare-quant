"""可选的 AI 解读：借鉴 TradingAgents 的多智能体思路（简化版）。

TradingAgents 的精髓不是“LLM 选股”，而是**决策结构**：
  多个独立分析师各看一个维度 → 多空辩论 → 交易员下决定 → 风控/组合经理终审，
  且强调 point-in-time（只准用当天已发布的数据，防前视）。

本模块把它落地为两次 LLM 调用：
  第1轮（并行视角）：基本面分析师 / 技术·筹码分析师 各写一段独立评估
  第2轮（决策终审）：交易员汇总两份评估给出 买入/观察/回避 + 理由，风控官挑毛病
未配置 AI_API_KEY 时优雅跳过。单次调用模式（AI_MULTI_ROLE=0）保留为兜底。
"""

from __future__ import annotations

import json
import os

import requests

from .config import Config

_SYSTEM = (
    "你是资深A股研究员。只依据给你的数据做判断，数据里没有的信息就说不确定，"
    "绝不编造财报数字、新闻或未来走势。输出用中文，简洁、给结论也给理由。"
    "所有产出仅供个人研究参考，不构成投资建议。"
)

_DATA_NOTE = (
    "注意：以上均为程序抓取的截至运行日已公开数据（财务为最新已披露一期、行情为当日快照）。"
    "不要假设任何未发布的信息。"
)


def _stock_lines(top: list[dict], deep: list[dict], limit: int = 15) -> str:
    lines = []
    for i, (t, d) in enumerate(zip(top, deep), 1):
        code = t.get("code", "")
        name = d.get("name") or t.get("name", "")
        lines.append(
            f"{i}. {name}({code}) {t.get('industry','')} 现价{t.get('price','')} "
            f"综合分{t.get('score','')} | 规则建议: {d.get('rec','—')} | "
            f"建议买入{d.get('buy','—')}/止损{d.get('stop','—')} | 信号: {d.get('badges','—')}"
        )
    return "\n".join(lines[:limit])


def _call(cfg: Config, prompt: str) -> str:
    url = cfg.ai_base_url.rstrip("/") + "/chat/completions"
    r = requests.post(
        url,
        headers={"Authorization": f"Bearer {cfg.ai_api_key}", "Content-Type": "application/json"},
        json={"model": cfg.ai_model,
              "messages": [{"role": "system", "content": _SYSTEM},
                           {"role": "user", "content": prompt}],
              "temperature": 0.4, "max_tokens": 900},
        timeout=cfg.ai_timeout,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def _header(meta: dict) -> str:
    return (f"股票池：{meta.get('universe','')}；因子权重 价值/质量/动量/低波="
            f"{meta.get('w_value')}/{meta.get('w_quality')}/{meta.get('w_momentum')}/{meta.get('w_lowvol')}；"
            f"默认剔除板块：{meta.get('exclude_industry','无')}")


def single_pass(cfg: Config, top: list[dict], deep: list[dict], meta: dict) -> str:
    prompt = (
        "基于以下量化选股输出，写一份 300-500 字的中文解读：按行业/质地归类；"
        "指出信号最好与最需谨慎的标的及原因；结尾提醒这是模型信号需自行核对。\n\n"
        + _header(meta) + "\n\n候选（含深度信号）：\n" + _stock_lines(top, deep)
    )
    return _call(cfg, prompt)


def multi_role(cfg: Config, top: list[dict], deep: list[dict], meta: dict) -> str:
    """两轮决策：①基本面/技术筹码两份独立评估 ②交易员决策+风控终审。"""
    data = _header(meta) + "\n\n候选（含深度信号与筹码/建议买入价）：\n" + _stock_lines(top, deep) + "\n\n" + _DATA_NOTE

    # ---- 第一轮：两个独立视角 ----
    p_fund = (
        "【基本面分析师】只看质地：ROE 持续性、现金流/净利、毛利、成长、雷区。"
        "把候选分为 优质 / 一般 / 有雷 三档，各点名并给一句理由。不要讨论买点。\n\n" + data)
    p_tech = (
        "【技术与筹码分析师】只看买点与筹码位置：均线/量能、获利盘比例、价格相对筹码成本线。"
        "特别指出：哪些是主力浮盈巨大的追高票（接盘风险大），哪些是基本面尚可但主力浮盈小或浮亏的低位机会。"
        "不要讨论财报质量。\n\n" + data)
    try:
        fund_view = _call(cfg, p_fund)
    except Exception as e:
        fund_view = f"（基本面分析失败：{e}）"
    try:
        tech_view = _call(cfg, p_tech)
    except Exception as e:
        tech_view = f"（技术分析失败：{e}）"

    # ---- 第二轮：交易员 + 风控终审 ----
    p_final = (
        "【交易员+风控终审】下面是两位分析师的独立评估。请：\n"
        "1) 对两位观点有分歧的标的做出最终裁决（买入观察/回避），说明取舍；\n"
        "2) 风控官单独挑出最危险的 2-3 只和理由（重点：主力获利盘过高、追高、现金流差、雷区）；\n"
        "3) 给出一个纪律清单（分批、止损、单票仓位上限）。\n"
        "注意“基本面好但股价跌了很多”可能是黄金坑也可能是价值陷阱，请分别说明判别条件。\n\n"
        + data + "\n\n== 基本面分析师 ==\n" + fund_view + "\n\n== 技术·筹码分析师 ==\n" + tech_view)
    try:
        final = _call(cfg, p_final)
    except Exception as e:
        final = f"（终审失败：{e}）"

    return ("### AI 多角色解读（基本面视角）\n\n" + fund_view
            + "\n\n### AI 多角色解读（技术·筹码视角）\n\n" + tech_view
            + "\n\n### AI 交易员·风控终审\n\n" + final)


def ai_interpret(cfg: Config, top: list[dict], deep: list[dict], meta: dict) -> str:
    if not cfg.ai_api_key:
        return ""
    try:
        if cfg.ai_multi_role:
            return multi_role(cfg, top, deep, meta)
        return single_pass(cfg, top, deep, meta)
    except Exception as e:
        return f"（AI 解读失败：{e}））".replace("））", "）")


def write_ai_report(cfg: Config, text: str) -> str:
    if not text:
        return ""
    os.makedirs(cfg.report_dir, exist_ok=True)
    path = os.path.join(cfg.report_dir, "ai.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("## AI 解读（模型生成，仅供参考，请自行核对数据）\n\n" + text + "\n")
    return path


def save_json_field(cfg: Config, text: str) -> None:
    path = os.path.join(cfg.report_dir, "latest.json")
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        data["ai"] = text
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass