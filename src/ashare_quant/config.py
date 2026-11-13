"""A股多因子科学选股 —— 配置。所有参数都可被环境变量覆盖。"""

from __future__ import annotations

import os


def _env(key: str, default):
    v = os.environ.get(key)
    if v is None or v == "":
        return default
    if isinstance(default, bool):
        return v.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(float(v))
    if isinstance(default, float):
        return float(v)
    return v


class Config:
    def __init__(self) -> None:
        # ---- 股票池范围 ----
        self.incl_bj: bool = _env("INCL_BJ", False)      # 是否包含北交所（默认排除，流动性差）
        self.min_listing_days: int = _env("MIN_LISTING_DAYS", 60)   # 排除上市不足 N 天（次新）
        self.exclude_st: bool = _env("EXCLUDE_ST", True) # 排除 ST / *ST / 退市
        self.min_amount: float = _env("MIN_AMOUNT", 5e7) # 单日成交额下限(元)，流动性门槛，默认 5000 万
        self.min_float_cap: float = _env("MIN_FLOAT_CAP", 0.0)      # 流通市值下限(元)，默认不限
        self.exclude_negative_equity: bool = _env("EXCLUDE_NEG_EQUITY", True)  # 排除净资产为负(资不抵债)：其 ROE/PE 无意义
        # 按行业关键词剔除整块板块（如地产）。逗号分隔，命中行业名即剔除。
        self.exclude_industry_kw: list[str] = [k.strip() for k in
            os.environ.get("EXCLUDE_INDUSTRY_KW", "地产,房地产").split(",") if k.strip()]

        # ---- 选股 ----
        self.top_n: int = _env("TOPN", 50)               # 输出前 N 名
        self.coarse_top: int = _env("COARSE_TOP", 100)   # 降级路径粗筛目标：东财失败时先粗筛 N 只，再 baostock 补齐基本面后精筛出 top_n
        self.size_neutral: bool = _env("SIZE_NEUTRAL", True)  # 市值中性化：在市值分位内去均值，消除大小盘偏差
        self.size_buckets: int = _env("SIZE_BUCKETS", 5) # 市值分位数量

        # 因子权重（价值 / 质量 / 动量 / 低估值质量）
        # 质量权重默认 1.5：拉高对高 ROE/高毛利公司的偏好，削弱银行“低 PE/PB 天然占优”的影响
        self.w_value: float = _env("W_VALUE", 1.0)
        self.w_quality: float = _env("W_QUALITY", 1.5)
        self.w_momentum: float = _env("W_MOMENTUM", 1.0)
        self.w_lowvol: float = _env("W_LOWVOL", 0.5)     # 低波动（历史60日真实波动率，默认权重较小）

        # 剔除涨停板（当日无法真实买入，避免“看得见买不进”）
        self.exclude_limit_up: bool = _env("EXCLUDE_LIMIT_UP", True)

        # ---- 深度校验（基本面/成长/技术面/雷区）----
        self.deep_top: int = _env("DEEP_TOP", 50)      # 对综合分前 N 名做深度校验
        self.roe_min: float = _env("ROE_MIN", 15.0)    # 连续3年ROE及格线(%)
        self.cashflow_min: float = _env("CASHFLOW_MIN", 1.0)  # 经营现金/净利 及格线
        self.debt_max: float = _env("DEBT_MAX", 65.0)  # 资产负债率警戒(%)(银行/地产除外)
        self.goodwill_max: float = _env("GOODWILL_MAX", 0.20)  # 商誉/净资产 雷区阈值
        self.pledge_max: float = _env("PLEDGE_MAX", 0.3)       # 大股东质押率雷区阈值

        # ---- 历史60日真实波动率（低波动因子数据源）----
        self.vol_days: int = _env("VOL_DAYS", 60)      # 波动率回看天数
        self.vol_concurrency: int = _env("VOL_CONCURRENCY", 8)  # 并行抓取线程数
        self.vol_annualize: float = _env("VOL_ANNUALIZE", 252.0)  # 年化因子

        # ---- 大基金（国家队/社保/公募）新增持股加成 ----
        self.fund_bonus_new: float = _env("FUND_BONUS_NEW", 0.5)   # 本季新增持大基金 → 分数加成
        self.fund_bonus_hold: float = _env("FUND_BONUS_HOLD", 0.25)  # 持续持有大基金 → 分数加成
        self.fund_keywords: list[str] = [k.strip() for k in
            os.environ.get("FUND_KEYWORDS",
                "社保基金,全国社保,基本养老保险,养老金,中央汇金,证金,中国证券金融,"
                "国家集成电路产业投资基金,大基金,中国国有企业结构调整基金").split(",") if k.strip()]

        # ---- 筹码位置（追高风险 / 主力浮盈机会）----
        # 核心逻辑：价格远高于筹码成本线 或 获利盘过高 → 多数持仓者浮盈巨大 → 接盘风险(减分)；
        #           基本面好且价格贴近成本线(主力浮盈小/浮亏) → 抛压小、机会大(加分)。
        self.chip_pen_dev: float = _env("CHIP_PEN_DEV", 0.20)      # 价/成本线 超过此值开始追高惩罚
        self.chip_pen_profit: float = _env("CHIP_PEN_PROFIT", 0.85)  # 获利盘 超过此比例开始接盘惩罚
        self.chip_bonus_dev: float = _env("CHIP_BONUS_DEV", 0.10)  # 价格贴近成本线 ±此范围内算"主力浮盈小"
        self.chip_bonus: float = _env("CHIP_BONUS", 0.6)           # 贴成本+基本面好 → 加分
        self.chip_bonus_dip: float = _env("CHIP_BONUS_DIP", 0.4)   # 主力明显浮亏+基本面好 → 加分(超跌机会)

        # ---- 行业 / 产业政策因子 ----
        # 命中国家规划/政策扶持的行业关键词时，给综合分加成（行业贝塔之外的产业逻辑）。
        # 名单可在环境变量 INDUSTRY_BONUS_KW 里改（逗号分隔，按行业名子串匹配）。
        self.industry_bonus_kw: list[str] = [k.strip() for k in
            os.environ.get("INDUSTRY_BONUS_KW",
                "半导体,芯片,集成电路,人工智能,算力,光模块,PCB,机器人,高端装备,新能源,光伏,储能,"
                "锂电池,钠电池,风电,电网,特高压,军工,国防,航天,卫星,低空经济,商业航天,"
                "创新药,医疗器械,生物科技,信创,国产软件,操作系统,数据要素,云计算,网络安全,"
                "量子科技,工业软件,减速器,一体化压铸,固态电池,氢能源,碳纤维,稀土,算力服务器").split(",")
            if k.strip()]
        self.industry_bonus: float = _env("INDUSTRY_BONUS", 0.5)   # 命中产业政策时的综合分加成

        # ---- 财务三期加权（基本面深度校验口径） ----
        # 取最近 3 个报告期，按 w0(最新)/w1/w2 加权，权重可调，建议和为 1.0。
        self.fin_w: tuple[float, float, float] = (
            _env("FIN_W0", 0.6), _env("FIN_W1", 0.2), _env("FIN_W2", 0.2))

        # ---- AI 解读（可选，OpenAI 兼容接口）----
        self.ai_base_url: str = os.environ.get("AI_BASE_URL", "https://api.openai.com/v1")
        self.ai_api_key: str = os.environ.get("AI_API_KEY", "")
        self.ai_model: str = os.environ.get("AI_MODEL", "gpt-4o-mini")
        self.ai_timeout: int = _env("AI_TIMEOUT", 90)
        # 多角色 AI（借鉴 TradingAgents 的“独立分析师→交易员辩论→风控→组合经理”结构）：
        # 开=多轮角色扮演生成更审慎的解读；关=单次总解读
        self.ai_multi_role: bool = _env("AI_MULTI_ROLE", True)

        # ---- 回测 / 历史 ----
        self.history_dir: str = os.path.join(".", "data", "history")
        self.report_dir: str = os.path.join(".", "reports")
        self.docs_dir: str = os.path.join(".", "docs")

    def snapshot_fields(self) -> list[str]:
        return [
            "f2", "f3", "f6", "f7", "f8", "f9", "f10",
            "f12", "f14", "f18", "f20", "f21", "f23", "f24",
            "f25", "f26", "f37", "f40", "f45", "f46", "f49",
            "f50", "f100",
        ]

    def market_fs(self) -> str:
        """东财 clist 的市场筛选串：深主板+创业板+沪主板+科创板（可选北交所）。"""
        base = ["m:0+t:6", "m:0+t:80", "m:1+t:2", "m:1+t:23"]
        if self.incl_bj:
            base.append("m:0+t:81+s:2048")
        return ",".join(base)


config = Config()