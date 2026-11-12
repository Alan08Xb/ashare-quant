"""数据抓取：东方财富公开接口（无需 token）。"""

from __future__ import annotations

import time
from typing import Any

import pandas as pd
import requests

# 全市场快照（clist）：一次调用可拉全市场所有字段
CLIST_URL = "https://push2.eastmoney.com/api/qt/clist/get"
# 镜像（延迟行情）：主站突发 502 时的回退，盘后收市价一致
CLIST_URL_ALT = "https://push2delay.eastmoney.com/api/qt/clist/get"
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


def _get_json(url: str, params: dict, retries: int = 7,
              alt_url: str | None = None) -> dict[str, Any]:
    """带退避重试的 GET。东财 push2 在连续翻页时经常返回瞬时 502/429，
    必须对 5xx/429 用更长的指数退避才撑得过去；主站反复失败时回退到镜像 alt_url。"""
    last: Exception | None = None
    candidates = [url] + ([alt_url] if alt_url and alt_url != url else [])
    for base in candidates:
        for i in range(retries):
            try:
                r = requests.get(base, params=params,
                                 headers={"User-Agent": UA}, timeout=25)
                r.raise_for_status()
                return r.json()
            except requests.exceptions.HTTPError as e:
                last = e
                # 5xx / 429 = 服务端瞬时故障，退避更长；其它 4xx 别狂试
                if e.response is not None and 500 <= e.response.status_code <= 599:
                    wait = min(2.0 ** (i + 1), 15.0)   # 2,4,8,15,15...
                else:
                    wait = 1.0
            except requests.exceptions.RequestException as e:
                last = e
                wait = 1.5 * (i + 1)
            if i == retries - 1:
                break
            time.sleep(wait)
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