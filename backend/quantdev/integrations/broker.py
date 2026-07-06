"""Broker 适配层：统一券商抽象 + 三种实现。

实盘交易中"查询能力比下单能力更重要"——必须能确认订单到底成交了没有、
账户实际持仓与现金是多少。因此抽象基类同时定义了交易接口和查询接口。

三种实现：
  - DisabledLiveBroker：查询返回空快照，任何交易动作一律拒绝（默认）。
  - PaperBroker：复用现有 Paper OMS 的账户/持仓/委托状态，作为模拟盘查询入口。
  - MockLiveBroker：持久化模拟真实券商的异常行为（拒单/部分成交/撤单失败/超时/
    状态未知），用于在不接真实资金的前提下压测 OMS 的恢复能力。
"""

import importlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional

from quantdev.config import settings
from quantdev.db import database
from quantdev.services.live_guard import live_guard
from quantdev.services.quotes import quote_service
from quantdev.store import new_id, utc_now


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
    sellable_quantity: Optional[int] = None


@dataclass(frozen=True)
class OrderSnapshot:
    broker_order_id: str
    client_order_id: str
    symbol: str
    side: str
    quantity: int
    filled_quantity: int
    status: str
    average_fill_price: Optional[float] = None


@dataclass(frozen=True)
class TradeSnapshot:
    trade_id: str
    broker_order_id: str
    symbol: str
    side: str
    quantity: int
    price: float
    fees: float = 0.0
    traded_at: str = ""


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
    def query_order_by_client_id(
        self, client_order_id: str
    ) -> Optional[OrderSnapshot]:
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

    def query_order_by_client_id(
        self, client_order_id: str
    ) -> Optional[OrderSnapshot]:
        return None

    def health_check(self) -> BrokerHealth:
        return BrokerHealth(
            healthy=True, mode=self.mode, message="实盘 Broker 处于安全禁用状态"
        )


