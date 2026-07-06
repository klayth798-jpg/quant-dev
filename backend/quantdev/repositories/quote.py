from typing import Any, Dict, List, Optional

from quantdev.db import database
from quantdev.repositories.base import new_id


class QuoteRepository:
    """QuoteRepository：quote 子域数据访问。"""

    def upsert_market_quote(self, quote: Dict[str, Any]) -> Dict[str, Any]:
        with database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO market_quote_history
                    (quote_id, symbol, price, bid, ask, quote_time, trade_date,
                     source, status, received_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id("quote"),
                    quote["symbol"],
                    quote["price"],
                    quote.get("bid"),
                    quote.get("ask"),
                    quote["quote_time"],
                    quote["trade_date"],
                    quote["source"],
                    quote["status"],
                    quote["received_at"],
                ),
            )
            connection.execute(
                """
                INSERT INTO market_quotes
                    (symbol, price, bid, ask, quote_time, trade_date,
                     source, status, received_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    price = excluded.price,
                    bid = excluded.bid,
                    ask = excluded.ask,
                    quote_time = excluded.quote_time,
                    trade_date = excluded.trade_date,
                    source = excluded.source,
                    status = excluded.status,
                    received_at = excluded.received_at
                WHERE excluded.quote_time >= market_quotes.quote_time
                """,
                (
                    quote["symbol"],
                    quote["price"],
                    quote.get("bid"),
                    quote.get("ask"),
                    quote["quote_time"],
                    quote["trade_date"],
                    quote["source"],
                    quote["status"],
                    quote["received_at"],
                ),
            )
        return quote

    def get_market_quote(self, symbol: str) -> Optional[Dict[str, Any]]:
        with database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM market_quotes WHERE symbol = ?", (symbol,)
            ).fetchone()
        return dict(row) if row else None

    def list_market_quotes(self) -> List[Dict[str, Any]]:
        with database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM market_quotes ORDER BY symbol"
            ).fetchall()
        return [dict(row) for row in rows]
