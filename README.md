# A股多因子科学选股

一个**可自我验证**的 A 股多因子选股工具：每天自动抓全市场数据，做清洗 → 因子打分 → 排名，
再对综合分靠前的候选做**深度校验**（基本面硬指标 / 成长 / 技术面 / 雷区），输出带信号徽章的
Top 组合报告；并把每天快照归档，随运行积累历史后**自动回测自身因子的有效性**（RankIC / 分位收益）。

- 数据源：东方财富公开接口（无需 token）
- 部署：GitHub Actions 每日定时 + GitHub Pages 网页报告
- 可选：接任意 OpenAI 兼容接口做 **AI 白话解读**
- 仅供研究参考，**不构成投资建议**

---

## 一、它做什么（“三环缺一不可”落地）

```
每天 09:15 定时（工作日）
   │
   ├─ 第一环·好公司（基本面硬指标）
   │     全市场快照 → 清洗股票池（剔除 ST/退市/次新/停牌/成交额过小/当日涨停/资不抵债）
   │     → 4 类因子打分：价值、质量(ROE/毛利/净利同比)、动量、低波动
   │     （默认市值中性化，消除“只因为小盘”的假有效）→ 综合分排名 Top N
   │
   ├─ 第二环·深度校验（对综合分靠前的候选逐只核验）── ★本版本新增★
   │     · 基本面：ROE 连续 3 年达标数、经营现金流/净利润(防纸面利润)、毛利率、
   │               资产负债率、营收/净利同比(成长加分)
   │     · 技术面：5/10/20/60 均线排列、20 日线(回踩不破?)、量能(放量/缩量)、
   │               筹码分布估算(获利盘/单峰双峰/成本均线)、近一年价格分位(估值位置近似)
   │     · 买点参考：**建议买入价 + 止损参考**（以 20 日线与筹码成本均线的较高者为支撑）
   │     · 大基金信号：看最新两季十大股东里是否有 国家队/社保/养老金/公募 等大基金，
   │               新增持股给分数加成（默认 +0.5），持续持有加成小（+0.25）
   │     · 雷区扫描：商誉/净资产、存贷双高、应收高、大股东质押(可选，需 akshare)
   │     → 每只给出“信号徽章”+ 规则建议（优选/等买点/观望/回避·雷区）
   │
   └─ 第三环·自我检验
         每日快照归档 → 回测自身：5 分组收益差 / RankIC / ICIR / 多头占优天数
```

> 你贴的框架被落成规则后，关键词对应：
> **ROE/毛利/现金流/负债/估值** → 第一环与第二环的基本面项
> **行业景气/增速** → 第二环的净利同比、营收同比（成长加分徽章）
> **20日线/量能/筹码分布** → 第二环技术面（定买点，不是选票）
> **商誉/质押/应收/存贷双高/ST/次新** → 第二环雷区 + 第一环清洗
> **分散/分批/止损** → 是纪律，工具管不到，报告里会提示你单票别超 20%、雷区别接

---

## 二、快速跑通

需要 Python 3.10+。用 `uv` 或 `pip`：

```bash
cd ashare-quant
pip install .
python -m ashare_quant run      # 完整跑（含深度校验）
python -m ashare_quant top       # 只看今日 Top 表
python -m ashare_quant history   # 已积累多少天快照
python -m ashare_quant backtest  # 只看自我检验
python -m ashare_quant ai        # 用已有 latest.json 重新生成 AI 解读(需配 key)
python -m ashare_quant feishu    # 把最新结果推送到飞书机器人(需配 FEISHU_WEBHOOK)
python -m ashare_quant nav 600519 # 单股近一年净值
```

产出：

| 文件 | 内容 |
|---|---|
| `reports/report.md` | 文字版（Top 组合 + 深度校验 + 自我检验）|
| `reports/latest.json` | 结构化结果（含深度信号，喂给 AI）|
| `reports/ai.md` | AI 解读（配了 key 才有）|
| `docs/index.html` | 网页版报告（Pages）|
| `data/history/*.csv` | 每日快照归档（回测素材）|

---

## 三、部署到 GitHub（每日自动 + 网页报告）

```bash
cd ashare-quant
git init && git add . && git commit -m "init"
git branch -M main
git remote add origin https://github.com/你的用户名/ashare-quant.git
git push -u origin main
```

