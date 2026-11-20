"""报告输出：Markdown + 自包含 HTML（供 GitHub Pages）。"""

from __future__ import annotations

import datetime as dt
import json
import numbers
import os

import pandas as pd

from .config import Config

FMT = {
    "rank": "int",
    "price": "{:.2f}",
    "pct": "{:+.2f}%",
    "ret_60": "{:+.1f}%",
    "pe": "{:.1f}",
    "pb": "{:.2f}",
    "roe": "{:.1f}%",
    "np_yoy": "{:+.0f}%",
    "gross_margin": "{:.0f}%",
    "mktcap": "{:.0f}亿",
    "floatcap": "{:.0f}亿",
    "turnover": "{:.1f}%",
    "score": "{:.2f}",
    "score_adj": "{:.2f}",
}


def _fmt_series(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    # 代码恒为 6 位字符串，绝不做数值化（否则 000498 → "498" 丢前导零）
    if "code" in out.columns:
        out["code"] = out["code"].astype(str).str.zfill(6)
    for c, f in FMT.items():
        if c in out.columns:
            s = pd.to_numeric(out[c], errors="coerce")
            if f == "int":
                out[c] = s.round(0).astype("Int64")
            elif f == "{:.0f}亿":
                out[c] = (s / 1e8).map(lambda x: f"{x:.0f}" if pd.notna(x) else "—")
            else:
                out[c] = s.map(lambda x: f.format(x) if pd.notna(x) else "—")
    return out


def render_markdown(top: pd.DataFrame, meta: dict, backtest: dict, deep: pd.DataFrame | None = None) -> str:
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    bt = backtest or {}
    lines = [
        f"# A股多因子选股 · {now}",
        "",
        f"> **股票池**：{meta.get('universe','')}  ",
        f"> **因子权重**：价值 {meta.get('w_value')} / 质量 {meta.get('w_quality')} "
        f"/ 动量 {meta.get('w_momentum')} / 低波动 {meta.get('w_lowvol')}  "
        f"（市值中性化：{'开' if meta.get('size_neutral') else '关'}）",
        "",
    ]
    if meta.get("degraded"):
        lines += [
            "> ⚠️ **今日东财接口异常，数据已降级（新浪源）**：行业/动量等字段缺失，",
            "> 综合分不具可比性，深度校验可能缺失。仅供参考。",
            "",
        ]
    if bt.get("ok"):
        lines += [
            "## 自我检验（基于已积累历史快照）",
            "",
            "| 周期 | 分组均值收益(最高分-最低分) | RankIC | ICIR | 多头占优天数占比 |",
            "|---|---|---|---|---|",
        ]
        hz = bt.get("horizons", {})
        for n in sorted(hz, key=int):
            p = hz[n]
            lines.append(f"| {n}日 | {p['spread_mean']:.2%} | {p['ic_mean']:.3f} | "
                         f"{p['icir']:.2f} | {p['hit_rate']:.0%} |")
        lines += [
            "",
            f"> 说明：用“后 N 个快照日收盘/当日收盘-1”近似 N 周期收益，衡量综合分对未来收益的预测力。",
            f"> 按快照日近似（非交易日）：停牌/退市的股票会从快照序列中消失，导致其 fwd_N 实际跨过更多自然日。",
            f"> 主周期 {bt.get('main_horizon')}日（持仓周期）。历史天数越少越不具统计意义；建议积累 60 个交易日后才参考。",
            "",
        ]
    else:
        lines += [
            "## 自我检验",
            "",
            f"> 历史快照尚不足（{bt.get('days',0)} 个交易日），继续运行会自动积累。",
            "",
        ]

    lines += ["## Top 组合", ""]
    cols = [c for c in ["rank", "code", "name", "industry", "price", "pct",
                        "ret_60", "roe", "np_yoy", "gross_margin", "pe", "pb",
                        "floatcap", "turnover", "score_adj", "score"] if c in top.columns]
    disp = _fmt_series(top[cols].copy())
    header = "| " + " | ".join({"rank": "排名", "code": "代码", "name": "名称", "industry": "行业",
                                "price": "现价", "pct": "今涨跌", "ret_60": "60日涨幅",
                                "roe": "ROE%", "np_yoy": "净利同比", "gross_margin": "毛利率",
                                "pe": "PE", "pb": "PB", "floatcap": "流通市值",
                                "turnover": "换手", "score_adj": "精调分", "score": "因子分"}.get(c, c) for c in cols) + " |"
    sep = "|" + "---|" * len(cols)
    rows = [header, sep]
    for _, r in disp.iterrows():
        rows.append("| " + " | ".join(str(r[c]) for c in cols) + " |")
    lines += rows
    lines += ["", "### 深度校验（基本面 / 技术面 / 雷区）", "",
              "> 建议标签是规则化归类，不是荐股；雷区类需自行再核对数据。", ""]
    if deep is not None and not deep.empty:
        dcols = [c for c in ["code", "name", "buy", "stop", "ma20", "main_cost", "conc90", "chip_adj", "pattern_adj", "rec", "badges"] if c in deep.columns]
        if dcols:
            dhead = {"code": "代码", "name": "名称", "buy": "建议买入", "stop": "止损参考",
                     "ma20": "20日线", "main_cost": "30日主力成本", "conc90": "90%筹码集中度",
                     "chip_adj": "筹码调整", "pattern_adj": "形态加分", "rec": "规则建议", "badges": "信号徽章"}
            lines.append("| " + " | ".join(dhead[c] for c in dcols) + " |")
            lines.append("|" + "---|" * len(dcols))
            for _, r in deep[dcols].iterrows():
                lines.append("| " + " | ".join(str(r[c]) for c in dcols) + " |")
            lines.append("")
    lines += ["",
        "---",
        "*仅供研究参考，不构成投资建议。工具会随运行自动积累历史并回测自身有效性。*",
        "",
    ]
    return "\n".join(lines)


def render_html(top: pd.DataFrame, meta: dict, backtest: dict, deep: pd.DataFrame | None = None) -> str:
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    bt = backtest or {}
    bt_html = ""
    if bt.get("ok"):
            hz = bt.get("horizons", {})
            rows = []
            for n in sorted(hz, key=int):
                p = hz[n]
                rows.append(f"<tr><td>{n}日</td><td class=\"big\">{p['spread_mean']:.2%}</td>"
                            f"<td>{p['ic_mean']:.3f}</td><td>{p['icir']:.2f}</td>"
                            f"<td>{p['hit_rate']:.0%}</td><td>{p['valid_days']}天</td></tr>")
            bt_html = f""""
            <div class="card">
              <h3>自我检验 <span class="tag">历史 {bt.get('days')} 个交易日 · 主周期 {bt.get('main_horizon')}日</span></h3>
              <table class="bt">
                <tr><th>周期</th><th>分组收益(最高分−最低分)</th><th>RankIC</th><th>ICIR</th><th>多头占优占比</th><th>样本天数</th></tr>
                {''.join(rows)}
              </table>
              <p class="hint">后 N 个快照日收盘/当日收盘−1 近似收益（非交易日：停牌/退市股会跨更多自然日）· 分数越高后续收益是否越高，数据自己说话。积累 60 个交易日后才有统计意义。</p>
            </div>"""
    else:
        bt_html = f"""
        <div class="card"><h3>自我检验</h3>
        <p class="hint">历史快照尚不足（{bt.get('days',0)} 个交易日），继续每日运行会自动积累后开始回测。</p></div>"""

    cols = [c for c in ["rank", "code", "name", "industry", "price", "pct",
                        "ret_60", "roe", "np_yoy", "gross_margin", "pe", "pb",
                        "floatcap", "turnover", "score_adj", "score"] if c in top.columns]
    disp = _fmt_series(top[cols].copy())
    heads = {"rank": "排名", "code": "代码", "name": "名称", "industry": "行业",
             "price": "现价", "pct": "今涨跌", "ret_60": "60日涨幅", "roe": "ROE%",
             "np_yoy": "净利同比", "gross_margin": "毛利率", "pe": "PE", "pb": "PB",
             "floatcap": "流通市值", "turnover": "换手", "score_adj": "精调分", "score": "因子分"}
    thead = "".join(f"<th>{heads.get(c, c)}</th>" for c in cols)
    trs = []
    for _, r in disp.iterrows():
        tds = "".join(f"<td>{r[c]}</td>" for c in cols)
        trs.append(f"<tr>{tds}</tr>")
    tbody = "\n        ".join(trs)

    # 深度校验卡片
    deep_html = ""
    if deep is not None and not deep.empty:
        dcols = [c for c in ["code", "name", "buy", "stop", "ma20", "main_cost", "conc90", "chip_adj", "pattern_adj", "rec", "badges"] if c in deep.columns]
        if dcols:
            dheads = {"code": "代码", "name": "名称", "buy": "建议买入", "stop": "止损参考",
                      "ma20": "20日线", "main_cost": "30日主力成本", "conc90": "90%筹码集中度",
                      "chip_adj": "筹码调整", "pattern_adj": "形态加分", "rec": "规则建议", "badges": "信号徽章"}
            dthead = "".join(f"<th>{dheads.get(c, c)}</th>" for c in dcols)
            dtrs = []
            for _, r in deep[dcols].iterrows():
                dtrs.append("<tr>" + "".join(f"<td>{r[c]}</td>" for c in dcols) + "</tr>")
            deep_html = f"""
<div class="card">
  <h3>深度校验 <span class="tag">基本面 / 技术面 / 雷区</span></h3>
  <p class="hint">规则建议标签仅用于归类，非荐股；带 ⚠/❌ 的雷区项请自行核对原始财报。</p>
  <div style="overflow-x:auto"><table><thead><tr>{dthead}</tr></thead>
  <tbody>{chr(10).join(dtrs)}</tbody></table></div>
</div>"""

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>A股多因子选股 · {now}</title>
<style>
:root{{--bg:#0f1115;--card:#1a1d24;--line:#2a2e38;--tx:#e6e8ee;--mut:#9aa0ad;--acc:#4f8cff;--up:#e05b5b;--down:#3fbf6f}}
*{{box-sizing:border-box}}body{{margin:0;font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;background:var(--bg);color:var(--tx);padding:24px}}
.wrap{{max-width:1080px;margin:0 auto}}
h1{{font-size:22px}}h1 small{{color:var(--mut);font-weight:400;font-size:14px;margin-left:10px}}
h3{{margin:0 0 12px}}p{{color:var(--mut);line-height:1.6;font-size:13px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;margin:18px 0}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{padding:8px 10px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap}}
th{{color:var(--mut);font-weight:600;position:sticky;top:0;background:var(--card)}}
td.big{{font-size:20px;font-weight:700;color:var(--acc)}}
.tag{{background:#2a2e38;color:var(--mut);font-size:11px;padding:2px 8px;border-radius:20px;font-weight:400}}
.hint{{margin:10px 0 0;font-size:12px}}
.meta{{display:flex;gap:8px;flex-wrap:wrap}}
.meta span{{background:#22262f;border:1px solid var(--line);padding:4px 10px;border-radius:8px;font-size:12px;color:var(--mut)}}
.warn{{margin-top:10px;padding:8px 12px;border:1px solid #b58900;border-radius:8px;background:rgba(181,137,0,.12);color:#e8c766;font-size:12px}}
.disclaimer{{font-size:12px;color:var(--mut);margin-top:20px}}
</style></head><body><div class="wrap">
<h1>A股多因子选股 <small>更新于 {now}</small></h1>
<div class="meta">
  <span>股票池：{meta.get('universe','')}</span>
  <span>市值中性化：{'开' if meta.get('size_neutral') else '关'}</span>
  <span>权重 价值{meta.get('w_value')} 质量{meta.get('w_quality')} 动量{meta.get('w_momentum')} 低波{meta.get('w_lowvol')}</span>
  {'<div class="warn">⚠️ 今日东财接口异常，数据已降级（新浪源）：行业/动量等字段缺失，综合分不具可比性，深度校验可能缺失，仅供参考。</div>' if meta.get('degraded') else ''}
</div>
{bt_html}
<div class="card">
  <h3>Top 组合</h3>
  <div style="overflow-x:auto"><table><thead><tr>{thead}</tr></thead>
  <tbody>{tbody}</tbody></table></div>
</div>
{deep_html}
<p class="disclaimer">仅供研究参考，不构成投资建议。数据来自东方财富公开接口，可能存在缺失或延迟。</p>
</div></body></html>"""


def _sanitize(o):
    """递归把 numpy 标量转成 Python 标量，NaN/Inf 变 None，保证可 json 序列化。"""
    if isinstance(o, dict):
        return {k: _sanitize(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_sanitize(x) for x in o]
    if isinstance(o, bool) or o is None or isinstance(o, str):
        return o
    if isinstance(o, (int, float)):
        return None if o != o else o
    if hasattr(o, "item"):  # numpy 标量
        try:
            f = float(o)
            return None if f != f else f
        except Exception:
            return str(o)
    return o


def write_reports(top: pd.DataFrame, meta: dict, backtest: dict, cfg: Config,
                  deep: pd.DataFrame | None = None, ai_text: str = "") -> dict[str, str]:
    os.makedirs(cfg.report_dir, exist_ok=True)
    os.makedirs(cfg.docs_dir, exist_ok=True)
    md = render_markdown(top, meta, backtest, deep)
    html = render_html(top, meta, backtest, deep)
    md_path = os.path.join(cfg.report_dir, "report.md")
    html_path = os.path.join(cfg.docs_dir, "index.html")
    json_path = os.path.join(cfg.report_dir, "latest.json")
    if ai_text:
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md + "\n\n" + ai_text + "\n")
    else:
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    payload = {
        "generated": dt.datetime.now().isoformat(timespec="seconds"),
        "meta": meta,
        "backtest": {k: (round(float(v), 6) if isinstance(v, numbers.Number) and not isinstance(v, bool) else v) for k, v in (backtest or {}).items()},
        "top": top.to_dict(orient="records"),
        "deep": _sanitize(deep.to_dict(orient="records")) if deep is not None and not deep.empty else [],
        "ai": ai_text,
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return {"md": md_path, "html": html_path, "json": json_path}