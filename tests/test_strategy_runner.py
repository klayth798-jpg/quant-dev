from dataclasses import replace
from datetime import date

from quantdev.db import database
from quantdev.models import PaperOrderRequest, StrategyConfigRequest
from quantdev.services.calendar import MarketClock
from quantdev.services.execution import paper_execution_service
from quantdev.services.live_guard import live_guard
from quantdev.services.strategy import strategy_runner_service
from quantdev.store import store, utc_now


def test_strategy_runner_persists_plan_and_is_idempotent(monkeypatch):
    import quantdev.services.execution_router as router_module
    import quantdev.services.strategy as strategy_module

    signal_date = store.snapshot_max_date(store.latest_snapshot_id())
    monkeypatch.setattr(
        strategy_module.trading_calendar_service,
        "market_clock",
        lambda moment=None: MarketClock(
            timestamp="2026-06-04T10:00:00+08:00",
            is_trading_day=True,
            is_trading_session=True,
            session="morning",
            calendar_source="calendar",
            can_trade=True,
        ),
    )
    monkeypatch.setattr(
        strategy_module.trading_calendar_service,
        "latest_completed_trading_day",
        lambda moment=None: date.fromisoformat(signal_date),
    )
    monkeypatch.setattr(
        strategy_module.trading_calendar_service,
        "today",
        lambda: date(2026, 6, 4),
    )
    monkeypatch.setattr(
        router_module,
        "settings",
        replace(router_module.settings, broker_mode="paper"),
    )
    strategy = strategy_runner_service.create(
        StrategyConfigRequest(
            name="动量模拟实盘",
            factor_id="momentum_20",
            top_n=3,
            max_order_notional=20_000,
        )
    )

    first = strategy_runner_service.run_once(strategy["strategy_id"])
    second = strategy_runner_service.run_once(strategy["strategy_id"])

    assert first["status"] in {"COMPLETED", "PARTIAL"}
    assert first["targets"]
    assert first["intents"]
    assert all(item["paper_order_id"] for item in first["intents"])
    assert second["idempotent"] is True
    assert second["run_id"] == first["run_id"]


def test_open_order_reservation_is_released_on_cancel():
    before = paper_execution_service.get_account()
    opened = paper_execution_service.submit(
        PaperOrderRequest(
            client_order_id="reservation-open-1",
            symbol="000001.SZ",
            side="buy",
            quantity=100,
            order_type="limit",
            limit_price=0.01,
        )
    )
    assert opened["status"] == "OPEN"
    assert opened["account"]["reserved_cash"] > 0

    cancelled = paper_execution_service.cancel(opened["order_id"])

    assert cancelled["order"]["status"] == "CANCELLED"
    assert cancelled["account"]["reserved_cash"] == 0
    assert cancelled["account"]["cash"] == before["cash"]


def test_stale_quote_rejects_order():
    with database.transaction(immediate=True) as connection:
        connection.execute(
            """
            UPDATE market_quotes
            SET quote_time = '2020-01-01T00:00:00+00:00'
            WHERE symbol = '000001.SZ'
            """
        )
    result = paper_execution_service.submit(
        PaperOrderRequest(
            client_order_id="stale-quote-order",
            symbol="000001.SZ",
            side="buy",
            quantity=100,
        )
    )
    assert result["status"] == "REJECTED"
    assert "过期" in result["reject_reason"]


def test_daily_loss_activates_kill_switch():
    account = paper_execution_service.get_account()
    today = date.today().isoformat()
    with database.transaction(immediate=True) as connection:
        connection.execute(
            """
            INSERT INTO account_daily_snapshots
                (account_id, trade_date, start_equity, current_equity,
                 daily_pnl, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_id, trade_date) DO UPDATE SET
                start_equity = excluded.start_equity,
                current_equity = excluded.current_equity,
                daily_pnl = excluded.daily_pnl,
                updated_at = excluded.updated_at
            """,
            (
                account["account_id"],
                today,
                account["equity"] + 2_000,
                account["equity"],
                -2_000,
                utc_now(),
            ),
        )

    # get_account 是只读路径，不再产生写库或熔断副作用
    refreshed = paper_execution_service.get_account()
    assert refreshed["daily_pnl"] <= -1_000
    assert live_guard.kill_switch_active() is False

    # 熔断发生在下单（写）路径：触及日亏限额的委托会被拒并激活 Kill Switch
    result = paper_execution_service.submit(
        PaperOrderRequest(
            client_order_id="daily-loss-guard-1",
            symbol="000001.SZ",
            side="buy",
            quantity=100,
        )
    )
    assert result["status"] == "REJECTED"
    assert live_guard.kill_switch_active() is True
