"""历史快照归档：供滚动回测（IC / 分位收益）积累数据。"""

from __future__ import annotations

import datetime as dt
import os

import pandas as pd

from .config import Config

# 存档用列
SNAPSHOT_COLS = [
    "code", "name", "industry", "price", "pct", "amount", "turnover",
    "pe", "pb", "mktcap", "floatcap", "ret_60", "ret_ytd", "roe",
    "gross_margin", "np_yoy", "net_profit",
    "f_value", "f_quality", "f_momentum", "f_lowvol", "score", "rank",
]


def snapshot_path(cfg: Config, day: str) -> str:
    return os.path.join(cfg.history_dir, f"{day}.csv")


def save_snapshot(df: pd.DataFrame, cfg: Config, day: str) -> str:
    os.makedirs(cfg.history_dir, exist_ok=True)
    out = df[[c for c in SNAPSHOT_COLS if c in df.columns]].copy()
    out.insert(0, "date", day)
    path = snapshot_path(cfg, day)
    out.to_csv(path, index=False, encoding="utf-8-sig")
    return path


def load_history(cfg: Config) -> pd.DataFrame:
    """读回所有历史快照（含当日），拼接成一个长表。"""
    if not os.path.isdir(cfg.history_dir):
        return pd.DataFrame()
    frames = []
    for fn in sorted(os.listdir(cfg.history_dir)):
        if fn.endswith(".csv"):
            frames.append(pd.read_csv(os.path.join(cfg.history_dir, fn), dtype={"code": str}))
    if not frames:
        return pd.DataFrame()
    h = pd.concat(frames, ignore_index=True)
    h["date"] = pd.to_datetime(h["date"], errors="coerce")
    h["date_n"] = h["date"].dt.strftime("%Y%m%d")
    return h


def list_available_days(cfg: Config) -> list[str]:
    if not os.path.isdir(cfg.history_dir):
        return []
    return sorted(fn.replace(".csv", "") for fn in os.listdir(cfg.history_dir) if fn.endswith(".csv"))