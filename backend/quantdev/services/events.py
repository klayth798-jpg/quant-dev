"""全局领域事件总线：统一发布 + 持久化 + 进程内订阅。

实盘系统里"发生了什么"必须可审计、可回放。本模块在 append-only 的
domain_events 表之上提供一个轻量事件总线：

  - publish()：先把事件持久化（先落库再通知，保证不丢事件），再同步分发给订阅者。
  - subscribe()：进程内订阅者（如告警、Kill Switch 联动）按事件类型接收回调。

订阅者回调中的异常不会影响事件持久化，也不会中断其它订阅者——事件已落库，
回调失败只记录到审计日志，避免"一个监听器报错导致整条交易链路崩溃"。

常见事件类型常量见 EventType。新增类型直接传字符串即可，无需改表结构。
"""

from typing import Any, Callable, Dict, List

from quantdev.store import store

Subscriber = Callable[[Dict[str, Any]], None]


class EventType:
    ORDER_SUBMITTED = "order.submitted"
    ORDER_REJECTED = "order.rejected"
    ORDER_FILLED = "order.filled"
    RECONCILIATION_COMPLETED = "reconciliation.completed"
    RECONCILIATION_BREAK = "reconciliation.break"
    KILL_SWITCH_ACTIVATED = "kill_switch.activated"
    KILL_SWITCH_DEACTIVATED = "kill_switch.deactivated"


class EventBus:
    def __init__(self) -> None:
        self._subscribers: Dict[str, List[Subscriber]] = {}

    def subscribe(self, event_type: str, handler: Subscriber) -> None:
        self._subscribers.setdefault(event_type, []).append(handler)

    def publish(
        self,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: Dict[str, Any],
        actor: str = "system",
    ) -> Dict[str, Any]:
        """先持久化事件，再同步分发给订阅者。返回落库后的事件记录。"""
        event = store.record_domain_event(
            event_type=event_type,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            payload=payload,
            actor=actor,
        )
        for handler in self._subscribers.get(event_type, []):
            try:
                handler(event)
            except Exception as exc:  # 订阅者失败不影响已落库的事件与其它订阅者。
                store.audit(
                    actor="event-bus",
                    action="subscriber_error",
                    resource_type="domain_event",
                    resource_id=event["event_id"],
                    payload={"event_type": event_type, "error": str(exc)},
                )
        return event


event_bus = EventBus()
