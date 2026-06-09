import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

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
    initial_cash REAL NOT NULL,
    status TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_positions (
    account_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    average_cost REAL NOT NULL,
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
    reject_reason TEXT,
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
"""


class Database:
    def __init__(self, path: Path = settings.database_path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection, None, None]:
        connection = self.connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def migrate(self) -> None:
        with self.transaction() as connection:
            connection.executescript(SCHEMA)


database = Database()
