# API 与权限清单

## 本地样例模式

不需要外部 API。项目自带可复现样例数据，可以完整运行因子、回测、风控和模拟交易。

## 真实 A 股模式

### Tinyshare 兼容接口

环境变量：

```dotenv
QUANTDEV_DATA_MODE=real
MARKET_DATA_SDK=tinyshare
TINYSHARE_TOKEN=
TUSHARE_FINANCIAL_VIP=true
```

安装命令：

```bash
make install-tinyshare
```

Tinyshare 通过兼容 SDK 提供下列 Tushare 风格接口。授权码只允许写入本地 `.env` 或
部署密钥系统，不得提交到 Git。

### Tushare Pro

环境变量：`TUSHARE_TOKEN`

已接入：

- `stock_basic`：证券主数据和上市状态
- `trade_cal`：交易日历
- `daily`：未复权日线
- `adj_factor`：复权因子
- `daily_basic`：估值、股本和市值
- `fina_indicator` / `fina_indicator_vip`：财务指标及公告日期
- `index_basic` / `index_weight`：指数定义和历史成分权重

需要能够覆盖所选接口的积分权限。根据 Tushare 官方接口说明，普通财务指标和指数权重
通常要求至少 2000 积分；全市场 `fina_indicator_vip` 通常要求 5000 积分。实际权限以
Tushare 账户页面和接口返回为准。

配置：

```dotenv
TUSHARE_TOKEN=
TUSHARE_FINANCIAL_VIP=false
TUSHARE_CALL_INTERVAL_SECONDS=0.35
TUSHARE_DEFAULT_INDICES=000300.SH,000905.SH,000852.SH
```

Token 或代理授权码请只写入项目根目录 `.env` 或部署密钥系统，不要发在聊天消息或提交
到 GitHub。
首次同步一年日线会按交易日调用行情、复权和每日指标接口，通常需要数分钟；之后会跳过
已完成日期。

同步 API：

- `POST /api/data/tushare/sync`
- `GET /api/data/status`
- `GET /api/data/sync-runs`
- `GET /api/data/sync-runs/{run_id}`
- `GET /api/data/indices`
- `GET /api/data/financials/{symbol}`

当前尚未接入分钟线、历史停复牌、历史 ST 标签和交易所官方每日涨跌停价格。回测中的
ST 与涨跌停约束是基于最新名称和板块规则的近似模型。

## 盘中行情接口

模拟实盘订单默认要求盘中行情：

- `GET /api/live/quotes`：查询当前缓存行情。
- `POST /api/live/quotes`：写入最新价、买一、卖一、时间、来源和状态。

系统会保存最新行情和历史接收记录，并拒绝未来时间、买价高于卖价、价差超过
`QUANTDEV_QUOTE_MAX_SPREAD_BPS`、未知状态或不存在标的。估值只使用同一交易日且未过期
的实时行情，过期后降级到最近日线，不把旧盘中价格冒充当前价格。

业务读接口可配置只读密钥：

```http
X-Read-Key: <只读密钥>
```

管理写接口在配置 `QUANTDEV_ADMIN_API_KEY` 后要求：

```http
X-Admin-Key: <管理员密钥>
```

实盘审批和提交建议使用操作员密钥：

```dotenv
QUANTDEV_OPERATOR_KEYS=alice:<alice-key>,bob:<bob-key>
```

配置后系统按密钥反查操作员身份，不再信任客户端自报的 `X-Operator`。双人复核时，
审批人和提交人必须使用不同操作员密钥。

行情源必须由用户选定的实时数据服务提供。Tinyshare 5k 权益在本项目中主要用于日线、
每日指标、财务和指数数据；是否包含稳定盘中行情能力应以代理服务合同为准，不能由日线
权限推断。

## 策略与 OMS API

- `GET/POST /api/strategies`
- `POST /api/strategies/{strategy_id}/enabled`
- `POST /api/strategies/run`
- `GET /api/strategies/runs`
- `GET /api/strategies/runs/{run_id}`
- `POST /api/strategies/intents/{intent_id}/approve`
- `POST /api/strategies/intents/{intent_id}/reject`
- `POST /api/strategies/intents/{intent_id}/submit`
- `GET /api/paper/account`
- `POST /api/paper/orders`
- `POST /api/paper/orders/{order_id}/cancel`
- `POST /api/paper/match`
- `GET /api/live/orders`
- `POST /api/live/orders/{order_id}/cancel`
- `POST /api/live/sync`
- `POST /api/live/reconcile`

策略运行、行情写入、订单、撤单、Kill Switch、对账和数据同步属于写操作，应通过
管理员或操作员认证并只暴露在本机或受控网络。生产部署建议同时配置只读密钥，避免
研究数据和任务 payload 在公网裸读。

### deep-research-quant

环境变量：

- `DEEP_RESEARCH_BASE_URL`
- `DEEP_RESEARCH_API_KEY`

需要确认：

- 可调用的生产/测试地址
- 鉴权 Header
- 研究任务提交接口
- 流式结果接口
- 行情、公告和新闻工具的使用权限

本项目只会向它开放因子、回测和风险的只读数据，不开放订单、现金或持仓写权限。

## 实盘 Broker

券商 API 需要支持：

- 报单与撤单
- 委托和成交查询
- 资金、持仓和可卖数量
- 交易回报推送
- 模拟环境或独立测试账户
- IP 白名单及密钥轮换

在模拟盘验收前，不需要申请或提供券商密钥。

配置入口：

```dotenv
QUANTDEV_BROKER_MODE=live
QUANTDEV_BROKER_ADAPTER=your_package.qmt:create_broker
QUANTDEV_LIVE_TRADING_ENABLED=true
QUANTDEV_REQUIRE_ORDER_APPROVAL=true
```

`QUANTDEV_BROKER_ADAPTER` 指向一个无参数工厂函数，返回 `BrokerAdapter` 实例。当前
`MockLiveBroker` 会持久化模拟账户、持仓、订单和成交，用于拒单、超时、未知状态、
部分成交和撤单失败测试。它不是实际券商适配器。`live` 模式不会回退到 Mock；真实
适配器必须让 `health_check().mode == "live"`，否则报单和实盘就绪度都会失败关闭。

真实 Broker 默认建议保持：

```dotenv
QUANTDEV_BROKER_DRY_RUN=true
```

dry-run 模式下，账户、持仓、委托和成交查询透传真实适配器，但报单/撤单不会发送到真实
柜台，只产生本地 dry-run 回执。关闭 dry-run 前，就绪度不会放行真实下单阶段。

实盘审批记录包含操作人、理由、请求哈希和有效期。券商正式生产账户仍应在网关或组织
权限层增加登录、RBAC、IP 白名单和密钥轮换。

## 已发现的旧仓库安全事项

`deep-research-quant` 中存在提交到 Git 的 `.env` 和硬编码密钥。继续复用前必须撤销旧
密钥、清理 Git 历史并改用部署密钥系统。
