"""数据抓取：东方财富公开接口（无需 token）。"""

from __future__ import annotations

import random
import sys
import time
from typing import Any

import pandas as pd
import numpy as np
import requests

# 全市场快照（clist）：一次调用可拉全市场所有字段
CLIST_URL = "https://push2.eastmoney.com/api/qt/clist/get"
# 镜像（延迟行情）：主站突发 502 时的回退，盘后收市价一致
CLIST_URL_ALT = "https://push2delay.eastmoney.com/api/qt/clist/get"
# 新浪全市场行情备用源：东财常屏蔽 GitHub Actions 机房 IP，此时换新浪（不挑机房 IP）
SINA_URL = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            "Market_Center.getHQNodeData")
# 个股历史 K 线（push2his）：供“组合历史净值”参考用
KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
# 个股财务指标（datacenter，F10）
F10_URL = "https://datacenter.eastmoney.com/securities/api/data/v1/get"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# 字段 -> 友好列名
FIELD_MAP = {
    "f2": "price",          # 最新价
    "f3": "pct",            # 涨跌幅 %
    "f6": "amount",         # 成交额(元)
    "f7": "amplitude",      # 振幅 %
    "f8": "turnover",       # 换手率 %
    "f9": "pe",             # 市盈率(动态)
    "f10": "vol_ratio",     # 量比
    "f12": "code",          # 代码
    "f14": "name",          # 名称
    "f18": "prev_close",    # 昨收
    "f20": "mktcap",        # 总市值(元)
    "f21": "floatcap",      # 流通市值(元)
    "f23": "pb",            # 市净率
    "f24": "ret_60",        # 60日涨跌幅 %
    "f25": "ret_ytd",       # 年初至今涨跌幅 %
    "f26": "ipo_date",      # 上市日期 YYYYMMDD
    "f37": "roe",           # 净资产收益率(加权) %
    "f40": "revenue",       # 营业收入(元)
    "f45": "net_profit",    # 净利润(元)
    "f46": "np_yoy",        # 净利润同比 %
    "f49": "gross_margin",  # 销售毛利率 %
    "f50": "total_assets",  # 总资产(元)
    "f100": "industry",     # 所属行业
}


def _get_json(url: str, params: dict, retries: int = 4,
              alt_url: str | None = None) -> dict[str, Any]:
    """带退避重试的 GET。东财 push2 在高峰时段会突发 502/429（可持续几分钟）。

    这里重试必须"快"：每次最多 ~10-20s 就放弃，交给外层整体重试
    （workflow 自动重试 / NAS 定时补跑）。若单页长时间重试，整个任务会
    卡几小时最后仍失败——比快速失败更糟。
    """
    last: Exception | None = None
    candidates = [url] + ([alt_url] if alt_url and alt_url != url else [])
    for base in candidates:
        for i in range(retries):
            try:
                r = requests.get(base, params=params,
                                 headers={"User-Agent": UA}, timeout=20)
                r.raise_for_status()
                return r.json()
            except requests.exceptions.HTTPError as e:
                last = e
                # 5xx/429 退避，封顶 6s；其它 4xx 稍等
                if e.response is not None and 500 <= e.response.status_code <= 599:
                    wait = min(1.5 ** (i + 1), 6.0)
                else:
                    wait = 1.0
            except requests.exceptions.RequestException as e:
                last = e
                wait = min(1.5 * (i + 1), 6.0)
            if i == retries - 1:
                break
            time.sleep(wait + random.uniform(0, 1.0))   # 加抖动，避免与东财限流同频撞击
    if last is not None:
        raise last
    raise RuntimeError("unreachable")


def fetch_snapshot(fs: str, fields: list[str]) -> pd.DataFrame:
    """拉取全市场当日快照，返回已重命名的 DataFrame（未清洗）。"""
    fld = ",".join(fields)
    rows: list[dict] = []
    page = 1
    per_page = 100   # 东财 clist 每页最多返回 100 条，pz 再大也被截断
    total = None
    while True:
        data = _get_json(CLIST_URL, {
            "pn": page, "pz": per_page, "po": 1, "np": 1, "fltt": 2, "invt": 2,
            "fid": "f3", "fs": fs, "fields": fld,
        }, alt_url=CLIST_URL_ALT).get("data")
        if not data:
            break
        total = data.get("total", total)
        diff = data.get("diff") or []
        if not diff:
            break
        rows.extend(diff)
        if total is not None and len(rows) >= total:
            break
        page += 1
        time.sleep(0.5)   # 东财对高频翻页会 502，页间隔拉长到 0.5s
    df = pd.DataFrame(rows)
    df = df.rename(columns={k: v for k, v in FIELD_MAP.items() if k in df.columns})
    return df


