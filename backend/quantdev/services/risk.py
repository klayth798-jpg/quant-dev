from dataclasses import dataclass
from typing import Any, Dict, List

from quantdev.models import RiskCheckRequest
from quantdev.store import store


@dataclass(frozen=True)
class RiskPolicy:
    max_single_position: float = 0.25
    max_total_exposure: float = 1.0
    max_daily_turnover: float = 0.50
    max_portfolio_drawdown: float = 0.15
    max_order_notional_weight: float = 0.20


class RiskService:
    def __init__(self, policy: RiskPolicy = None):
        self.policy = policy or RiskPolicy()

    def evaluate(self, request: RiskCheckRequest) -> Dict[str, Any]:
        breaches: List[Dict[str, str]] = []
        weights_by_symbol: Dict[str, float] = {}
        for position in request.positions:
            weights_by_symbol[position.symbol] = (
                weights_by_symbol.get(position.symbol, 0.0) + position.weight
            )
        total_weight = sum(weights_by_symbol.values())
        for symbol, weight in weights_by_symbol.items():
            if weight > self.policy.max_single_position:
                breaches.append(
                    {
                        "rule_code": "MAX_SINGLE_POSITION",
                        "severity": "high",
                        "message": "{} 仓位 {:.1%} 超过 {:.1%}".format(
                            symbol,
                            weight,
                            self.policy.max_single_position,
                        ),
                    }
                )
        checks = [
            (
                total_weight > self.policy.max_total_exposure,
                "MAX_TOTAL_EXPOSURE",
                "high",
                "总敞口 {:.1%} 超过 {:.1%}".format(
                    total_weight, self.policy.max_total_exposure
                ),
            ),
            (
                request.proposed_turnover > self.policy.max_daily_turnover,
                "MAX_DAILY_TURNOVER",
                "medium",
                "计划换手率 {:.1%} 超过 {:.1%}".format(
                    request.proposed_turnover, self.policy.max_daily_turnover
                ),
            ),
            (
                request.current_drawdown > self.policy.max_portfolio_drawdown,
                "MAX_DRAWDOWN",
                "critical",
                "当前回撤 {:.1%} 触发组合熔断线 {:.1%}".format(
                    request.current_drawdown, self.policy.max_portfolio_drawdown
                ),
            ),
            (
                request.order_notional_weight > self.policy.max_order_notional_weight,
                "MAX_ORDER_NOTIONAL",
                "high",
                "单笔委托占净值 {:.1%} 超过 {:.1%}".format(
                    request.order_notional_weight,
                    self.policy.max_order_notional_weight,
                ),
            ),
        ]
        for failed, code, severity, message in checks:
            if failed:
                breaches.append(
                    {"rule_code": code, "severity": severity, "message": message}
                )
        return {
            "approved": not breaches,
            "decision": "APPROVED" if not breaches else "REJECTED",
            "breaches": breaches,
            "total_exposure": round(total_weight, 4),
            "policy": {
                "max_single_position": self.policy.max_single_position,
                "max_total_exposure": self.policy.max_total_exposure,
                "max_daily_turnover": self.policy.max_daily_turnover,
                "max_portfolio_drawdown": self.policy.max_portfolio_drawdown,
                "max_order_notional_weight": self.policy.max_order_notional_weight,
            },
        }

    def check(self, request: RiskCheckRequest) -> Dict[str, Any]:
        result = self.evaluate(request)
        for breach in result["breaches"]:
            store.save_risk_event(
                breach["severity"],
                breach["rule_code"],
                breach["message"],
                request.model_dump(),
            )
        return result


risk_service = RiskService()
