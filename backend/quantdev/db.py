import re
import sqlite3
from abc import ABC, abstractmethod
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator, Iterable, Optional, Sequence

from quantdev.config import settings

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS instruments (
    symbol TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    exchange TEXT NOT NULL,
    asset_type TEXT NOT NULL,
    industry TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS prices (
    symbol TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL,
    amount REAL NOT NULL,
    adj_factor REAL NOT NULL DEFAULT 1,
    source TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    PRIMARY KEY (symbol, trade_date, snapshot_id),
    FOREIGN KEY (symbol) REFERENCES instruments(symbol)
);

CREATE INDEX IF NOT EXISTS idx_prices_date ON prices(trade_date);
CREATE INDEX IF NOT EXISTS idx_prices_snapshot_date_symbol
ON prices(snapshot_id, trade_date, symbol);

CREATE TABLE IF NOT EXISTS instrument_metadata (
    symbol TEXT PRIMARY KEY,
    raw_symbol TEXT NOT NULL,
    area TEXT,
    market TEXT,
    currency TEXT,
    list_status TEXT NOT NULL,
    list_date TEXT,
    delist_date TEXT,
    is_hs TEXT,
    source TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (symbol) REFERENCES instruments(symbol)
);

CREATE TABLE IF NOT EXISTS trade_calendar (
    exchange TEXT NOT NULL,
    cal_date TEXT NOT NULL,
    is_open INTEGER NOT NULL,
    pretrade_date TEXT,
    source TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (exchange, cal_date)
);

CREATE TABLE IF NOT EXISTS daily_indicators (
    symbol TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    turnover_rate REAL,
    volume_ratio REAL,
    pe REAL,
    pe_ttm REAL,
    pb REAL,
    ps_ttm REAL,
    dv_ttm REAL,
    total_share REAL,
    float_share REAL,
    free_share REAL,
    total_mv REAL,
    circ_mv REAL,
    source TEXT NOT NULL,
    sync_run_id TEXT NOT NULL,
    PRIMARY KEY (symbol, trade_date),
    FOREIGN KEY (symbol) REFERENCES instruments(symbol)
);

CREATE INDEX IF NOT EXISTS idx_daily_indicators_date_symbol
ON daily_indicators(trade_date, symbol);

CREATE TABLE IF NOT EXISTS financial_indicators (
    symbol TEXT NOT NULL,
    ann_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    update_flag TEXT NOT NULL DEFAULT '',
    eps REAL,
    bps REAL,
    roe REAL,
    roa REAL,
    roic REAL,
    grossprofit_margin REAL,
    netprofit_margin REAL,
    debt_to_assets REAL,
    current_ratio REAL,
    quick_ratio REAL,
    ocfps REAL,
    netprofit_yoy REAL,
    tr_yoy REAL,
    or_yoy REAL,
    q_netprofit_yoy REAL,
    q_sales_yoy REAL,
    source TEXT NOT NULL,
    sync_run_id TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    PRIMARY KEY (symbol, ann_date, end_date, update_flag),
    FOREIGN KEY (symbol) REFERENCES instruments(symbol)
);

CREATE INDEX IF NOT EXISTS idx_financial_symbol_end
ON financial_indicators(symbol, end_date);

CREATE TABLE IF NOT EXISTS indices (
    index_code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    fullname TEXT,
    market TEXT,
    publisher TEXT,
    index_type TEXT,
    category TEXT,
    base_date TEXT,
    base_point REAL,
    list_date TEXT,
    weight_rule TEXT,
    description TEXT,
    exp_date TEXT,
    source TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS index_constituents (
    index_code TEXT NOT NULL,
    symbol TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    weight REAL NOT NULL,
    source TEXT NOT NULL,
    sync_run_id TEXT NOT NULL,
    PRIMARY KEY (index_code, symbol, trade_date),
    FOREIGN KEY (index_code) REFERENCES indices(index_code),
    FOREIGN KEY (symbol) REFERENCES instruments(symbol)
);

CREATE INDEX IF NOT EXISTS idx_index_constituents_date
ON index_constituents(index_code, trade_date);

CREATE INDEX IF NOT EXISTS idx_index_constituents_code_date_symbol
ON index_constituents(index_code, trade_date, symbol);

CREATE TABLE IF NOT EXISTS data_sync_runs (
    run_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    dataset TEXT NOT NULL,
    status TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    stats_json TEXT NOT NULL,
    error_message TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS factor_definitions (
    factor_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    category TEXT NOT NULL,
    expression TEXT NOT NULL,
    version TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS factor_runs (
    run_id TEXT PRIMARY KEY,
    factor_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (factor_id) REFERENCES factor_definitions(factor_id)
);

CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    factor_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    manifest_id TEXT,
    config_json TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    equity_json TEXT NOT NULL,
    trades_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_events (
    event_id TEXT PRIMARY KEY,
    severity TEXT NOT NULL,
    rule_code TEXT NOT NULL,
    message TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_accounts (
    account_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    cash REAL NOT NULL,
    reserved_cash REAL NOT NULL DEFAULT 0,
    initial_cash REAL NOT NULL,
    status TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_positions (
    account_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    average_cost REAL NOT NULL,
    frozen_quantity INTEGER NOT NULL DEFAULT 0,
    frozen_date TEXT,
    reserved_quantity INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_id, symbol),
    FOREIGN KEY (account_id) REFERENCES paper_accounts(account_id),
    FOREIGN KEY (symbol) REFERENCES instruments(symbol)
);

CREATE TABLE IF NOT EXISTS paper_orders (
    order_id TEXT PRIMARY KEY,
    client_order_id TEXT NOT NULL UNIQUE,
    account_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    order_type TEXT NOT NULL,
    limit_price REAL,
    status TEXT NOT NULL,
    filled_quantity INTEGER NOT NULL DEFAULT 0,
    average_fill_price REAL,
    reserved_cash REAL NOT NULL DEFAULT 0,
    reserved_quantity INTEGER NOT NULL DEFAULT 0,
    broker_order_id TEXT,
    last_error TEXT,
    strategy_run_id TEXT,
    order_intent_id TEXT,
    version INTEGER NOT NULL DEFAULT 0,
    reject_reason TEXT,
    submitted_at TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (account_id) REFERENCES paper_accounts(account_id),
    FOREIGN KEY (symbol) REFERENCES instruments(symbol)
);

CREATE TABLE IF NOT EXISTS paper_fills (
    fill_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    price REAL NOT NULL,
    fees REAL NOT NULL,
    filled_at TEXT NOT NULL,
    FOREIGN KEY (order_id) REFERENCES paper_orders(order_id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    audit_id TEXT PRIMARY KEY,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS system_flags (
    flag TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    reason TEXT,
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS order_events (
    event_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (order_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_order_events_order ON order_events(order_id, seq);

CREATE TABLE IF NOT EXISTS reconciliations (
    recon_id TEXT PRIMARY KEY,
    broker_mode TEXT NOT NULL,
    status TEXT NOT NULL,
    cash_diff REAL NOT NULL,
    position_break_count INTEGER NOT NULL,
    breaks_json TEXT NOT NULL,
    local_json TEXT NOT NULL,
    broker_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reconciliations_created ON reconciliations(created_at);

CREATE TABLE IF NOT EXISTS domain_events (
    event_id TEXT PRIMARY KEY,
    seq INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_domain_events_seq ON domain_events(seq);
CREATE INDEX IF NOT EXISTS idx_domain_events_type ON domain_events(event_type);
CREATE INDEX IF NOT EXISTS idx_domain_events_aggregate
ON domain_events(aggregate_type, aggregate_id);

CREATE TABLE IF NOT EXISTS market_quotes (
    symbol TEXT PRIMARY KEY,
    price REAL NOT NULL,
    bid REAL,
    ask REAL,
    quote_time TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    received_at TEXT NOT NULL,
    FOREIGN KEY (symbol) REFERENCES instruments(symbol)
);

CREATE INDEX IF NOT EXISTS idx_market_quotes_time ON market_quotes(quote_time);

CREATE TABLE IF NOT EXISTS market_quote_history (
    quote_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    price REAL NOT NULL,
    bid REAL,
    ask REAL,
    quote_time TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    received_at TEXT NOT NULL,
    FOREIGN KEY (symbol) REFERENCES instruments(symbol)
);

CREATE INDEX IF NOT EXISTS idx_market_quote_history_symbol_time
ON market_quote_history(symbol, quote_time);

CREATE TABLE IF NOT EXISTS dataset_manifests (
    manifest_id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    sync_run_id TEXT,
    max_trade_date TEXT,
    row_count INTEGER NOT NULL,
    instrument_count INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dataset_manifests_snapshot_created
ON dataset_manifests(snapshot_id, created_at);

CREATE TABLE IF NOT EXISTS account_daily_snapshots (
    account_id TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    start_equity REAL NOT NULL,
    current_equity REAL NOT NULL,
    daily_pnl REAL NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_id, trade_date),
    FOREIGN KEY (account_id) REFERENCES paper_accounts(account_id)
);

CREATE TABLE IF NOT EXISTS strategy_configs (
    strategy_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    factor_id TEXT NOT NULL,
    top_n INTEGER NOT NULL,
    rebalance_days INTEGER NOT NULL,
    universe_json TEXT NOT NULL,
    neutralize INTEGER NOT NULL DEFAULT 0,
    order_type TEXT NOT NULL,
    max_order_notional REAL NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS strategy_runs (
    run_id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    signal_date TEXT,
    snapshot_id TEXT,
    manifest_id TEXT,
    status TEXT NOT NULL,
    error_message TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    UNIQUE (strategy_id, trade_date),
    FOREIGN KEY (strategy_id) REFERENCES strategy_configs(strategy_id)
);

CREATE TABLE IF NOT EXISTS strategy_targets (
    run_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    score REAL NOT NULL,
    target_weight REAL NOT NULL,
    target_quantity INTEGER NOT NULL,
    PRIMARY KEY (run_id, symbol),
    FOREIGN KEY (run_id) REFERENCES strategy_runs(run_id)
);

CREATE TABLE IF NOT EXISTS order_intents (
    intent_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    client_order_id TEXT NOT NULL UNIQUE,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    order_type TEXT NOT NULL,
    limit_price REAL,
    status TEXT NOT NULL,
    paper_order_id TEXT,
    live_order_id TEXT,
    broker_order_id TEXT,
    execution_mode TEXT NOT NULL DEFAULT 'paper',
    approval_status TEXT NOT NULL DEFAULT 'NOT_REQUIRED',
    request_hash TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES strategy_runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_order_intents_run ON order_intents(run_id);

CREATE TABLE IF NOT EXISTS order_approvals (
    approval_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    reason TEXT,
    approved_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (intent_id) REFERENCES order_intents(intent_id)
);

CREATE INDEX IF NOT EXISTS idx_order_approvals_intent
ON order_approvals(intent_id, created_at);

CREATE TABLE IF NOT EXISTS live_orders (
    order_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL UNIQUE,
    client_order_id TEXT NOT NULL UNIQUE,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    order_type TEXT NOT NULL,
    limit_price REAL,
    request_hash TEXT NOT NULL,
    approval_id TEXT,
    broker_order_id TEXT UNIQUE,
    status TEXT NOT NULL,
    filled_quantity INTEGER NOT NULL DEFAULT 0,
    average_fill_price REAL,
    last_error TEXT,
    submitted_at TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (intent_id) REFERENCES order_intents(intent_id),
    FOREIGN KEY (approval_id) REFERENCES order_approvals(approval_id)
);

CREATE INDEX IF NOT EXISTS idx_live_orders_status ON live_orders(status);

CREATE TABLE IF NOT EXISTS live_trades (
    trade_id TEXT PRIMARY KEY,
    broker_trade_id TEXT NOT NULL UNIQUE,
    order_id TEXT NOT NULL,
    broker_order_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    price REAL NOT NULL,
    fees REAL NOT NULL DEFAULT 0,
    traded_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (order_id) REFERENCES live_orders(order_id)
);

CREATE TABLE IF NOT EXISTS live_account_state (
    account_id TEXT PRIMARY KEY,
    cash REAL NOT NULL,
    initial_cash REAL NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS live_account_daily_snapshots (
    account_id TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    start_equity REAL NOT NULL,
    current_equity REAL NOT NULL,
    daily_pnl REAL NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_id, trade_date)
);

CREATE TABLE IF NOT EXISTS live_positions (
    account_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    sellable_quantity INTEGER NOT NULL DEFAULT 0,
    average_cost REAL NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_id, symbol)
);

CREATE TABLE IF NOT EXISTS system_leases (
    lease_key TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mock_broker_orders (
    broker_order_id TEXT PRIMARY KEY,
    client_order_id TEXT NOT NULL UNIQUE,
    scenario TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    order_type TEXT NOT NULL,
    limit_price REAL,
    filled_quantity INTEGER NOT NULL DEFAULT 0,
    average_fill_price REAL,
    status TEXT NOT NULL,
    message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mock_broker_trades (
    trade_id TEXT PRIMARY KEY,
    broker_order_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    price REAL NOT NULL,
    traded_at TEXT NOT NULL,
    FOREIGN KEY (broker_order_id) REFERENCES mock_broker_orders(broker_order_id)
);

CREATE TABLE IF NOT EXISTS mock_broker_accounts (
    account_id TEXT PRIMARY KEY,
    cash REAL NOT NULL,
    initial_cash REAL NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mock_broker_positions (
    account_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    average_cost REAL NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_id, symbol)
);

CREATE TABLE IF NOT EXISTS verification_runs (
    verification_id TEXT PRIMARY KEY,
    check_type TEXT NOT NULL,
    status TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_verification_runs_type_created
ON verification_runs(check_type, created_at);
"""

TASK_SCHEMA = """
CREATE TABLE IF NOT EXISTS task_jobs (
    task_id TEXT PRIMARY KEY,
    task_type TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    result_json TEXT,
    error_message TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    worker_id TEXT,
    created_at TEXT NOT NULL,
    queued_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_task_jobs_status_created
ON task_jobs(status, created_at);
"""

FACTOR_CACHE_AND_MONEY_SCHEMA = """
CREATE TABLE IF NOT EXISTS factor_score_snapshots (
    factor_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    signal_date TEXT NOT NULL,
    neutralize INTEGER NOT NULL DEFAULT 0,
    scores_json TEXT NOT NULL,
    score_count INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (factor_id, snapshot_id, signal_date, neutralize),
    FOREIGN KEY (factor_id) REFERENCES factor_definitions(factor_id)
);

CREATE INDEX IF NOT EXISTS idx_factor_score_snapshots_created
ON factor_score_snapshots(created_at);

ALTER TABLE paper_accounts ADD COLUMN cash_cents BIGINT;
ALTER TABLE paper_accounts ADD COLUMN reserved_cash_cents BIGINT;
ALTER TABLE paper_accounts ADD COLUMN initial_cash_cents BIGINT;
ALTER TABLE paper_positions ADD COLUMN average_cost_cents BIGINT;
ALTER TABLE paper_orders ADD COLUMN limit_price_cents BIGINT;
ALTER TABLE paper_orders ADD COLUMN average_fill_price_cents BIGINT;
ALTER TABLE paper_orders ADD COLUMN reserved_cash_cents BIGINT;
ALTER TABLE paper_fills ADD COLUMN price_cents BIGINT;
ALTER TABLE paper_fills ADD COLUMN fees_cents BIGINT;
ALTER TABLE account_daily_snapshots ADD COLUMN start_equity_cents BIGINT;
ALTER TABLE account_daily_snapshots ADD COLUMN current_equity_cents BIGINT;
ALTER TABLE account_daily_snapshots ADD COLUMN daily_pnl_cents BIGINT;

ALTER TABLE live_orders ADD COLUMN limit_price_cents BIGINT;
ALTER TABLE live_orders ADD COLUMN average_fill_price_cents BIGINT;
ALTER TABLE live_trades ADD COLUMN price_cents BIGINT;
ALTER TABLE live_trades ADD COLUMN fees_cents BIGINT;
ALTER TABLE live_account_state ADD COLUMN cash_cents BIGINT;
ALTER TABLE live_account_state ADD COLUMN initial_cash_cents BIGINT;
ALTER TABLE live_account_daily_snapshots ADD COLUMN start_equity_cents BIGINT;
ALTER TABLE live_account_daily_snapshots ADD COLUMN current_equity_cents BIGINT;
ALTER TABLE live_account_daily_snapshots ADD COLUMN daily_pnl_cents BIGINT;
ALTER TABLE live_positions ADD COLUMN average_cost_cents BIGINT;
ALTER TABLE reconciliations ADD COLUMN cash_diff_cents BIGINT;

ALTER TABLE mock_broker_orders ADD COLUMN limit_price_cents BIGINT;
ALTER TABLE mock_broker_orders ADD COLUMN average_fill_price_cents BIGINT;
ALTER TABLE mock_broker_trades ADD COLUMN price_cents BIGINT;
ALTER TABLE mock_broker_accounts ADD COLUMN cash_cents BIGINT;
ALTER TABLE mock_broker_accounts ADD COLUMN initial_cash_cents BIGINT;
ALTER TABLE mock_broker_positions ADD COLUMN average_cost_cents BIGINT;

UPDATE paper_accounts
SET cash_cents = CAST(ROUND(cash * 100) AS BIGINT),
    reserved_cash_cents = CAST(ROUND(reserved_cash * 100) AS BIGINT),
    initial_cash_cents = CAST(ROUND(initial_cash * 100) AS BIGINT);
UPDATE paper_positions SET average_cost_cents = CAST(ROUND(average_cost * 100) AS BIGINT);
UPDATE paper_orders
SET limit_price_cents = CASE
        WHEN limit_price IS NULL THEN NULL
        ELSE CAST(ROUND(limit_price * 100) AS BIGINT)
    END,
    average_fill_price_cents = CASE
        WHEN average_fill_price IS NULL THEN NULL
        ELSE CAST(ROUND(average_fill_price * 100) AS BIGINT)
    END,
    reserved_cash_cents = CAST(ROUND(reserved_cash * 100) AS BIGINT);
UPDATE paper_fills
SET price_cents = CAST(ROUND(price * 100) AS BIGINT),
    fees_cents = CAST(ROUND(fees * 100) AS BIGINT);
UPDATE account_daily_snapshots
SET start_equity_cents = CAST(ROUND(start_equity * 100) AS BIGINT),
    current_equity_cents = CAST(ROUND(current_equity * 100) AS BIGINT),
    daily_pnl_cents = CAST(ROUND(daily_pnl * 100) AS BIGINT);

UPDATE live_orders
SET limit_price_cents = CASE
        WHEN limit_price IS NULL THEN NULL
        ELSE CAST(ROUND(limit_price * 100) AS BIGINT)
    END,
    average_fill_price_cents = CASE
        WHEN average_fill_price IS NULL THEN NULL
        ELSE CAST(ROUND(average_fill_price * 100) AS BIGINT)
    END;
UPDATE live_trades
SET price_cents = CAST(ROUND(price * 100) AS BIGINT),
    fees_cents = CAST(ROUND(fees * 100) AS BIGINT);
UPDATE live_account_state
SET cash_cents = CAST(ROUND(cash * 100) AS BIGINT),
    initial_cash_cents = CAST(ROUND(initial_cash * 100) AS BIGINT);
UPDATE live_account_daily_snapshots
SET start_equity_cents = CAST(ROUND(start_equity * 100) AS BIGINT),
    current_equity_cents = CAST(ROUND(current_equity * 100) AS BIGINT),
    daily_pnl_cents = CAST(ROUND(daily_pnl * 100) AS BIGINT);
UPDATE live_positions SET average_cost_cents = CAST(ROUND(average_cost * 100) AS BIGINT);
UPDATE reconciliations SET cash_diff_cents = CAST(ROUND(cash_diff * 100) AS BIGINT);

UPDATE mock_broker_orders
SET limit_price_cents = CASE
        WHEN limit_price IS NULL THEN NULL
        ELSE CAST(ROUND(limit_price * 100) AS BIGINT)
    END,
    average_fill_price_cents = CASE
        WHEN average_fill_price IS NULL THEN NULL
        ELSE CAST(ROUND(average_fill_price * 100) AS BIGINT)
    END;
UPDATE mock_broker_trades SET price_cents = CAST(ROUND(price * 100) AS BIGINT);
UPDATE mock_broker_accounts
SET cash_cents = CAST(ROUND(cash * 100) AS BIGINT),
    initial_cash_cents = CAST(ROUND(initial_cash * 100) AS BIGINT);
UPDATE mock_broker_positions SET average_cost_cents = CAST(ROUND(average_cost * 100) AS BIGINT);
"""

MIGRATIONS = (
    (1, SCHEMA),
    (2, TASK_SCHEMA),
    (3, FACTOR_CACHE_AND_MONEY_SCHEMA),
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _postgres_script(script: str) -> str:
    without_pragmas = "\n".join(
        line for line in script.splitlines() if not line.strip().upper().startswith("PRAGMA ")
    )
    return re.sub(r"\bREAL\b", "DOUBLE PRECISION", without_pragmas)


def _qmark_to_postgres(sql: str) -> str:
    """Convert DB-API qmark placeholders without touching quoted SQL text."""
    result = []
    quote: Optional[str] = None
    index = 0
    while index < len(sql):
        character = sql[index]
        if quote:
            result.append(character)
            if character == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    result.append(sql[index + 1])
                    index += 1
                else:
                    quote = None
        elif character in ("'", '"'):
            quote = character
            result.append(character)
        elif character == "?":
            result.append("%s")
        else:
            result.append(character)
        index += 1
    return "".join(result)


class PostgresConnection:
    """Small compatibility wrapper preserving the store layer's qmark SQL."""

    def __init__(self, connection):
        self._connection = connection

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            if exc_type is None:
                self._connection.commit()
            else:
                self._connection.rollback()
        finally:
            self._connection.close()

    def execute(self, sql: str, params: Optional[Sequence[Any]] = None):
        converted = _qmark_to_postgres(sql)
        if params is None:
            return self._connection.execute(converted)
        return self._connection.execute(converted, tuple(params))

    def executemany(self, sql: str, params: Iterable[Sequence[Any]]):
        cursor = self._connection.cursor()
        cursor.executemany(
            _qmark_to_postgres(sql),
            [tuple(item) for item in params],
        )
        return cursor

    def executescript(self, script: str) -> None:
        for statement in _postgres_script(script).split(";"):
            if statement.strip():
                self._connection.execute(statement)

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()


class DatabaseBackend(ABC):
    """数据库后端抽象：统一连接、事务、迁移和健康检查。"""

    @abstractmethod
    def connect(self):  # pragma: no cover - 由具体后端实现
        raise NotImplementedError

    @abstractmethod
    def transaction(self, immediate: bool = False):  # pragma: no cover
        raise NotImplementedError

    @abstractmethod
    def migrate(self) -> None:  # pragma: no cover
        raise NotImplementedError

    @abstractmethod
    def ping(self) -> bool:  # pragma: no cover
        raise NotImplementedError

    @property
    @abstractmethod
    def display_name(self) -> str:  # pragma: no cover
        raise NotImplementedError


class SqliteDatabase(DatabaseBackend):
    backend = "sqlite"

    def __init__(self, path: Optional[Path] = None):
        self.path = path or settings.database_path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @property
    def display_name(self) -> str:
        return str(self.path)

    def ping(self) -> bool:
        try:
            with self.connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    @contextmanager
    def transaction(
        self, immediate: bool = False
    ) -> Generator[sqlite3.Connection, None, None]:
        connection = self.connect()
        try:
            if immediate:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def migrate(self) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            applied = {
                int(row["version"])
                for row in connection.execute(
                    "SELECT version FROM schema_migrations"
                ).fetchall()
            }
            for version, script in MIGRATIONS:
                if version in applied:
                    continue
                connection.executescript(script)
                connection.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (version, _utc_now()),
                )
            self._add_missing_columns(connection)

    @staticmethod
    def _add_missing_columns(connection: sqlite3.Connection) -> None:
        """对已存在的表幂等补列（IF NOT EXISTS 的 CREATE 不会给旧表加列）。"""
        expected = {
            "paper_positions": {
                "frozen_quantity": "INTEGER NOT NULL DEFAULT 0",
                "frozen_date": "TEXT",
                "reserved_quantity": "INTEGER NOT NULL DEFAULT 0",
            },
            "paper_accounts": {
                "reserved_cash": "REAL NOT NULL DEFAULT 0",
            },
            "paper_orders": {
                "filled_quantity": "INTEGER NOT NULL DEFAULT 0",
                "average_fill_price": "REAL",
                "reserved_cash": "REAL NOT NULL DEFAULT 0",
                "reserved_quantity": "INTEGER NOT NULL DEFAULT 0",
                "broker_order_id": "TEXT",
                "last_error": "TEXT",
                "strategy_run_id": "TEXT",
                "order_intent_id": "TEXT",
                "version": "INTEGER NOT NULL DEFAULT 0",
                "submitted_at": "TEXT",
                "completed_at": "TEXT",
            },
            "order_intents": {
                "live_order_id": "TEXT",
                "broker_order_id": "TEXT",
                "execution_mode": "TEXT NOT NULL DEFAULT 'paper'",
                "approval_status": "TEXT NOT NULL DEFAULT 'NOT_REQUIRED'",
                "request_hash": "TEXT",
            },
            "backtest_runs": {
                "manifest_id": "TEXT",
            },
            "strategy_runs": {
                "manifest_id": "TEXT",
            },
        }
        for table, columns in expected.items():
            existing = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info({})".format(table)
                ).fetchall()
            }
            for column, ddl in columns.items():
                if column not in existing:
                    connection.execute(
                        "ALTER TABLE {} ADD COLUMN {} {}".format(table, column, ddl)
                    )


class PostgresDatabase(DatabaseBackend):
    backend = "postgresql"

    def __init__(self, url: Optional[str] = None):
        url = url or settings.database_url
        if not url:
            raise ValueError(
                "PostgreSQL 后端需要配置 QUANTDEV_DATABASE_URL"
            )
        self.url = url

    @property
    def display_name(self) -> str:
        return "postgresql"

    @staticmethod
    def _driver():
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - packaging guard
            raise RuntimeError(
                "缺少 PostgreSQL 驱动，请安装项目依赖 psycopg[binary]"
            ) from exc
        return psycopg, dict_row

    def connect(self) -> PostgresConnection:
        psycopg, dict_row = self._driver()
        connection = psycopg.connect(
            self.url,
            row_factory=dict_row,
            connect_timeout=10,
        )
        return PostgresConnection(connection)

    @contextmanager
    def transaction(self, immediate: bool = False):
        connection = self.connect()
        try:
            if immediate:
                connection.execute("SELECT pg_advisory_xact_lock(817412)")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def ping(self) -> bool:
        try:
            with self.connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except Exception:
            return False

    def migrate(self) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            applied = {
                int(row["version"])
                for row in connection.execute(
                    "SELECT version FROM schema_migrations"
                ).fetchall()
            }
            for version, script in MIGRATIONS:
                if version in applied:
                    continue
                connection.executescript(script)
                connection.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (version, _utc_now()),
                )


# 向后兼容别名：历史代码以 Database 引用 SQLite 实现。
Database = SqliteDatabase


def create_database() -> DatabaseBackend:
    """按配置选择数据库后端。"""
    backend = settings.database_backend
    if backend in ("sqlite", ""):
        return SqliteDatabase()
    if backend in ("postgres", "postgresql"):
        return PostgresDatabase()
    raise ValueError("未知的数据库后端：{}".format(backend))


database = create_database()