# 东财有、新浪缺失的字段（置 NaN 占位，相关因子自动归 0）
_SINA_MISSING = ["vol_ratio", "ret_60", "ret_ytd", "ipo_date", "roe", "revenue",
                 "net_profit", "np_yoy", "gross_margin", "total_assets", "industry"]


def fetch_snapshot_sina() -> pd.DataFrame:
    """新浪全市场快照（备用源，不挑机房 IP）。列名与东财对齐，缺失字段置 NaN。

    东财 push2 常对 GitHub Actions 的云端 IP 限流返回 502，此时快照换新浪。
    新浪缺质量/动量/行业类字段（ROE/净利/行业/60日涨幅等），打分时对应因子归 0。
    """
    rows: list[dict] = []
    page, num = 1, 100
    while True:
        data = _get_json(SINA_URL, {"page": page, "num": num, "sort": "symbol",
                                    "asc": 1, "node": "hs_a"}, retries=3)
        if not data:
            break
        rows.extend(data)
        if len(data) < num:
            break
        page += 1
        time.sleep(0.3)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df = df[~df["symbol"].astype(str).str.startswith("bj")]   # 滤掉北交所
    out = pd.DataFrame({
        "code": df["code"].astype(str).str.zfill(6),
        "name": df["name"].astype(str),
        "price": pd.to_numeric(df["trade"], errors="coerce"),
        "pct": pd.to_numeric(df["changepercent"], errors="coerce"),
        "amount": pd.to_numeric(df["amount"], errors="coerce"),
        "prev_close": pd.to_numeric(df["settlement"], errors="coerce"),
        "pe": pd.to_numeric(df["per"], errors="coerce"),
        "pb": pd.to_numeric(df["pb"], errors="coerce"),
        "mktcap": pd.to_numeric(df["mktcap"], errors="coerce") * 1e4,
        "floatcap": pd.to_numeric(df["nmc"], errors="coerce") * 1e4,
        "turnover": pd.to_numeric(df["turnoverratio"], errors="coerce"),
    })
    hi = pd.to_numeric(df["high"], errors="coerce")
    lo = pd.to_numeric(df["low"], errors="coerce")
    prev = pd.to_numeric(df["settlement"], errors="coerce")
    out["amplitude"] = ((hi - lo) / prev * 100.0).replace([np.inf, -np.inf], np.nan)
    for c in _SINA_MISSING:
        out[c] = np.nan
    return out


def fetch_market_snapshot(fs: str, fields: list[str]) -> pd.DataFrame:
    """全市场快照：优先东财，失败自动回退新浪。

    东财对 GitHub Actions 机房 IP 常持续 502（重试无用），此时用新浪兜底，
    保证每天至少能出榜单，而不是整日失败。
    """
    try:
        return fetch_snapshot(fs, fields)
    except Exception as e:
        print(f"  东财快照失败（{type(e).__name__}: {e}），回退新浪源…", file=sys.stderr)
        snap = fetch_snapshot_sina()
        if snap.empty:
            raise RuntimeError("东财与新浪快照均失败") from e
        print(f"  已用新浪快照（{len(snap)} 只；缺质量/动量/行业字段→相关因子归 0）",
              file=sys.stderr)
        return snap


def fetch_kline(code: str, beg: str, end: str, market: str = None) -> pd.DataFrame:
    """拉取单只个股日 K 线（前复权）。beg/end 格式 YYYYMMDD。"""
    if market is None:
        market = "1" if code.startswith(("6", "9")) else "0"
    secid = f"{market}.{code}"
    data = _get_json(KLINE_URL, {
        "secid": secid, "klt": 101, "fqt": 1, "beg": beg, "end": end,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
    }).get("data")
    if not data or not data.get("klines"):
        return pd.DataFrame()
    cols = ["date", "open", "close", "high", "low", "volume", "amount",
            "amplitude", "pct", "chg", "turnover"]
    recs = [dict(zip(cols, k.split(","))) for k in data["klines"]]
    out = pd.DataFrame(recs)
    out["date"] = pd.to_datetime(out["date"])
    for c in ["open", "close", "high", "low", "volume", "amount", "amplitude", "pct", "turnover"]:
        out[c] = pd.to_numeric(out[c], errors="coerce")
    return out


def secu_code(code: str) -> str:
    """600519 -> 600519.SH（用于 datacenter 过滤条件）。"""
    return f"{code}.{'SH' if code.startswith(('6', '9')) else 'SZ'}"


