from typing import Any, Dict, List, Optional

from quantdev.db import database
from quantdev.repositories.base import decode_json, new_id, utc_now


class SyncRepository:
    """SyncRepository：sync 子域数据访问。"""

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

    def save_dataset_manifest(
        self,
        snapshot_id: str,
        provider: str,
        sync_run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        summary = self.price_summary(snapshot_id)
        manifest = {
            "manifest_id": new_id("manifest"),
            "snapshot_id": snapshot_id,
            "provider": provider,
            "sync_run_id": sync_run_id,
            "max_trade_date": summary["end_date"],
            "row_count": int(summary["row_count"]),
            "instrument_count": int(summary["instrument_count"]),
            "status": "SEALED",
            "created_at": utc_now(),
        }
        with database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO dataset_manifests
                    (manifest_id, snapshot_id, provider, sync_run_id,
                     max_trade_date, row_count, instrument_count, status,
                     created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    manifest["manifest_id"],
                    snapshot_id,
                    provider,
                    sync_run_id,
                    manifest["max_trade_date"],
                    manifest["row_count"],
                    manifest["instrument_count"],
                    manifest["status"],
                    manifest["created_at"],
                ),
            )
        return manifest

    def latest_dataset_manifest(
        self, snapshot_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        query = "SELECT * FROM dataset_manifests"
        params: List[Any] = []
        if snapshot_id:
            query += " WHERE snapshot_id = ?"
            params.append(snapshot_id)
        query += " ORDER BY created_at DESC LIMIT 1"
        with database.connect() as connection:
            row = connection.execute(query, params).fetchone()
        return dict(row) if row else None
