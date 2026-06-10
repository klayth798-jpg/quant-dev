"""日终对账：对比本地账本与 Broker 端快照，发现资金/持仓差异即告警。

实盘安全的关键一环——本地 OMS 记录的现金与持仓，必须与券商真实账户一致。
任何一笔订单状态误判、漏成交、重复扣费都会在这里暴露为"对账差异(break)"。

对账口径：
  - 现金：|本地现金 - Broker 现金| 超过 cash_tolerance 视为差异。
  - 持仓：以 symbol 为键，逐一比对数量；本地有 Broker 无、Broker 有本地无、
    或数量不一致，都记为一条持仓差异。

对账结果 status：
  - BALANCED：无任何差异。
  - BREAK：存在现金或持仓差异，需人工核查（实盘中通常应触发 Kill Switch）。
"""

from typing import Any, Dict, List, Optional

from quantdev.integrations.broker import BrokerAdapter, get_broker
from quantdev.services.events import EventType, event_bus
from quantdev.services.execution import paper_execution_service
from quantdev.store import store


class ReconciliationService:
    cash_tolerance = 0.01

    def run(self, broker: Optional[BrokerAdapter] = None) -> Dict[str, Any]:
        """执行一次对账并落库，返回对账报告。"""
        broker = broker or get_broker()
        local = self._local_snapshot()
        remote = self._broker_snapshot(broker)
        breaks = self._diff(local, remote)
        report = {
            "broker_mode": broker.mode,
            "status": "BALANCED" if not breaks else "BREAK",
            "cash_diff": round(remote["cash"] - local["cash"], 2),
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
        return report

    def _local_snapshot(self) -> Dict[str, Any]:
        account = paper_execution_service.get_account()
        return {
            "cash": round(float(account["cash"]), 2),
            "positions": {
                item["symbol"]: int(item["quantity"]) for item in account["positions"]
            },
        }

    def _broker_snapshot(self, broker: BrokerAdapter) -> Dict[str, Any]:
        account = broker.get_account()
        return {
            "cash": round(float(account.cash), 2),
            "positions": {
                item.symbol: int(item.quantity) for item in broker.get_positions()
            },
        }

    def _diff(
        self, local: Dict[str, Any], remote: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        breaks: List[Dict[str, Any]] = []
        cash_diff = remote["cash"] - local["cash"]
        if abs(cash_diff) > self.cash_tolerance:
            breaks.append(
                {
                    "type": "cash",
                    "symbol": None,
                    "local": local["cash"],
                    "broker": remote["cash"],
                    "diff": round(cash_diff, 2),
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
        return breaks


reconciliation_service = ReconciliationService()