class UnconfiguredLiveBroker(DisabledLiveBroker):
    """配置选择了 live，但尚未安装具体券商适配器。"""

    mode = "unconfigured_live"

    def __init__(self, message: str = "") -> None:
        self.message = message or (
            "QUANTDEV_BROKER_MODE=live，但没有已注册的真实券商适配器"
        )

    def submit_order(self, order: BrokerOrder) -> BrokerOrderAck:
        raise PermissionError("尚未配置真实券商适配器，实盘订单已阻断")

    def cancel_order(self, broker_order_id: str) -> CancelAck:
        raise PermissionError("尚未配置真实券商适配器，实盘撤单已阻断")

    def health_check(self) -> BrokerHealth:
        return BrokerHealth(
            healthy=False,
            mode=self.mode,
            message=self.message,
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
        result = self._oms.cancel(broker_order_id)
        order = result["order"]
        return CancelAck(
            accepted=order["status"] == "CANCELLED",
            broker_order_id=broker_order_id,
            status=order["status"],
            message=order.get("reject_reason") or "",
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
                sellable_quantity=int(item["sellable_quantity"]),
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
                    average_fill_price=(
                        float(item["average_fill_price"])
                        if item["average_fill_price"] is not None
                        else None
                    ),
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
                fees=float(row["fees"]),
                traded_at=row["filled_at"],
            )
            for row in rows
        ]

    def query_order(self, broker_order_id: str) -> Optional[OrderSnapshot]:
        for order in self.get_orders():
            if order.broker_order_id == broker_order_id:
                return order
        return None

    def query_order_by_client_id(
        self, client_order_id: str
    ) -> Optional[OrderSnapshot]:
        for order in self.get_orders():
            if order.client_order_id == client_order_id:
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
      - timeout：网络超时（先持久化 UNKNOWN，再抛 TimeoutError）
      - unknown：查询订单返回 UNKNOWN 状态
    下单前仍走 live_guard 的三层拦截，作为 Broker 层最后防线。
    """

    mode = "mock_live"

    def __init__(self, scenario: str = "normal") -> None:
        self.scenario = scenario
        self.account_id = "mock-live"
        with database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO mock_broker_accounts
                    (account_id, cash, initial_cash, updated_at)
                VALUES (?, 1000000, 1000000, ?)
                ON CONFLICT(account_id) DO NOTHING
                """,
                (self.account_id, utc_now()),
            )

    def submit_order(self, order: BrokerOrder) -> BrokerOrderAck:
        existing = self.query_order_by_client_id(order.client_order_id)
        if existing:
            same = (
                existing.symbol == order.symbol
                and existing.side == order.side
                and existing.quantity == order.quantity
            )
            if not same:
                raise ValueError("client_order_id 已被其他券商委托使用")
            return BrokerOrderAck(
                accepted=existing.status != "REJECTED",
                broker_order_id=existing.broker_order_id,
                status=existing.status,
                filled_quantity=existing.filled_quantity,
            )
        execution_price = self._price(order)
        notional = order.quantity * execution_price
        decision = live_guard.check_live_order(notional, approved=order.approved)
        if not decision.allowed:
            raise PermissionError("实盘下单被安全守卫拒绝：" + decision.reason)
        broker_order_id = new_id("mock")
        status = "UNKNOWN"
        filled = 0
        message = ""
        if self.scenario == "reject":
            status = "REJECTED"
            message = "券商拒单"
        elif self.scenario == "partial":
            filled = max(order.quantity // 2 // 100 * 100, 0)
            status = "PARTIALLY_FILLED"
        elif self.scenario == "normal":
            filled = order.quantity
            status = "FILLED"
        self._persist_order(
            broker_order_id, order, status, filled, execution_price, message
        )
        if self.scenario == "timeout":
            raise TimeoutError(
                "券商接口超时，订单已按 UNKNOWN 持久化，请用 client_order_id 查询"
            )
        return BrokerOrderAck(
            accepted=status not in {"REJECTED"},
            broker_order_id=broker_order_id,
            status=status,
            filled_quantity=filled,
            message=message,
        )

    def cancel_order(self, broker_order_id: str) -> CancelAck:
        if self.scenario == "timeout":
            raise TimeoutError("券商接口超时，撤单状态未知")
        if self.scenario == "cancel_fail":
            return CancelAck(
                accepted=False, broker_order_id=broker_order_id,
                status="CANCEL_FAILED", message="券商撤单失败（订单可能已成交）",
            )
        order = self.query_order(broker_order_id)
        if order is None:
            return CancelAck(
                accepted=False, broker_order_id=broker_order_id,
                status="UNKNOWN", message="未找到该订单",
            )
        if order.status in {"FILLED", "REJECTED", "CANCELLED", "PARTIALLY_CANCELLED"}:
            return CancelAck(
                accepted=False,
                broker_order_id=broker_order_id,
                status=order.status,
                message="终态订单不可撤销",
            )
        status = "PARTIALLY_CANCELLED" if order.filled_quantity else "CANCELLED"
        with database.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE mock_broker_orders
                SET status = ?, updated_at = ?
                WHERE broker_order_id = ?
                """,
                (status, utc_now(), broker_order_id),
            )
        return CancelAck(
            accepted=True, broker_order_id=broker_order_id, status=status
        )

    def get_account(self) -> AccountSnapshot:
        with database.connect() as connection:
            account = connection.execute(
                "SELECT * FROM mock_broker_accounts WHERE account_id = ?",
                (self.account_id,),
            ).fetchone()
            positions = connection.execute(
                "SELECT * FROM mock_broker_positions WHERE account_id = ?",
                (self.account_id,),
            ).fetchall()
        market_value = 0.0
        for position in positions:
            quote = quote_service.get(position["symbol"])
            price = quote.price or float(position["average_cost"])
            market_value += int(position["quantity"]) * price
        return AccountSnapshot(
            account_id=self.account_id,
            cash=float(account["cash"]),
            market_value=market_value,
            equity=float(account["cash"]) + market_value,
        )

    def get_positions(self) -> List[PositionSnapshot]:
        with database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM mock_broker_positions
                WHERE account_id = ? ORDER BY symbol
                """,
                (self.account_id,),
            ).fetchall()
        return [
            PositionSnapshot(
                symbol=row["symbol"],
                quantity=int(row["quantity"]),
                average_cost=float(row["average_cost"]),
                sellable_quantity=int(row["quantity"]),
            )
            for row in rows
        ]

    def get_orders(self) -> List[OrderSnapshot]:
        with database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM mock_broker_orders ORDER BY created_at"
            ).fetchall()
        return [self._order_snapshot(row) for row in rows]

    def get_trades(self) -> List[TradeSnapshot]:
        with database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM mock_broker_trades ORDER BY traded_at"
            ).fetchall()
        return [
            TradeSnapshot(
                trade_id=row["trade_id"],
                broker_order_id=row["broker_order_id"],
                symbol=row["symbol"],
                side=row["side"],
                quantity=int(row["quantity"]),
                price=float(row["price"]),
                traded_at=row["traded_at"],
            )
            for row in rows
        ]

    def query_order(self, broker_order_id: str) -> Optional[OrderSnapshot]:
        with database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM mock_broker_orders WHERE broker_order_id = ?",
                (broker_order_id,),
            ).fetchone()
        return self._order_snapshot(row) if row else None

    def query_order_by_client_id(
        self, client_order_id: str
    ) -> Optional[OrderSnapshot]:
        with database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM mock_broker_orders WHERE client_order_id = ?",
                (client_order_id,),
            ).fetchone()
        return self._order_snapshot(row) if row else None

    def _price(self, order: BrokerOrder) -> float:
        if order.limit_price > 0:
            return float(order.limit_price)
        quote = quote_service.get(order.symbol)
        if not quote.fresh or not quote.price:
            raise ValueError(
                "市价单缺少新鲜行情：{}".format(quote.reason or order.symbol)
            )
        return float(quote.price)

    def _persist_order(
        self,
        broker_order_id: str,
        order: BrokerOrder,
        status: str,
        filled: int,
        price: float,
        message: str,
    ) -> None:
        now = utc_now()
        with database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO mock_broker_orders
                    (broker_order_id, client_order_id, scenario, symbol, side,
                     quantity, order_type, limit_price, filled_quantity,
                     average_fill_price, status, message, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    broker_order_id,
                    order.client_order_id,
                    self.scenario,
                    order.symbol,
                    order.side,
                    order.quantity,
                    order.order_type,
                    order.limit_price or None,
                    filled,
                    price if filled else None,
                    status,
                    message,
                    now,
                    now,
                ),
            )
            if filled:
                self._apply_fill(
                    connection,
                    broker_order_id,
                    order.symbol,
                    order.side,
                    filled,
                    price,
                )

    def _apply_fill(
        self,
        connection,
        broker_order_id: str,
        symbol: str,
        side: str,
        quantity: int,
        price: float,
    ) -> None:
        now = utc_now()
        account = connection.execute(
            "SELECT * FROM mock_broker_accounts WHERE account_id = ?",
            (self.account_id,),
        ).fetchone()
        position = connection.execute(
            """
            SELECT * FROM mock_broker_positions
            WHERE account_id = ? AND symbol = ?
            """,
            (self.account_id, symbol),
        ).fetchone()
        gross = quantity * price
        if side == "buy":
            if float(account["cash"]) < gross:
                raise ValueError("Mock Broker 可用资金不足")
            old_quantity = int(position["quantity"]) if position else 0
            old_cost = float(position["average_cost"]) if position else 0.0
            new_quantity = old_quantity + quantity
            average_cost = (old_quantity * old_cost + gross) / new_quantity
            connection.execute(
                """
                INSERT INTO mock_broker_positions
                    (account_id, symbol, quantity, average_cost, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(account_id, symbol) DO UPDATE SET
                    quantity = excluded.quantity,
                    average_cost = excluded.average_cost,
                    updated_at = excluded.updated_at
                """,
                (self.account_id, symbol, new_quantity, average_cost, now),
            )
            cash = float(account["cash"]) - gross
        else:
            if not position or int(position["quantity"]) < quantity:
                raise ValueError("Mock Broker 持仓不足")
            remaining = int(position["quantity"]) - quantity
            if remaining:
                connection.execute(
                    """
                    UPDATE mock_broker_positions
                    SET quantity = ?, updated_at = ?
                    WHERE account_id = ? AND symbol = ?
                    """,
                    (remaining, now, self.account_id, symbol),
                )
            else:
                connection.execute(
                    """
                    DELETE FROM mock_broker_positions
                    WHERE account_id = ? AND symbol = ?
                    """,
                    (self.account_id, symbol),
                )
            cash = float(account["cash"]) + gross
        connection.execute(
            """
            UPDATE mock_broker_accounts
            SET cash = ?, updated_at = ?
            WHERE account_id = ?
            """,
            (cash, now, self.account_id),
        )
        connection.execute(
            """
            INSERT INTO mock_broker_trades
                (trade_id, broker_order_id, symbol, side, quantity,
                 price, traded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("mtrade"),
                broker_order_id,
                symbol,
                side,
                quantity,
                price,
                now,
            ),
        )

    @staticmethod
    def _order_snapshot(row) -> OrderSnapshot:
        return OrderSnapshot(
            broker_order_id=row["broker_order_id"],
            client_order_id=row["client_order_id"],
            symbol=row["symbol"],
            side=row["side"],
            quantity=int(row["quantity"]),
            filled_quantity=int(row["filled_quantity"]),
            status=row["status"],
            average_fill_price=(
                float(row["average_fill_price"])
                if row["average_fill_price"] is not None
                else None
            ),
        )

    def health_check(self) -> BrokerHealth:
        healthy = self.scenario != "timeout"
        return BrokerHealth(
            healthy=healthy, mode=self.mode,
            message="模拟券商场景：{}".format(self.scenario),
        )


class GuardedBroker(BrokerAdapter):
    """包装真实券商适配器：任何下单在委托给适配器前，强制经过 live_guard 判定。

    纵深防御要对真实资金也成立——即使 submit_intent 的前置风控被绕过或读到陈旧状态，
    真实下单仍有 Broker 层这道独立守门，而不依赖第三方适配器“自觉”调用 live_guard。
    查询/撤单（降低风险的动作）直接透传。
    """

    def __init__(self, delegate: BrokerAdapter) -> None:
        self._delegate = delegate
        self.mode = delegate.mode

    def _notional(self, order: BrokerOrder) -> float:
        if order.limit_price and order.limit_price > 0:
            return order.quantity * float(order.limit_price)
        quote = quote_service.get(order.symbol)
        if not quote.fresh or not quote.price:
            raise PermissionError(
                "实盘市价单缺少新鲜行情，Broker 守卫拒绝下单：{}".format(
                    quote.reason or order.symbol
                )
            )
        return order.quantity * float(quote.price)

    def submit_order(self, order: BrokerOrder) -> BrokerOrderAck:
        decision = live_guard.check_live_order(
            self._notional(order), approved=order.approved
        )
        if not decision.allowed:
            raise PermissionError("实盘下单被安全守卫拒绝：" + decision.reason)
        return self._delegate.submit_order(order)

    def cancel_order(self, broker_order_id: str) -> CancelAck:
        return self._delegate.cancel_order(broker_order_id)

    def get_account(self) -> AccountSnapshot:
        return self._delegate.get_account()

    def get_positions(self) -> List[PositionSnapshot]:
        return self._delegate.get_positions()

    def get_orders(self) -> List[OrderSnapshot]:
        return self._delegate.get_orders()

    def get_trades(self) -> List[TradeSnapshot]:
        return self._delegate.get_trades()

    def query_order(self, broker_order_id: str) -> Optional[OrderSnapshot]:
        return self._delegate.query_order(broker_order_id)

    def query_order_by_client_id(
        self, client_order_id: str
    ) -> Optional[OrderSnapshot]:
        return self._delegate.query_order_by_client_id(client_order_id)

    def health_check(self) -> BrokerHealth:
        return self._delegate.health_check()


class DryRunBroker(BrokerAdapter):
    """真实 Broker dry-run 包装器：查询透传，交易动作只生成本地回执。

    用途是先接入券商/柜台的只读查询能力，验证账户、持仓、委托和对账链路；在
    `QUANTDEV_BROKER_DRY_RUN=true` 时，即使系统进入 live 模式，也不会向真实柜台发送
    报单或撤单请求。
    """

    mode = "live"

    def __init__(self, delegate: BrokerAdapter) -> None:
        self._delegate = delegate

    def submit_order(self, order: BrokerOrder) -> BrokerOrderAck:
        return BrokerOrderAck(
            accepted=True,
            broker_order_id="dryrun:{}".format(order.client_order_id),
            status="DRY_RUN",
            filled_quantity=0,
            message="dry-run：订单未发送到真实 Broker",
        )

    def cancel_order(self, broker_order_id: str) -> CancelAck:
        return CancelAck(
            accepted=True,
            broker_order_id=broker_order_id,
            status="CANCELLED",
            message="dry-run：撤单未发送到真实 Broker",
        )

    def get_account(self) -> AccountSnapshot:
        return self._delegate.get_account()

    def get_positions(self) -> List[PositionSnapshot]:
        return self._delegate.get_positions()

    def get_orders(self) -> List[OrderSnapshot]:
        return self._delegate.get_orders()

    def get_trades(self) -> List[TradeSnapshot]:
        return self._delegate.get_trades()

    def query_order(self, broker_order_id: str) -> Optional[OrderSnapshot]:
        if broker_order_id.startswith("dryrun:"):
            return None
        return self._delegate.query_order(broker_order_id)

    def query_order_by_client_id(
        self, client_order_id: str
    ) -> Optional[OrderSnapshot]:
        return self._delegate.query_order_by_client_id(client_order_id)

    def health_check(self) -> BrokerHealth:
        health = self._delegate.health_check()
        return BrokerHealth(
            healthy=health.healthy,
            mode="live",
            message="dry-run 查询透传；交易不会发送。{}".format(health.message),
        )


def get_broker() -> BrokerAdapter:
    """按生效的 broker 模式返回 Broker 实例。

    总开关未开时，live 会被 live_guard 降级为 disabled，确保配置不一致时安全优先。
    真实券商适配器统一用 GuardedBroker 包裹，确保 live_guard 兜底对真实资金生效。
    """
    mode = live_guard.resolved_broker_mode()
    if mode == "live":
        if not settings.broker_adapter:
            return UnconfiguredLiveBroker()
        try:
            module_name, attribute = settings.broker_adapter.split(":", 1)
            factory = getattr(importlib.import_module(module_name), attribute)
            broker = factory() if callable(factory) else factory
            if not isinstance(broker, BrokerAdapter):
                raise TypeError("配置对象未实现 BrokerAdapter")
            if broker.health_check().mode != "live":
                raise TypeError("真实 Broker health_check().mode 必须返回 live")
            if settings.broker_dry_run:
                broker = DryRunBroker(broker)
            return GuardedBroker(broker)
        except Exception as exc:
            return UnconfiguredLiveBroker(
                "加载真实券商适配器失败：{}".format(exc)
            )
    if mode == "mock_live":
        return MockLiveBroker()
    if mode == "paper":
        return PaperBroker()
    return DisabledLiveBroker()
