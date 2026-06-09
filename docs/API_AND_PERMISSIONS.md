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

## 实盘前才需要

券商 API 需要支持：

- 报单与撤单
- 委托和成交查询
- 资金、持仓和可卖数量
- 交易回报推送
- 模拟环境或独立测试账户
- IP 白名单及密钥轮换

在模拟盘验收前，不需要申请或提供券商密钥。

## 已发现的旧仓库安全事项

`deep-research-quant` 中存在提交到 Git 的 `.env` 和硬编码密钥。继续复用前必须撤销旧
密钥、清理 Git 历史并改用部署密钥系统。