- **开 Pages**：Settings → Pages → Source 选 **Deploy from a branch** → `main` / 目录 **`/docs`** → Save
- **手动跑一次**：Actions → `daily-a-share-quant` → **Run workflow**
- 之后每个工作日早上 09:45（北京时间）自动跑，节假日无行情自动跳过

> 隐私说明：选股结果默认**不提交回公开仓库**（不会暴露在仓库/Pages 上），
> 只在运行时生成并直接推送到飞书。若要公开网页报告，把 `daily.yml` 里被注释的
> "提交并推送报告与历史数据" 步骤恢复即可。

### 可选：开启 AI 解读

在仓库 **Settings → Secrets and variables → Actions** 里：

- **Secrets** → 新建 `AI_API_KEY` = 你的接口密钥（可接任意 OpenAI 兼容服务，如 DeepSeek、OpenAI 等）
- **Variables** → 可选：`AI_BASE_URL`（默认 `https://api.openai.com/v1`）、`AI_MODEL`（默认 `gpt-4o-mini`）

不配这些也没关系，工具会跳过 AI 步骤，其余全部照常。

### 可选：每日推送到飞书

在任意飞书群里加一个**自定义机器人**，把它的 Webhook 地址放进 GitHub Actions：

- 飞书里建群 → 群设置 → **群机器人 → 添加机器人 → 自定义机器人** → 复制 Webhook
- GitHub **Settings → Secrets and variables → Actions**：
  - **Secrets** → 新建 `FEISHU_WEBHOOK` = 你复制的 Webhook 地址
  - **Variables** → 可选 `PAGE_URL` = 你的 GitHub Pages 报告网址（如 `https://用户名.github.io/ashare-quant/`），消息里会附上完整报告链接
- 之后每次跑完选股，会自动往群里推卡片：**选股结果表**（排名/代码/名称/行业/现价/今涨跌/60日涨幅/ROE/净利同比/毛利率/PE/PB/流通市值/换手/综合分，默认 30 条）
  + **深度校验表**（代码/名称/建议买入/止损参考/筹码调整/规则建议/信号徽章，徽章已精简）+ AI 解读（若已配置）

本地想手动发：`export FEISHU_WEBHOOK='你的webhook'` 后 `python -m ashare_quant feishu`。
不配 `FEISHU_WEBHOOK` 则完全跳过推送，不影响选股。

---

## 四、配置（环境变量，全部可调）

