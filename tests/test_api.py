from dataclasses import replace

import quantdev.api as api_module
from fastapi.testclient import TestClient
from quantdev.api import app
from quantdev.store import store


def test_dashboard_and_factor_endpoints():
    with TestClient(app) as client:
        dashboard = client.get("/api/dashboard")
        factors = client.get("/api/factors")

    assert dashboard.status_code == 200
    assert dashboard.json()["instrument_count"] == 8
    assert factors.status_code == 200
    assert len(factors.json()["items"]) == 5


def test_risk_endpoint():
    with TestClient(app) as client:
        response = client.post(
            "/api/risk/check",
            json={
                "positions": [{"symbol": "600519.SH", "weight": 0.2}],
                "proposed_turnover": 0.3,
                "current_drawdown": 0.05,
                "order_notional_weight": 0.1,
            },
        )

    assert response.status_code == 200
    assert response.json()["approved"] is True


def test_paper_order_event_stream():
    with TestClient(app) as client:
        symbol = client.get("/api/factors").json()  # 确保应用已启动
        account = client.get("/api/paper/account").json()
        # 用账户里有行情的标的下一笔市价买单（市价单必成交，且不受涨跌停限价约束）。
        symbol = account["positions"][0]["symbol"] if account["positions"] else None
        if symbol is None:
            # demo 数据下账户初始无持仓，从行情挑一个标的。
            symbol = "600519.SH"
        submit = client.post(
            "/api/paper/orders",
            json={
                "client_order_id": "evtflow-0001",
                "symbol": symbol,
                "side": "buy",
                "quantity": 100,
                "order_type": "market",
            },
        )
        assert submit.status_code == 200
        order_id = submit.json()["order_id"]
        events = client.get(f"/api/paper/orders/{order_id}/events").json()["events"]

    types = [event["event_type"] for event in events]
    assert types[0] == "ORDER_CREATED"
    # seq 必须从 1 起单调递增。
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    assert any(t in {"FILLED", "REJECTED", "OPEN"} for t in types)


def test_tushare_sync_requires_token(monkeypatch):
    monkeypatch.setattr(
        api_module,
        "settings",
        replace(api_module.settings, tushare_token=""),
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/data/tushare/sync",
            json={
                "start_date": "2026-06-01",
                "end_date": "2026-06-03",
            },
        )

    assert response.status_code == 409
    assert "TOKEN" in response.json()["detail"]


def test_default_broker_is_disabled():
    from quantdev.integrations.broker import (
        BrokerOrder,
        DisabledLiveBroker,
        get_broker,
    )

    broker = get_broker()
    assert isinstance(broker, DisabledLiveBroker)
    assert broker.health_check().healthy is True
    assert broker.get_positions() == []
    try:
        broker.submit_order(
            BrokerOrder("disabled1", "600519.SH", "buy", 100, "limit", 10.0)
        )
        assert False, "disabled broker 不应允许下单"
    except PermissionError:
        pass


def test_mock_live_broker_scenarios(monkeypatch):
    import quantdev.services.calendar as calendar_module
    import quantdev.services.live_guard as guard_module
    from quantdev.integrations.broker import BrokerOrder, MockLiveBroker

    # 开启实盘配置，让守卫放行后才能验证各异常场景。
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
    # 守卫含交易时段闸，测试不受运行时刻影响：强制处于可交易时段。
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

    order = BrokerOrder(
        "mocktest1", "600519.SH", "buy", 200, "limit", 10.0, approved=True
    )

    partial = MockLiveBroker(scenario="partial")
    ack = partial.submit_order(order)
    assert ack.status == "PARTIALLY_FILLED"
    assert 0 < ack.filled_quantity < order.quantity

    reject = MockLiveBroker(scenario="reject")
    assert reject.submit_order(order).accepted is False

    cancel_fail = MockLiveBroker(scenario="cancel_fail")
    assert cancel_fail.cancel_order("mock-000001").accepted is False

    timeout = MockLiveBroker(scenario="timeout")
    try:
        timeout.submit_order(order)
        assert False, "timeout 场景应抛 TimeoutError"
    except TimeoutError:
        pass
    assert timeout.health_check().healthy is False


def test_mock_live_broker_blocked_without_live_config():
    from quantdev.integrations.broker import BrokerOrder, MockLiveBroker

    # 默认配置下实盘总开关未开，即使直接用 MockLiveBroker 也应被守卫拒绝。
    order = BrokerOrder(
        "guarded1", "600519.SH", "buy", 100, "limit", 10.0, approved=True
    )
    try:
        MockLiveBroker(scenario="normal").submit_order(order)
        assert False, "守卫应拦截未开启实盘的下单"
    except PermissionError:
        pass


