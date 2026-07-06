# 模拟实盘与真实实盘操作手册

本文档对应 2026 年 6 月 13 日。执行顺序不可跳级。

## 0. 先处理密钥

聊天消息中出现过的 Tushare Token 和 Tinyshare 授权码应视为已暴露。正式模拟实盘前：

1. 在服务商后台撤销旧授权码并生成新码。
2. 只把新码写入本机 `.env` 或部署密钥系统。
3. 确认 `.env` 被 `.gitignore` 排除。
4. 生成独立只读密钥、管理员密钥和两把操作员密钥：

```bash
openssl rand -hex 32
```

不要在聊天、截图、日志或 Git 中再次粘贴任何真实密钥。

## 1. 数据恢复到最新

当前本地活动快照结束于 `2026-06-10`。2026 年 6 月 13 日是周六，最近完整交易日为
`2026-06-12`，开始模拟盘前先补齐 6 月 11 日和 6 月 12 日：

```bash
cd /Users/bytedance/Documents/量化交易架构
.venv/bin/python -m quantdev.cli sync-market \
  --start-date 2026-06-11 \
  --end-date 2026-06-12
```

然后检查：

```bash
curl -sS http://127.0.0.1:8000/api/data/status
curl -sS http://127.0.0.1:8000/api/live/market-clock
curl -sS http://127.0.0.1:8000/api/live/readiness
```

若当天尚未收盘，日线最新日期通常应等于最近一个已完整收盘的交易日，不要求提前出现
当天收盘数据；交易日历必须覆盖当天。

## 2. 配置自动模拟盘

`.env` 使用以下基线：

```dotenv
QUANTDEV_ENV=production
QUANTDEV_DATA_MODE=real
QUANTDEV_BROKER_MODE=paper
QUANTDEV_LIVE_TRADING_ENABLED=false
QUANTDEV_REQUIRE_ORDER_APPROVAL=true
QUANTDEV_APPROVAL_TTL_SECONDS=300
QUANTDEV_MAX_LIVE_ORDER_NOTIONAL=5000
QUANTDEV_MAX_DAILY_LOSS=1000
QUANTDEV_READ_API_KEY=上一步生成的只读随机值
QUANTDEV_PAPER_ENFORCE_SESSION=true
QUANTDEV_PAPER_QUOTE_MODE=realtime
QUANTDEV_QUOTE_MAX_AGE_SECONDS=15
QUANTDEV_QUOTE_MAX_SPREAD_BPS=100
QUANTDEV_CALENDAR_FAIL_CLOSED=true
QUANTDEV_AUTO_KILL_ON_RECON_BREAK=true
QUANTDEV_RUNNER_POLL_SECONDS=5
QUANTDEV_RUNNER_LEASE_SECONDS=60
QUANTDEV_RECONCILIATION_MAX_AGE_MINUTES=1440
QUANTDEV_ADMIN_API_KEY=上一步生成的随机值
QUANTDEV_OPERATOR_KEYS=alice:操作员A随机值,bob:操作员B随机值
QUANTDEV_BROKER_DRY_RUN=true
```

迁移并启动：

```bash
.venv/bin/python -m quantdev.cli migrate
make dev
```

## 3. 接入盘中行情

模拟实盘默认不会使用过期日线冒充实时价格。行情进程应持续调用：

```http
POST /api/live/quotes
X-Admin-Key: <管理员密钥>
Content-Type: application/json
```

请求体示例：

```json
{
  "symbol": "600519.SH",
  "price": 1500.0,
  "bid": 1499.9,
  "ask": 1500.1,
  "quote_time": "2026-06-10T10:15:00+08:00",
  "source": "your-realtime-feed",
  "status": "tradable"
}
```

行情源至少覆盖策略候选股票和当前持仓。超过 15 秒、状态非 `tradable`、时间在未来或
缺失的行情不会成交。若暂时没有盘中行情，只能将 `QUANTDEV_PAPER_QUOTE_MODE=eod`
用于收盘后链路验证，不应把它称为盘中模拟实盘。

## 4. 创建并手动验证策略

1. 打开“策略运行”。
2. 保存只读密钥和管理员/操作员密钥。
3. 创建策略时保持“停用”。
4. 单票目标金额先设为 1,000 至 5,000 元。
5. 点击“运行”，检查目标、订单意图、订单和持仓。
6. 同一交易日再次点击，确认返回原 `run_id` 且没有重复成交。
7. 制造一个未触及限价单，确认现金/持仓被冻结，撤单后释放。
8. 当日买入后尝试卖出，确认 T+1 拒单。

## 5. 启动连续模拟实盘

策略人工验证通过后启用策略，再启动独立运行器：

```bash
make paper-runner
```

Docker：

```bash
docker compose --profile paper-live up -d
```

每日检查：

```bash
curl -sS http://127.0.0.1:8000/api/paper/account
curl -sS http://127.0.0.1:8000/api/strategies/runs
curl -sS -X POST http://127.0.0.1:8000/api/live/reconcile \
  -H "X-Admin-Key: $QUANTDEV_ADMIN_API_KEY"
```

连续运行至少 20 个交易日。期间必须覆盖正常成交、挂单、撤单、T+1、行情中断、进程
重启、日亏损熔断和恢复。

## 6. 运行 Broker 异常压测

```bash
make verify-broker
```

命令会实际运行异常测试并把结果写入 `verification_runs`。就绪度只接受 30 天内的
`PASSED` 记录，单纯存在测试文件不算通过。

