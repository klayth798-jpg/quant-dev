from typing import Any, Dict, Optional

from quantdev.db import database


class CalendarRepository:
    """CalendarRepository：calendar 子域数据访问。"""

    def calendar_entry(
        self, cal_date: str, exchange: str = "SSE"
    ) -> Optional[Dict[str, Any]]:
        """查询交易日历中某一天的记录；无数据时返回 None（由调用方决定兜底）。"""
        with database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM trade_calendar WHERE exchange = ? AND cal_date = ?",
                (exchange, cal_date),
            ).fetchone()
        return dict(row) if row else None

    def previous_open_day(
        self, cal_date: str, exchange: str = "SSE", inclusive: bool = False
    ) -> Optional[str]:
        operator = "<=" if inclusive else "<"
        with database.connect() as connection:
            row = connection.execute(
                """
                SELECT MAX(cal_date) AS cal_date
                FROM trade_calendar
                WHERE exchange = ? AND is_open = 1 AND cal_date {} ?
                """.format(operator),
                (exchange, cal_date),
            ).fetchone()
        return str(row["cal_date"]) if row and row["cal_date"] else None

    def next_open_day(
        self, cal_date: str, exchange: str = "SSE"
    ) -> Optional[str]:
        with database.connect() as connection:
            row = connection.execute(
                """
                SELECT MIN(cal_date) AS cal_date
                FROM trade_calendar
                WHERE exchange = ? AND is_open = 1 AND cal_date > ?
                """,
                (exchange, cal_date),
            ).fetchone()
        return str(row["cal_date"]) if row and row["cal_date"] else None
