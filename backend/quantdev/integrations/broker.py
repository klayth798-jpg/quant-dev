"""Broker 适配层：统一券商抽象 + 三种实现。

实盘交易中"查询能力比下单能力更重要"——必须能确认订单到底成交了没有、
账户实际持仓与现金是多少。因此抽象基类同时定义了交易接口和查询接口。

三种实现：
  - DisabledLiveBroker：查询返回空快照，任何交易动作一律拒绝（默认）。
  - PaperBroker：复用现有 Paper OMS 的账户/持仓/委托状态，作为模拟盘查询入口。
  - MockLiveBroker：模拟真实券商的异常行为（拒单/部分成交/撤单失败/超时/状态未知），
    用于在不接真实资金的前提下压测 OMS 的异常处理能力。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, List, Optional

from quantdev.services.live_guard import live_guard


@dataclass(frozen=True)
class BrokerOrder:
    client_order_id: str
    symbol: str
    side: str
    quantity: int
    order_type: str = "limit"
    limit_price: float = 0.0
    approved: bool = False


@dataclass(frozen=True)
class BrokerOrderAck:
    accepted: bool
    broker_order_id: str
    status: str
    filled_quantity: int = 0
    message: str = ""


@dataclass(frozen=True)
class CancelAck:
    accepted: bool
    broker_order_id: str
    status: str
    message: str = ""


@dataclass(frozen=True)
class AccountSnapshot:
    account_id: str
    cash: float
    market_value: float
    equity: float


@dataclass(frozen=True)
class PositionSnapshot:
    symbol: str
    quantity: int
    average_cost: float


@dataclass(frozen=True)
class OrderSnapshot:
    broker_order_id: str
    client_order_id: str
    symbol: str
    side: str
    quantity: int
    filled_quantity: int
    status: str


@dataclass(frozen=True)
class TradeSnapshot:
    trade_id: str
    broker_order_id: str
    symbol: str
    side: str
    quantity: int
    price: float


@dataclass(frozen=True)
class BrokerHealth:
    healthy: bool
    mode: str
    message: str = ""


class BrokerAdapter(ABC):
    mode = "abstract"

    # ---- 交易接口 ----
    @abstractmethod
    def submit_order(self, order: BrokerOrder) -> BrokerOrderAck:
        raise NotImplementedError

    @abstractmethod
    def cancel_order(self, broker_order_id: str) -> CancelAck:
        raise NotImplementedError

    # ---- 查询接口 ----
    @abstractmethod
    def get_account(self) -> AccountSnapshot:
        raise NotImplementedError

    @abstractmethod
    def get_positions(self) -> List[PositionSnapshot]:
        raise NotImplementedError

    @abstractmethod
    def get_orders(self) -> List[OrderSnapshot]:
        raise NotImplementedError

    @abstractmethod
    def get_trades(self) -> List[TradeSnapshot]:
        raise NotImplementedError

    @abstractmethod
    def query_order(self, broker_order_id: str) -> Optional[OrderSnapshot]:
        raise NotImplementedError

    @abstractmethod
    def health_check(self) -> BrokerHealth:
        raise NotImplementedError


class DisabledLiveBroker(BrokerAdapter):
    """实盘 Broker 未启用：查询返回空快照，交易动作一律拒绝。"""

    mode = "disabled"

    def submit_order(self, order: BrokerOrder) -> BrokerOrderAck:
        raise PermissionError("实盘 Broker 未启用，订单不会被发送")

    def cancel_order(self, broker_order_id: str) -> CancelAck:
        raise PermissionError("实盘 Broker 未启用，撤单不会被发送")

    def get_account(self) -> AccountSnapshot:
        return AccountSnapshot(
            account_id="disabled", cash=0.0, market_value=0.0, equity=0.0
        )

    def get_positions(self) -> List[PositionSnapshot]:
        return []

    def get_orders(self) -> List[OrderSnapshot]:
        return []

    def get_trades(self) -> List[TradeSnapshot]:
        return []

    def query_order(self, broker_order_id: str) -> Optional[OrderSnapshot]:
        return None

    def health_check(self) -> BrokerHealth:
        return BrokerHealth(
            healthy=True, mode=self.mode, message="实盘 Broker 处于安全禁用状态"
        )


class PaperBroker(BrokerAdapter):
    """模拟盘 Broker：从现有 Paper OMS 状态读取账户、持仓与委托。

    交易动作仍委托给 PaperExecutionService（带风控与 Kill Switch），
    这里主要提供与实盘一致的查询视图。
    """

    mode = "paper"

    def __init__(self) -> None:
        from quantdev.services.execution import paper_execution_service

        self._oms = paper_execution_service

    def submit_order(self, order: BrokerOrder) -> BrokerOrderAck:
        from quantdev.models import PaperOrderRequest

        result = self._oms.submit(
            PaperOrderRequest(
                client_order_id=order.client_order_id,
                symbol=order.symbol,
                side=order.side,
                quantity=order.quantity,
                order_type=order.order_type,
                limit_price=order.limit_price or None,
            )
        )
        status = result.get("status") or (
            result.get("order", {}).get("status") if result.get("idempotent") else ""
        )
        return BrokerOrderAck(
            accepted=status != "REJECTED",
            broker_order_id=result.get("order_id")
            or result.get("order", {}).get("order_id", ""),
            status=status or "UNKNOWN",
            filled_quantity=order.quantity if status == "FILLED" else 0,
            message=result.get("reject_reason") or "",
        )

    def cancel_order(self, broker_order_id: str) -> CancelAck:
        return CancelAck(
            accepted=False,
            broker_order_id=broker_order_id,
            status="UNSUPPORTED",
            message="模拟盘暂不支持撤单（订单即时成交或挂单）",
        )

    def get_account(self) -> AccountSnapshot:
        account = self._oms.get_account()
        return AccountSnapshot(
            account_id=account["account_id"],
            cash=account["cash"],
            market_value=account["market_value"],
            equity=account["equity"],
        )

    def get_positions(self) -> List[PositionSnapshot]:
        account = self._oms.get_account()
        return [
            PositionSnapshot(
                symbol=item["symbol"],
                quantity=int(item["quantity"]),
                average_cost=float(item["average_cost"]),
            )
            for item in account["positions"]
        ]

    def get_orders(self) -> List[OrderSnapshot]:
        account = self._oms.get_account()
        orders: List[OrderSnapshot] = []
        for item in account["orders"]:
            status = item["status"]
            orders.append(
                OrderSnapshot(
                    broker_order_id=item["order_id"],
                    client_order_id=item["client_order_id"],
                    symbol=item["symbol"],
                    side=item["side"],
                    quantity=int(item["quantity"]),
                    filled_quantity=int(item["quantity"])
                    if status == "FILLED"
                    else 0,
                    status=status,
                )
            )
        return orders

    def get_trades(self) -> List[TradeSnapshot]:
        from quantdev.db import database

        with database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM paper_fills ORDER BY filled_at DESC LIMIT 50"
            ).fetchall()
        return [
            TradeSnapshot(
                trade_id=row["fill_id"],
                broker_order_id=row["order_id"],
                symbol=row["symbol"],
                side=row["side"],
                quantity=int(row["quantity"]),
                price=float(row["price"]),
            )
            for row in rows
        ]

    def query_order(self, broker_order_id: str) -> Optional[OrderSnapshot]:
        for order in self.get_orders():
            if order.broker_order_id == broker_order_id:
                return order
        return None

    def health_check(self) -> BrokerHealth:
        return BrokerHealth(healthy=True, mode=self.mode, message="模拟盘可用")


class MockLiveBroker(BrokerAdapter):
    """模拟真实券商行为，用于异常压测。

    通过 scenario 注入不同异常：
      - normal：正常全部成交
      - partial：部分成交
      - reject：券商拒单
      - cancel_fail：撤单失败
      - timeout：网络超时（抛 TimeoutError）
      - unknown：查询订单返回 UNKNOWN 状态
    下单前仍走 live_guard 的三层拦截，作为 Broker 层最后防线。
    """

    mode = "mock_live"

    def __init__(self, scenario: str = "normal") -> None:
        self.scenario = scenario
        self._orders: Dict[str, OrderSnapshot] = {}
        self._seq = 0

    def _next_id(self) -> str:
        self._seq += 1
        return "mock-{:06d}".format(self._seq)

    def submit_order(self, order: BrokerOrder) -> BrokerOrderAck:
        notional = order.quantity * (order.limit_price or 0.0)
        decision = live_guard.check_live_order(notional, approved=order.approved)
        if not decision.allowed:
            raise PermissionError("实盘下单被安全守卫拒绝：" + decision.reason)
        if self.scenario == "timeout":
            raise TimeoutError("券商接口超时，订单状态未知")
        broker_order_id = self._next_id()
        if self.scenario == "reject":
            snapshot = OrderSnapshot(
                broker_order_id, order.client_order_id, order.symbol,
                order.side, order.quantity, 0, "REJECTED",
            )
            self._orders[broker_order_id] = snapshot
            return BrokerOrderAck(
                accepted=False, broker_order_id=broker_order_id,
                status="REJECTED", message="券商拒单",
            )
        if self.scenario == "partial":
            filled = max(order.quantity // 2 // 100 * 100, 0)
            snapshot = OrderSnapshot(
                broker_order_id, order.client_order_id, order.symbol,
                order.side, order.quantity, filled, "PARTIALLY_FILLED",
            )
            self._orders[broker_order_id] = snapshot
            return BrokerOrderAck(
                accepted=True, broker_order_id=broker_order_id,
                status="PARTIALLY_FILLED", filled_quantity=filled,
            )
        if self.scenario == "unknown":
            snapshot = OrderSnapshot(
                broker_order_id, order.client_order_id, order.symbol,
                order.side, order.quantity, 0, "UNKNOWN",
            )
            self._orders[broker_order_id] = snapshot
            return BrokerOrderAck(
                accepted=True, broker_order_id=broker_order_id, status="UNKNOWN",
            )
        snapshot = OrderSnapshot(
            broker_order_id, order.client_order_id, order.symbol,
            order.side, order.quantity, order.quantity, "FILLED",
        )
        self._orders[broker_order_id] = snapshot
        return BrokerOrderAck(
            accepted=True, broker_order_id=broker_order_id,
            status="FILLED", filled_quantity=order.quantity,
        )

    def cancel_order(self, broker_order_id: str) -> CancelAck:
        if self.scenario == "timeout":
            raise TimeoutError("券商接口超时，撤单状态未知")
        if self.scenario == "cancel_fail":
            return CancelAck(
                accepted=False, broker_order_id=broker_order_id,
                status="CANCEL_FAILED", message="券商撤单失败（订单可能已成交）",
            )
        if broker_order_id not in self._orders:
            return CancelAck(
                accepted=False, broker_order_id=broker_order_id,
                status="UNKNOWN", message="未找到该订单",
            )
        self._orders[broker_order_id] = OrderSnapshot(
            broker_order_id,
            self._orders[broker_order_id].client_order_id,
            self._orders[broker_order_id].symbol,
            self._orders[broker_order_id].side,
            self._orders[broker_order_id].quantity,
            self._orders[broker_order_id].filled_quantity,
            "CANCELLED",
        )
        return CancelAck(
            accepted=True, broker_order_id=broker_order_id, status="CANCELLED"
        )

    def get_account(self) -> AccountSnapshot:
        return AccountSnapshot(
            account_id="mock-live", cash=0.0, market_value=0.0, equity=0.0
        )

    def get_positions(self) -> List[PositionSnapshot]:
        return []

    def get_orders(self) -> List[OrderSnapshot]:
        return list(self._orders.values())

    def get_trades(self) -> List[TradeSnapshot]:
        return []

    def query_order(self, broker_order_id: str) -> Optional[OrderSnapshot]:
        return self._orders.get(broker_order_id)

    def health_check(self) -> BrokerHealth:
        healthy = self.scenario != "timeout"
        return BrokerHealth(
            healthy=healthy, mode=self.mode,
            message="模拟券商场景：{}".format(self.scenario),
        )


def get_broker() -> BrokerAdapter:
    """按生效的 broker 模式返回 Broker 实例。

    总开关未开时，live 会被 live_guard 降级为 disabled，确保配置不一致时安全优先。
    """
    mode = live_guard.resolved_broker_mode()
    if mode == "live":
        return MockLiveBroker()
    if mode == "paper":
        return PaperBroker()
    return DisabledLiveBroker()
