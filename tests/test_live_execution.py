from dataclasses import replace
from datetime import date

import pytest
from quantdev.db import database
from quantdev.integrations.broker import (
    AccountSnapshot,
    BrokerAdapter,
    BrokerHealth,
    BrokerOrder,
    BrokerOrderAck,
    CancelAck,
    GuardedBroker,
    MockLiveBroker,
    UnconfiguredLiveBroker,
)
from quantdev.services.calendar import MarketClock
from quantdev.services.execution_router import execution_router
from quantdev.services.live_execution import live_execution_service
from quantdev.store import store, utc_now


def _insert_live_intent(intent_id: str = "intent-live-1") -> str:
    now = utc_now()
    with database.transaction(immediate=True) as connection:
        connection.execute(
            """
            INSERT INTO strategy_configs
                (strategy_id, name, factor_id, top_n, rebalance_days,
                 universe_json, neutralize, order_type, max_order_notional,
                 enabled, created_at, updated_at)
            VALUES ('strat-live', '实盘审批测试', 'momentum_20', 1, 5,
                    '[]', 0, 'limit', 5000, 0, ?, ?)
            """,
            (now, now),
        )
        connection.execute(
            """
            INSERT INTO strategy_runs
                (run_id, strategy_id, trade_date, signal_date, snapshot_id,
                 status, started_at)
            VALUES ('run-live', 'strat-live', ?, ?, ?, 'RUNNING', ?)
            """,
            (
                date.today().isoformat(),
                store.snapshot_max_date(store.latest_snapshot_id()),
                store.latest_snapshot_id(),
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO order_intents
                (intent_id, run_id, client_order_id, symbol, side, quantity,
                 order_type, limit_price, status, execution_mode,
                 approval_status, created_at, updated_at)
            VALUES (?, 'run-live', 'live-client-0001', '600519.SH', 'buy',
                    200, 'limit', 10, 'PENDING_APPROVAL', 'live',
                    'PENDING', ?, ?)
            """,
            (intent_id, now, now),
        )
    return intent_id


@pytest.fixture
def live_ready(monkeypatch):
    import quantdev.services.calendar as calendar_module
    import quantdev.services.live_execution as live_module
    import quantdev.services.live_guard as guard_module
    import quantdev.services.pretrade as pretrade_module

    live_settings = replace(
        guard_module.settings,
        broker_mode="live",
        live_trading_enabled=True,
        require_order_approval=True,
        max_live_order_notional=100_000,
        max_daily_loss=10_000,
        approval_ttl_seconds=300,
        quote_max_spread_bps=100,
    )
    monkeypatch.setattr(guard_module, "settings", live_settings)
    monkeypatch.setattr(live_module, "settings", live_settings)
    monkeypatch.setattr(pretrade_module, "settings", live_settings)
    monkeypatch.setattr(
        calendar_module.trading_calendar_service,
        "market_clock",
        lambda moment=None: MarketClock(
            timestamp="2026-06-13T10:00:00+08:00",
            is_trading_day=True,
            is_trading_session=True,
            session="morning",
            calendar_source="calendar",
            can_trade=True,
        ),
    )


def test_live_intent_requires_persisted_approval(live_ready):
    intent_id = _insert_live_intent()
    broker = MockLiveBroker(scenario="normal")

    with pytest.raises(PermissionError, match="尚未审批"):
        live_execution_service.submit_intent(intent_id, broker=broker)

    approved = live_execution_service.approve_intent(intent_id, actor="reviewer-a")
    result = live_execution_service.submit_intent(intent_id, broker=broker)

    assert approved["approval"]["approved_by"] == "reviewer-a"
    assert result["order"]["status"] == "FILLED"
    assert result["order"]["broker_order_id"]
    assert store.get_order_intent(intent_id)["status"] == "FILLED"


def test_approval_invalidated_when_order_changes(live_ready):
    intent_id = _insert_live_intent()
    broker = MockLiveBroker(scenario="normal")
    live_execution_service.approve_intent(intent_id)
    with database.transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE order_intents SET quantity = 300 WHERE intent_id = ?",
            (intent_id,),
        )

    with pytest.raises(PermissionError, match="重新审批"):
        live_execution_service.submit_intent(intent_id, broker=broker)

    intent = store.get_order_intent(intent_id)
    assert intent["approval_status"] == "INVALIDATED"
    assert intent["status"] == "PENDING_APPROVAL"


def test_execution_router_preserves_valid_approval(live_ready, monkeypatch):
    import quantdev.services.execution_router as router_module

    intent_id = _insert_live_intent()
    live_execution_service.approve_intent(intent_id, actor="reviewer-a")
    monkeypatch.setattr(
        router_module,
        "settings",
        replace(
            router_module.settings,
            broker_mode="live",
            require_order_approval=True,
        ),
    )

    result = execution_router.dispatch_intent(intent_id)
    intent = store.get_order_intent(intent_id)

    assert result["status"] == "APPROVED"
    assert intent["approval_status"] == "APPROVED"


def test_live_timeout_keeps_unknown_and_is_idempotent(live_ready, monkeypatch):
    intent_id = _insert_live_intent()
    broker = MockLiveBroker(scenario="timeout")
    monkeypatch.setattr(
        broker,
        "health_check",
        lambda: BrokerHealth(True, "mock_live", "提交阶段注入超时"),
    )
    live_execution_service.approve_intent(intent_id)

    first = live_execution_service.submit_intent(intent_id, broker=broker)
    second = live_execution_service.submit_intent(intent_id, broker=broker)

    assert first["order"]["status"] == "UNKNOWN"
    assert second["idempotent"] is True
    assert len(store.list_live_orders()) == 1


def test_normal_double_submit_creates_single_broker_order(live_ready):
    """并发/重复提交同一意图只产生一笔真实委托（防 TOCTOU 双重下单）。"""
    intent_id = _insert_live_intent()
    broker = MockLiveBroker(scenario="normal")
    live_execution_service.approve_intent(intent_id)

    first = live_execution_service.submit_intent(intent_id, broker=broker)
    second = live_execution_service.submit_intent(intent_id, broker=broker)

    assert first["idempotent"] is False
    assert first["order"]["status"] == "FILLED"
    assert second["idempotent"] is True
    # 关键：券商侧只有一笔委托，本地只有一条实盘订单。
    assert len(broker.get_orders()) == 1
    assert len(store.list_live_orders()) == 1


def test_submit_blocked_while_lease_held(live_ready):
    """提交租约被占用时（模拟另一并发提交进行中），拒绝重复提交。"""
    intent_id = _insert_live_intent()
    broker = MockLiveBroker(scenario="normal")
    live_execution_service.approve_intent(intent_id)
    # 抢占同一 client_order_id 的提交锁
    assert store.acquire_lease("live-submit:live-client-0001", "other", 60)

    with pytest.raises(ValueError, match="正在提交中"):
        live_execution_service.submit_intent(intent_id, broker=broker)

    # 锁未占用时仍可正常提交
    store.release_lease("live-submit:live-client-0001", "other")
    result = live_execution_service.submit_intent(intent_id, broker=broker)
    assert result["order"]["status"] == "FILLED"


def test_create_live_order_returns_created_flag(live_ready):
    intent_id = _insert_live_intent()
    live_execution_service.approve_intent(intent_id)
    intent = store.get_order_intent(intent_id)
    order1, created1 = store.create_live_order(intent, "hash-1", None)
    order2, created2 = store.create_live_order(intent, "hash-1", None)
    assert created1 is True
    assert created2 is False
    assert order1["order_id"] == order2["order_id"]


def test_daily_pnl_anchors_to_funded_equity_not_intraday():
    """首单发生在权益下跌之后时，日亏损必须按注资基准起算，不被绕过。"""
    store.sync_live_account_state("acc-anchor", 1_000_000, [])
    # 当日第一次快照就已经亏损 5 万（开盘跳空/首单延迟），无前一日快照。
    snap = store.update_live_daily_snapshot("acc-anchor", "2026-06-16", 950_000)
    assert snap["start_equity"] == 1_000_000
    assert snap["daily_pnl"] == -50_000
    # 当日后续快照锚点不变（start_equity 一旦确定不再移动）。
    snap2 = store.update_live_daily_snapshot("acc-anchor", "2026-06-16", 940_000)
    assert snap2["start_equity"] == 1_000_000
    assert snap2["daily_pnl"] == -60_000
    # 次日锚点用前一日收盘权益。
    snap3 = store.update_live_daily_snapshot("acc-anchor", "2026-06-17", 935_000)
    assert snap3["start_equity"] == 940_000


def test_daily_loss_blocks_buys_and_alerts_but_does_not_trap_exits(live_ready, monkeypatch):
    """日亏损触限：阻断新增买入 + critical 告警，但不自动全局冻结（保留止损离场，ptr-2 修正）。"""
    import quantdev.services.pretrade as pretrade_module
    from quantdev.integrations.broker import AccountSnapshot
    from quantdev.services.live_guard import live_guard
    from quantdev.services.pretrade import pretrade_risk_service

    intent_id = _insert_live_intent()
    broker = MockLiveBroker(scenario="normal")
    # 券商账户体现 5 万浮亏（equity 远低于注资基准），超过 live_ready 的 1 万日亏损限额。
    monkeypatch.setattr(
        broker,
        "get_account",
        lambda: AccountSnapshot(
            account_id="mock-live", cash=1_000_000.0, market_value=0.0, equity=950_000.0
        ),
    )
    alerts = []
    monkeypatch.setattr(
        pretrade_module.alert_service,
        "send",
        lambda *a, **k: alerts.append((a, k)),
    )
    intent = store.get_order_intent(intent_id)  # 买单
    with pytest.raises(PermissionError, match="当日亏损"):
        pretrade_risk_service.check(intent, broker, approved=True)
    # 告警已发，但不自动激活全局 Kill Switch（避免锁死止损单）。
    assert alerts
    assert live_guard.kill_switch_active() is False


def test_update_live_order_terminal_guard(live_ready):
    """终态保护：已 FILLED 的订单不能被陈旧/回退快照降级为非终态。"""
    intent_id = _insert_live_intent()
    broker = MockLiveBroker(scenario="normal")
    live_execution_service.approve_intent(intent_id)
    result = live_execution_service.submit_intent(intent_id, broker=broker)
    order_id = result["order"]["order_id"]
    assert store.get_live_order(order_id)["status"] == "FILLED"

    # 试图把终态降级为非终态 → 被拒绝，保持 FILLED。
    store.update_live_order(order_id, status="OPEN")
    assert store.get_live_order(order_id)["status"] == "FILLED"


def test_dual_approval_blocks_self_submit(live_ready):
    """双人复核（maker-checker）：审批人不能同时提交，需另一操作员提交（sec-1）。"""
    intent_id = _insert_live_intent()
    broker = MockLiveBroker(scenario="normal")
    live_execution_service.approve_intent(intent_id, actor="alice")

    with pytest.raises(PermissionError, match="双人复核"):
        live_execution_service.submit_intent(intent_id, broker=broker, actor="alice")

    # 另一操作员提交则放行。
    result = live_execution_service.submit_intent(intent_id, broker=broker, actor="bob")
    assert result["order"]["status"] == "FILLED"


def test_reconciliation_independent_ledger_detects_missed_fill(live_ready):
    """独立影子账本对账：基线之后券商成交但本地漏记，应检出三类差异（recon-1/recon-5）。"""
    from quantdev.integrations.broker import BrokerOrder
    from quantdev.services.reconciliation import reconciliation_service

    intent_id = _insert_live_intent()
    broker = MockLiveBroker(scenario="normal")
    live_execution_service.approve_intent(intent_id)
    live_execution_service.submit_intent(intent_id, broker=broker)
    live_execution_service.sync_broker(broker)  # 灌入 live_trades + 账户基线

    # 首次对账建立基线锚点（含已记录成交），应平衡。
    balanced = reconciliation_service.run(broker=broker)
    assert balanced["status"] == "BALANCED", balanced["breaks"]

    # 基线之后券商又成交一笔，但本地从未 sync_broker（漏记成交）。
    broker.submit_order(
        BrokerOrder(
            client_order_id="missed-fill-0001",
            symbol="600519.SH",
            side="buy",
            quantity=100,
            order_type="limit",
            limit_price=10,
            approved=True,
        )
    )
    broken = reconciliation_service.run(broker=broker)
    assert broken["status"] == "BREAK"
    types = {item["type"] for item in broken["breaks"]}
    assert "trade" in types
    assert "cash" in types
    assert "position" in types


def test_live_factory_never_falls_back_to_mock(monkeypatch):
    import quantdev.services.live_guard as guard_module
    from quantdev.integrations.broker import get_broker

    monkeypatch.setattr(
        guard_module,
        "settings",
        replace(
            guard_module.settings,
            broker_mode="live",
            live_trading_enabled=True,
        ),
    )

    broker = get_broker()
    assert isinstance(broker, UnconfiguredLiveBroker)
    assert broker.health_check().healthy is False


class _RecordingLiveBroker(BrokerAdapter):
    """实现 BrokerAdapter 的“真实券商”假体，但故意不自带 live_guard 检查。"""

    mode = "live"

    def __init__(self) -> None:
        self.submitted = []

    def submit_order(self, order: BrokerOrder) -> BrokerOrderAck:
        self.submitted.append(order)
        return BrokerOrderAck(
            accepted=True,
            broker_order_id="real-1",
            status="FILLED",
            filled_quantity=order.quantity,
        )

    def cancel_order(self, broker_order_id):
        return CancelAck(True, broker_order_id, "CANCELLED")

    def get_account(self):
        return AccountSnapshot("real", 1_000_000, 0.0, 1_000_000)

    def get_positions(self):
        return []

    def get_orders(self):
        return []

    def get_trades(self):
        return []

    def query_order(self, broker_order_id):
        return None

    def query_order_by_client_id(self, client_order_id):
        return None

    def health_check(self):
        return BrokerHealth(True, "live", "fake live")


def test_real_broker_wrapped_and_guarded(live_ready, monkeypatch):
    """真实券商适配器被 GuardedBroker 包裹，下单强制经过 live_guard。"""
    import types

    import quantdev.integrations.broker as broker_module
    from quantdev.integrations.broker import get_broker
    from quantdev.services.live_guard import live_guard

    fake = _RecordingLiveBroker()
    monkeypatch.setattr(
        broker_module,
        "settings",
        replace(broker_module.settings, broker_mode="live", broker_adapter="x:make"),
    )
    monkeypatch.setattr(
        broker_module.importlib,
        "import_module",
        lambda name: types.SimpleNamespace(make=lambda: fake),
    )

    broker = get_broker()
    assert isinstance(broker, GuardedBroker)

    order = BrokerOrder(
        client_order_id="c-guard-1",
        symbol="600519.SH",
        side="buy",
        quantity=200,
        order_type="limit",
        limit_price=10,
        approved=True,
    )
    ack = broker.submit_order(order)
    assert ack.status == "FILLED"
    assert len(fake.submitted) == 1

    # 未审批的订单：被守卫拦截，绝不透传到真实券商。
    unapproved = replace(order, approved=False)
    with pytest.raises(PermissionError, match="安全守卫拒绝"):
        broker.submit_order(unapproved)
    assert len(fake.submitted) == 1

    # Kill Switch 激活：即便已审批也被拦截。
    live_guard.activate_kill_switch("演练")
    with pytest.raises(PermissionError, match="安全守卫拒绝"):
        broker.submit_order(order)
    assert len(fake.submitted) == 1
