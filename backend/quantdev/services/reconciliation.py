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

import json
from typing import Any, Dict, List, Optional

from quantdev.config import settings
from quantdev.integrations.broker import BrokerAdapter, get_broker
from quantdev.money import cents_to_yuan, round_money, to_cents
from quantdev.services.events import EventType, event_bus
from quantdev.services.execution import paper_execution_service
from quantdev.services.live_guard import live_guard
from quantdev.store import store


class ReconciliationService:
    baseline_flag = "live_ledger_baseline"

    @property
    def cash_tolerance_cents(self) -> int:
        # 现金对账容差（分），由 QUANTDEV_RECONCILIATION_CASH_TOLERANCE_CENTS 配置。
        # 默认 0（精确到分）；接真实券商时可放宽几分，吸收券商费用/逐笔舍入口径差异，
        # 避免无害尾差误触发自动熔断。
        return settings.reconciliation_cash_tolerance_cents

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

    def _ledger_baseline(self, broker: BrokerAdapter) -> Dict[str, Any]:
        """获取/建立影子账本基线锚点（recon-1 基线双算修复）。

        首次对账时以“当前券商现金/持仓 + 截至此刻已记录的成交集合”作为基线，之后只检测
        基线之后新产生的漂移，避免把基线现金里已包含的历史成交再减一遍而误触发熔断。
        """
        existing = store.get_flag(self.baseline_flag)
        if existing and existing.get("value"):
            try:
                return json.loads(existing["value"])
            except (ValueError, TypeError):
                pass
        account = broker.get_account()
        baseline = {
            "cash": round_money(account.cash),
            "positions": {
                item.symbol: int(item.quantity) for item in broker.get_positions()
            },
            "trade_ids": store.derive_live_ledger()["trade_ids"],
        }
        store.set_flag(
            self.baseline_flag,
            json.dumps(baseline),
            "建立影子账本基线",
            "reconciliation",
        )
        return baseline

    def _local_snapshot(self, broker: BrokerAdapter) -> Dict[str, Any]:
        if broker.mode in {"live", "mock_live"}:
            # 用独立推导的影子账本（基线锚点 + 之后的成交流水）作为“本地”一侧，而不是
            # 券商状态的本地副本，这样才能与券商真值真正三方比对（recon-1）。
            baseline = self._ledger_baseline(broker)
            ledger = store.derive_live_ledger(baseline=baseline)
            if not ledger["has_baseline"]:
                return {
                    "cash": 0.0,
                    "positions": {},
                    "orders": {},
                    "trade_ids": [],
                    "state_missing": True,
                }
            return {
                "cash": ledger["cash"],
                "positions": ledger["positions"],
                "orders": {
                    item["client_order_id"]: {
                        "status": item["status"],
                        "filled_quantity": int(item["filled_quantity"]),
                    }
                    for item in store.list_live_orders()
                },
                "trade_ids": ledger["trade_ids"],
                "state_missing": False,
            }
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
            "trade_ids": (
                [item.trade_id for item in broker.get_trades()]
                if broker.mode in {"live", "mock_live"}
                else []
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
        # 逐笔成交对账（recon-5）：券商有、本地成交流水无 = 漏成交（最危险，必报）。
        # 仅检“漏成交”方向：券商 get_trades 可能是窗口化的，本地多出的旧成交不据此判幻象，
        # 避免窗口化导致的假差异在 live 模式误触 Kill Switch。
        broker_trades = set(remote.get("trade_ids", []))
        local_trades = set(local.get("trade_ids", []))
        for trade_id in sorted(broker_trades - local_trades):
            breaks.append(
                {
                    "type": "trade",
                    "symbol": None,
                    "broker_trade_id": trade_id,
                    "local": None,
                    "broker": trade_id,
                    "diff": "missing_in_local",
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