def _f10_finance(report_name: str, code: str, columns: str, size: int) -> pd.DataFrame:
    data = _get_json(F10_URL, {
        "reportName": report_name, "columns": columns,
        "filter": f'(SECUCODE="{secu_code(code)}")',
        "pageNumber": 1, "pageSize": size,
        "sortTypes": "-1", "sortColumns": "REPORT_DATE",
        "source": "HSF10", "client": "PC",
    }).get("result")
    rows = (data or {}).get("data") or []
    return pd.DataFrame(rows)


def fetch_main_finance(code: str, size: int = 16) -> pd.DataFrame:
    """主要财务指标（季频）：ROE、毛利率、营收/净利同比、经营现金、负债率、应收占比。"""
    cols = ("SECUCODE,REPORT_DATE,REPORT_YEAR,REPORT_TYPE,ROEJQ,ROEKCJQ,XSMLL,XSJLL,"
            "PARENTNETPROFIT,PARENTNETPROFITTZ,TOTALOPERATEREVE,TOTALOPERATEREVETZ,"
            "MGJYXJJE,EPSJB,ZCFZL,YSZKYYSR,BPS")
    df = _f10_finance("RPT_F10_FINANCE_MAINFINADATA", code, cols, size)
    for c in ["ROEJQ", "ROEKCJQ", "XSMLL", "XSJLL", "PARENTNETPROFIT", "PARENTNETPROFITTZ",
              "TOTALOPERATEREVE", "TOTALOPERATEREVETZ", "MGJYXJJE", "EPSJB", "ZCFZL",
              "YSZKYYSR", "BPS"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def fetch_balance(code: str, size: int = 4) -> pd.DataFrame:
    """资产负债表（季频）：商誉、货币资金、短期借款、总资产、负债、应收、存货。"""
    cols = ("SECUCODE,REPORT_DATE,GOODWILL,MONETARYFUNDS,SHORT_LOAN,TOTAL_ASSETS,"
            "TOTAL_LIABILITIES,ACCOUNTS_RECE,NOTE_RECE,OTHER_RECE,INVENTORY,"
            "TOTAL_CURRENT_ASSETS,TOTAL_PARENT_EQUITY")
    df = _f10_finance("RPT_F10_FINANCE_GBALANCE", code, cols, size)
    for c in ["GOODWILL", "MONETARYFUNDS", "SHORT_LOAN", "TOTAL_ASSETS", "TOTAL_LIABILITIES",
              "ACCOUNTS_RECE", "NOTE_RECE", "OTHER_RECE", "INVENTORY", "TOTAL_CURRENT_ASSETS",
              "TOTAL_PARENT_EQUITY"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def fetch_kline_close(code: str, beg: str, end: str) -> pd.Series:
    """拉单只个股日线收盘价（前复权，只取日期+收盘，轻量）。返回 index=date 的 close Series。"""
    market = "1" if code.startswith(("6", "9")) else "0"
    data = _get_json(KLINE_URL, {
        "secid": f"{market}.{code}", "klt": 101, "fqt": 1, "beg": beg, "end": end,
        "fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51,f53",
    }).get("data")
    if not data or not data.get("klines"):
        return pd.Series(dtype=float)
    rows = [k.split(",") for k in data["klines"]]
    s = pd.Series({r[0]: float(r[1]) for r in rows})
    s.index = pd.to_datetime(s.index)
    return s.sort_index()


def fetch_pool_closes(codes: list[str], beg: str, end: str,
                      concurrency: int = 8) -> dict[str, pd.Series]:
    """并发抓取一批股票的历史收盘价。返回 {code: close_Series}，失败/退市股缺席。"""
    import concurrent.futures as cf
    out: dict[str, pd.Series] = {}
    with cf.ThreadPoolExecutor(max_workers=concurrency) as ex:
        fut = {ex.submit(fetch_kline_close, c, beg, end): c for c in codes}
        for f in cf.as_completed(fut):
            c = fut[f]
            try:
                s = f.result()
                if not s.empty:
                    out[c] = s
            except Exception:
                continue
    return out


def fetch_top_holders(code: str, size: int = 20) -> pd.DataFrame:
    """十大股东（含是否新进 HOLDER_NEW）。取多期用于比较新增。"""
    data = _get_json(F10_URL, {
        "reportName": "RPT_F10_EH_HOLDERS", "columns": "ALL",
        "filter": f'(SECUCODE="{secu_code(code)}")',
        "pageNumber": 1, "pageSize": size,
        "sortTypes": "-1", "sortColumns": "END_DATE",
        "source": "HSF10", "client": "PC",
    }).get("result")
    rows = (data or {}).get("data") or []
    return pd.DataFrame(rows)