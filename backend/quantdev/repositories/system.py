import json
from typing import Any, Dict, List, Optional

from quantdev.db import database
from quantdev.repositories.base import decode_json, new_id, utc_now


class SystemRepository:
    """SystemRepository：system 子域数据访问。"""

    def save_risk_event(
        self, severity: str, rule_code: str, message: str, payload: Dict[str, Any]
    ) -> None:
        with database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO risk_events
                    (event_id, severity, rule_code, message, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id("risk"),
                    severity,
                    rule_code,
                    message,
                    json.dumps(payload, ensure_ascii=True),
                    utc_now(),
                ),
            )

    def audit(
        self,
        actor: str,
        action: str,
        resource_type: str,
        resource_id: str,
        payload: Dict[str, Any],
    ) -> None:
        with database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO audit_log
                    (audit_id, actor, action, resource_type, resource_id,
                     payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id("audit"),
                    actor,
                    action,
                    resource_type,
                    resource_id,
                    json.dumps(payload, ensure_ascii=True),
                    utc_now(),
                ),
            )

    def get_flag(self, flag: str) -> Optional[Dict[str, Any]]:
        with database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM system_flags WHERE flag = ?", (flag,)
            ).fetchone()
        return dict(row) if row else None

    def set_flag(
        self, flag: str, value: str, reason: str, updated_by: str
    ) -> Dict[str, Any]:
        now = utc_now()
        with database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO system_flags (flag, value, reason, updated_by, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(flag) DO UPDATE SET
                    value = excluded.value,
                    reason = excluded.reason,
                    updated_by = excluded.updated_by,
                    updated_at = excluded.updated_at
                """,
                (flag, value, reason, updated_by, now),
            )
        return {
            "flag": flag,
            "value": value,
            "reason": reason,
            "updated_by": updated_by,
            "updated_at": now,
        }

    def record_domain_event(
        self,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: Dict[str, Any],
        actor: str = "system",
    ) -> Dict[str, Any]:
        """持久化一条全局领域事件（append-only），seq 全局单调递增。"""
        event_id = new_id("evt")
        created_at = utc_now()
        with database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM domain_events"
            ).fetchone()
            next_seq = int(row["max_seq"]) + 1
            connection.execute(
                """
                INSERT INTO domain_events
                    (event_id, seq, event_type, aggregate_type, aggregate_id,
                     actor, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    next_seq,
                    event_type,
                    aggregate_type,
                    aggregate_id,
                    actor,
                    json.dumps(payload, ensure_ascii=True),
                    created_at,
                ),
            )
        return {
            "event_id": event_id,
            "seq": next_seq,
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "actor": actor,
            "payload": payload,
            "created_at": created_at,
        }

    def list_domain_events(
        self,
        limit: int = 50,
        event_type: Optional[str] = None,
        aggregate_type: Optional[str] = None,
        aggregate_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        query = "SELECT * FROM domain_events WHERE 1 = 1"
        params: List[Any] = []
        if event_type:
            query += " AND event_type = ?"
            params.append(event_type)
        if aggregate_type:
            query += " AND aggregate_type = ?"
            params.append(aggregate_type)
        if aggregate_id:
            query += " AND aggregate_id = ?"
            params.append(aggregate_id)
        query += " ORDER BY seq DESC LIMIT ?"
        params.append(limit)
        with database.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = decode_json(item.pop("payload_json"))
            result.append(item)
        return result

    def refresh_money_cents(self) -> None:
        """同步金额影子列（元 -> 分）。

        这是 float/REAL 到 cents/NUMERIC 的渐进迁移桥：旧列继续服务现有读取逻辑，
        新列提供生产账本逐步切换所需的整数金额真相。
        """
        updates = (
            (
                "paper_accounts",
                {
                    "cash_cents": "cash",
                    "reserved_cash_cents": "reserved_cash",
                    "initial_cash_cents": "initial_cash",
                },
            ),
            ("paper_positions", {"average_cost_cents": "average_cost"}),
            (
                "paper_orders",
                {
                    "limit_price_cents": "limit_price",
                    "average_fill_price_cents": "average_fill_price",
                    "reserved_cash_cents": "reserved_cash",
                },
            ),
            ("paper_fills", {"price_cents": "price", "fees_cents": "fees"}),
            (
                "account_daily_snapshots",
                {
                    "start_equity_cents": "start_equity",
                    "current_equity_cents": "current_equity",
                    "daily_pnl_cents": "daily_pnl",
                },
            ),
            (
                "live_orders",
                {
                    "limit_price_cents": "limit_price",
                    "average_fill_price_cents": "average_fill_price",
                },
            ),
            ("live_trades", {"price_cents": "price", "fees_cents": "fees"}),
            (
                "live_account_state",
                {"cash_cents": "cash", "initial_cash_cents": "initial_cash"},
            ),
            (
                "live_account_daily_snapshots",
                {
                    "start_equity_cents": "start_equity",
                    "current_equity_cents": "current_equity",
                    "daily_pnl_cents": "daily_pnl",
                },
            ),
            ("live_positions", {"average_cost_cents": "average_cost"}),
            ("reconciliations", {"cash_diff_cents": "cash_diff"}),
            (
                "mock_broker_orders",
                {
                    "limit_price_cents": "limit_price",
                    "average_fill_price_cents": "average_fill_price",
                },
            ),
            ("mock_broker_trades", {"price_cents": "price"}),
            (
                "mock_broker_accounts",
                {"cash_cents": "cash", "initial_cash_cents": "initial_cash"},
            ),
            ("mock_broker_positions", {"average_cost_cents": "average_cost"}),
        )
        with database.transaction(immediate=True) as connection:
            for table, columns in updates:
                assignments = [
                    "{target} = CASE WHEN {source} IS NULL THEN NULL "
                    "ELSE CAST(ROUND({source} * 100) AS BIGINT) END".format(
                        target=target,
                        source=source,
                    )
                    for target, source in columns.items()
                ]
                connection.execute(
                    "UPDATE {} SET {}".format(table, ", ".join(assignments))
                )
