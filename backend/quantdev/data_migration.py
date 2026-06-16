import sqlite3
from pathlib import Path
from typing import Dict, List

from quantdev.config import settings
from quantdev.db import PostgresDatabase

TABLES = (
    "instruments",
    "prices",
    "instrument_metadata",
    "trade_calendar",
    "daily_indicators",
    "financial_indicators",
    "indices",
    "index_constituents",
    "data_sync_runs",
    "factor_definitions",
    "factor_runs",
    "backtest_runs",
    "risk_events",
    "paper_accounts",
    "paper_positions",
    "paper_orders",
    "paper_fills",
    "audit_log",
    "system_flags",
    "order_events",
    "reconciliations",
    "domain_events",
    "market_quotes",
    "market_quote_history",
    "dataset_manifests",
    "account_daily_snapshots",
    "strategy_configs",
    "strategy_runs",
    "strategy_targets",
    "order_intents",
    "order_approvals",
    "live_orders",
    "live_trades",
    "live_account_state",
    "live_account_daily_snapshots",
    "live_positions",
    "system_leases",
    "mock_broker_orders",
    "mock_broker_trades",
    "mock_broker_accounts",
    "mock_broker_positions",
    "verification_runs",
    "task_jobs",
)


def migrate_sqlite_to_postgres(source_path: Path) -> Dict[str, int]:
    if not settings.database_url:
        raise ValueError("请先配置 PostgreSQL 的 QUANTDEV_DATABASE_URL")
    source_path = source_path.expanduser().resolve()
    if not source_path.is_file():
        raise ValueError("SQLite 文件不存在：{}".format(source_path))

    try:
        import psycopg
        from psycopg import sql
    except ImportError as exc:  # pragma: no cover - packaging guard
        raise RuntimeError("缺少 psycopg[binary]，请先安装项目依赖") from exc

    target_backend = PostgresDatabase(settings.database_url)
    target_backend.migrate()
    source = sqlite3.connect(
        "file:{}?mode=ro".format(source_path),
        uri=True,
    )
    source.row_factory = sqlite3.Row
    copied: Dict[str, int] = {}
    try:
        source_tables = {
            str(row["name"])
            for row in source.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        with psycopg.connect(settings.database_url) as target:
            for table in TABLES:
                if table not in source_tables:
                    continue
                with target.cursor() as cursor:
                    cursor.execute(
                        sql.SQL("SELECT COUNT(*) FROM {}").format(
                            sql.Identifier(table)
                        )
                    )
                    if int(cursor.fetchone()[0]) != 0:
                        raise ValueError(
                            "目标表 {} 非空；请使用全新的 PostgreSQL 数据库".format(
                                table
                            )
                        )
                columns: List[str] = [
                    str(row["name"])
                    for row in source.execute(
                        "PRAGMA table_info({})".format(table)
                    ).fetchall()
                ]
                source_cursor = source.execute(
                    "SELECT {} FROM {}".format(
                        ", ".join('"{}"'.format(column) for column in columns),
                        '"{}"'.format(table),
                    )
                )
                count = 0
                with target.cursor() as cursor:
                    copy_statement = sql.SQL("COPY {} ({}) FROM STDIN").format(
                        sql.Identifier(table),
                        sql.SQL(", ").join(
                            sql.Identifier(column) for column in columns
                        ),
                    )
                    with cursor.copy(copy_statement) as copy:
                        while True:
                            rows = source_cursor.fetchmany(5000)
                            if not rows:
                                break
                            for row in rows:
                                copy.write_row(tuple(row))
                            count += len(rows)
                target.commit()
                copied[table] = count
    finally:
        source.close()
    return copied
