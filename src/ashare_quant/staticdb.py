"""静态基本面数据库：东财可用日全市场落盘，东财故障日回填慢变因子。

设计动机：东财经常屏蔽 GitHub Actions 机房 IP（返回 502）。而 行业/ROE/毛利率/
净利同比/上市日 这类因子是"慢变数据"（季度级披露），不必每日重抓。因此：

  - 东财快照成功日：把全市场这些慢变列落盘为静态库，随仓库提交跨运行存活。
  - 东财快照失败日（新浪源）：读回这份静态库，回填行业与基本面，让 quality 因子
    与 industry_bonus 在降级路径照样生效，避免退化成"低 PB 银行/高速刷屏"。

列约定与东财快照对齐（见 fetch.FIELD_MAP）：industry/roe/gross_margin/np_yoy/
net_profit/total_assets/revenue/ipo_date。新浪源缺这些列，东财通时补上。
"""

from __future__ import annotations

import datetime as dt
import os

import pandas as pd

# 慢变列：东财快照有、新浪缺、且不随每日行情大变的字段
SLOW_COLS = [
    "industry", "roe", "gross_margin", "np_yoy", "net_profit",
    "total_assets", "revenue", "ipo_date",
]


def static_path(cfg) -> str:
    """静态库 CSV 路径。"""
    return os.path.join(cfg.static_dir, "fundamentals.csv")


def save_static(snap: pd.DataFrame, cfg) -> bool:
    """把东财快照的慢变列落盘为静态库（全市场）。返回是否写成功。

    仅取当前快照里真实存在的慢变列；至少要有 code + 任意一列才写。
    """
    cols = [c for c in SLOW_COLS if c in snap.columns]
    if not cols:
        return False
    sub = snap[["code"] + cols].copy()
    sub["code"] = sub["code"].astype(str).str.zfill(6)
    sub["as_of"] = dt.date.today().isoformat()
    path = static_path(cfg)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    sub.to_csv(path, index=False)
    return True


def load_static(cfg) -> dict:
    """读静态库 → {code: {col: value}}。文件不存在/损坏/为空返回 {}（不报错）。"""
    path = static_path(cfg)
    try:
        df = pd.read_csv(path, dtype={"code": str})
    except Exception:
        return {}
    if df.empty or "code" not in df.columns:
        return {}
    df["code"] = df["code"].str.zfill(6)
    cols = [c for c in df.columns if c not in ("code", "as_of")]
    return {r["code"]: {c: (None if pd.isna(r[c]) else r[c]) for c in cols}
            for _, r in df.iterrows()}


def merge_static(snap: pd.DataFrame, static: dict,
                 cols: list[str] | None = None) -> pd.DataFrame:
    """把静态库慢变列回填到快照（只补 NaN/缺列，不覆盖新浪已有值）。返回新 DataFrame。

    - 已有列：仅填充 NaN（新浪可能个别有值）。
    - 缺失列：整体从静态库回填。
    代码在静态库里没有的，该列保持 NaN（下游自动归 0 / 跳过，不抛错）。
    """
    if not static:
        return snap
    cols = cols or SLOW_COLS
    out = snap.copy()
    codes = pd.Series(out["code"]).astype(str).str.zfill(6)
    static_df = pd.DataFrame.from_dict(static, orient="index")
    static_cols = set(static_df.columns)
    for c in cols:
        if c not in static_cols:
            continue
        st_ser = codes.map(static_df[c])          # 静态库该列按 code 对齐，缺 code→NaN
        if c in out.columns:
            out[c] = out[c].where(pd.notna(out[c]), st_ser)  # 只填 NaN 空位
        else:
            out[c] = st_ser                       # 缺列：整体回填
    return out