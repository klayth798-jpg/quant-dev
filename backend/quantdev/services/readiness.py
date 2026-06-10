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

from typing import Any, Dict, List

from quantdev.config import settings
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
        stages = [
            self._stage_real_data_backtest(),
            self._stage_continuous_paper(),
            self._stage_mock_broker_stress(),
            self._stage_semi_auto_live(),
            self._stage_small_capital_live(),
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
        ]
        return self._stage("real_data_backtest", "真实数据回测", checks)

    def _stage_continuous_paper(self) -> Dict[str, Any]:
        stats = store.paper_trading_stats()
        trading_days = int(stats.get("trading_days") or 0)
        filled = int(stats.get("filled_orders") or 0)
        checks = [
            _check(
                "模拟盘有成交记录",
                filled > 0,
                "成交笔数={}".format(filled),
            ),
            _check(
                "覆盖交易日数 >= {}".format(MIN_PAPER_TRADING_DAYS),
                trading_days >= MIN_PAPER_TRADING_DAYS,
                "已覆盖 {} 个交易日（{} ~ {}）".format(
                    trading_days,
                    stats.get("first_fill"),
                    stats.get("last_fill"),
                ),
            ),
        ]
        return self._stage("continuous_paper", "连续模拟盘", checks)

    def _stage_mock_broker_stress(self) -> Dict[str, Any]:
        from quantdev.config import PROJECT_ROOT

        stress_suite = PROJECT_ROOT / "tests" / "test_broker_stress.py"
        checks = [
            _check(
                "Mock Broker 异常压测套件存在",
                stress_suite.exists(),
                str(stress_suite),
            ),
        ]
        return self._stage("mock_broker_stress", "Mock Broker 压测", checks)

    def _stage_semi_auto_live(self) -> Dict[str, Any]:
        recons = store.list_reconciliations(limit=1)
        latest_balanced = bool(recons) and recons[0]["status"] == "BALANCED"
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
                    recons[0]["status"] if recons else "无对账记录"
                ),
            ),
        ]
        return self._stage("semi_auto_live", "半自动实盘", checks)

    def _stage_small_capital_live(self) -> Dict[str, Any]:
        within_ceiling = (
            0 < settings.max_live_order_notional <= SMALL_CAPITAL_NOTIONAL_CEILING
        )
        checks = [
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
