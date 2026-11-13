"""baostock_src 单元测试（不打真实网络：报告推算/代码前缀/容错行为）。"""
import datetime as dt

import pandas as pd

from ashare_quant import baostock_src as B


def test_report_candidates_by_month():
    # 1-3月：最新是三季报（上年Q3）
    assert B._report_candidates(dt.date(2026, 2, 10))[0] == (2025, 3)
    # 4-6月：一季报已出
    assert B._report_candidates(dt.date(2026, 5, 20))[0] == (2026, 1)
    # 7-9月：中报未全出，一季报在手（中报8月底起才有）
    assert B._report_candidates(dt.date(2026, 7, 1))[0] == (2026, 1)
    # 10-12月：三季报已出
    assert B._report_candidates(dt.date(2026, 11, 13))[0] == (2026, 3)
    # 候选永远有 ≥3 档兜底
    for m in range(1, 13):
        c = B._report_candidates(dt.date(2026, m, 15))
        assert len(c) >= 3


def test_sym_prefix():
    assert B._sym("600519") == "sh.600519"
    assert B._sym("000001") == "sz.000001"
    assert B._sym("300750") == "sz.300750"
    assert B._sym("688981") == "sh.688981"
    assert B._sym("1") == "sz.000001"     # 6 位补零


def test_to_float():
    assert B._to_float("12.3") == 12.3
    assert pd.isna(B._to_float(""))
    assert pd.isna(B._to_float(None))
    assert pd.isna(B._to_float("nan"))


def test_enrich_survives_total_failure(monkeypatch):
    """baostock 整体故障（登录失败）时 enrich 不抛异常，全 NaN 返回。"""
    def _boom():
        raise RuntimeError("baostock 登录失败: timeout")
    monkeypatch.setattr(B, "_session", _boom)
    out = B.enrich(["600519", "000001"], fill_ret60=True)
    assert len(out) == 2
    assert out.index.name == "code"


def test_cache_dedup(monkeypatch):
    """同一 code 第二次调用直接走缓存，不再打网络。"""
    calls = []
    def _session():
        calls.append(1)
        return object()
    monkeypatch.setattr(B, "_session", _session)
    B._cache.clear()
    B._cache["600519"] = {"roe": 25.0}
    r = B.fetch_quality("600519")
    assert r == {"roe": 25.0}
    assert calls == []     # 命中缓存 → 不触发 _session
