import pytest
from quantdev.db import database
from quantdev.models import (
    AgentResearchRequest,
    BacktestRequest,
    PaperOrderRequest,
    PositionInput,
    RiskCheckRequest,
)
from quantdev.services.agent import research_agent_service
from quantdev.services.backtest import backtest_service
from quantdev.services.execution import paper_execution_service
from quantdev.services.factors import factor_service
from quantdev.services.risk import risk_service
from quantdev.store import store


def test_factor_evaluation_is_reproducible():
    first = factor_service.evaluate("momentum_20", forward_days=5)
    second = factor_service.evaluate("momentum_20", forward_days=5)

    assert first["metrics"] == second["metrics"]
    assert first["metrics"]["observations"] > 1000
    assert first["snapshot_id"] == "demo-cn-equity-20260605-v1"


def test_momentum_uses_adjusted_close():
    rows = [
        {"close": 10.0, "adj_factor": 1.0, "volume": 100.0}
        for _ in range(21)
    ]
    rows[-1] = {"close": 5.0, "adj_factor": 2.0, "volume": 100.0}

    assert factor_service._factor_value("momentum_20", rows, 20) == 0.0


def test_backtest_generates_equity_and_trades():
    result = backtest_service.run(
        BacktestRequest(factor_id="momentum_20", top_n=3, rebalance_days=5)
    )

    assert result["status"] == "completed"
    assert len(result["equity_curve"]) > 300
    assert len(result["trades"]) > 0
    assert result["metrics"]["final_equity"] > 0
    assert result["metrics"]["max_drawdown"] <= 0


def test_multi_index_universe_uses_each_latest_snapshot():
    with database.transaction() as connection:
        connection.executemany(
            """
            INSERT INTO indices (index_code, name, source, updated_at)
            VALUES (?, ?, 'test', '2026-06-01')
            """,
            [
                ("000300.SH", "沪深300"),
                ("000905.SH", "中证500"),
            ],
        )
        connection.executemany(
            """
            INSERT INTO index_constituents
                (index_code, symbol, trade_date, weight, source, sync_run_id)
            VALUES (?, ?, ?, 1, 'test', 'run-test')
            """,
            [
                ("000300.SH", "000858.SZ", "2026-05-28"),
                ("000300.SH", "000001.SZ", "2026-06-01"),
                ("000905.SH", "000333.SZ", "2026-05-29"),
            ],
        )

    universe = backtest_service._build_universe(["000300.SH", "000905.SH"])

    assert backtest_service._universe_at(universe, "2026-06-01") == {
        "000001.SZ",
        "000333.SZ",
    }


def test_risk_engine_rejects_concentrated_portfolio():
    result = risk_service.check(
        RiskCheckRequest(
            positions=[PositionInput(symbol="600519.SH", weight=0.45)],
            proposed_turnover=0.7,
            current_drawdown=0.16,
            order_notional_weight=0.3,
        )
    )

    assert result["approved"] is False
    assert {item["rule_code"] for item in result["breaches"]} == {
        "MAX_SINGLE_POSITION",
        "MAX_DAILY_TURNOVER",
        "MAX_DRAWDOWN",
        "MAX_ORDER_NOTIONAL",
    }


def test_risk_engine_aggregates_duplicate_symbols():
    result = risk_service.check(
        RiskCheckRequest(
            positions=[
                PositionInput(symbol="600519.SH", weight=0.2),
                PositionInput(symbol="600519.SH", weight=0.2),
            ]
        )
    )

    assert result["approved"] is False
    assert result["breaches"][0]["rule_code"] == "MAX_SINGLE_POSITION"


def test_paper_order_is_filled_and_idempotent():
    request = PaperOrderRequest(
        client_order_id="test-order-0001",
        symbol="000001.SZ",
        side="buy",
        quantity=100,
    )
    first = paper_execution_service.submit(request)
    second = paper_execution_service.submit(request)

    assert first["status"] == "FILLED"
    assert first["account"]["positions"][0]["quantity"] == 100
    assert second["idempotent"] is True
    assert second["account"]["positions"][0]["quantity"] == 100


def test_non_marketable_limit_order_remains_open():
    result = paper_execution_service.submit(
        PaperOrderRequest(
            client_order_id="test-limit-0001",
            symbol="000001.SZ",
            side="buy",
            quantity=100,
            order_type="limit",
            limit_price=0.01,
        )
    )

    assert result["status"] == "OPEN"
    assert result["fill_price"] is None
    assert result["account"]["positions"] == []


def test_paper_buy_reuses_portfolio_risk_policy():
    price = store.list_prices(
        symbol="000001.SZ",
        snapshot_id=store.latest_snapshot_id(),
    )[-1]["close"]
    quantity = int(150_000 / price / 100) * 100

    first = paper_execution_service.submit(
        PaperOrderRequest(
            client_order_id="test-risk-order-1",
            symbol="000001.SZ",
            side="buy",
            quantity=quantity,
        )
    )
    second = paper_execution_service.submit(
        PaperOrderRequest(
            client_order_id="test-risk-order-2",
            symbol="000001.SZ",
            side="buy",
            quantity=quantity,
        )
    )

    assert first["status"] == "FILLED"
    assert second["status"] == "REJECTED"
    assert "仓位" in second["reject_reason"]


def test_idempotency_key_cannot_be_reused_for_another_order():
    paper_execution_service.submit(
        PaperOrderRequest(
            client_order_id="test-order-conflict",
            symbol="000001.SZ",
            side="buy",
            quantity=100,
        )
    )

    with pytest.raises(ValueError, match="client_order_id"):
        paper_execution_service.submit(
            PaperOrderRequest(
                client_order_id="test-order-conflict",
                symbol="000333.SZ",
                side="buy",
                quantity=100,
            )
        )


def test_agent_has_no_execution_permissions():
    result = research_agent_service.research(
        AgentResearchRequest(
            question="分析动量因子",
            factor_id="momentum_20",
        )
    )

    assert "write:order" in result["forbidden_permissions"]
    assert "write:order" not in result["permissions"]
