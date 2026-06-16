"""实盘路线就绪度自检：把 5 阶段的"进入条件"做成可校验的检查清单。

实盘上线是一条单向递进的路线，每个阶段都有明确的进入门槛。本服务基于系统
真实状态（数据快照、回测记录、模拟盘成交、对账、安全配置）逐项判定，输出每个
阶段是否"就绪"，避免凭感觉跳阶段把真实资金暴露在未验证的链路上。

五个阶段（与路线图一致）：
  1. real_data_backtest   真实数据回测：已接入非 demo 数据并完成过回测。
  2. continuous_paper      连续模拟盘：模拟盘累计成交覆盖足够多的交易日。
  3. mock_broker_stress    Mock Broker 压测：异常压测套件存在且通过（人工确认）。
  4. semi_auto_live        半自动实盘：安全三件套就绪 + 审批开启 + 近期对账无差异。
  5. small_capital_live    小资金自动实盘：在半自动稳定运行基础上，限额设为小额。

每项检查返回 passed/detail，阶段 ready = 该阶段所有检查通过。
检查只读、无副作用，可随时调用。
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from quantdev.config import settings
from quantdev.integrations.broker import get_broker
from quantdev.services.calendar import trading_calendar_service
from quantdev.services.live_guard import live_guard
from quantdev.store import store

# 连续模拟盘要求覆盖的最少交易日数。
MIN_PAPER_TRADING_DAYS = 20
# 小资金实盘单笔名义金额上限（超过则视为"非小资金"）。
SMALL_CAPITAL_NOTIONAL_CEILING = 10_000.0


def _check(name: str, passed: bool, detail: str) -> Dict[str, Any]:
    return {"name": name, "passed": passed, "detail": detail}


class LiveReadinessService:
    def evaluate(self) -> Dict[str, Any]:
        semi_auto = self._stage_semi_auto_live()
        stages = [
            self._stage_real_data_backtest(),
            self._stage_continuous_paper(),
            self._stage_mock_broker_stress(),
            semi_auto,
            self._stage_small_capital_live(semi_auto["ready"]),
        ]
        # 当前所处阶段：第一个尚未就绪的阶段下标（全部就绪则为最后一阶段）。
        current = next(
            (i for i, s in enumerate(stages) if not s["ready"]), len(stages) - 1
        )
        return {
            "current_stage": stages[current]["key"],
            "all_ready": all(s["ready"] for s in stages),
            "stages": stages,
        }

    @staticmethod
    def _stage(key: str, title: str, checks: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "key": key,
            "title": title,
            "ready": all(c["passed"] for c in checks),
            "checks": checks,
        }

    def _stage_real_data_backtest(self) -> Dict[str, Any]:
        snapshot_id = store.latest_snapshot_id()
        not_demo = settings.data_mode != "demo"
        backtests = store.list_backtests(limit=1)
        latest_backtest = backtests[0] if backtests else None
        manifest = (
            store.latest_dataset_manifest(snapshot_id) if snapshot_id else None
        )
        expected_day = trading_calendar_service.latest_completed_trading_day()
        latest_day = store.snapshot_max_date(snapshot_id) if snapshot_id else None
        checks = [
            _check(
                "数据模式非 demo",
                not_demo,
                "当前 data_mode={}".format(settings.data_mode),
            ),
            _check(
                "存在数据快照",
                bool(snapshot_id),
                "latest_snapshot_id={}".format(snapshot_id),
            ),
            _check(
                "至少完成过一次回测",
                len(backtests) > 0,
                "回测记录数={}".format(len(backtests)),
            ),
            _check(
                "最近回测使用当前数据集",
                bool(
                    latest_backtest
                    and latest_backtest["snapshot_id"] == snapshot_id
                ),
                "backtest_snapshot={}，current_snapshot={}".format(
                    latest_backtest["snapshot_id"] if latest_backtest else None,
                    snapshot_id,
                ),
            ),
            _check(
                "当前数据集已有密封 Manifest",
                bool(
                    manifest
                    and manifest["max_trade_date"] == latest_day
                    and manifest["status"] == "SEALED"
                ),
                "manifest={}".format(manifest or "missing"),
            ),
            _check(
                "行情快照更新到最近完整交易日",
                expected_day is not None
                and latest_day == expected_day.isoformat(),
                "最新行情={}，应为={}".format(latest_day, expected_day),
            ),
        ]
        return self._stage("real_data_backtest", "真实数据回测", checks)

    def _stage_continuous_paper(self) -> Dict[str, Any]:
        stats = store.paper_trading_stats()
        strategy_days = int(stats.get("strategy_days") or 0)
        consecutive_days = int(stats.get("consecutive_strategy_days") or 0)
        filled = int(stats.get("filled_orders") or 0)
        checks = [
            _check(
                "模拟盘有成交记录",
                filled > 0,
                "成交笔数={}".format(filled),
            ),
            _check(
                "策略运行覆盖交易日数 >= {}".format(MIN_PAPER_TRADING_DAYS),
                strategy_days >= MIN_PAPER_TRADING_DAYS,
                "运行覆盖 {} 日，最长连续 {} 日（{} ~ {}）".format(
                    strategy_days,
                    consecutive_days,
                    stats.get("first_strategy_day"),
                    stats.get("last_strategy_day"),
                ),
            ),
            _check(
                "连续策略运行交易日数 >= {}".format(MIN_PAPER_TRADING_DAYS),
                consecutive_days >= MIN_PAPER_TRADING_DAYS,
                "最长连续运行 {} 个交易日".format(consecutive_days),
            ),
        ]
        return self._stage("continuous_paper", "连续模拟盘", checks)

    def _stage_mock_broker_stress(self) -> Dict[str, Any]:
        verification = store.latest_verification("mock_broker_stress")
        recent = False
        if verification:
            created = datetime.fromisoformat(verification["created_at"])
            recent = created >= datetime.now(timezone.utc) - timedelta(days=30)
        checks = [
            _check(
                "Mock Broker 异常压测最近 30 天通过",
                bool(
                    verification
                    and verification["status"] == "PASSED"
                    and recent
                ),
                "最近结果={}".format(
                    verification or "未运行 quantdev verify-broker"
                ),
            ),
        ]
        return self._stage("mock_broker_stress", "Mock Broker 压测", checks)

    def _stage_semi_auto_live(self) -> Dict[str, Any]:
        recons = store.list_reconciliations(limit=1)
        latest_recon = recons[0] if recons else None
        broker_health = get_broker().health_check()
        clock = trading_calendar_service.market_clock()
        now = datetime.now(timezone.utc)
        recon_recent = bool(
            latest_recon
            and datetime.fromisoformat(latest_recon["created_at"])
            >= now - timedelta(minutes=settings.reconciliation_max_age_minutes)
        )
        recon_matches_broker = bool(
            latest_recon
            and latest_recon["broker_mode"] == broker_health.mode
        )
        live_orders = store.list_live_orders()
        last_order_at = max(
            (datetime.fromisoformat(item["updated_at"]) for item in live_orders),
            default=None,
        )
        recon_after_orders = bool(
            latest_recon
            and (
                last_order_at is None
                or datetime.fromisoformat(latest_recon["created_at"]) >= last_order_at
            )
        )
        latest_balanced = bool(
            latest_recon
            and latest_recon["status"] == "BALANCED"
            and recon_recent
            and recon_matches_broker
            and recon_after_orders
        )
        checks = [
            _check(
                "实盘总开关已开启",
                settings.live_trading_enabled,
                "live_trading_enabled={}".format(settings.live_trading_enabled),
            ),
            _check(
                "订单审批已开启（半自动必须人工确认）",
                settings.require_order_approval,
                "require_order_approval={}".format(settings.require_order_approval),
            ),
            _check(
                "Kill Switch 未激活",
                not live_guard.kill_switch_active(),
                "kill_switch_active={}".format(live_guard.kill_switch_active()),
            ),
            _check(
                "最近一次对账无差异",
                latest_balanced,
                "最近对账状态={}".format(
                    latest_recon["status"] if latest_recon else "无对账记录"
                ),
            ),
            _check(
                "对账属于当前 Broker 且在有效期内",
                recon_recent and recon_matches_broker and recon_after_orders,
                "recent={}，broker_match={}，after_orders={}".format(
                    recon_recent, recon_matches_broker, recon_after_orders
                ),
            ),
            _check(
                "Broker 健康检查通过",
                broker_health.healthy,
                broker_health.message,
            ),
            _check(
                "已接入真实 Broker 适配器",
                broker_health.mode == "live",
                "当前适配器模式={}".format(broker_health.mode),
            ),
            _check(
                "交易日历覆盖当前日期",
                clock.calendar_source == "calendar",
                "calendar_source={}".format(clock.calendar_source),
            ),
            _check(
                "管理员 API 密钥已配置",
                bool(settings.admin_api_key),
                "QUANTDEV_ADMIN_API_KEY={}".format(
                    "configured" if settings.admin_api_key else "missing"
                ),
            ),
        ]
        return self._stage("semi_auto_live", "半自动实盘", checks)

    def _stage_small_capital_live(
        self, semi_auto_ready: bool
    ) -> Dict[str, Any]:
        within_ceiling = (
            0 < settings.max_live_order_notional <= SMALL_CAPITAL_NOTIONAL_CEILING
        )
        checks = [
            _check(
                "半自动实盘阶段已通过",
                semi_auto_ready,
                "semi_auto_live_ready={}".format(semi_auto_ready),
            ),
            _check(
                "Broker 模式为 live",
                settings.broker_mode == "live",
                "broker_mode={}".format(settings.broker_mode),
            ),
            _check(
                "单笔限额在小资金区间内（<= {:.0f}）".format(
                    SMALL_CAPITAL_NOTIONAL_CEILING
                ),
                within_ceiling,
                "max_live_order_notional={}".format(settings.max_live_order_notional),
            ),
            _check(
                "已设置每日最大亏损限额",
                settings.max_daily_loss > 0,
                "max_daily_loss={}".format(settings.max_daily_loss),
            ),
        ]
        return self._stage("small_capital_live", "小资金自动实盘", checks)


live_readiness_service = LiveReadinessService()
