import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from quantdev.db import database
from quantdev.repositories.base import decode_json, new_id, utc_now


class VerificationRepository:
    """VerificationRepository：verification 子域数据访问。"""

    def save_verification(
        self, check_type: str, status: str, details: Dict[str, Any]
    ) -> Dict[str, Any]:
        record = {
            "verification_id": new_id("verify"),
            "check_type": check_type,
            "status": status,
            "details": details,
            "created_at": utc_now(),
        }
        with database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO verification_runs
                    (verification_id, check_type, status, details_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    record["verification_id"],
                    check_type,
                    status,
                    json.dumps(details, ensure_ascii=True),
                    record["created_at"],
                ),
            )
        return record

    def latest_verification(self, check_type: str) -> Optional[Dict[str, Any]]:
        with database.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM verification_runs
                WHERE check_type = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (check_type,),
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["details"] = decode_json(item.pop("details_json"))
        return item

    def paper_trading_stats(self) -> Dict[str, Any]:
        """模拟盘成交与策略运行覆盖统计。"""
        with database.connect() as connection:
            fills = connection.execute(
                """
                SELECT filled_at
                FROM paper_fills
                ORDER BY filled_at
                """
            ).fetchall()
            run_rows = connection.execute(
                """
                SELECT DISTINCT trade_date
                FROM strategy_runs
                WHERE status NOT IN ('FAILED')
                ORDER BY trade_date
                """
            ).fetchall()
        cn_tz = timezone(timedelta(hours=8))
        fill_dates = sorted(
            {
                datetime.fromisoformat(row["filled_at"])
                .astimezone(cn_tz)
                .date()
                .isoformat()
                for row in fills
            }
        )
        strategy_dates = [str(row["trade_date"]) for row in run_rows]
        consecutive = 0
        if strategy_dates:
            strategy_set = set(strategy_dates)
            with database.connect() as connection:
                open_days = [
                    str(row["cal_date"])
                    for row in connection.execute(
                        """
                        SELECT cal_date FROM trade_calendar
                        WHERE exchange = 'SSE' AND is_open = 1
                          AND cal_date BETWEEN ? AND ?
                        ORDER BY cal_date
                        """,
                        (strategy_dates[0], strategy_dates[-1]),
                    ).fetchall()
                ]
            current = 0
            for trade_date in open_days or strategy_dates:
                if trade_date in strategy_set:
                    current += 1
                    consecutive = max(consecutive, current)
                else:
                    current = 0
        return {
            "filled_orders": len(fills),
            "trading_days": len(fill_dates),
            "first_fill": fills[0]["filled_at"] if fills else None,
            "last_fill": fills[-1]["filled_at"] if fills else None,
            "strategy_days": len(strategy_dates),
            "consecutive_strategy_days": consecutive,
            "first_strategy_day": strategy_dates[0] if strategy_dates else None,
            "last_strategy_day": strategy_dates[-1] if strategy_dates else None,
        }