def test_reconciliation_balanced_with_paper_broker():
    from quantdev.integrations.broker import PaperBroker
    from quantdev.services.reconciliation import reconciliation_service

    with TestClient(app):  # 确保应用已启动、demo 数据就绪
        # PaperBroker 与本地账本读自同一个 OMS，对账必然平衡。
        report = reconciliation_service.run(broker=PaperBroker())

    assert report["status"] == "BALANCED"
    assert report["breaks"] == []
    assert abs(report["cash_diff"]) < 0.01
    # 落库后可按 recon_id 取回。
    fetched = store.get_reconciliation(report["recon_id"])
    assert fetched is not None
    assert fetched["status"] == "BALANCED"


def test_reconciliation_detects_cash_and_position_break():
    from quantdev.integrations.broker import AccountSnapshot, PositionSnapshot
    from quantdev.services.reconciliation import reconciliation_service

    class StubBroker:
        mode = "stub"

        def get_account(self):
            # 故意制造现金差异。
            return AccountSnapshot(
                account_id="stub", cash=12345.0, market_value=0.0, equity=12345.0
            )

        def get_positions(self):
            # 故意制造一笔本地没有的持仓差异。
            return [
                PositionSnapshot(symbol="600519.SH", quantity=500, average_cost=10.0)
            ]

    with TestClient(app):
        report = reconciliation_service.run(broker=StubBroker())

    assert report["status"] == "BREAK"
    types = {item["type"] for item in report["breaks"]}
    assert "cash" in types
    assert "position" in types
    position_break = next(b for b in report["breaks"] if b["type"] == "position")
    assert position_break["symbol"] == "600519.SH"


def test_reconcile_endpoints():
    with TestClient(app) as client:
        triggered = client.post("/api/live/reconcile")
        assert triggered.status_code == 200
        recon_id = triggered.json()["recon_id"]

        listed = client.get("/api/live/reconciliations")
        assert listed.status_code == 200
        assert any(item["recon_id"] == recon_id for item in listed.json()["items"])

        detail = client.get(f"/api/live/reconciliations/{recon_id}")
        assert detail.status_code == 200
        assert detail.json()["recon_id"] == recon_id

        missing = client.get("/api/live/reconciliations/does-not-exist")
        assert missing.status_code == 404


def test_market_clock_sessions():
    from datetime import datetime, timedelta, timezone

    from quantdev.services.calendar import trading_calendar_service

    cn_tz = timezone(timedelta(hours=8))
    # 2026-06-10 是周三（工作日），日历无记录时按工作日兜底为交易日。
    morning = datetime(2026, 6, 10, 10, 0, tzinfo=cn_tz)
    clock = trading_calendar_service.market_clock(morning)
    assert clock.is_trading_day is True
    assert clock.session == "morning"
    assert clock.can_trade is True

    lunch = trading_calendar_service.market_clock(
        datetime(2026, 6, 10, 12, 0, tzinfo=cn_tz)
    )
    assert lunch.session == "lunch_break"
    assert lunch.can_trade is False

    after_close = trading_calendar_service.market_clock(
        datetime(2026, 6, 10, 15, 30, tzinfo=cn_tz)
    )
    assert after_close.session == "closed"
    assert after_close.can_trade is False

    # 2026-06-13 是周六，工作日兜底判定为非交易日。
    weekend = trading_calendar_service.market_clock(
        datetime(2026, 6, 13, 10, 0, tzinfo=cn_tz)
    )
    assert weekend.is_trading_day is False
    assert weekend.can_trade is False


def test_live_order_blocked_outside_trading_session(monkeypatch):
    import quantdev.services.calendar as calendar_module
    import quantdev.services.live_guard as guard_module
    from quantdev.integrations.broker import BrokerOrder, MockLiveBroker

    # 实盘配置全开、订单已审批，仅靠交易时段闸拦截。
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
    # 强制处于非交易时段。
    monkeypatch.setattr(
        calendar_module.trading_calendar_service,
        "market_clock",
        lambda moment=None: calendar_module.MarketClock(
            timestamp="2026-06-13T10:00:00+08:00",
            is_trading_day=False,
            is_trading_session=False,
            session="closed",
            calendar_source="weekday_fallback",
            can_trade=False,
        ),
    )
    order = BrokerOrder(
        "clock-blocked", "600519.SH", "buy", 100, "limit", 10.0, approved=True
    )
    try:
        MockLiveBroker(scenario="normal").submit_order(order)
        assert False, "非交易时段应被守卫拦截"
    except PermissionError as exc:
        assert "非交易时段" in str(exc)


def test_market_clock_endpoint():
    with TestClient(app) as client:
        response = client.get("/api/live/market-clock")
    assert response.status_code == 200
    body = response.json()
    assert set(body) >= {
        "is_trading_day",
        "is_trading_session",
        "session",
        "can_trade",
        "calendar_source",
    }


