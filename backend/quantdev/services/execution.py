from typing import Any, Dict, List, Optional

from quantdev.config import settings
from quantdev.db import database
from quantdev.models import PaperOrderRequest, PositionInput, RiskCheckRequest
from quantdev.money import quantize_yuan, round_money, to_decimal
from quantdev.services.calendar import trading_calendar_service
from quantdev.services.events import EventType, event_bus
from quantdev.services.live_guard import live_guard
from quantdev.services.quotes import quote_service
from quantdev.services.risk import risk_service
from quantdev.services.tradability import tradability_service
from quantdev.store import new_id, store, utc_now

TERMINAL_STATUSES = {"FILLED", "CANCELLED", "REJECTED"}
ACTIVE_STATUSES = {"OPEN", "PARTIALLY_FILLED", "UNKNOWN", "CANCEL_PENDING"}


class PaperExecutionService:
    account_id = "paper-primary"
    commission_rate = 0.0003
    stamp_duty_rate = 0.0005
    slippage_bps = 5.0
    lot_size = 100

    def ensure_account(self) -> None:
        with database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO paper_accounts
                    (account_id, name, cash, reserved_cash, initial_cash,
                     status, updated_at)
                VALUES (?, '主模拟账户', 1000000, 0, 1000000, 'active', ?)
                ON CONFLICT(account_id) DO NOTHING
                """,
                (self.account_id, utc_now()),
            )

    @staticmethod
    def _sellable_quantity(position, today: str) -> int:
        if not position:
            return 0
        total = int(position["quantity"])
        frozen_until = position["frozen_date"]
        t1_frozen = (
            int(position["frozen_quantity"] or 0)
            if frozen_until and today < frozen_until
            else 0
        )
        reserved = int(position["reserved_quantity"] or 0)
        return max(total - t1_frozen - reserved, 0)

    def _fees(self, gross: float, side: str) -> float:
        # 费用按定点数计算：成交额量化到“分”后再乘费率，避免浮点误差随笔数累积。
        if not gross:
            return 0.0
        gross_d = quantize_yuan(gross)
        commission = max(
            to_decimal("5.0"), gross_d * to_decimal(self.commission_rate)
        )
        stamp_duty = (
            gross_d * to_decimal(self.stamp_duty_rate) if side == "sell" else to_decimal(0)
        )
        return round_money(commission + stamp_duty)

    def _execution_price(
        self,
        quote,
        side: str,
        order_type: str,
        limit_price: Optional[float],
    ) -> tuple[bool, float]:
        reference = (
            quote.ask
            if side == "buy" and quote.ask is not None
            else quote.bid
            if side == "sell" and quote.bid is not None
            else quote.price
        )
        if reference is None:
            return False, 0.0
        direction = 1 if side == "buy" else -1
        simulated = reference * (1 + direction * self.slippage_bps / 10_000)
        if order_type == "market":
            return True, simulated
        if side == "buy":
            marketable = float(limit_price) >= reference
            return marketable, min(float(limit_price), simulated)
        marketable = float(limit_price) <= reference
        return marketable, max(float(limit_price), simulated)

    @staticmethod
    def _position_rows(connection) -> List[Any]:
        return connection.execute(
            "SELECT * FROM paper_positions WHERE account_id = ?",
            (PaperExecutionService.account_id,),
        ).fetchall()

    @staticmethod
    def _equity(account, positions, prices: Dict[str, float]) -> float:
        market_value = sum(
            int(row["quantity"])
            * prices.get(str(row["symbol"]), float(row["average_cost"]))
            for row in positions
        )
        return float(account["cash"]) + market_value

    def _read_daily_snapshot(
        self, connection, trade_date: str, equity: float
    ) -> Dict[str, float]:
        """只读计算当日盈亏基准，不写库、不触发任何副作用。"""
        existing = connection.execute(
            """
            SELECT * FROM account_daily_snapshots
            WHERE account_id = ? AND trade_date = ?
            """,
            (self.account_id, trade_date),
        ).fetchone()
        previous = connection.execute(
            """
            SELECT current_equity
            FROM account_daily_snapshots
            WHERE account_id = ? AND trade_date < ?
            ORDER BY trade_date DESC LIMIT 1
            """,
            (self.account_id, trade_date),
        ).fetchone()
        start_equity = (
            float(existing["start_equity"])
            if existing
            else float(previous["current_equity"])
            if previous
            else equity
        )
        daily_pnl = equity - start_equity
        return {
            "start_equity": start_equity,
            "current_equity": equity,
            "daily_pnl": daily_pnl,
        }

    def _update_daily_snapshot(
        self, connection, trade_date: str, equity: float
    ) -> Dict[str, float]:
        now = utc_now()
        snapshot = self._read_daily_snapshot(connection, trade_date, equity)
        start_equity = snapshot["start_equity"]
        daily_pnl = snapshot["daily_pnl"]
        connection.execute(
            """
            INSERT INTO account_daily_snapshots
                (account_id, trade_date, start_equity, current_equity,
                 daily_pnl, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_id, trade_date) DO UPDATE SET
                current_equity = excluded.current_equity,
                daily_pnl = excluded.daily_pnl,
                updated_at = excluded.updated_at
            """,
            (
                self.account_id,
                trade_date,
                start_equity,
                equity,
                daily_pnl,
                now,
            ),
        )
        return {
            "start_equity": start_equity,
            "current_equity": equity,
            "daily_pnl": daily_pnl,
        }

    def _clock_rejection(self) -> Optional[str]:
        if not settings.paper_enforce_session:
            return None
        clock = trading_calendar_service.market_clock()
        if clock.calendar_source == "missing" and settings.calendar_fail_closed:
            return "交易日历未覆盖当前日期，模拟实盘禁止下单"
        if not clock.can_trade:
            return "当前非交易时段（{}），模拟实盘禁止下单".format(clock.session)
        return None

    def _validate_request(self, request: PaperOrderRequest) -> tuple[str, str]:
        side = request.side.lower()
        order_type = request.order_type.lower()
        if side not in {"buy", "sell"}:
            raise ValueError("side 只能是 buy 或 sell")
        if order_type not in {"market", "limit"}:
            raise ValueError("order_type 只能是 market 或 limit")
        if request.quantity % self.lot_size != 0:
            raise ValueError("A股委托数量必须是 100 股的整数倍")
        if order_type == "limit" and not request.limit_price:
            raise ValueError("限价单必须提供 limit_price")
        return side, order_type

    def get_account(self) -> Dict[str, Any]:
        self.ensure_account()
        prices = quote_service.valuation_prices()
        today = trading_calendar_service.today().isoformat()
        with database.connect() as connection:
            account = connection.execute(
                "SELECT * FROM paper_accounts WHERE account_id = ?",
                (self.account_id,),
            ).fetchone()
            positions = connection.execute(
                """
                SELECT p.*, i.name
                FROM paper_positions p
                JOIN instruments i ON i.symbol = p.symbol
                WHERE p.account_id = ?
                ORDER BY p.symbol
                """,
                (self.account_id,),
            ).fetchall()
            orders = connection.execute(
                """
                SELECT * FROM paper_orders
                WHERE account_id = ?
                ORDER BY created_at DESC LIMIT 100
                """,
                (self.account_id,),
            ).fetchall()
            equity = self._equity(account, positions, prices)
            daily = self._read_daily_snapshot(connection, today, equity)

        position_items = []
        market_value = 0.0
        unrealized_pnl = 0.0
        for row in positions:
            item = dict(row)
            last_price = prices.get(item["symbol"], item["average_cost"])
            value = int(item["quantity"]) * last_price
            pnl = (last_price - float(item["average_cost"])) * int(item["quantity"])
            market_value += value
            unrealized_pnl += pnl
            sellable = self._sellable_quantity(row, today)
            item.update(
                {
                    "last_price": round(last_price, 4),
                    "market_value": round(value, 2),
                    "unrealized_pnl": round(pnl, 2),
                    "sellable_quantity": sellable,
                    "t1_frozen_quantity": (
                        int(item["frozen_quantity"] or 0)
                        if item["frozen_date"] and today < item["frozen_date"]
                        else 0
                    ),
                    "reserved_quantity": int(item["reserved_quantity"] or 0),
                }
            )
            position_items.append(item)
        cash = float(account["cash"])
        reserved_cash = float(account["reserved_cash"] or 0)
        result = {
            "account_id": self.account_id,
            "name": account["name"],
            "status": account["status"],
            "cash": round_money(cash),
            "reserved_cash": round_money(reserved_cash),
            "available_cash": round_money(to_decimal(cash) - to_decimal(reserved_cash)),
            "market_value": round_money(market_value),
            "equity": round_money(equity),
            "total_return": round(equity / account["initial_cash"] - 1, 6),
            "daily_pnl": round_money(daily["daily_pnl"]),
            "daily_loss_limit": settings.max_daily_loss,
            "unrealized_pnl": round_money(unrealized_pnl),
            "positions": position_items,
            "orders": [dict(row) for row in orders],
        }
        return result

    def submit(self, request: PaperOrderRequest) -> Dict[str, Any]:
        self.ensure_account()
        side, order_type = self._validate_request(request)
        now = utc_now()
        today = trading_calendar_service.today().isoformat()
        prices = quote_service.valuation_prices()
        quote = quote_service.get(request.symbol)
        order_id = new_id("ord")
        risk_request = None
        risk_result = None
        activate_kill_reason = None

        with database.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT * FROM paper_orders WHERE client_order_id = ?",
                (request.client_order_id,),
            ).fetchone()
            if existing:
                same_request = (
                    existing["symbol"] == request.symbol
                    and existing["side"] == side
                    and int(existing["quantity"]) == request.quantity
                    and existing["order_type"] == order_type
                    and existing["limit_price"] == request.limit_price
                )
                if not same_request:
                    raise ValueError("client_order_id 已被其他委托使用")
                existing_order = dict(existing)
                idempotent = True
            else:
                idempotent = False
                account = connection.execute(
                    "SELECT * FROM paper_accounts WHERE account_id = ?",
                    (self.account_id,),
                ).fetchone()
                positions = self._position_rows(connection)
                current_position = next(
                    (row for row in positions if row["symbol"] == request.symbol),
                    None,
                )
                equity = self._equity(account, positions, prices)
                daily = self._update_daily_snapshot(connection, today, equity)
                rejection = None
                if live_guard.kill_switch_active():
                    rejection = "Kill Switch 已激活，禁止新建委托"
                elif daily["daily_pnl"] <= -settings.max_daily_loss:
                    rejection = "当日亏损 {:.2f} 已触发限额 {:.2f}".format(
                        daily["daily_pnl"], settings.max_daily_loss
                    )
                    activate_kill_reason = rejection
                else:
                    rejection = self._clock_rejection()
                if not rejection and not quote.fresh:
                    rejection = quote.reason or "行情不可用"

                marketable, fill_price = self._execution_price(
                    quote, side, order_type, request.limit_price
                )
                notional_price = (
                    fill_price
                    if marketable
                    else float(request.limit_price or quote.price or 0)
                )
                gross = round_money(request.quantity * notional_price)
                fees = self._fees(gross, side)
                if not rejection:
                    tradability = tradability_service.evaluate(request.symbol)
                    if tradability.halted:
                        rejection = tradability.halt_reason
                    elif order_type == "limit":
                        rejection = tradability_service.check_limit_price(
                            request.symbol, side, float(request.limit_price)
                        )

                available_cash = float(account["cash"]) - float(
                    account["reserved_cash"] or 0
                )
                sellable = self._sellable_quantity(current_position, today)
                if side == "buy" and not rejection and gross + fees > available_cash:
                    rejection = "可用资金不足（已冻结 {:.2f}）".format(
                        float(account["reserved_cash"] or 0)
                    )
                if side == "sell" and not rejection and sellable < request.quantity:
                    t1_frozen = (
                        int(current_position["frozen_quantity"] or 0)
                        if current_position
                        and current_position["frozen_date"]
                        and today < current_position["frozen_date"]
                        else 0
                    )
                    if t1_frozen:
                        rejection = (
                            "A股 T+1 冻结中：{} 股将于 {} 解冻，可卖 {}，委托 {}"
                        ).format(
                            t1_frozen,
                            current_position["frozen_date"],
                            sellable,
                            request.quantity,
                        )
                    else:
                        rejection = "可卖数量不足：可卖 {}，委托 {}".format(
                            sellable, request.quantity
                        )
                if side == "buy" and not rejection and equity > 0:
                    projected = {
                        row["symbol"]: int(row["quantity"]) for row in positions
                    }
                    projected[request.symbol] = (
                        projected.get(request.symbol, 0) + request.quantity
                    )
                    projected_weights = [
                        PositionInput(
                            symbol=symbol,
                            weight=quantity
                            * (
                                notional_price
                                if symbol == request.symbol
                                else prices.get(symbol, 0)
                            )
                            / equity,
                        )
                        for symbol, quantity in projected.items()
                        if quantity > 0 and prices.get(symbol, notional_price) > 0
                    ]
                    risk_request = RiskCheckRequest(
                        positions=projected_weights,
                        proposed_turnover=gross / equity,
                        current_drawdown=max(
                            0.0, 1 - equity / float(account["initial_cash"])
                        ),
                        order_notional_weight=gross / equity,
                    )
                    risk_result = risk_service.evaluate(risk_request)
                    if not risk_result["approved"]:
                        rejection = "；".join(
                            breach["message"]
                            for breach in risk_result["breaches"]
                        )

                status = (
                    "REJECTED"
                    if rejection
                    else "FILLED"
                    if marketable
                    else "OPEN"
                )
                reserve_cash = (
                    round_money(to_decimal(gross) + to_decimal(fees))
                    if status == "OPEN" and side == "buy"
                    else 0
                )
                reserve_quantity = (
                    request.quantity if status == "OPEN" and side == "sell" else 0
                )
                connection.execute(
                    """
                    INSERT INTO paper_orders
                        (order_id, client_order_id, account_id, symbol, side,
                         quantity, order_type, limit_price, status,
                         filled_quantity, average_fill_price, reserved_cash,
                         reserved_quantity, strategy_run_id, order_intent_id,
                         version, reject_reason, submitted_at, completed_at,
                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0,
                            ?, ?, ?, ?, ?)
                    """,
                    (
                        order_id,
                        request.client_order_id,
                        self.account_id,
                        request.symbol,
                        side,
                        request.quantity,
                        order_type,
                        request.limit_price,
                        status,
                        request.quantity if status == "FILLED" else 0,
                        fill_price if status == "FILLED" else None,
                        reserve_cash,
                        reserve_quantity,
                        getattr(request, "strategy_run_id", None),
                        getattr(request, "order_intent_id", None),
                        rejection,
                        now,
                        now if status in TERMINAL_STATUSES else None,
                        now,
                        now,
                    ),
                )
                self._append_initial_events(
                    connection,
                    order_id,
                    request,
                    risk_result,
                    status,
                    rejection,
                    marketable,
                )
                if status == "OPEN":
                    self._apply_reservation(
                        connection,
                        request.symbol,
                        side,
                        reserve_cash,
                        reserve_quantity,
                    )
                elif status == "FILLED":
                    self._apply_fill(
                        connection,
                        order_id,
                        request.symbol,
                        side,
                        request.quantity,
                        fill_price,
                        today,
                        release_reservations=False,
                    )
                existing_order = dict(
                    connection.execute(
                        "SELECT * FROM paper_orders WHERE order_id = ?", (order_id,)
                    ).fetchone()
                )

        if activate_kill_reason and not live_guard.kill_switch_active():
            live_guard.activate_kill_switch(
                activate_kill_reason, actor="daily-loss-guard"
            )
        store.refresh_money_cents()
        if idempotent:
            return {
                "idempotent": True,
                "order": existing_order,
                "account": self.get_account(),
            }
        self._publish_after_order(existing_order)
        if risk_result and not risk_result["approved"] and risk_request:
            for breach in risk_result["breaches"]:
                store.save_risk_event(
                    breach["severity"],
                    breach["rule_code"],
                    breach["message"],
                    risk_request.model_dump(),
                )
        store.audit(
            actor="local-user",
            action="submit_paper_order",
            resource_type="paper_order",
            resource_id=order_id,
            payload={
                "status": existing_order["status"],
                "symbol": request.symbol,
                "side": side,
            },
        )
        return self._response(existing_order)

    def _append_initial_events(
        self,
        connection,
        order_id: str,
        request: PaperOrderRequest,
        risk_result,
        status: str,
        rejection: Optional[str],
        marketable: bool,
    ) -> None:
        store.append_order_event(
            connection,
            order_id,
            "ORDER_CREATED",
            request.model_dump(),
        )
        if risk_result is not None:
            store.append_order_event(
                connection,
                order_id,
                "RISK_APPROVED" if risk_result["approved"] else "RISK_REJECTED",
                {
                    "approved": risk_result["approved"],
                    "breaches": [
                        breach["rule_code"] for breach in risk_result["breaches"]
                    ],
                },
            )
        store.append_order_event(
            connection,
            order_id,
            status,
            {"reject_reason": rejection, "marketable": marketable},
        )

    def _apply_reservation(
        self,
        connection,
        symbol: str,
        side: str,
        reserve_cash: float,
        reserve_quantity: int,
    ) -> None:
        now = utc_now()
        if side == "buy":
            connection.execute(
                """
                UPDATE paper_accounts
                SET reserved_cash = reserved_cash + ?, updated_at = ?
                WHERE account_id = ?
                """,
                (reserve_cash, now, self.account_id),
            )
        else:
            connection.execute(
                """
                UPDATE paper_positions
                SET reserved_quantity = reserved_quantity + ?, updated_at = ?
                WHERE account_id = ? AND symbol = ?
                """,
                (reserve_quantity, now, self.account_id, symbol),
            )

    def _release_reservation(self, connection, order) -> None:
        now = utc_now()
        if float(order["reserved_cash"] or 0) > 0:
            connection.execute(
                """
                UPDATE paper_accounts
                SET reserved_cash = CASE
                        WHEN reserved_cash - ? < 0 THEN 0
                        ELSE reserved_cash - ?
                    END,
                    updated_at = ?
                WHERE account_id = ?
                """,
                (
                    float(order["reserved_cash"]),
                    float(order["reserved_cash"]),
                    now,
                    self.account_id,
                ),
            )
        if int(order["reserved_quantity"] or 0) > 0:
            connection.execute(
                """
                UPDATE paper_positions
                SET reserved_quantity = CASE
                        WHEN reserved_quantity - ? < 0 THEN 0
                        ELSE reserved_quantity - ?
                    END,
                    updated_at = ?
                WHERE account_id = ? AND symbol = ?
                """,
                (
                    int(order["reserved_quantity"]),
                    int(order["reserved_quantity"]),
                    now,
                    self.account_id,
                    order["symbol"],
                ),
            )

    def _apply_fill(
        self,
        connection,
        order_id: str,
        symbol: str,
        side: str,
        quantity: int,
        price: float,
        today: str,
        release_reservations: bool,
    ) -> None:
        now = utc_now()
        order = connection.execute(
            "SELECT * FROM paper_orders WHERE order_id = ?", (order_id,)
        ).fetchone()
        if release_reservations:
            self._release_reservation(connection, order)
        account = connection.execute(
            "SELECT * FROM paper_accounts WHERE account_id = ?", (self.account_id,)
        ).fetchone()
        position = connection.execute(
            """
            SELECT * FROM paper_positions
            WHERE account_id = ? AND symbol = ?
            """,
            (self.account_id, symbol),
        ).fetchone()
        gross = round_money(quantity * price)
        fees = self._fees(gross, side)
        if side == "buy":
            unlock_day = trading_calendar_service.next_trading_day(
                trading_calendar_service.today()
            )
            if unlock_day is None:
                raise ValueError("交易日历缺少下一交易日，无法计算 T+1 解冻日")
            unlock_date = unlock_day.isoformat()
            old_quantity = int(position["quantity"]) if position else 0
            old_cost = float(position["average_cost"]) if position else 0.0
            new_quantity = old_quantity + quantity
            average_cost = (old_quantity * old_cost + gross + fees) / new_quantity
            prior_frozen = (
                int(position["frozen_quantity"] or 0)
                if position
                and position["frozen_date"]
                and today < position["frozen_date"]
                else 0
            )
            connection.execute(
                """
                INSERT INTO paper_positions
                    (account_id, symbol, quantity, average_cost,
                     frozen_quantity, frozen_date, reserved_quantity, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 0, ?)
                ON CONFLICT(account_id, symbol) DO UPDATE SET
                    quantity = excluded.quantity,
                    average_cost = excluded.average_cost,
                    frozen_quantity = excluded.frozen_quantity,
                    frozen_date = excluded.frozen_date,
                    updated_at = excluded.updated_at
                """,
                (
                    self.account_id,
                    symbol,
                    new_quantity,
                    average_cost,
                    prior_frozen + quantity,
                    unlock_date,
                    now,
                ),
            )
            new_cash = round_money(
                to_decimal(account["cash"]) - to_decimal(gross) - to_decimal(fees)
            )
        else:
            remaining = int(position["quantity"]) - quantity
            if remaining:
                connection.execute(
                    """
                    UPDATE paper_positions
                    SET quantity = ?, updated_at = ?
                    WHERE account_id = ? AND symbol = ?
                    """,
                    (remaining, now, self.account_id, symbol),
                )
            else:
                connection.execute(
                    "DELETE FROM paper_positions WHERE account_id = ? AND symbol = ?",
                    (self.account_id, symbol),
                )
            new_cash = round_money(
                to_decimal(account["cash"]) + to_decimal(gross) - to_decimal(fees)
            )
        connection.execute(
            """
            UPDATE paper_accounts
            SET cash = ?, updated_at = ?
            WHERE account_id = ?
            """,
            (new_cash, now, self.account_id),
        )
        connection.execute(
            """
            INSERT INTO paper_fills
                (fill_id, order_id, symbol, side, quantity, price, fees, filled_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (new_id("fill"), order_id, symbol, side, quantity, price, fees, now),
        )
        connection.execute(
            """
            UPDATE paper_orders
            SET status = 'FILLED', filled_quantity = quantity,
                average_fill_price = ?, reserved_cash = 0,
                reserved_quantity = 0, completed_at = ?,
                updated_at = ?, version = version + 1
            WHERE order_id = ?
            """,
            (price, now, now, order_id),
        )
        store.append_order_event(
            connection,
            order_id,
            "FILLED_CONFIRMED",
            {"quantity": quantity, "price": round(price, 4), "fees": round(fees, 2)},
        )

    def cancel(self, order_id: str) -> Dict[str, Any]:
        terminal = False
        with database.transaction(immediate=True) as connection:
            order = connection.execute(
                "SELECT * FROM paper_orders WHERE order_id = ?", (order_id,)
            ).fetchone()
            if not order:
                raise ValueError("订单不存在")
            if order["status"] in TERMINAL_STATUSES:
                terminal = True
                result = dict(order)
            else:
                self._release_reservation(connection, order)
                now = utc_now()
                connection.execute(
                    """
                    UPDATE paper_orders
                    SET status = 'CANCELLED', reserved_cash = 0,
                        reserved_quantity = 0, completed_at = ?,
                        updated_at = ?, version = version + 1
                    WHERE order_id = ?
                    """,
                    (now, now, order_id),
                )
                store.append_order_event(
                    connection, order_id, "CANCELLED", {"source": "local-user"}
                )
                result = dict(
                    connection.execute(
                        "SELECT * FROM paper_orders WHERE order_id = ?", (order_id,)
                    ).fetchone()
                )
        if terminal:
            return {
                "idempotent": True,
                "order": result,
                "account": self.get_account(),
            }
        store.refresh_money_cents()
        self._refresh_strategy_intent(result)
        event_bus.publish(
            "order.cancelled",
            "paper_order",
            order_id,
            {"status": "CANCELLED"},
            actor="local-user",
        )
        return {"idempotent": False, **self._response(result)}

    def match_open_orders(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        clock_rejection = self._clock_rejection()
        if clock_rejection:
            return {"matched": 0, "skipped": 0, "reason": clock_rejection}
        query = "SELECT order_id FROM paper_orders WHERE status = 'OPEN'"
        params: List[Any] = []
        if symbol:
            query += " AND symbol = ?"
            params.append(symbol)
        with database.connect() as connection:
            ids = [row["order_id"] for row in connection.execute(query, params)]
        matched = 0
        skipped = 0
        matched_orders = []
        for order_id in ids:
            with database.transaction(immediate=True) as connection:
                order = connection.execute(
                    "SELECT * FROM paper_orders WHERE order_id = ?", (order_id,)
                ).fetchone()
                if not order or order["status"] != "OPEN":
                    continue
                quote = quote_service.get(order["symbol"])
                if not quote.fresh:
                    skipped += 1
                    continue
                marketable, price = self._execution_price(
                    quote,
                    order["side"],
                    order["order_type"],
                    order["limit_price"],
                )
                if not marketable:
                    skipped += 1
                    continue
                self._apply_fill(
                    connection,
                    order_id,
                    order["symbol"],
                    order["side"],
                    int(order["quantity"]) - int(order["filled_quantity"] or 0),
                    price,
                    trading_calendar_service.today().isoformat(),
                    release_reservations=True,
                )
                matched += 1
                matched_orders.append(
                    dict(
                        connection.execute(
                            "SELECT * FROM paper_orders WHERE order_id = ?",
                            (order_id,),
                        ).fetchone()
                    )
                )
        for order in matched_orders:
            self._refresh_strategy_intent(order)
        if matched:
            store.refresh_money_cents()
        return {"matched": matched, "skipped": skipped, "reason": ""}

    def recover_active_orders(self) -> Dict[str, Any]:
        return self.match_open_orders()

    def _response(self, order: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "idempotent": False,
            "order_id": order["order_id"],
            "status": order["status"],
            "reject_reason": order.get("reject_reason"),
            "fill_price": order.get("average_fill_price"),
            "fees": None,
            "order": order,
            "account": self.get_account(),
        }

    @staticmethod
    def _publish_after_order(order: Dict[str, Any]) -> None:
        status = order["status"]
        event_type = (
            EventType.ORDER_FILLED
            if status == "FILLED"
            else EventType.ORDER_REJECTED
            if status == "REJECTED"
            else EventType.ORDER_SUBMITTED
        )
        event_bus.publish(
            event_type=event_type,
            aggregate_type="paper_order",
            aggregate_id=order["order_id"],
            payload={
                "status": status,
                "symbol": order["symbol"],
                "side": order["side"],
                "quantity": order["quantity"],
                "reject_reason": order.get("reject_reason"),
            },
            actor="local-user",
        )

    @staticmethod
    def _refresh_strategy_intent(order: Dict[str, Any]) -> None:
        intent_id = order.get("order_intent_id")
        if not intent_id:
            return
        from quantdev.services.strategy import strategy_runner_service

        strategy_runner_service.refresh_intent(
            intent_id,
            order["status"],
            paper_order_id=order["order_id"],
            error_message=order.get("reject_reason"),
        )


paper_execution_service = PaperExecutionService()
