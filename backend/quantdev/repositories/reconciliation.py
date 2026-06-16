import json
from typing import Any, Dict, List, Optional

from quantdev.db import database
from quantdev.repositories.base import decode_json, new_id, utc_now


class ReconciliationRepository:
    """ReconciliationRepository：reconciliation 子域数据访问。"""

    def save_reconciliation(self, report: Dict[str, Any]) -> str:
        recon_id = new_id("recon")
        with database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO reconciliations
                    (recon_id, broker_mode, status, cash_diff, position_break_count,
                     breaks_json, local_json, broker_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    recon_id,
                    report["broker_mode"],
                    report["status"],
                    report["cash_diff"],
                    len(report["breaks"]),
                    json.dumps(report["breaks"], ensure_ascii=True),
                    json.dumps(report["local"], ensure_ascii=True),
                    json.dumps(report["broker"], ensure_ascii=True),
                    utc_now(),
                ),
            )
        return recon_id

    def list_reconciliations(self, limit: int = 20) -> List[Dict[str, Any]]:
        with database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM reconciliations ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._decode_reconciliation(row) for row in rows]

    def get_reconciliation(self, recon_id: str) -> Optional[Dict[str, Any]]:
        with database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM reconciliations WHERE recon_id = ?", (recon_id,)
            ).fetchone()
        return self._decode_reconciliation(row) if row else None

    @staticmethod
    def _decode_reconciliation(row) -> Dict[str, Any]:
        item = dict(row)
        item["breaks"] = decode_json(item.pop("breaks_json"))
        item["local"] = decode_json(item.pop("local_json"))
        item["broker"] = decode_json(item.pop("broker_json"))
        return item