def test_t1_same_day_buy_not_sellable_next_day_sellable(monkeypatch):
    from datetime import date

    import quantdev.services.execution as execution_module

    with TestClient(app) as client:
        # 第一天：买入 100 股茅台并成交。
        day_one = date(2026, 6, 10)
        monkeypatch.setattr(
            execution_module.trading_calendar_service, "today", lambda: day_one
        )
        buy = client.post(
            "/api/paper/orders",
            json={
                "client_order_id": "t1-buy-0001",
                "symbol": "601899.SH",
                "side": "buy",
                "quantity": 100,
                "order_type": "market",
            },
        )
        assert buy.status_code == 200
        assert buy.json()["status"] == "FILLED"

        # 当日持仓应全部被冻结（可卖 0）。
        account = client.get("/api/paper/account").json()
        position = next(
            p for p in account["positions"] if p["symbol"] == "601899.SH"
        )
        assert position["sellable_quantity"] == 0
        assert position["frozen_quantity"] == position["quantity"]

        # 当日卖出应被 T+1 拒绝（市价单跳过涨跌停限价校验，确保命中的是 T+1）。
        same_day_sell = client.post(
            "/api/paper/orders",
            json={
                "client_order_id": "t1-sell-sameday",
                "symbol": "601899.SH",
                "side": "sell",
                "quantity": 100,
                "order_type": "market",
            },
        )
        assert same_day_sell.status_code == 200
        assert same_day_sell.json()["status"] == "REJECTED"
        assert "T+1" in same_day_sell.json()["reject_reason"]

        # 次日：冻结解除，可卖。
        day_two = date(2026, 6, 11)
        monkeypatch.setattr(
            execution_module.trading_calendar_service, "today", lambda: day_two
        )
        account = client.get("/api/paper/account").json()
        position = next(
            p for p in account["positions"] if p["symbol"] == "601899.SH"
        )
        assert position["sellable_quantity"] == position["quantity"]

        next_day_sell = client.post(
            "/api/paper/orders",
            json={
                "client_order_id": "t1-sell-nextday",
                "symbol": "601899.SH",
                "side": "sell",
                "quantity": 100,
                "order_type": "market",
            },
        )
        assert next_day_sell.status_code == 200
        assert next_day_sell.json()["status"] == "FILLED"


def test_tradability_endpoint_returns_limits():
    with TestClient(app) as client:
        response = client.get("/api/live/tradability/600519.SH")
    assert response.status_code == 200
    body = response.json()
    assert body["symbol"] == "600519.SH"
    assert body["halted"] is False
    assert body["limit_ratio"] == 0.10
    assert body["limit_up"] is not None and body["limit_down"] is not None
    assert body["limit_up"] > body["limit_down"]


def test_buy_limit_above_limit_up_rejected():
    with TestClient(app) as client:
        info = client.get("/api/live/tradability/600519.SH").json()
        order = client.post(
            "/api/paper/orders",
            json={
                "client_order_id": "limit-up-breach",
                "symbol": "600519.SH",
                "side": "buy",
                "quantity": 100,
                "order_type": "limit",
                "limit_price": round(info["limit_up"] + 5, 2),
            },
        )
    assert order.status_code == 200
    body = order.json()
    assert body["status"] == "REJECTED"
    assert "涨停" in body["reject_reason"]


def test_sell_limit_below_limit_down_rejected():
    with TestClient(app) as client:
        info = client.get("/api/live/tradability/600519.SH").json()
        order = client.post(
            "/api/paper/orders",
            json={
                "client_order_id": "limit-down-breach",
                "symbol": "600519.SH",
                "side": "sell",
                "quantity": 100,
                "order_type": "limit",
                "limit_price": max(round(info["limit_down"] - 5, 2), 0.01),
            },
        )
    assert order.status_code == 200
    body = order.json()
    assert body["status"] == "REJECTED"
    assert "跌停" in body["reject_reason"]


def test_halted_symbol_rejected(monkeypatch):
    import quantdev.services.execution as execution_module
    from quantdev.services.tradability import Tradability

    halted = Tradability(
        symbol="600519.SH",
        halted=True,
        halt_reason="疑似停牌：测试注入",
        previous_close=None,
        limit_up=None,
        limit_down=None,
        limit_ratio=0.10,
        is_st=False,
    )
    monkeypatch.setattr(
        execution_module.tradability_service, "evaluate", lambda symbol: halted
    )
    with TestClient(app) as client:
        order = client.post(
            "/api/paper/orders",
            json={
                "client_order_id": "halted-order",
                "symbol": "600519.SH",
                "side": "buy",
                "quantity": 100,
                "order_type": "market",
            },
        )
    assert order.status_code == 200
    body = order.json()
    assert body["status"] == "REJECTED"
    assert "停牌" in body["reject_reason"]
