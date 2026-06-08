from typing import Any, Dict

from quantdev.db import database
from quantdev.models import PaperOrderRequest
from quantdev.store import new_id, store, utc_now


class PaperExecutionService:
    account_id = "paper-primary"
    commission_rate = 0.0003
    stamp_duty_rate = 0.0005
    slippage_bps = 5.0
    lot_size = 100
    max_order_weight = 0.20

    def ensure_account(self) -> None:
        with database.transaction() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO paper_accounts
                    (account_id, name, cash, initial_cash, status, updated_at)
                VALUES (?, '主模拟账户', 1000000, 1000000, 'active', ?)
                """,
                (self.account_id, utc_now()),
            )

    def _latest_prices(self, connection) -> Dict[str, float]:
        rows = connection.execute(
            """
            SELECT p.symbol, p.close
            FROM prices p
            JOIN (
                SELECT symbol, MAX(trade_date) AS max_date
                FROM prices
                WHERE snapshot_id = ?
                GROUP BY symbol
            ) latest
              ON latest.symbol = p.symbol AND latest.max_date = p.trade_date
            WHERE p.snapshot_id = ?
            """,
            (store.latest_snapshot_id(), store.latest_snapshot_id()),
        ).fetchall()
        return {row["symbol"]: float(row["close"]) for row in rows}

    def get_account(self) -> Dict[str, Any]:
        self.ensure_account()
        with database.connect() as connection:
            account = connection.execute(
                "SELECT * FROM paper_accounts WHERE account_id = ?", (self.account_id,)
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
                ORDER BY created_at DESC LIMIT 30
                """,
                (self.account_id,),
            ).fetchall()
            prices = self._latest_prices(connection)
        position_items = []
        market_value = 0.0
        unrealized_pnl = 0.0
        for row in positions:
            item = dict(row)
            last_price = prices.get(item["symbol"], item["average_cost"])
            value = item["quantity"] * last_price
            pnl = (last_price - item["average_cost"]) * item["quantity"]
            market_value += value
            unrealized_pnl += pnl
            item.update(
                {
                    "last_price": round(last_price, 4),
                    "market_value": round(value, 2),
                    "unrealized_pnl": round(pnl, 2),
                }
            )
            position_items.append(item)
        cash = float(account["cash"])
        equity = cash + market_value
        return {
            "account_id": self.account_id,
            "name": account["name"],
            "status": account["status"],
            "cash": round(cash, 2),
            "market_value": round(market_value, 2),
            "equity": round(equity, 2),
            "total_return": round(equity / account["initial_cash"] - 1, 6),
            "unrealized_pnl": round(unrealized_pnl, 2),
            "positions": position_items,
            "orders": [dict(row) for row in orders],
        }

    def submit(self, request: PaperOrderRequest) -> Dict[str, Any]:
        self.ensure_account()
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

        now = utc_now()
        order_id = new_id("ord")
        with database.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM paper_orders WHERE client_order_id = ?",
                (request.client_order_id,),
            ).fetchone()
            if existing:
                return {
                    "idempotent": True,
                    "order": dict(existing),
                    "account": self.get_account(),
                }
            account = connection.execute(
                "SELECT * FROM paper_accounts WHERE account_id = ?", (self.account_id,)
            ).fetchone()
            prices = self._latest_prices(connection)
            if request.symbol not in prices:
                raise ValueError("标的没有可用行情")
            market_price = prices[request.symbol]
            if order_type == "limit":
                if side == "buy" and request.limit_price < market_price:
                    fill_price = request.limit_price
                elif side == "sell" and request.limit_price > market_price:
                    fill_price = request.limit_price
                else:
                    fill_price = request.limit_price
            else:
                direction = 1 if side == "buy" else -1
                fill_price = market_price * (
                    1 + direction * self.slippage_bps / 10_000
                )
            gross = request.quantity * fill_price
            positions = connection.execute(
                "SELECT * FROM paper_positions WHERE account_id = ?",
                (self.account_id,),
            ).fetchall()
            market_value = sum(
                row["quantity"] * prices.get(row["symbol"], row["average_cost"])
                for row in positions
            )
            equity = float(account["cash"]) + market_value
            reject_reason = None
            if equity and gross / equity > self.max_order_weight:
                reject_reason = "单笔委托超过账户净值的 20%"

            commission = max(5.0, gross * self.commission_rate)
            stamp_duty = gross * self.stamp_duty_rate if side == "sell" else 0.0
            fees = commission + stamp_duty
            current_position = connection.execute(
                """
                SELECT * FROM paper_positions
                WHERE account_id = ? AND symbol = ?
                """,
                (self.account_id, request.symbol),
            ).fetchone()
            if side == "buy" and gross + fees > account["cash"]:
                reject_reason = "可用资金不足"
            if side == "sell" and (
                not current_position or current_position["quantity"] < request.quantity
            ):
                reject_reason = "可卖持仓不足"

            status = "REJECTED" if reject_reason else "FILLED"
            connection.execute(
                """
                INSERT INTO paper_orders
                    (order_id, client_order_id, account_id, symbol, side, quantity,
                     order_type, limit_price, status, reject_reason, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    reject_reason,
                    now,
                    now,
                ),
            )
            if not reject_reason:
                if side == "buy":
                    old_quantity = current_position["quantity"] if current_position else 0
                    old_cost = current_position["average_cost"] if current_position else 0.0
                    new_quantity = old_quantity + request.quantity
                    average_cost = (
                        old_quantity * old_cost + gross + fees
                    ) / new_quantity
                    connection.execute(
                        """
                        INSERT INTO paper_positions
                            (account_id, symbol, quantity, average_cost, updated_at)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(account_id, symbol) DO UPDATE SET
                            quantity = excluded.quantity,
                            average_cost = excluded.average_cost,
                            updated_at = excluded.updated_at
                        """,
                        (
                            self.account_id,
                            request.symbol,
                            new_quantity,
                            average_cost,
                            now,
                        ),
                    )
                    new_cash = account["cash"] - gross - fees
                else:
                    remaining = current_position["quantity"] - request.quantity
                    if remaining:
                        connection.execute(
                            """
                            UPDATE paper_positions
                            SET quantity = ?, updated_at = ?
                            WHERE account_id = ? AND symbol = ?
                            """,
                            (remaining, now, self.account_id, request.symbol),
                        )
                    else:
                        connection.execute(
                            """
                            DELETE FROM paper_positions
                            WHERE account_id = ? AND symbol = ?
                            """,
                            (self.account_id, request.symbol),
                        )
                    new_cash = account["cash"] + gross - fees
                connection.execute(
                    """
                    UPDATE paper_accounts SET cash = ?, updated_at = ?
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
                    (
                        new_id("fill"),
                        order_id,
                        request.symbol,
                        side,
                        request.quantity,
                        fill_price,
                        fees,
                        now,
                    ),
                )

        store.audit(
            actor="local-user",
            action="submit_paper_order",
            resource_type="paper_order",
            resource_id=order_id,
            payload={"status": status, "symbol": request.symbol, "side": side},
        )
        return {
            "idempotent": False,
            "order_id": order_id,
            "status": status,
            "reject_reason": reject_reason,
            "fill_price": round(fill_price, 4) if not reject_reason else None,
            "fees": round(fees, 2) if not reject_reason else None,
            "account": self.get_account(),
        }


paper_execution_service = PaperExecutionService()
