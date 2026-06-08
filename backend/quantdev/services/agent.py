from typing import Any, Dict, List

from quantdev.config import settings
from quantdev.models import AgentResearchRequest
from quantdev.store import store


class ResearchAgentService:
    """Read-only research facade.

    The service intentionally has no broker, order or cash-ledger dependency.
    A remote deep-research agent can later be connected behind this facade.
    """

    def research(self, request: AgentResearchRequest) -> Dict[str, Any]:
        evidence: List[Dict[str, Any]] = []
        factor = store.get_factor(request.factor_id) if request.factor_id else None
        backtest = (
            store.get_backtest(request.backtest_run_id)
            if request.backtest_run_id
            else None
        )
        if factor:
            evidence.append(
                {
                    "type": "factor_definition",
                    "id": factor["factor_id"],
                    "name": factor["name"],
                    "version": factor["version"],
                    "status": factor["status"],
                }
            )
        if backtest:
            evidence.append(
                {
                    "type": "backtest",
                    "id": backtest["run_id"],
                    "name": backtest["name"],
                    "metrics": backtest["metrics"],
                }
            )
        latest_backtests = store.list_backtests(limit=3)
        if not backtest and latest_backtests:
            evidence.append(
                {
                    "type": "recent_backtests",
                    "items": [
                        {
                            "run_id": item["run_id"],
                            "factor_id": item["factor_id"],
                            "metrics": item["metrics"],
                        }
                        for item in latest_backtests
                    ],
                }
            )

        lines = [
            "研究问题：{}".format(request.question),
            "",
            "当前结论：",
        ]
        if backtest:
            metrics = backtest["metrics"]
            lines.extend(
                [
                    "- 回测年化收益率为 {:.2%}，夏普比率为 {:.2f}。".format(
                        metrics.get("annualized_return", 0),
                        metrics.get("sharpe_ratio", 0),
                    ),
                    "- 最大回撤为 {:.2%}，总换手率为 {:.2f} 倍。".format(
                        metrics.get("max_drawdown", 0),
                        metrics.get("turnover_ratio", 0),
                    ),
                ]
            )
        elif factor:
            lines.append(
                "- 已定位因子 {} v{}，需要先运行标准评估与成本后回测。".format(
                    factor["name"], factor["version"]
                )
            )
        else:
            lines.append("- 当前请求未绑定具体因子或回测，将其视为研究假设。")
        lines.extend(
            [
                "",
                "研究约束：",
                "- 结果仅用于研究，不构成交易指令。",
                "- Agent 没有订单、账户和资金写权限。",
                "- 因子必须通过数据泄漏检查、样本外检验和人工审批后才能进入模拟盘。",
            ]
        )
        mode = "remote-ready" if settings.deep_research_base_url else "local-readonly"
        return {
            "mode": mode,
            "answer": "\n".join(lines),
            "evidence": evidence,
            "permissions": ["read:factor", "read:backtest", "read:risk"],
            "forbidden_permissions": ["write:order", "write:cash", "write:position"],
        }


research_agent_service = ResearchAgentService()

