"""日终对账：对比本地账本与 Broker 端快照，发现资金/持仓差异即告警。

实盘安全的关键一环——本地 OMS 记录的现金与持仓，必须与券商真实账户一致。
任何一笔订单状态误判、漏成交、重复扣费都会在这里暴露为"对账差异(break)"。

对账口径：
  - 现金：两侧金额量化到“分”后逐分比较，差异超过 cash_tolerance_cents 视为差异。
  - 持仓：以 symbol 为键，逐一比对数量；本地有 Broker 无、Broker 有本地无、
    或数量不一致，都记为一条持仓差异。

对账结果 status：
  - BALANCED：无任何差异。
  - BREAK：存在现金或持仓差异，需人工核查（实盘中通常应触发 Kill Switch）。
"""

from typing import Any, Dict, List, Optional

from quantdev.config import settings
from quantdev.integrations.broker import BrokerAdapter, get_broker
from quantdev.money import cents_to_yuan, round_money, to_cents
from quantdev.services.events import EventType, event_bus
from quantdev.services.execution import paper_execution_service
from quantdev.services.live_guard import live_guard
from quantdev.store import store


class ReconciliationService:
    # 现金对账容差（单位：分）。两侧金额都已量化到分，默认 0 表示任何“分”级
    # 差异都视为真实差异——彻底消除浮点边界把 1 分真实漂移当作平衡的旧问题。
    cash_tolerance_cents = 0

    def run(self, broker: Optional[BrokerAdapter] = None) -> Dict[str, Any]:
        """执行一次对账并落库，返回对账报告。"""
        broker = broker or get_broker()
        local = self._local_snapshot(broker)
        remote = self._broker_snapshot(broker)
        breaks = self._diff(local, remote)
        report = {
            "broker_mode": broker.mode,
            "status": "BALANCED" if not breaks else "BREAK",
            "cash_diff": cents_to_yuan(to_cents(remote["cash"]) - to_cents(local["cash"])),
            "breaks": breaks,
            "local": local,
            "broker": remote,
        }
        recon_id = store.save_reconciliation(report)
        report["recon_id"] = recon_id
        store.audit(
            actor="local-user",
            action="run_reconciliation",
            resource_type="reconciliation",
            resource_id=recon_id,
            payload={
                "status": report["status"],
                "broker_mode": broker.mode,
                "break_count": len(breaks),
            },
        )
        event_bus.publish(
            event_type=EventType.RECONCILIATION_BREAK
            if breaks
            else EventType.RECONCILIATION_COMPLETED,
            aggregate_type="reconciliation",
            aggregate_id=recon_id,
            payload={
                "status": report["status"],
                "broker_mode": broker.mode,
                "break_count": len(breaks),
                "cash_diff": report["cash_diff"],
            },
            actor="local-user",
        )
        if (
            breaks
            and settings.auto_kill_on_reconciliation_break
            and broker.mode in {"live", "mock_live"}
            and not live_guard.kill_switch_active()
        ):
            live_guard.activate_kill_switch(
                "对账差异 {} 项，recon_id={}".format(len(breaks), recon_id),
                actor="reconciliation-guard",
            )
        return report

    def _local_snapshot(self, broker: BrokerAdapter) -> Dict[str, Any]:
        if broker.mode in {"live", "mock_live"}:
            snapshot = store.local_live_snapshot()
            if snapshot is None:
                return {
                    "cash": 0.0,
                    "positions": {},
                    "orders": {},
                    "state_missing": True,
                }
            snapshot["orders"] = {
                item["client_order_id"]: {
                    "status": item["status"],
                    "filled_quantity": int(item["filled_quantity"]),
                }
                for item in store.list_live_orders()
            }
            snapshot["state_missing"] = False
            return snapshot
        account = paper_execution_service.get_account()
        return {
            "cash": round_money(account["cash"]),
            "positions": {
                item["symbol"]: int(item["quantity"]) for item in account["positions"]
            },
            "orders": {},
            "state_missing": False,
        }

    def _broker_snapshot(self, broker: BrokerAdapter) -> Dict[str, Any]:
        account = broker.get_account()
        return {
            "cash": round_money(account.cash),
            "positions": {
                item.symbol: int(item.quantity) for item in broker.get_positions()
            },
            "orders": (
                {
                    item.client_order_id: {
                        "status": item.status,
                        "filled_quantity": int(item.filled_quantity),
                    }
                    for item in broker.get_orders()
                }
                if broker.mode in {"live", "mock_live"}
                else {}
            ),
        }

    def _diff(
        self, local: Dict[str, Any], remote: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        breaks: List[Dict[str, Any]] = []
        if local.get("state_missing"):
            breaks.append(
                {
                    "type": "local_state",
                    "symbol": None,
                    "local": None,
                    "broker": "available",
                    "diff": "missing",
                }
            )
        cash_diff_cents = to_cents(remote["cash"]) - to_cents(local["cash"])
        if abs(cash_diff_cents) > self.cash_tolerance_cents:
            breaks.append(
                {
                    "type": "cash",
                    "symbol": None,
                    "local": local["cash"],
                    "broker": remote["cash"],
                    "diff": cents_to_yuan(cash_diff_cents),
                }
            )
        symbols = sorted(set(local["positions"]) | set(remote["positions"]))
        for symbol in symbols:
            local_qty = local["positions"].get(symbol, 0)
            broker_qty = remote["positions"].get(symbol, 0)
            if local_qty != broker_qty:
                breaks.append(
                    {
                        "type": "position",
                        "symbol": symbol,
                        "local": local_qty,
                        "broker": broker_qty,
                        "diff": broker_qty - local_qty,
                    }
                )
        client_ids = sorted(set(local.get("orders", {})) | set(remote.get("orders", {})))
        for client_order_id in client_ids:
            local_order = local.get("orders", {}).get(client_order_id)
            broker_order = remote.get("orders", {}).get(client_order_id)
            if local_order != broker_order:
                breaks.append(
                    {
                        "type": "order",
                        "symbol": None,
                        "client_order_id": client_order_id,
                        "local": local_order,
                        "broker": broker_order,
                        "diff": "status_or_fill",
                    }
                )
        return breaks


reconciliation_service = ReconciliationService()
