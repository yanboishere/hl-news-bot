# hl-news-bot

Hyperliquid 永续合约新闻交易 bot 的可运行骨架（Python 3.10+）。默认 **paper 模式**：接真实新闻流和真实 Hyperliquid 价格，但不下单，在本地按盘口模拟成交、收手续费、记 SQLite。

> 设计文档：[`docs/HL-news-bot-design.md`](docs/HL-news-bot-design.md)。本 README 只讲怎么跑。

## 安装

```bash
cd hl-news-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# 可选：pip install telethon anthropic
```

## 跑法

```bash
# 1. 回放：把 data/replay.jsonl 里 14 条历史标题灌进完整管线（分类→风控→paper 成交→落库）
python -m hlnews.run --replay data/replay.jsonl --duration 15

# 2. paper 实盘：Tree of Alpha 免费 WS + Hyperliquid 新上币探测 + BLS 宏观数据定时轮询
python -m hlnews.run

# 3. 看汇总
python -m hlnews.run --summary

# 4. 事件回测：按 config.yaml 的模板参数，用 Binance 1 分钟 K 线跑历史事件
python -m hlnews.backtest --events data/events.jsonl --delay 20
python -m hlnews.backtest --events data/events.jsonl --sweep        # 入场延迟 5s→300s 扫描
```

## 目录

```
hlnews/
  models.py    数据类型：NewsItem / Classification / Signal / Position
  feeds.py     新闻源：Tree of Alpha WS、HL 新上币 meta diff、Telegram（Telethon，可选）、JSONL 回放
  macro.py     宏观：BLS 日程解析、ForexFactory 共识、发布秒级轮询 bls.gov 并解析 CPI/NFP 数字
  classify.py  双车道分类：规则快车道（正则/数字解析）+ LLM 慢车道（可选）
  engine.py    主循环：去重→分类→融合→二源佐证→风控→执行→持仓管理（止损/止盈/时间止损）
  risk.py      风控闸：置信度、日/周亏损熔断、冷却、仓位=风险预算÷止损距离、成本/止损比
  hl.py        Hyperliquid 客户端：行情、盘口滑点估计、paper 成交、live 下单（IOC+交易所侧止损）
  store.py     SQLite：news / classifications / signals / decisions / positions / equity
  backtest.py  事件回测器
  run.py       入口
config.yaml    全部参数
data/replay.jsonl   14 条回放标题（含假新闻+辟谣对、谣言、噪音）
data/events.jsonl   45 个历史事件（16 次 CPI 意外 × BTC/ETH + 13 个非计划冲击）
```

## 切到 live 之前

1. 在 app.hyperliquid.xyz/API 创建 **API wallet**（只能签单，不能提币），导出私钥。
2. `export HL_AGENT_KEY=0x...`（API wallet 私钥）`export HL_ACCOUNT=0x...`（主账户地址）。
3. `config.yaml` 里 `mode: live`，`account.equity_usd` 会被链上权益覆盖。
4. 先在 testnet 跑：`hyperliquid.network: testnet`（水龙头要求同地址在主网存过款；testnet 上 xyz 的资产 ID 和主网不同，代码按名字解析所以不受影响）。
5. 一个进程一个 API wallet。SDK 用毫秒时间戳当 nonce，两个进程共用一个 key 会撞 nonce。

## 已验证（2026-10-01）

- Tree of Alpha 免费 WS 可连，推送 Twitter / Truth Social `type: direct` 消息；REST 历史 ~370 条/天。
- HL `allMids` RTT 0.3–0.5 s（深圳经代理）；BTC 盘口 ±0.1% 深度约 $20M，$1k–$10k 单滑点 <1 bp；xyz:TSLA ±10 bps 深度约 $28 万/边。
- BLS 页面解析：CPI YoY 3.4 / 核心 2.4、NFP +162k，与 2026-09 发布一致；日程解析出 10-02 NFP、10-14 CPI。
- 回放 14 条：Bybit 黑客→做空 ETH，CPI 热→做空 BTC；假 SEC 推文被"辟谣反转"规则拦下；谣言被 is_rumor 拦下；特朗普/关税类因"需二源佐证"挂起。
- 规则快车道跑过去一周 2597 条真实新闻：0 条触发下单，49 条进入待复核（规则置信上限：可信来源 0.80、其余 0.60；ETF/监管/脱锚类门槛 0.85 需 LLM 车道）。
