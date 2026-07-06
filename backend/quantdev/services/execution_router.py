from typing import Any, Dict

from quantdev.config import settings
from quantdev.integrations.broker import get_broker
from quantdev.models import PaperOrderRequest
from quantdev.services.execution import paper_execution_service
from quantdev.services.live_execution import live_execution_service
from quantdev.store import store


class ExecutionRouter:
    def configured_mode(self) -> str:
        if settings.broker_mode == "live":
            return "live"
        if settings.broker_mode == "paper":
            return "paper"
        return "disabled"

    def account(self) -> Dict[str, Any]:
        mode = self.configured_mode()
        if mode == "paper":
            return paper_execution_service.get_account()
        if mode == "live":
            broker = get_broker()
            health = broker.health_check()
            if not health.healthy or health.mode != "live":
                raise ValueError("真实 Broker 不可用：{}".format(health.message))
            account = broker.get_account()
            positions = broker.get_positions()
            return {
                "account_id": account.account_id,
                "cash": account.cash,
                "available_cash": account.cash,
                "market_value": account.market_value,
                "equity": account.equity,
                "positions": [
                    {
                        "symbol": item.symbol,
                        "quantity": item.quantity,
                        "sellable_quantity": (
                            item.sellable_quantity
                            if item.sellable_quantity is not None
                            else item.quantity
                        ),
                        "average_cost": item.average_cost,
                    }
                    for item in positions
                ],
            }
        raise ValueError("Broker 模式未启用，策略不会生成交易订单")

    def dispatch_intent(self, intent_id: str) -> Dict[str, Any]:
        intent = store.get_order_intent(intent_id)
        if not intent:
            raise ValueError("订单意图不存在")
        mode = intent["execution_mode"]
        if mode == "paper":
            result = paper_execution_service.submit(
                PaperOrderRequest(
                    client_order_id=intent["client_order_id"],
                    symbol=intent["symbol"],
                    side=intent["side"],
                    quantity=int(intent["quantity"]),
                    order_type=intent["order_type"],
                    limit_price=intent.get("limit_price"),
                    strategy_run_id=intent["run_id"],
                    order_intent_id=intent["intent_id"],
                )
            )
            order = result.get("order") or {}
            status = result.get("status") or order.get("status", "UNKNOWN")
            order_id = result.get("order_id") or order.get("order_id")
            store.update_order_intent(
                intent_id,
                status=status,
                paper_order_id=order_id,
                approval_status="NOT_REQUIRED",
                clear_error=True,
            )
            return {
                "intent_id": intent_id,
                "status": status,
                "order_id": order_id,
            }
        if mode == "live":
            if settings.require_order_approval:
                if intent["approval_status"] == "APPROVED":
                    updated = store.update_order_intent(
                        intent_id,
                        status="APPROVED",
                        approval_status="APPROVED",
                        clear_error=True,
                    )
                    return {
                        "intent_id": intent_id,
                        "status": updated["status"],
                        "order_id": None,
                    }
                updated = store.update_order_intent(
                    intent_id,
                    status="PENDING_APPROVAL",
                    approval_status="PENDING",
                    clear_error=True,
                )
                return {
                    "intent_id": intent_id,
                    "status": updated["status"],
                    "order_id": None,
                }
            result = live_execution_service.submit_intent(
                intent_id, actor="execution-router"
            )
            return {
                "intent_id": intent_id,
                "status": result["order"]["status"],
                "order_id": result["order"]["order_id"],
            }
        raise PermissionError("Broker 模式未启用，订单意图已阻断")


execution_router = ExecutionRouter()
