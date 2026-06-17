# 实盘上线配置 Checklist

> 面向"接真实券商、上小资金"的运维清单。按阶段逐项确认；**每一项都必须实际验证过**，
> 而不是"配了就算"。打勾前请亲自跑一遍对应的验证动作。

本清单对应本轮"实盘安全加固"后的代码。配置项默认值见 `.env.example` 与 `compose.prod.yml`。

---

## 阶段 0 — 密钥与访问控制（公网暴露，最高优先级）

- [ ] **轮换并妥善保管所有密钥**：`QUANTDEV_ADMIN_API_KEY`、券商 `BROKER_API_KEY/SECRET`、
      `TUSHARE_TOKEN`、`POSTGRES_PASSWORD`、`REDIS_PASSWORD`。确认 `.env` 未入库
      （`git ls-files | grep env` 只应有 `.env.example`）。
- [ ] **双人复核身份**：配置 `QUANTDEV_OPERATOR_KEYS="alice:<keyA>,bob:<keyB>"`（每人一把独立
      密钥），并 `QUANTDEV_REQUIRE_DUAL_APPROVAL=true`。配置后身份由密钥反查派生，`X-Operator`
      头被忽略——审批人与提交人必须用**不同密钥**，否则 submit 被拒。
      - 验证：alice 的 key 审批 → alice 的 key 提交应 **409 双人复核**；bob 的 key 提交应放行。
- [ ] **网络第一道闸**：`QUANTDEV_ALLOWED_IPS` 收紧到办公网 / VPN 出口 CIDR（**不要**保留
      `private_ranges` 这种宽网段）。敏感读接口（账户/持仓/盈亏/对账/事件）不应裸暴露公网。
      - 验证：从白名单外 IP 访问应 403；账户/持仓 GET 未带 key 应 401。
- [ ] 评估是否再叠加 Caddy `basic_auth` 或把整站放到 VPN / 堡垒机后（`infra/Caddyfile` 有注释模板）。

## 阶段 1 — 告警通道（出事必须有人被叫醒）

- [ ] 配置 `QUANTDEV_ALERT_WEBHOOK_URL`（企业微信群机器人 webhook）。
- [ ] **联调演练**：手动触发一次 Kill Switch（`POST /api/live/kill-switch`），确认企业微信
      **真的收到告警**。这是上线硬门槛——"能把人叫醒"没验证过不算数。
- [ ] 确认告警覆盖：Kill Switch 激活、对账 BREAK、订单 UNKNOWN、被拒实盘单、运行器循环失败、
      日亏损触限。（这些已在代码里接好，演练确认通道通即可。）

## 阶段 2 — 数据与行情

- [ ] 真实日线/财务/日历/成分已同步到最近完整交易日（`GET /api/data/status`）。
- [ ] **盘中行情接入**：当前无内置盘中行情源，必须有一个常驻服务把实时行情
      `POST /api/live/quotes`（带断线重连）。确认 `QUANTDEV_QUOTE_MAX_AGE_SECONDS`（默认 15s）
      与 `QUANTDEV_QUOTE_MAX_SPREAD_BPS`（默认 100）合理。
      - 验证：停掉行情源，确认下单被"行情陈旧"闸门拦下（fail-closed）。
- [ ] `QUANTDEV_CALENDAR_FAIL_CLOSED=true`（缺日历时拒绝下单）。

## 阶段 3 — 券商适配器

- [ ] 实现并配置真实 `BrokerAdapter`：`QUANTDEV_BROKER_ADAPTER=模块:工厂`，其
      `health_check().mode` 必须返回 `"live"`，否则系统失败关闭、不会用 Mock 代替。
- [ ] 适配器异常约定（与本轮 OMS 修复对齐）：
      - **业务拒单**（资金/持仓不足、参数非法、明确拒单）抛 `ValueError`/`PermissionError` → 落 REJECTED。
      - **连接/超时等不确定异常**抛 `TimeoutError` 或其它异常 → 落 UNKNOWN + 查询确认 + 告警（绝不当已拒）。
      - 提供 `query_order_by_client_id`（恢复巡检靠它收敛中间态）。
- [ ] `make verify-broker`（MockLiveBroker 异常压测）30 天内通过。

## 阶段 4 — 风控与熔断阈值（保守起步）

- [ ] `QUANTDEV_MAX_LIVE_ORDER_NOTIONAL`（单笔上限）、`QUANTDEV_MAX_DAILY_LOSS`（日亏损）设到
      **小资金能承受**的保守值。
