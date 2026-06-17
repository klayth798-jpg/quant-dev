import json
from typing import Any, Dict, List, Optional, Tuple

from quantdev.db import database
from quantdev.money import round_money, to_decimal
from quantdev.repositories.base import decode_json, new_id, utc_now


class OrderRepository:
    """OrderRepository：order 子域数据访问。"""

    def get_order_intent(self, intent_id: str) -> Optional[Dict[str, Any]]:
        with database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM order_intents WHERE intent_id = ?",
                (intent_id,),
            ).fetchone()
        return dict(row) if row else None

    def update_order_intent(
        self,
        intent_id: str,
        *,
        status: Optional[str] = None,
        paper_order_id: Optional[str] = None,
        live_order_id: Optional[str] = None,
        broker_order_id: Optional[str] = None,
        approval_status: Optional[str] = None,
        request_hash: Optional[str] = None,
        error_message: Optional[str] = None,
        clear_error: bool = False,
    ) -> Dict[str, Any]:
        values = {
            "status": status,
            "paper_order_id": paper_order_id,
            "live_order_id": live_order_id,
            "broker_order_id": broker_order_id,
            "approval_status": approval_status,
            "request_hash": request_hash,
            "error_message": error_message,
        }
        assignments = [f"{key} = ?" for key, value in values.items() if value is not None]
        params = [value for value in values.values() if value is not None]
        if clear_error and error_message is None:
            assignments.append("error_message = NULL")
        assignments.append("updated_at = ?")
        params.extend([utc_now(), intent_id])
        with database.transaction(immediate=True) as connection:
            if assignments:
                connection.execute(
                    "UPDATE order_intents SET {} WHERE intent_id = ?".format(
                        ", ".join(assignments)
                    ),
                    params,
                )
            row = connection.execute(
                "SELECT * FROM order_intents WHERE intent_id = ?",
                (intent_id,),
            ).fetchone()
        if not row:
            raise ValueError("订单意图不存在")
        return dict(row)

    def save_order_approval(
        self,
        intent_id: str,
        request_hash: str,
        approved_by: str,
        reason: str,
        expires_at: str,
    ) -> Dict[str, Any]:
        now = utc_now()
        approval_id = new_id("approval")
        with database.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE order_approvals
                SET status = 'REVOKED', revoked_at = ?
                WHERE intent_id = ? AND status = 'APPROVED'
                """,
                (now, intent_id),
            )
            connection.execute(
                """
                INSERT INTO order_approvals
                    (approval_id, intent_id, request_hash, status, approved_by,
                     reason, approved_at, expires_at, created_at)
                VALUES (?, ?, ?, 'APPROVED', ?, ?, ?, ?, ?)
                """,
                (
                    approval_id,
                    intent_id,
                    request_hash,
                    approved_by,
                    reason,
                    now,
                    expires_at,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE order_intents
                SET status = 'APPROVED', approval_status = 'APPROVED',
                    request_hash = ?,
                    updated_at = ?
                WHERE intent_id = ?
                """,
                (request_hash, now, intent_id),
            )
        return self.get_order_approval(approval_id)

    def get_order_approval(self, approval_id: str) -> Optional[Dict[str, Any]]:
        with database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM order_approvals WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
        return dict(row) if row else None

    def latest_order_approval(self, intent_id: str) -> Optional[Dict[str, Any]]:
        with database.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM order_approvals
                WHERE intent_id = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (intent_id,),
            ).fetchone()
        return dict(row) if row else None

    def reject_order_intent(
        self, intent_id: str, reason: str, actor: str
    ) -> Dict[str, Any]:
        now = utc_now()
        with database.transaction(immediate=True) as connection:
            updated = connection.execute(
                """
                UPDATE order_intents
                SET status = 'REJECTED', approval_status = 'REJECTED',
                    error_message = ?, updated_at = ?
                WHERE intent_id = ?
                  AND status NOT IN ('FILLED', 'CANCELLED', 'REJECTED')
                """,
                (reason, now, intent_id),
            ).rowcount
            connection.execute(
                """
                UPDATE order_approvals
                SET status = 'REVOKED', revoked_at = ?
                WHERE intent_id = ? AND status = 'APPROVED'
                """,
                (now, intent_id),
            )
        if not updated:
            existing = self.get_order_intent(intent_id)
            if not existing:
                raise ValueError("订单意图不存在")
        self.audit(
            actor=actor,
            action="reject_order_intent",
            resource_type="order_intent",
            resource_id=intent_id,
            payload={"reason": reason},
        )
        return self.get_order_intent(intent_id)

    def create_live_order(
        self,
        intent: Dict[str, Any],
        request_hash: str,
        approval_id: Optional[str],
    ) -> Tuple[Dict[str, Any], bool]:
        """原子创建实盘订单。

        返回 ``(order, created)``：当该意图已存在实盘订单时 ``created=False`` 且返回
        既有订单，调用方据此短路、绝不向券商重复下单（防 TOCTOU 双重委托）。
        """
        now = utc_now()
        order_id = new_id("lord")
        with database.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT * FROM live_orders WHERE intent_id = ?",
                (intent["intent_id"],),
            ).fetchone()
            if existing:
                return dict(existing), False
            connection.execute(
                """
                INSERT INTO live_orders
                    (order_id, intent_id, client_order_id, symbol, side, quantity,
                     order_type, limit_price, request_hash, approval_id, status,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'SUBMITTING', ?, ?)
                """,
                (
                    order_id,
                    intent["intent_id"],
                    intent["client_order_id"],
                    intent["symbol"],
                    intent["side"],
                    intent["quantity"],
                    intent["order_type"],
                    intent.get("limit_price"),
                    request_hash,
                    approval_id,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE order_intents
                SET live_order_id = ?, status = 'SUBMITTING',
                    request_hash = ?, updated_at = ?
                WHERE intent_id = ?
                """,
                (order_id, request_hash, now, intent["intent_id"]),
            )
        return self.get_live_order(order_id), True

    def get_live_order(self, order_id: str) -> Optional[Dict[str, Any]]:
        with database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM live_orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_live_order_by_client_id(
        self, client_order_id: str
    ) -> Optional[Dict[str, Any]]:
        with database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM live_orders WHERE client_order_id = ?",
                (client_order_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_live_orders(
        self, statuses: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        query = "SELECT * FROM live_orders"
        params: List[Any] = []
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            query += f" WHERE status IN ({placeholders})"
            params.extend(statuses)
        query += " ORDER BY created_at"
        with database.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def update_live_order(
        self,
        order_id: str,
        *,
        status: str,
        broker_order_id: Optional[str] = None,
        filled_quantity: Optional[int] = None,
        average_fill_price: Optional[float] = None,
        last_error: Optional[str] = None,
    ) -> Dict[str, Any]:
        now = utc_now()
        terminal_statuses = {
            "FILLED",
            "CANCELLED",
            "PARTIALLY_CANCELLED",
            "REJECTED",
        }
        terminal = status in terminal_statuses
        with database.transaction(immediate=True) as connection:
            current = connection.execute(
                "SELECT * FROM live_orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
            if not current:
                raise ValueError("实盘订单不存在")
            # 终态保护：订单已是终态后，拒绝被恢复/巡检的陈旧/回退快照降级为非终态，
            # 否则已确定的 FILLED/CANCELLED 会被覆盖、filled_quantity 被改写，污染对账基线。
            if (
                current["status"] in terminal_statuses
                and status not in terminal_statuses
            ):
                return self.get_live_order(order_id)
            broker_id = broker_order_id or current["broker_order_id"]
            filled = (
                int(filled_quantity)
                if filled_quantity is not None
                else int(current["filled_quantity"] or 0)
            )
            average = (
                average_fill_price
                if average_fill_price is not None
                else current["average_fill_price"]
            )
            connection.execute(
                """
                UPDATE live_orders
                SET status = ?, broker_order_id = ?, filled_quantity = ?,
                    average_fill_price = ?, last_error = ?,
                    submitted_at = COALESCE(submitted_at, ?),
                    completed_at = CASE WHEN ? THEN ? ELSE completed_at END,
                    updated_at = ?
                WHERE order_id = ?
                """,
                (
                    status,
                    broker_id,
                    filled,
                    average,
                    last_error,
                    now,
                    int(terminal),
                    now,
                    now,
                    order_id,
                ),
            )
            connection.execute(
                """
                UPDATE order_intents
                SET status = ?, broker_order_id = ?, error_message = ?,
                    updated_at = ?
                WHERE intent_id = ?
                """,
                (
                    status,
                    broker_id,
                    last_error,
                    now,
                    current["intent_id"],
                ),
            )
        return self.get_live_order(order_id)

    def save_live_trade(
        self, order_id: str, trade: Dict[str, Any]
    ) -> bool:
        with database.transaction(immediate=True) as connection:
            inserted = connection.execute(
                """
                INSERT INTO live_trades
                    (trade_id, broker_trade_id, order_id, broker_order_id,
                     symbol, side, quantity, price, fees, traded_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(broker_trade_id) DO NOTHING
                """,
                (
                    new_id("ltrade"),
                    trade["broker_trade_id"],
                    order_id,
                    trade["broker_order_id"],
                    trade["symbol"],
                    trade["side"],
                    trade["quantity"],
                    trade["price"],
                    trade.get("fees", 0.0),
                    trade["traded_at"],
                    utc_now(),
                ),
            ).rowcount
        return bool(inserted)

    def sync_live_account_state(
        self,
        account_id: str,
        cash: float,
        positions: List[Dict[str, Any]],
    ) -> None:
        now = utc_now()
        with database.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT * FROM live_account_state WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO live_account_state
                    (account_id, cash, initial_cash, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    cash = excluded.cash,
                    updated_at = excluded.updated_at
                """,
                (
                    account_id,
                    cash,
                    float(existing["initial_cash"]) if existing else cash,
                    now,
                ),
            )
            connection.execute(
                "DELETE FROM live_positions WHERE account_id = ?",
                (account_id,),
            )
            connection.executemany(
                """
                INSERT INTO live_positions
                    (account_id, symbol, quantity, sellable_quantity,
                     average_cost, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        account_id,
                        item["symbol"],
                        item["quantity"],
                        item.get("sellable_quantity", item["quantity"]),
                        item["average_cost"],
                        now,
                    )
                    for item in positions
                ],
            )

    def update_live_daily_snapshot(
        self, account_id: str, trade_date: str, equity: float
    ) -> Dict[str, Any]:
        now = utc_now()
        with database.transaction(immediate=True) as connection:
            current = connection.execute(
                """
                SELECT * FROM live_account_daily_snapshots
                WHERE account_id = ? AND trade_date = ?
                """,
                (account_id, trade_date),
            ).fetchone()
            previous = connection.execute(
                """
                SELECT current_equity
                FROM live_account_daily_snapshots
                WHERE account_id = ? AND trade_date < ?
                ORDER BY trade_date DESC LIMIT 1
                """,
                (account_id, trade_date),
            ).fetchone()
            if current:
                # 当日已锚定：start_equity 一旦确定就不再移动。
                start_equity = float(current["start_equity"])
            elif previous:
                # 用前一交易日的收盘权益作为今日开盘锚点（昨收权益）。
                start_equity = float(previous["current_equity"])
            else:
                # 无任何历史快照：锚定到账户注资基准(initial_cash)，而不是可能已
                # 盘中缩水的当前 equity；否则首单发生在权益下跌之后时，daily_pnl 会
                # 从接近 0 起算，日亏损限额被“首单延迟/开盘跳空”绕过。
                state = connection.execute(
                    "SELECT initial_cash FROM live_account_state WHERE account_id = ?",
                    (account_id,),
                ).fetchone()
                start_equity = (
                    float(state["initial_cash"])
                    if state and state["initial_cash"] is not None
                    else equity
                )
            daily_pnl = equity - start_equity
            connection.execute(
                """
                INSERT INTO live_account_daily_snapshots
                    (account_id, trade_date, start_equity, current_equity,
                     daily_pnl, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, trade_date) DO UPDATE SET
                    current_equity = excluded.current_equity,
                    daily_pnl = excluded.daily_pnl,
                    updated_at = excluded.updated_at
                """,
                (
                    account_id,
                    trade_date,
                    start_equity,
                    equity,
                    daily_pnl,
                    now,
                ),
            )
        return {
            "account_id": account_id,
            "trade_date": trade_date,
            "start_equity": start_equity,
            "current_equity": equity,
            "daily_pnl": daily_pnl,
            "updated_at": now,
        }

    def local_live_snapshot(self) -> Optional[Dict[str, Any]]:
        with database.connect() as connection:
            account = connection.execute(
                "SELECT * FROM live_account_state ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
            if not account:
                return None
            positions = connection.execute(
                "SELECT * FROM live_positions WHERE account_id = ?",
                (account["account_id"],),
            ).fetchall()
        return {
            "account_id": account["account_id"],
            "cash": float(account["cash"]),
            "positions": {
                row["symbol"]: int(row["quantity"]) for row in positions
            },
            "updated_at": account["updated_at"],
        }

    def derive_live_ledger(
        self, baseline: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """从基线锚点 + 成交流水(live_trades)独立推导现金与持仓（对账的“独立账本”一侧）。

        关键：不读 live_account_state（它只是券商状态的本地副本），而是用 OMS 自己记录的
        逐笔成交独立推导，从而能与券商真值做三方比对、检出真实漂移（漏成交/重复扣费）。

        baseline（推荐）：``{cash, positions, trade_ids}``——某个已知时点的券商现金/持仓
        与“截至该时点已记录的成交集合”。推导时只回放 trade_ids 之外（基线之后）的成交，
        避免把基线现金里已包含的成交再减一遍（否则会产生幽灵差异、误触发自动熔断）。
        不传 baseline 时退化为“注资基准 initial_cash + 全部成交”（仅适用于从零起记的账户）。
        """
        with database.connect() as connection:
            state = connection.execute(
                "SELECT account_id, initial_cash FROM live_account_state "
                "ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
            trades = connection.execute(
                "SELECT symbol, side, quantity, price, fees, broker_trade_id "
                "FROM live_trades ORDER BY traded_at"
            ).fetchall()
        if baseline is not None:
            cash = to_decimal(baseline.get("cash", 0))
            positions: Dict[str, int] = {
                symbol: int(qty)
                for symbol, qty in (baseline.get("positions") or {}).items()
            }
            skip = set(baseline.get("trade_ids") or [])
        else:
            cash = to_decimal(float(state["initial_cash"])) if state else to_decimal(0)
            positions = {}
            skip = set()
        trade_ids: List[str] = []
        for trade in trades:
            trade_ids.append(trade["broker_trade_id"])
            if trade["broker_trade_id"] in skip:
                continue  # 基线之前的成交已包含在 baseline.cash/positions 中，不重复回放
            quantity = int(trade["quantity"])
            gross = to_decimal(round_money(quantity * float(trade["price"])))
            fees = to_decimal(trade["fees"] or 0)
            if str(trade["side"]).lower() == "buy":
                cash = cash - gross - fees
                positions[trade["symbol"]] = positions.get(trade["symbol"], 0) + quantity
            else:
                cash = cash + gross - fees
                positions[trade["symbol"]] = positions.get(trade["symbol"], 0) - quantity
        return {
            "account_id": state["account_id"] if state else None,
            "cash": round_money(cash),
            "positions": {symbol: qty for symbol, qty in positions.items() if qty != 0},
            "trade_ids": trade_ids,
            "trade_count": len(trade_ids),
            "has_baseline": (baseline is not None) or (state is not None),
        }

    def append_order_event(
        self,
        connection,
        order_id: str,
        event_type: str,
        payload: Dict[str, Any],
    ) -> None:
        """向订单事件流追加一条事件（append-only）。

        seq 在同一 order_id 下单调递增，复用调用方的事务连接以保证原子性。
        """
        row = connection.execute(
            "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM order_events WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        next_seq = int(row["max_seq"]) + 1
        connection.execute(
            """
            INSERT INTO order_events
                (event_id, order_id, seq, event_type, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("oevt"),
                order_id,
                next_seq,
                event_type,
                json.dumps(payload, ensure_ascii=True),
                utc_now(),
            ),
        )

    def list_order_events(self, order_id: str) -> List[Dict[str, Any]]:
        with database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM order_events WHERE order_id = ? ORDER BY seq",
                (order_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = decode_json(item.pop("payload_json"))
            result.append(item)
        return result
