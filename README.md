# A股多因子科学选股

一个**可自我验证**的 A 股多因子选股工具：每个交易日自动抓全市场数据，
清洗 → 因子打分 → 排名，再对靠前的候选做**深度校验**（基本面 / 技术面 / 雷区），
输出带信号徽章的 Top 组合报告；每日快照归档，积累历史后**自动回测自身因子有效性**。

- 数据源：东方财富公开接口（无需 token），被限流时自动回退新浪源
- 部署：GitHub Actions 每日定时
- 可选：AI 白话解读（任意 OpenAI 兼容接口）、飞书群推送
- 仅供研究参考，**不构成投资建议**

---

## 特色

**三环流水线**

1. **好公司**：清洗股票池（剔除 ST/退市/次新/停牌/流动性差/当日涨停/资不抵债），
   按 价值、质量、动量、低波动 四类因子打分（默认市值中性化，消除"只因为小盘"的假有效），综合分排名。
2. **深度校验**：对综合分靠前的候选逐只核验——
   - 基本面：ROE 连续达标、经营现金流/净利、毛利率、负债率、营收/净利同比
   - 技术面：均线排列、20 日线、量能、筹码分布估算（获利盘/成本均线）、近一年价格分位
   - 雷区：商誉/净资产、存贷双高、应收高、大股东质押
   - 每只给出**信号徽章 + 规则建议**（优选 / 等买点 / 观望 / 回避·雷区），并附建议买入价与止损参考
3. **自我检验**：每日快照归档后自动回测（RankIC / 分位收益差），诚实展示这套因子当前到底有没有效，而不是拍胸脯承诺收益。

**透明规则，不是荐股**：所有徽章与建议都来自写死的可查规则，可随时在代码里核对。

---

## 快速开始

需要 Python 3.10+：

```bash
cd ashare-quant
pip install .
python -m ashare_quant run        # 完整跑（含深度校验）
python -m ashare_quant top        # 只看今日 Top 表
python -m ashare_quant backtest   # 只看自我检验
```

产出：`reports/report.md`（文字报告）、`reports/latest.json`（结构化结果）、
`docs/index.html`（网页版）、`data/history/*.csv`（每日快照，回测素材）。

## 部署到 GitHub（每日自动 + 网页报告）

```bash
git init && git add . && git commit -m "init"
git branch -M main
git remote add origin https://github.com/你的用户名/ashare-quant.git
git push -u origin main
```

- **开 Pages**：Settings → Pages → Source 选 Deploy from a branch → `main` / `/docs`
- **手动跑一次**：Actions → `daily-a-share-quant` → Run workflow
- 之后每个工作日开盘后自动跑，节假日无行情自动跳过

### 可选开关（都可在仓库 Secrets/Variables 里配，不配就自动跳过）

| 功能 | 配置 |
|---|---|
| AI 白话解读 | Secrets 加 `AI_API_KEY`（可接任意 OpenAI 兼容服务），可选 `AI_BASE_URL` / `AI_MODEL` |
| 飞书群推送 | 群里加自定义机器人，Secrets 加 `FEISHU_WEBHOOK`，可选 `PAGE_URL`（消息里附报告链接）|

选股参数（Top N、因子权重、清洗门槛、雷区阈值等）都支持环境变量覆盖，
默认值在 `src/ashare_quant/config.py`，改 GitHub Actions 的话编辑 `.github/workflows/daily.yml` 的 `env:` 段。

---

## 使用提醒

- 数据来自公开接口，偶有缺失/延迟；**9:30 开盘前快照无效**，定时任务请安排在开盘后
- 筹码分布是**估算**（基于换手率衰减，非精确数据），主力对倒/锁仓会失真，别当主力底牌
- 已知偏误：价值+质量因子天然对银行/白酒友好，银行板块的"高负债⚠"是结构性误报，需自行心里剔除
- 大股东质押检查依赖 akshare（`pip install akshare`），没装则自动跳过
- 自我检验需**积累约 60 个交易日**后才有统计意义
- 工具管不到交易纪律：分散、分批、止损靠你自己，报告里只提示单票仓位上限
- 所有"建议买入价/止损参考"均为技术面参考值，**不是买卖指令**

---

MIT License © 2026 · 仅供学习研究，投资有风险，入市需谨慎。
