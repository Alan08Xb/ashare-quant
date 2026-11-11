import datetime as dt
from ashare_quant.fetch import fetch_kline, fetch_snapshot
from ashare_quant.config import Config

cfg = Config()
snap = fetch_snapshot(cfg.market_fs(), cfg.snapshot_fields())
row = snap[snap["code"].astype(str) == "601123"]
print("快照行:")
print(row[["code", "name", "industry", "price", "pct", "ret_60", "ret_ytd", "ipo_date", "mktcap"]].to_string(index=False))

k = fetch_kline("601123", "20250101", dt.date.today().strftime("%Y%m%d"))
print("\nK线行数:", len(k), "首日:", k["date"].iloc[0].date(), "末日:", k["date"].iloc[-1].date())
c = k["close"]
n = len(c)
print("上市以来涨幅: %+.1f%%" % ((c.iloc[-1] / c.iloc[0] - 1) * 100))
print("上市以来最高:", round(c.max(), 2), "最低:", round(c.min(), 2))
print("距最高回撤: %+.1f%%" % ((c.iloc[-1] / c.max() - 1) * 100))
if n > 61:
    print("真实60日涨幅: %+.1f%%" % ((c.iloc[-1] / c.iloc[-61] - 1) * 100))
else:
    print("真实60日涨幅: 不可算（只有%d根K线）" % n)
print("20日涨幅: %+.1f%%" % ((c.iloc[-1] / c.iloc[-21] - 1) * 100))
print("近一年价格分位:", round((c.iloc[-1] - c.min()) / (c.max() - c.min()), 3))