覆盖场景：

- 报单超时后保留 `UNKNOWN`，按 `client_order_id` 查询。
- 券商拒单。
- 部分成交。
- 状态未知。
- 撤单失败和撤单超时。
- 已成交终态不可撤销。
- 服务重建后仍能查询订单、成交、现金和持仓。

## 7. 实现真实 Broker 适配器

在选定券商后，新增实现类并完整实现 `BrokerAdapter`：

```text
submit_order
cancel_order
get_account
get_positions
get_orders
get_trades
query_order
query_order_by_client_id
health_check
```

还必须完成：

1. 客户端委托号到券商委托号的持久化映射。
2. 超时后先查询，禁止直接重发。
3. 成交回报推送与定时查询双通道。
4. 部分成交、废单、撤单中、部成部撤等券商状态映射。
5. 券商可卖数量、费用和交易所拒单原因保存。
6. 测试环境契约测试和断网恢复测试。
7. `health_check().mode` 返回 `live`；否则就绪度不会放行。

适配器通过工厂函数加载：

```dotenv
QUANTDEV_BROKER_ADAPTER=your_package.qmt:create_broker
```

工厂函数不接收参数，返回 `BrokerAdapter` 实例。系统启动时不会因为
`QUANTDEV_BROKER_MODE=live` 自动选择 Mock Broker；适配器为空、类型错误、健康检查失败
或返回非 `live` 模式都会阻断实盘。

未完成这一节前，即使设置 `QUANTDEV_BROKER_MODE=live`，也不代表已经接入真实券商。

## 8. 半自动真实盘

使用券商测试环境或独立小资金账户，配置：

```dotenv
QUANTDEV_BROKER_MODE=live
QUANTDEV_BROKER_ADAPTER=your_package.qmt:create_broker
QUANTDEV_LIVE_TRADING_ENABLED=true
QUANTDEV_BROKER_DRY_RUN=true
QUANTDEV_REQUIRE_ORDER_APPROVAL=true
QUANTDEV_APPROVAL_TTL_SECONDS=300
QUANTDEV_MAX_LIVE_ORDER_NOTIONAL=1000
QUANTDEV_MAX_DAILY_LOSS=200
```

先启动 dry-run live runner：

```bash
make live-runner
```

Docker：

```bash
docker compose -f compose.prod.yml --profile live up -d live-runner
```

dry-run 阶段只验证真实账户查询、策略意图、审批、对账和告警；报单/撤单不会发往真实柜台。
`/api/live/readiness` 会在 `QUANTDEV_BROKER_DRY_RUN=true` 时阻断真实下单就绪阶段。

操作规则：

1. 打开“策略运行”，运行停用策略并点击对应运行记录的“查看意图”。
2. 人工核对标的、方向、数量、限价、行情时间和当日累计风险。
3. 点击“批准”；系统保存操作人、理由、请求哈希和过期时间。
4. 在审批有效期内点击“提交”。提交前系统会重新执行全部硬风控。
5. 盘中定时调用 `POST /api/live/sync`，同步委托、成交、资金和持仓。
6. 对未完成订单可在意图面板点击“撤单”，或调用实盘撤单 API。
7. 收盘后调用 `POST /api/live/reconcile` 完成现金、持仓、订单和成交对账。
8. 任何对账差异、行情中断、未知订单或连续拒单立即熔断。

对应 API：

```text
POST /api/strategies/intents/{intent_id}/approve
POST /api/strategies/intents/{intent_id}/reject
POST /api/strategies/intents/{intent_id}/submit
POST /api/live/orders/{order_id}/cancel
POST /api/live/sync
POST /api/live/reconcile
```

所有写操作都必须携带 `X-Admin-Key` 或操作员密钥。配置 `QUANTDEV_OPERATOR_KEYS` 后，
审批人与提交人身份由密钥反查绑定，前端自报的 `X-Operator` 不再作为可信身份来源。
审批后若数量、方向、价格等请求内容发生变化，原审批哈希自动失效，必须重新审批。

dry-run 稳定后，且 readiness 除 dry-run 项外全部通过，再把
`QUANTDEV_BROKER_DRY_RUN=false`，重启 `app` 与 `live-runner`，进入半自动真实下单。

至少稳定运行 20 个交易日，再评估是否进入自动审批。

## 9. 小资金自动实盘

只有 `/api/live/readiness` 全部通过后才允许：

1. 单笔限额保持不超过 10,000 元，建议从 1,000 元开始。
2. 日亏损上限不超过可承受净值的 0.2% 至 0.5%。
3. 首周只运行一个策略、一个账户和一个 Broker。
4. 每日开盘前检查数据、日历、行情源、Broker 健康和 Kill Switch。
5. 午间和收盘后对账，保留人工一键熔断渠道。
6. 不在未通知的情况下自动解除 Kill Switch。

## 10. 紧急停止

前端点击“立即熔断”，或调用：

```bash
curl -sS -X POST http://127.0.0.1:8000/api/live/kill-switch \
  -H "Content-Type: application/json" \
  -H "X-Admin-Key: $QUANTDEV_ADMIN_API_KEY" \
  -d '{"active":true,"reason":"人工紧急停止"}'
```

熔断后：

1. 停止策略运行器。
2. 查询 Broker 端全部未完成订单。
3. 按券商状态决定是否撤单，不假设超时等于失败。
4. 执行对账并保存差异。
5. 查明原因、人工确认后再解除。
