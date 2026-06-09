# Quant Dev

一个可运行的量化研究、因子评估、策略回测、风险控制和模拟交易工作台。

当前版本提供完整的本地纵向链路：

- A 股研究数据集、运行参数和结果留痕
- Tushare Pro 真实 A 股日线、复权因子、每日估值、财务指标和指数历史成分
- 因子注册、版本和 RankIC/ICIR 评估
- 包含佣金、印花税、滑点和 100 股整数手的回测
- 组合预交易风控
- 幂等模拟委托、成交、现金和持仓账本
- 只读研究 Agent 权限边界
- FastAPI 和零构建 Web 工作台

实盘交易默认从代码层禁用。

## 快速开始

```bash
make install
make bootstrap
make test
make dev
```

打开 [http://127.0.0.1:8000](http://127.0.0.1:8000)。

API 文档位于 [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)。

## 真实 A 股数据

项目支持官方 Tushare SDK 和兼容的 Tinyshare SDK。复制环境变量模板并填写授权信息：

```bash
cp .env.example .env
```

```dotenv
QUANTDEV_DATA_MODE=real
MARKET_DATA_SDK=tinyshare
TINYSHARE_TOKEN=你的授权码
TUSHARE_FINANCIAL_VIP=true
TUSHARE_CALL_INTERVAL_SECONDS=0.15
TUSHARE_DEFAULT_INDICES=000300.SH,000905.SH,000852.SH
```

Tinyshare 首次使用需要单独安装：

```bash
make install-tinyshare
```

使用官方 Tushare 时改为：

```dotenv
MARKET_DATA_SDK=tushare
TUSHARE_TOKEN=你的_token
TUSHARE_FINANCIAL_VIP=false
```

`QUANTDEV_DATA_MODE=real` 会关闭启动时的 demo 数据注入。重启服务后进入“数据中心”，
选择日期和数据模块并创建同步任务。同步在后台执行，页面会
展示任务状态、写入行数与失败原因。已完成的交易日和指数月份会自动跳过，因此日常更新
可以继续使用同一个日期区间。

需要清空现有样本数据并切换到真实库时：

```bash
make reset-real
.venv/bin/python -m quantdev.cli sync-market \
  --start-date 2025-01-02 \
  --end-date 2026-06-08
```

数据落库时执行以下单位标准化：

- 日线成交量：手转为股
- 日线成交额：千元转为元
- 总股本、流通股本：万股转为股
- 总市值、流通市值：万元转为元
- 财务指标保留 Tushare 原始比例单位，并保存原始 JSON

普通财务接口按股票逐个拉取，默认最多同步 100 个指数成分股；账户具备 VIP 财务接口
权限时，将 `TUSHARE_FINANCIAL_VIP=true`，系统会改为按报告期拉取全市场数据。

## 内置样例数据

首次启动会生成固定随机种子的样例快照：

- 8 个 A 股研究标的
- 2,960 条日线
- 2025-01-02 至 2026-06-03
- 4 个基础因子

因此在没有外部 API 的情况下，也能验证整个研究和交易链路。

## 目录

```text
backend/quantdev/
  api.py                 HTTP API 与前端入口
  analytics.py           统计指标
  db.py                  SQLite 研究与账本数据库
  services/
    market.py            数据快照
    factors.py           因子计算与评估
    backtest.py          成本后事件回测
    risk.py              预交易风控
    execution.py         模拟 OMS 与账本
    agent.py             只读研究 Agent
  integrations/
    market_data.py       行情数据适配器
    broker.py            Broker 边界
frontend/                量化工作台
tests/                   服务和 API 测试
docs/                    架构与权限文档
infra/                   容器部署
```

## 外部 API

本地样例链路不要求任何 API。真实 A 股数据需要 `TUSHARE_TOKEN` 或
`TINYSHARE_TOKEN`；复用原
`deep-research-quant` 服务时需要它的只读服务地址和鉴权方式。

详细信息见 [docs/API_AND_PERMISSIONS.md](docs/API_AND_PERMISSIONS.md)。

## 使用文档

- [项目代码讲解](docs/PROJECT_CODE_GUIDE.md)
- [前端操作手册](docs/FRONTEND_OPERATION_GUIDE.md)
- [架构说明](docs/ARCHITECTURE.md)
- [API 与权限清单](docs/API_AND_PERMISSIONS.md)

## 安全约束

- 不允许把 `.env`、API Key、券商密钥提交到 Git。
- Agent 仅依赖因子、回测和风险读取接口。
- Broker 适配器默认使用 `DisabledLiveBroker`。
- 实盘开放前必须增加身份认证、双人审批、Kill Switch 和每日对账。
