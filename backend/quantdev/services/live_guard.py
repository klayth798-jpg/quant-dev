"""实盘安全守卫：实盘相关动作必须经过这里的三层拦截判定。

设计原则——实盘下单的放行需要同时满足三层独立条件，任何一层不通过即拒绝：
  1. 配置层：QUANTDEV_LIVE_TRADING_ENABLED=true 且 QUANTDEV_BROKER_MODE=live。
  2. 运行层：Kill Switch 未激活（system_flags 表中 kill_switch != 'on'）。
  3. 审批层：若 QUANTDEV_REQUIRE_ORDER_APPROVAL=true，则订单必须已审批。

这里不直接执行下单，只负责"是否允许"的判定，由 API、Service、Broker 三层分别调用，
形成纵深防御：即使某一层被绕过，其它层仍会拦住真实资金风险。
"""

from dataclasses import dataclass
from typing import List

from quantdev.config import settings
from quantdev.store import store

KILL_SWITCH_FLAG = "kill_switch"


@dataclass(frozen=True)
class GuardDecision:
    allowed: bool
    reasons: List[str]

    @property
    def reason(self) -> str:
        return "；".join(self.reasons)


class LiveTradingGuard:
    """实盘动作的统一安全判定入口。"""

    def kill_switch_active(self) -> bool:
        flag = store.get_flag(KILL_SWITCH_FLAG)
        return bool(flag and flag["value"] == "on")

    def kill_switch_status(self) -> dict:
        flag = store.get_flag(KILL_SWITCH_FLAG)
        if not flag:
            return {"active": False, "reason": None, "updated_at": None}
        return {
            "active": flag["value"] == "on",
            "reason": flag.get("reason"),
            "updated_by": flag.get("updated_by"),
            "updated_at": flag.get("updated_at"),
        }

    def activate_kill_switch(self, reason: str, actor: str = "local-user") -> dict:
        record = store.set_flag(KILL_SWITCH_FLAG, "on", reason, actor)
        store.audit(
            actor=actor,
            action="activate_kill_switch",
            resource_type="system_flag",
            resource_id=KILL_SWITCH_FLAG,
            payload={"reason": reason},
        )
        from quantdev.services.events import EventType, event_bus

        event_bus.publish(
            event_type=EventType.KILL_SWITCH_ACTIVATED,
            aggregate_type="system_flag",
            aggregate_id=KILL_SWITCH_FLAG,
            payload={"reason": reason},
            actor=actor,
        )
        return record

    def deactivate_kill_switch(self, reason: str, actor: str = "local-user") -> dict:
        record = store.set_flag(KILL_SWITCH_FLAG, "off", reason, actor)
        store.audit(
            actor=actor,
            action="deactivate_kill_switch",
            resource_type="system_flag",
            resource_id=KILL_SWITCH_FLAG,
            payload={"reason": reason},
        )
        from quantdev.services.events import EventType, event_bus

        event_bus.publish(
            event_type=EventType.KILL_SWITCH_DEACTIVATED,
            aggregate_type="system_flag",
            aggregate_id=KILL_SWITCH_FLAG,
            payload={"reason": reason},
            actor=actor,
        )
        return record

    def check_live_order(
        self,
        notional: float,
        approved: bool = False,
    ) -> GuardDecision:
        """实盘下单的三层拦截判定。返回是否放行及全部拒绝原因。"""
        reasons: List[str] = []
        # 第一层：配置开关。
        if not settings.live_trading_enabled:
            reasons.append("实盘交易总开关未开启（QUANTDEV_LIVE_TRADING_ENABLED）")
        if settings.broker_mode != "live":
            reasons.append(
                "Broker 模式非 live（当前 {}）".format(settings.broker_mode)
            )
        # 第二层：Kill Switch 运行时拦截。
        if self.kill_switch_active():
            status = self.kill_switch_status()
            reasons.append(
                "Kill Switch 已激活：{}".format(status.get("reason") or "未说明原因")
            )
        # 第三层：订单审批。
        if settings.require_order_approval and not approved:
            reasons.append("订单未经过审批（QUANTDEV_REQUIRE_ORDER_APPROVAL）")
        # 时间闸：实盘只能在交易日的连续竞价时段下单。
        from quantdev.services.calendar import trading_calendar_service

        clock = trading_calendar_service.market_clock()
        if not clock.can_trade:
            reasons.append(
                "当前非交易时段（{}），实盘禁止下单".format(clock.session)
            )
        # 限额：单笔名义金额上限。
        if notional > settings.max_live_order_notional:
            reasons.append(
                "单笔金额 {:.2f} 超过实盘上限 {:.2f}".format(
                    notional, settings.max_live_order_notional
                )
            )
        return GuardDecision(allowed=not reasons, reasons=reasons)

    def resolved_broker_mode(self) -> str:
        """实际生效的 broker 模式：总开关未开时，live 一律降级为 disabled。"""
        if settings.broker_mode == "live" and not settings.live_trading_enabled:
            return "disabled"
        return settings.broker_mode


live_guard = LiveTradingGuard()