- [ ] 理解日亏损行为（本轮修正）：触限时**阻断新增买入 + 告警，但放行卖出离场**，不自动全局冻结
      （避免锁死止损单）；是否手动 Kill 由人工据告警决定。
- [ ] `QUANTDEV_REQUIRE_ORDER_APPROVAL=true`、`QUANTDEV_LIVE_TRADING_ENABLED` 起步保持谨慎。

## 阶段 5 — 对账（实盘真正的安全网）

- [ ] `QUANTDEV_RECONCILIATION_INTERVAL_SECONDS`（默认 300s）：live runner 每轮按此间隔自动对账。
- [ ] **必须运行一个 live 执行守护进程**：当前 `compose.prod.yml` 只有 `paper-runner`。实盘需让
      runner 在 live 模式常驻（`paper-runner` 进程在 live 模式下会做 sync→恢复中间态→定时对账），
      或新增 `live-runner` 服务。**没有常驻 runner，UNKNOWN 订单与漂移无人自动收敛。**
- [ ] `QUANTDEV_AUTO_KILL_ON_RECON_BREAK=true`（对账差异自动熔断）。
- [ ] 接真实券商前，按需放宽 `QUANTDEV_RECONCILIATION_CASH_TOLERANCE_CENTS`（默认 0=精确到分），
      吸收券商费用/逐笔舍入口径的无害尾差，避免误触熔断。
      - 验证：制造一笔"券商成交但本地漏记"，确认对账 **BREAK** 并告警/熔断。

## 阶段 6 — 可靠性与备份

- [ ] **备份强化**：`infra/postgres-backup.sh` 当前无校验、无异地、无加密、无 PITR。上线前改为
      校验式备份（dump 后 `pg_restore --list`）+ 加密 + 推送异地对象存储 + WAL 归档（PITR）。
- [ ] **做一次真实恢复演练**：空库恢复一次备份，记录 RTO，确认数据可用。没演练过的备份等于没有备份。
- [ ] 确认 `migrate` 一次性容器成功后才启动 API；主机重启后整栈能自起。

## 阶段 7 — 灰度上线节奏（不可跨阶段）

1. [ ] 真实数据回测通过且结论可信（注意已知的幸存者/ST 偏差，见 ARCHITECTURE 回测边界）。
2. [ ] 连续模拟盘覆盖 **≥ 20 个交易日**，含部分成交、撤单、超时、对账差异等场景。
3. [ ] 接券商**测试环境**，半自动逐单审批 + 每日对账，跑顺。
4. [ ] **小资金**实盘，单笔与日亏损限额保持保守，盯盘 + 告警双保险。
5. [ ] 稳定后再逐步放宽，**禁止一次性跨阶段**或一上来就自动审批。

---

## 关键环境变量速查

| 变量 | 作用 | 实盘建议 |
| --- | --- | --- |
| `QUANTDEV_OPERATOR_KEYS` | 每操作员独立密钥（双人复核身份） | `alice:k1,bob:k2` |
| `QUANTDEV_REQUIRE_DUAL_APPROVAL` | 审批人≠提交人 | `true` |
| `QUANTDEV_ALERT_WEBHOOK_URL` | 企业微信告警 | 必填并演练 |
| `QUANTDEV_ALLOWED_IPS` | Caddy IP 白名单 | VPN/办公网 CIDR |
| `QUANTDEV_BROKER_ADAPTER` | 真实券商适配器 | `模块:工厂` |
| `QUANTDEV_MAX_LIVE_ORDER_NOTIONAL` | 单笔上限 | 保守 |
| `QUANTDEV_MAX_DAILY_LOSS` | 日亏损 | 保守 |
| `QUANTDEV_RECONCILIATION_INTERVAL_SECONDS` | 自动对账间隔 | `300` |
| `QUANTDEV_RECONCILIATION_CASH_TOLERANCE_CENTS` | 现金对账容差(分) | 接券商后按费用口径设 |
| `QUANTDEV_QUOTE_MAX_AGE_SECONDS` | 行情时效闸 | `15` 或更严 |
| `QUANTDEV_AUTO_KILL_ON_RECON_BREAK` | 对账差异熔断 | `true` |
| `QUANTDEV_CALENDAR_FAIL_CLOSED` | 缺日历拒单 | `true` |
