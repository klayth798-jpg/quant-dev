"""P2 测试：数据库后端抽象层 + 全局领域事件总线。

覆盖：
  - 后端工厂：默认 sqlite；postgres 显式未实现报错；未知后端报错。
  - 事件总线：先持久化再分发；订阅者收到事件；订阅者异常不影响落库。
  - 端到端：下单 / 对账 / Kill Switch 会写入 domain_events，可经 /api/events 查询。
"""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from quantdev.api import app
from quantdev.store import store


def test_database_factory_defaults_to_sqlite():
    import quantdev.db as db_module

    backend = db_module.create_database()
    assert backend.backend == "sqlite"


def test_database_factory_postgres_not_implemented(monkeypatch):
    import quantdev.db as db_module

    monkeypatch.setattr(
        db_module, "settings", replace(db_module.settings, database_backend="postgres")
    )
    with pytest.raises(NotImplementedError):
        db_module.create_database()


def test_database_factory_unknown_backend(monkeypatch):
    import quantdev.db as db_module

    monkeypatch.setattr(
        db_module, "settings", replace(db_module.settings, database_backend="mysql")
    )
    with pytest.raises(ValueError):
        db_module.create_database()


def test_event_bus_persists_and_dispatches():
    from quantdev.services.events import EventBus

    bus = EventBus()
    received = []
    bus.subscribe("test.event", lambda evt: received.append(evt))

    event = bus.publish(
        event_type="test.event",
        aggregate_type="unit",
        aggregate_id="agg-1",
        payload={"hello": "world"},
        actor="tester",
    )

    # 订阅者收到的就是已落库的事件。
    assert len(received) == 1
    assert received[0]["event_id"] == event["event_id"]
    # 已持久化，可查询。
    stored = store.list_domain_events(event_type="test.event")
    assert any(item["event_id"] == event["event_id"] for item in stored)
    assert stored[0]["payload"] == {"hello": "world"}


def test_event_bus_seq_monotonic():
    from quantdev.services.events import EventBus

    bus = EventBus()
    first = bus.publish("seq.test", "unit", "a", {})
    second = bus.publish("seq.test", "unit", "b", {})
    assert second["seq"] == first["seq"] + 1


def test_event_bus_subscriber_error_does_not_break_persistence():
    from quantdev.services.events import EventBus

    bus = EventBus()

    def boom(_evt):
        raise RuntimeError("subscriber failed")

    ok_received = []
    bus.subscribe("err.event", boom)
    bus.subscribe("err.event", lambda evt: ok_received.append(evt))

    event = bus.publish("err.event", "unit", "x", {"n": 1})

    # 一个订阅者抛错不影响其它订阅者，也不影响事件落库。
    assert len(ok_received) == 1
    stored = store.list_domain_events(event_type="err.event")
    assert any(item["event_id"] == event["event_id"] for item in stored)


def test_order_publishes_domain_event():
    with TestClient(app) as client:
        order = client.post(
            "/api/paper/orders",
            json={
                "client_order_id": "evt-order-1",
                "symbol": "600519.SH",
                "side": "buy",
                "quantity": 100,
                "order_type": "market",
            },
        )
        assert order.status_code == 200
        order_id = order.json()["order_id"]

        events = client.get(
            "/api/events", params={"aggregate_type": "paper_order"}
        ).json()["items"]

    assert any(e["aggregate_id"] == order_id for e in events)
    assert any(e["event_type"].startswith("order.") for e in events)


def test_kill_switch_publishes_domain_event():
    with TestClient(app) as client:
        client.post(
            "/api/live/kill-switch",
            json={"active": True, "reason": "事件测试"},
        )
        events = client.get(
            "/api/events", params={"event_type": "kill_switch.activated"}
        ).json()["items"]
    assert len(events) >= 1
    assert events[0]["payload"]["reason"] == "事件测试"


def test_reconciliation_publishes_domain_event():
    from quantdev.integrations.broker import PaperBroker
    from quantdev.services.reconciliation import reconciliation_service

    with TestClient(app) as client:
        report = reconciliation_service.run(broker=PaperBroker())
        events = client.get(
            "/api/events", params={"aggregate_type": "reconciliation"}
        ).json()["items"]

    assert any(e["aggregate_id"] == report["recon_id"] for e in events)


def test_events_endpoint_limit_and_filter():
    with TestClient(app) as client:
        # 制造两类事件。
        client.post(
            "/api/live/kill-switch", json={"active": True, "reason": "a"}
        )
        client.post(
            "/api/live/kill-switch", json={"active": False, "reason": "b"}
        )
        activated = client.get(
            "/api/events", params={"event_type": "kill_switch.activated"}
        ).json()["items"]
        deactivated = client.get(
            "/api/events", params={"event_type": "kill_switch.deactivated"}
        ).json()["items"]
        limited = client.get("/api/events", params={"limit": 1}).json()["items"]

    assert all(e["event_type"] == "kill_switch.activated" for e in activated)
    assert all(e["event_type"] == "kill_switch.deactivated" for e in deactivated)
    assert len(limited) == 1
