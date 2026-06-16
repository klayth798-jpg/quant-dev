import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from quantdev.config import settings
from quantdev.integrations.broker import BrokerAdapter, BrokerOrder, get_broker
from quantdev.services.pretrade import pretrade_risk_service
from quantdev.store import new_id, store

ACTIVE_LIVE_STATUSES = {
    "SUBMITTING",
    "SUBMITTED",
    "OPEN",
    "PARTIALLY_FILLED",
    "UNKNOWN",
    "CANCEL_PENDING",
}
TERMINAL_LIVE_STATUSES = {
    "FILLED",
    "CANCELLED",
    "PARTIALLY_CANCELLED",
    "REJECTED",
}


class LiveExecutionService:
    @staticmethod
    def request_hash(intent: Dict[str, Any]) -> str:
        payload = {
            "client_order_id": intent["client_order_id"],
            "symbol": intent["symbol"],
            "side": intent["side"],
            "quantity": int(intent["quantity"]),
            "order_type": intent["order_type"],
            "limit_price": (
                float(intent["limit_price"])
                if intent.get("limit_price") is not None
                else None
            ),
        }
        encoded = json.dumps(
            payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def approve_intent(
        self,
        intent_id: str,
        *,
        actor: str = "local-admin",
        reason: str = "",
    ) -> Dict[str, Any]:
        intent = store.get_order_intent(intent_id)
        if not intent:
            raise ValueError("订单意图不存在")
        if intent["execution_mode"] != "live":
            raise ValueError("只有 live 订单意图需要人工审批")
        if intent["status"] in TERMINAL_LIVE_STATUSES:
            raise ValueError("终态订单意图不能审批")
        if intent.get("live_order_id"):
            order = store.get_live_order(intent["live_order_id"])
            if order and order["status"] in ACTIVE_LIVE_STATUSES:
                raise ValueError("订单已经提交券商，不能重新审批")
        request_hash = self.request_hash(intent)
        expires_at = (
            datetime.now(timezone.utc)
            + timedelta(seconds=settings.approval_ttl_seconds)
        ).isoformat()
        approval = store.save_order_approval(
            intent_id,
            request_hash,
            actor,
            reason,
            expires_at,
        )
        store.audit(
            actor=actor,
            action="approve_order_intent",
            resource_type="order_intent",
            resource_id=intent_id,
            payload={
                "approval_id": approval["approval_id"],
                "request_hash": request_hash,
                "expires_at": expires_at,
            },
        )
        return {"intent": store.get_order_intent(intent_id), "approval": approval}

    def reject_intent(
        self,
        intent_id: str,
        reason: str,
        *,
        actor: str = "local-admin",
    ) -> Dict[str, Any]:
        intent = store.get_order_intent(intent_id)
        if not intent:
            raise ValueError("订单意图不存在")
        if intent.get("live_order_id"):
            order = store.get_live_order(intent["live_order_id"])
            if order and order["status"] in ACTIVE_LIVE_STATUSES:
                raise ValueError("订单已提交券商，请先撤单并确认终态")
        result = store.reject_order_intent(intent_id, reason, actor)
        self._refresh_strategy(result["run_id"])
        return result

    def submit_intent(
        self,
        intent_id: str,
        broker: Optional[BrokerAdapter] = None,
    ) -> Dict[str, Any]:
        intent = store.get_order_intent(intent_id)
        if not intent:
            raise ValueError("订单意图不存在")
        if intent["execution_mode"] != "live":
            raise ValueError("该订单意图不是 live 模式")

        # 串行锁：同一 client_order_id 的提交禁止并发。否则两个并发请求会同时通过
        # 下面的“无既有订单”判断、各自向券商下单，造成一个意图变两笔真实委托。
        lock_key = "live-submit:{}".format(intent["client_order_id"])
        lock_owner = new_id("submit")
        if not store.acquire_lease(
            lock_key, lock_owner, max(settings.runner_lease_seconds, 30)
        ):
            raise ValueError("该订单正在提交中，请勿并发重复提交")
        try:
            return self._submit_intent_locked(intent_id, intent, broker)
        finally:
            store.release_lease(lock_key, lock_owner)

    def _submit_intent_locked(
        self,
        intent_id: str,
        intent: Dict[str, Any],
        broker: Optional[BrokerAdapter],
    ) -> Dict[str, Any]:
        existing = (
            store.get_live_order(intent["live_order_id"])
            if intent.get("live_order_id")
            else store.get_live_order_by_client_id(intent["client_order_id"])
        )
        if existing and existing["status"] in (
            ACTIVE_LIVE_STATUSES | TERMINAL_LIVE_STATUSES
        ):
            return {"idempotent": True, "order": existing}

        approval = self._valid_approval(intent)
        broker = broker or get_broker()
        try:
            pretrade_risk_service.check(intent, broker, approved=True)
        except Exception as exc:
            store.update_order_intent(
                intent_id,
                status="BLOCKED",
                error_message=str(exc),
            )
            self._refresh_strategy(intent["run_id"])
            raise
        order, created = store.create_live_order(
            intent,
            self.request_hash(intent),
            approval["approval_id"] if approval else None,
        )
        if not created:
            # 既有订单：另一并发提交（或先前的提交）已创建并下单，绝不重复发送。
            self._refresh_strategy(intent["run_id"])
            return {"idempotent": True, "order": order}
        broker_request = BrokerOrder(
            client_order_id=intent["client_order_id"],
            symbol=intent["symbol"],
            side=intent["side"],
            quantity=int(intent["quantity"]),
            order_type=intent["order_type"],
            limit_price=float(intent.get("limit_price") or 0),
            approved=True,
        )
        try:
            ack = broker.submit_order(broker_request)
        except TimeoutError as exc:
            snapshot = broker.query_order_by_client_id(intent["client_order_id"])
            if snapshot:
                order = self._apply_snapshot(order["order_id"], snapshot)
            else:
                order = store.update_live_order(
                    order["order_id"],
                    status="UNKNOWN",
                    last_error=str(exc),
                )
            self._refresh_strategy(intent["run_id"])
            return {"idempotent": False, "order": order, "timeout": True}
        except Exception as exc:
            order = store.update_live_order(
                order["order_id"],
                status="REJECTED",
                last_error=str(exc),
            )
            self._refresh_strategy(intent["run_id"])
            raise

        order = store.update_live_order(
            order["order_id"],
            status=ack.status,
            broker_order_id=ack.broker_order_id or None,
            filled_quantity=ack.filled_quantity,
            last_error=ack.message or None,
        )
        self._refresh_strategy(intent["run_id"])
        return {"idempotent": False, "order": order}

    def cancel_order(
        self,
        order_id: str,
        broker: Optional[BrokerAdapter] = None,
    ) -> Dict[str, Any]:
        order = store.get_live_order(order_id)
        if not order:
            raise ValueError("实盘订单不存在")
        if order["status"] in TERMINAL_LIVE_STATUSES:
            return {"idempotent": True, "order": order}
        if not order.get("broker_order_id"):
            raise ValueError("订单尚无券商委托号，必须先查询确认后再撤单")
        broker = broker or get_broker()
        store.update_live_order(order_id, status="CANCEL_PENDING")
        try:
            ack = broker.cancel_order(order["broker_order_id"])
        except TimeoutError as exc:
            result = store.update_live_order(
                order_id, status="UNKNOWN", last_error=str(exc)
            )
            self._refresh_strategy_for_order(result)
            return {"idempotent": False, "order": result, "timeout": True}
        result = store.update_live_order(
            order_id,
            status=ack.status,
            broker_order_id=ack.broker_order_id,
            last_error=ack.message or None,
        )
        self._refresh_strategy_for_order(result)
        return {"idempotent": False, "order": result}

    def sync_broker(
        self, broker: Optional[BrokerAdapter] = None
    ) -> Dict[str, Any]:
        broker = broker or get_broker()
        updated = 0
        for snapshot in broker.get_orders():
            local = store.get_live_order_by_client_id(snapshot.client_order_id)
            if not local:
                continue
            self._apply_snapshot(local["order_id"], snapshot)
            updated += 1

        trades_added = 0
        for trade in broker.get_trades():
            local = next(
                (
                    item
                    for item in store.list_live_orders()
                    if item.get("broker_order_id") == trade.broker_order_id
                ),
                None,
            )
            if not local:
                continue
            trades_added += int(
                store.save_live_trade(
                    local["order_id"],
                    {
                        "broker_trade_id": trade.trade_id,
                        "broker_order_id": trade.broker_order_id,
                        "symbol": trade.symbol,
                        "side": trade.side,
                        "quantity": trade.quantity,
                        "price": trade.price,
                        "fees": trade.fees,
                        "traded_at": trade.traded_at
                        or datetime.now(timezone.utc).isoformat(),
                    },
                )
            )

        account = broker.get_account()
        positions = broker.get_positions()
        store.sync_live_account_state(
            account.account_id,
            account.cash,
            [
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
        )
        for order in store.list_live_orders():
            self._refresh_strategy_for_order(order)
        return {
            "orders_updated": updated,
            "trades_added": trades_added,
            "account_id": account.account_id,
        }

    def _valid_approval(self, intent: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not settings.require_order_approval:
            return None
        approval = store.latest_order_approval(intent["intent_id"])
        if not approval or approval["status"] != "APPROVED":
            raise PermissionError("订单意图尚未审批")
        now = datetime.now(timezone.utc)
        expires_at = datetime.fromisoformat(approval["expires_at"]).astimezone(
            timezone.utc
        )
        if expires_at <= now:
            store.update_order_intent(
                intent["intent_id"],
                status="PENDING_APPROVAL",
                approval_status="EXPIRED",
                error_message="审批已过期",
            )
            raise PermissionError("订单审批已过期")
        if approval["request_hash"] != self.request_hash(intent):
            store.update_order_intent(
                intent["intent_id"],
                status="PENDING_APPROVAL",
                approval_status="INVALIDATED",
                error_message="订单内容已变化，原审批失效",
            )
            raise PermissionError("订单内容已变化，必须重新审批")
        return approval

    @staticmethod
    def _apply_snapshot(order_id: str, snapshot) -> Dict[str, Any]:
        return store.update_live_order(
            order_id,
            status=snapshot.status,
            broker_order_id=snapshot.broker_order_id,
            filled_quantity=snapshot.filled_quantity,
            average_fill_price=snapshot.average_fill_price,
        )

    @staticmethod
    def _refresh_strategy(run_id: str) -> None:
        from quantdev.services.strategy import strategy_runner_service

        strategy_runner_service.refresh_run(run_id)

    def _refresh_strategy_for_order(self, order: Dict[str, Any]) -> None:
        intent = store.get_order_intent(order["intent_id"])
        if intent:
            self._refresh_strategy(intent["run_id"])


live_execution_service = LiveExecutionService()
