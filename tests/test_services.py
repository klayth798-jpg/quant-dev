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


def test_factor_evaluation_is_reproducible():
    first = factor_service.evaluate("momentum_20", forward_days=5)
    second = factor_service.evaluate("momentum_20", forward_days=5)

    assert first["metrics"] == second["metrics"]
    assert first["metrics"]["observations"] > 1000
    assert first["snapshot_id"] == "demo-cn-equity-20260605-v1"


def test_backtest_generates_equity_and_trades():
    result = backtest_service.run(
        BacktestRequest(factor_id="momentum_20", top_n=3, rebalance_days=5)
    )

    assert result["status"] == "completed"
    assert len(result["equity_curve"]) > 300
    assert len(result["trades"]) > 0
    assert result["metrics"]["final_equity"] > 0
    assert result["metrics"]["max_drawdown"] <= 0


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


def test_agent_has_no_execution_permissions():
    result = research_agent_service.research(
        AgentResearchRequest(
            question="分析动量因子",
            factor_id="momentum_20",
        )
    )

    assert "write:order" in result["forbidden_permissions"]
    assert "write:order" not in result["permissions"]