| 变量 | 默认 | 含义 |
|---|---|---|
| `TOPN` | `50` | 输出前 N 名 |
| `DEEP_TOP` | `50` | 对综合分前 N 名做深度校验（越多越慢） |
| `INCL_BJ` | `false` | 是否包含北交所 |
| `EXCLUDE_INDUSTRY_KW` | `地产,房地产` | 按行业名关键词剔除整块板块（默认剔地产）|
| `MIN_LISTING_DAYS` | `60` | 排除次新（上市不足天数）|
| `EXCLUDE_ST` | `true` | 排除 ST/*ST/退市 |
| `MIN_AMOUNT` | `50000000` | 单日成交额下限(元)，流动性门槛 |
| `MIN_FLOAT_CAP` | `0` | 流通市值下限(元) |
| `EXCLUDE_NEG_EQUITY` | `true` | 排除净资产为负(资不抵债) |
| `EXCLUDE_LIMIT_UP` | `true` | 剔除当日涨停 |
| `SIZE_NEUTRAL` | `true` | 市值中性化（建议保持）|
| `W_VALUE/W_QUALITY/W_MOMENTUM/W_LOWVOL` | `1/1.5/1/0.5` | 四类因子权重（质量 1.5 压制银行低PE/PB占优）|
| `VOL_DAYS` | `60` | 低波动因子回看天数（历史真实波动率）|
| `VOL_CONCURRENCY` | `8` | 波动率并行抓取线程数 |
| `FUND_BONUS_NEW` | `0.5` | 本季大基金新增持股的分数加成 |
| `FUND_BONUS_HOLD` | `0.25` | 持续持大基金的分数加成 |
| `FUND_KEYWORDS` | 社保/养老金/汇金/证金/大基金等 | 判定“大基金”的股东名关键词 |
| `ROE_MIN` | `15` | 深度校验：ROE 连续达标线(%) |
| `CASHFLOW_MIN` | `1` | 经营现金/净利 及格线 |
| `DEBT_MAX` | `65` | 资产负债率警戒(%)(银行/地产除外) |
| `GOODWILL_MAX` | `0.20` | 商誉/净资产 雷区阈值 |
| `PLEDGE_MAX` | `0.3` | 大股东质押率雷区阈值 |
| `INDUSTRY_BONUS_KW` | 半导体/新能源/军工/创新药等 | 命中政策扶持行业的行业关键词（逗号分隔） |
| `INDUSTRY_BONUS` | `0.5` | 命中产业政策时的综合分加成 |
| `FIN_W0/FIN_W1/FIN_W2` | `0.6/0.2/0.2` | 财务三期加权（最新/前一/再前），建议和为 1 |
| `AI_BASE_URL/AI_API_KEY/AI_MODEL` | 见上 | AI 解读 |

在 GitHub Actions 里改 `.github/workflows/daily.yml` 的 `env:` 段即可。

---

## 五、方法学与已知局限

**清洗**：ST/退市、次新(<60天)、停牌、成交额过小、当日涨停、资不抵债——先把“买不进/不靠谱”的踢掉。

**因子**：每个因子缩尾(1%/99%) → 截面 z-score → 市值中性化(按市值分 5 桶桶内去均值)。
综合分 = w·价值 + w·质量 + w·动量 + w·低波动 + 产业政策加成。

**深度校验规则建议**（透明，不是荐股）：
- 雷区触发（商誉超标 / 存贷双高 / 质押超标）→ `回避·雷区`
- 质量达标（ROE 近 3 年≥2 年 且 现金流≥1）且 趋势好 → `优选·可分批观察` / `等回踩` / `等买点`
- 否则 → `一般·观望`
- 大基金（国家队/社保/养老金/公募）本季新增持股 → 分数 +0.5，持续持有 → +0.25（会推高排名）
- 每只给出 **建议买入价**（20日线与 60 日主力筹码成本均线的较高者）与 **止损参考**（支撑下方 5%）

**自我检验**：用后一天收盘/当天收盘−1 近似次日收益，把股票按综合分分 5 组比较最高-最低收益差、
RankIC、ICIR。**积累 60 个交易日后才有统计意义**。

**已知局限（请读）**：
- 财务基本面指标已改为**最近三期加权**（默认 0.6/0.2/0.2）；未做分红除权复权，回测收益是近似
- 行业中性化未做：价值+质量天然对银行/白酒友好，已通过提高质量权重(W_QUALITY=1.5)、剔除地产板块缓解，但银行的 `高负债⚠` 仍是结构性误报，需自行在心里剔除银行板块
- “低波动”已改为历史 60 日真实波动率（年化），缺历史数据的个股回退到当日振幅代理
- 筹码分布是**估算**（基于历史换手率衰减，非精确数据）；主力对倒/锁仓会失真，别当主力底牌
- 大股东质押依赖 akshare：`pip install akshare` 后才生效，否则该项自动跳过
- 数据来自东方财富公开接口，偶有缺失/延迟

---

## 六、项目结构

```
ashare-quant/
├─ .github/workflows/daily.yml   # 每日自动运行 + 推送
├─ src/ashare_quant/
│  ├─ config.py    # 配置（环境变量覆盖）
│  ├─ fetch.py     # 东方财富数据（快照/K线/财务）
│  ├─ universe.py  # 股票池清洗
│  ├─ factors.py   # 因子计算（缩尾/标准化/市值中性）
│  ├─ deep.py      # 深度校验（基本面/技术面/雷区）★
│  ├─ ai.py        # 可选 AI 解读（OpenAI 兼容）★
│  ├─ feishu.py    # 可选 飞书机器人推送 ★
│  ├─ history.py   # 每日快照归档
│  ├─ backtest.py  # 自我检验（IC/分位）
│  ├─ report.py    # Markdown + HTML
│  └─ cli.py       # 命令行入口
├─ tests/          # 单元测试（合成数据，不联网）
└─ README.md
```

## 七、测试

```bash
pip install pytest
python -m pytest tests/ -q
```

全部用合成数据，不依赖网络。

---

MIT License © 2026 · 仅供学习研究，投资有风险，入市需谨慎。