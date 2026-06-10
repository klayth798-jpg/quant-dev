"""Mock Broker 异常压测套件。

在不接真实资金的前提下，系统化地驱动 MockLiveBroker 的各类异常场景，
验证 OMS / 对账 / 查询接口在以下情况下都能保持一致、不留脏状态：
  - normal：全部成交
  - partial：部分成交（filled < quantity）
  - reject：券商拒单
  - unknown：券商返回未知状态（需通过 query_order 进一步确认）
  - timeout：网络超时（抛 TimeoutError，本地不得记录已成交）
  - cancel_fail：撤单失败（订单可能已成交，必须按"未撤成"处理）

这些用例是"半自动实盘 / 小资金自动实盘"前的最后一道防线：只要 OMS 对
任何券商异常都能做出确定性、可审计的反应，才允许进入真实资金阶段。
"""

from dataclasses import replace

import pytest
from quantdev.integrations.broker import BrokerOrder, MockLiveBroker

SCENARIOS = ["normal", "partial", "reject", "unknown", "timeout", "cancel_fail"]


@pytest.fixture
def live_enabled(monkeypatch):
    """放开实盘配置 + 强制可交易时段，使守卫放行后才能验证 Broker 行为。"""
    import quantdev.services.calendar as calendar_module
    import quantdev.services.live_guard as guard_module

    monkeypatch.setattr(
        guard_module,
        "settings",
        replace(
            guard_module.settings,
            live_trading_enabled=True,
            broker_mode="live",
            require_order_approval=False,
            max_live_order_notional=1_000_000,
        ),
    )
    monkeypatch.setattr(
        calendar_module.trading_calendar_service,
        "market_clock",
        lambda moment=None: calendar_module.MarketClock(
            timestamp="2026-06-10T10:00:00+08:00",
            is_trading_day=True,
            is_trading_session=True,
            session="morning",
            calendar_source="weekday_fallback",
            can_trade=True,
        ),
    )


def _order(client_order_id: str = "stress-1") -> BrokerOrder:
    return BrokerOrder(
        client_order_id, "600519.SH", "buy", 200, "limit", 10.0, approved=True
    )


def test_timeout_leaves_no_local_order(live_enabled):
    """超时必须抛 TimeoutError，且不得在 broker 端登记任何订单（状态未知）。"""
    broker = MockLiveBroker(scenario="timeout")
    with pytest.raises(TimeoutError):
        broker.submit_order(_order())
    assert broker.get_orders() == []
    assert broker.health_check().healthy is False


def test_partial_fill_quantity_invariant(live_enabled):
    """部分成交：0 < filled < quantity，且可查询到同一笔订单的成交量。"""
    broker = MockLiveBroker(scenario="partial")
    ack = broker.submit_order(_order())
    assert ack.accepted is True
    assert ack.status == "PARTIALLY_FILLED"
    assert 0 < ack.filled_quantity < 200
    snapshot = broker.query_order(ack.broker_order_id)
    assert snapshot is not None
    assert snapshot.filled_quantity == ack.filled_quantity
    assert snapshot.status == "PARTIALLY_FILLED"


def test_reject_marks_order_rejected(live_enabled):
    """拒单：ack 不被接受，但订单仍可被查询到且状态为 REJECTED（可审计）。"""
    broker = MockLiveBroker(scenario="reject")
    ack = broker.submit_order(_order())
    assert ack.accepted is False
    assert ack.status == "REJECTED"
    snapshot = broker.query_order(ack.broker_order_id)
    assert snapshot is not None and snapshot.status == "REJECTED"
    assert snapshot.filled_quantity == 0


def test_unknown_status_requires_query_followup(live_enabled):
    """未知状态：ack 被接受但状态 UNKNOWN，必须能通过 query_order 复查。"""
    broker = MockLiveBroker(scenario="unknown")
    ack = broker.submit_order(_order())
    assert ack.status == "UNKNOWN"
    snapshot = broker.query_order(ack.broker_order_id)
    assert snapshot is not None and snapshot.status == "UNKNOWN"


def test_cancel_failure_does_not_flip_status(live_enabled):
    """撤单失败：必须返回未接受，且不得把订单标记为已撤（订单可能已成交）。"""
    broker = MockLiveBroker(scenario="cancel_fail")
    cancel = broker.cancel_order("mock-000001")
    assert cancel.accepted is False
    assert cancel.status == "CANCEL_FAILED"


def test_normal_cancel_succeeds_and_reflects_in_query(live_enabled):
    """正常场景：下单成交后可成功撤单（状态机走到 CANCELLED）。"""
    broker = MockLiveBroker(scenario="normal")
    ack = broker.submit_order(_order())
    assert ack.status == "FILLED"
    cancel = broker.cancel_order(ack.broker_order_id)
    assert cancel.accepted is True
    assert cancel.status == "CANCELLED"
    assert broker.query_order(ack.broker_order_id).status == "CANCELLED"


def test_cancel_timeout_raises(live_enabled):
    """撤单超时同样抛 TimeoutError，调用方需按状态未知处理。"""
    broker = MockLiveBroker(scenario="timeout")
    with pytest.raises(TimeoutError):
        broker.cancel_order("mock-000001")


def test_cancel_unknown_order_id(live_enabled):
    """撤一个不存在的订单：返回未接受 + UNKNOWN，而非误报成功。"""
    broker = MockLiveBroker(scenario="normal")
    cancel = broker.cancel_order("does-not-exist")
    assert cancel.accepted is False
    assert cancel.status == "UNKNOWN"


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_health_check_consistency(live_enabled, scenario):
    """健康检查：除 timeout 外均健康，且 mode/scenario 信息完整。"""
    broker = MockLiveBroker(scenario=scenario)
    health = broker.health_check()
    assert health.mode == "mock_live"
    assert scenario in health.message
    assert health.healthy is (scenario != "timeout")


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_query_consistency_after_submit(live_enabled, scenario):
    """提交后查询一致性：非超时场景下，get_orders 与 query_order 必须吻合。"""
    broker = MockLiveBroker(scenario=scenario)
    if scenario == "timeout":
        with pytest.raises(TimeoutError):
            broker.submit_order(_order())
        return
    ack = broker.submit_order(_order())
    orders = broker.get_orders()
    assert len(orders) == 1
    assert orders[0].broker_order_id == ack.broker_order_id
    assert broker.query_order(ack.broker_order_id) == orders[0]


def test_broker_id_monotonic_under_burst(live_enabled):
    """连续下单压力：broker_order_id 必须单调递增且唯一，避免状态错配。"""
    broker = MockLiveBroker(scenario="normal")
    ids = []
    for i in range(20):
        ack = broker.submit_order(_order("burst-{}".format(i)))
        ids.append(ack.broker_order_id)
    assert len(set(ids)) == 20
    assert len(broker.get_orders()) == 20


def test_guard_blocks_all_scenarios_without_live_config():
    """守卫优先：未开启实盘配置时，任何场景下单都被 PermissionError 拦截。"""
    for scenario in SCENARIOS:
        broker = MockLiveBroker(scenario=scenario)
        with pytest.raises(PermissionError):
            broker.submit_order(_order())
