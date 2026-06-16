import json
from typing import Any, Dict, List, Optional

from quantdev.db import database
from quantdev.repositories.base import decode_json, new_id, utc_now


class ResearchRepository:
    """ResearchRepository：research 子域数据访问。"""

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
                    (run_id, name, factor_id, snapshot_id, manifest_id,
                     config_json, metrics_json, equity_json, trades_json,
                     status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["run_id"],
                    payload["name"],
                    payload["factor_id"],
                    payload["snapshot_id"],
                    payload.get("manifest_id"),
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
                SELECT run_id, name, factor_id, snapshot_id, manifest_id,
                       config_json, metrics_json, status, created_at
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
