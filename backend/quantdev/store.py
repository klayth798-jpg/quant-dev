import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from quantdev.db import database


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return "{}_{}".format(prefix, uuid.uuid4().hex[:16])


def decode_json(value: str) -> Any:
    return json.loads(value)


class Store:
    def list_instruments(self) -> List[Dict[str, Any]]:
        with database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM instruments WHERE active = 1 ORDER BY symbol"
            ).fetchall()
        return [dict(row) for row in rows]

    def list_prices(
        self, symbol: Optional[str] = None, snapshot_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        query = "SELECT * FROM prices WHERE 1 = 1"
        params: List[Any] = []
        if symbol:
            query += " AND symbol = ?"
            params.append(symbol)
        if snapshot_id:
            query += " AND snapshot_id = ?"
            params.append(snapshot_id)
        query += " ORDER BY trade_date, symbol"
        with database.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def latest_snapshot_id(self) -> Optional[str]:
        with database.connect() as connection:
            row = connection.execute(
                "SELECT snapshot_id, MAX(trade_date) FROM prices GROUP BY snapshot_id "
                "ORDER BY MAX(trade_date) DESC LIMIT 1"
            ).fetchone()
        return str(row["snapshot_id"]) if row else None

    def get_data_sync_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        with database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM data_sync_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["parameters"] = decode_json(item.pop("parameters_json"))
        item["stats"] = decode_json(item.pop("stats_json"))
        return item

    def list_data_sync_runs(self, limit: int = 20) -> List[Dict[str, Any]]:
        with database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM data_sync_runs ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["parameters"] = decode_json(item.pop("parameters_json"))
            item["stats"] = decode_json(item.pop("stats_json"))
            result.append(item)
        return result

    def list_indices(self) -> List[Dict[str, Any]]:
        with database.connect() as connection:
            rows = connection.execute(
                """
                SELECT i.*,
                       (SELECT MAX(c.trade_date) FROM index_constituents c
                        WHERE c.index_code = i.index_code) AS latest_weight_date,
                       (SELECT COUNT(*) FROM index_constituents c
                        WHERE c.index_code = i.index_code
                          AND c.trade_date = (
                              SELECT MAX(c2.trade_date) FROM index_constituents c2
                              WHERE c2.index_code = i.index_code
                          )) AS latest_constituent_count
                FROM indices i
                ORDER BY i.index_code
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_constituent_symbols(self, index_codes: List[str]) -> List[str]:
        if not index_codes:
            return []
        placeholders = ",".join("?" for _ in index_codes)
        query = f"""
            SELECT DISTINCT c.symbol
            FROM index_constituents c
            WHERE c.index_code IN ({placeholders})
              AND c.trade_date = (
                  SELECT MAX(c2.trade_date)
                  FROM index_constituents c2
                  WHERE c2.index_code = c.index_code
              )
            ORDER BY c.symbol
        """
        with database.connect() as connection:
            rows = connection.execute(query, index_codes).fetchall()
        return [str(row["symbol"]) for row in rows]

    def list_financial_indicators(
        self, symbol: str, limit: int = 20
    ) -> List[Dict[str, Any]]:
        with database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM financial_indicators
                WHERE symbol = ?
                ORDER BY end_date DESC, ann_date DESC
                LIMIT ?
                """,
                (symbol, limit),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["raw"] = decode_json(item.pop("raw_json"))
            result.append(item)
        return result

    def list_factors(self) -> List[Dict[str, Any]]:
        with database.connect() as connection:
            rows = connection.execute(
                """
                SELECT f.*,
                       (SELECT metrics_json FROM factor_runs r
                        WHERE r.factor_id = f.factor_id
                        ORDER BY r.created_at DESC LIMIT 1) AS latest_metrics_json,
                       (SELECT run_id FROM factor_runs r
                        WHERE r.factor_id = f.factor_id
                        ORDER BY r.created_at DESC LIMIT 1) AS latest_run_id
                FROM factor_definitions f
                ORDER BY f.category, f.factor_id
                """
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            raw_metrics = item.pop("latest_metrics_json")
            item["latest_metrics"] = decode_json(raw_metrics) if raw_metrics else None
            result.append(item)
        return result

    def get_factor(self, factor_id: str) -> Optional[Dict[str, Any]]:
        with database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM factor_definitions WHERE factor_id = ?", (factor_id,)
            ).fetchone()
        return dict(row) if row else None

    def save_factor_run(
        self,
        factor_id: str,
        snapshot_id: str,
        parameters: Dict[str, Any],
        metrics: Dict[str, Any],
    ) -> Dict[str, Any]:
        run_id = new_id("fac")
        created_at = utc_now()
        with database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO factor_runs
                    (run_id, factor_id, snapshot_id, parameters_json, metrics_json,
                     status, created_at)
                VALUES (?, ?, ?, ?, ?, 'completed', ?)
                """,
                (
                    run_id,
                    factor_id,
                    snapshot_id,
                    json.dumps(parameters, ensure_ascii=True),
                    json.dumps(metrics, ensure_ascii=True),
                    created_at,
                ),
            )
        return {
            "run_id": run_id,
            "factor_id": factor_id,
            "snapshot_id": snapshot_id,
            "parameters": parameters,
            "metrics": metrics,
            "status": "completed",
            "created_at": created_at,
        }

    def save_backtest(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        with database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO backtest_runs
                    (run_id, name, factor_id, snapshot_id, config_json, metrics_json,
                     equity_json, trades_json, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["run_id"],
                    payload["name"],
                    payload["factor_id"],
                    payload["snapshot_id"],
                    json.dumps(payload["config"], ensure_ascii=True),
                    json.dumps(payload["metrics"], ensure_ascii=True),
                    json.dumps(payload["equity_curve"], ensure_ascii=True),
                    json.dumps(payload["trades"], ensure_ascii=True),
                    payload["status"],
                    payload["created_at"],
                ),
            )
        return payload

    def list_backtests(self, limit: int = 20) -> List[Dict[str, Any]]:
        with database.connect() as connection:
            rows = connection.execute(
                """
                SELECT run_id, name, factor_id, snapshot_id, config_json, metrics_json,
                       status, created_at
                FROM backtest_runs
                ORDER BY created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["config"] = decode_json(item.pop("config_json"))
            item["metrics"] = decode_json(item.pop("metrics_json"))
            result.append(item)
        return result

    def get_backtest(self, run_id: str) -> Optional[Dict[str, Any]]:
        with database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM backtest_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["config"] = decode_json(item.pop("config_json"))
        item["metrics"] = decode_json(item.pop("metrics_json"))
        item["equity_curve"] = decode_json(item.pop("equity_json"))
        item["trades"] = decode_json(item.pop("trades_json"))
        return item

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


store = Store()
