import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")


def _resolve_path(raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def _parse_operator_keys(raw: str) -> dict:
    """解析 QUANTDEV_OPERATOR_KEYS="alice:keyA,bob:keyB" 为 {key: operator} 反查表。

    把操作员身份绑定到各自的密钥（而非客户端可伪造的 X-Operator 头），使双人复核
    （maker-checker）可信：不同 actor 必然对应不同密钥。配置后即取代单一共享 admin key。
    """
    mapping: dict = {}
    for item in raw.split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        name, key = item.split(":", 1)
        name, key = name.strip(), key.strip()
        if name and key:
            mapping[key] = name
    return mapping


@dataclass(frozen=True)
class Settings:
    environment: str
    data_mode: str
    database_path: Path
    database_backend: str
    database_url: str
    redis_url: str
    task_backend: str
    task_queue_name: str
    task_visibility_timeout_seconds: int
    task_max_attempts: int
    worker_poll_seconds: int
    log_level: str
    deep_research_base_url: str
    deep_research_api_key: str
    market_data_sdk: str
    tushare_token: str
    tushare_financial_vip: bool
    tushare_call_interval_seconds: float
    tushare_default_indices: tuple
    broker_mode: str
    broker_adapter: str
    live_trading_enabled: bool
    require_order_approval: bool
    max_live_order_notional: float
    max_daily_loss: float
    paper_enforce_session: bool
    paper_quote_mode: str
    quote_max_age_seconds: int
    calendar_fail_closed: bool
    auto_kill_on_reconciliation_break: bool
    runner_poll_seconds: int
    runner_lease_seconds: int
    approval_ttl_seconds: int
    reconciliation_max_age_minutes: int
    quote_max_spread_bps: float
    read_api_key: str
    admin_api_key: str
    admin_allow_insecure: bool
    alert_webhook_url: str
    require_dual_approval: bool
    reconciliation_interval_seconds: int
    reconciliation_cash_tolerance_cents: int
    operator_keys: dict
    broker_dry_run: bool

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            environment=os.getenv("QUANTDEV_ENV", "development"),
            data_mode=os.getenv("QUANTDEV_DATA_MODE", "demo").lower(),
            database_path=_resolve_path(
                os.getenv("QUANTDEV_DATABASE_PATH", "./data/quantdev.db")
            ),
            database_backend=os.getenv("QUANTDEV_DATABASE_BACKEND", "sqlite").lower(),
            database_url=os.getenv("QUANTDEV_DATABASE_URL", ""),
            redis_url=os.getenv("QUANTDEV_REDIS_URL", "redis://127.0.0.1:6379/0"),
            task_backend=os.getenv("QUANTDEV_TASK_BACKEND", "inline").lower(),
            task_queue_name=os.getenv(
                "QUANTDEV_TASK_QUEUE_NAME", "quantdev:tasks"
            ),
            task_visibility_timeout_seconds=int(
                os.getenv("QUANTDEV_TASK_VISIBILITY_TIMEOUT_SECONDS", "1800")
            ),
            task_max_attempts=int(os.getenv("QUANTDEV_TASK_MAX_ATTEMPTS", "3")),
            worker_poll_seconds=int(
                os.getenv("QUANTDEV_WORKER_POLL_SECONDS", "5")
            ),
            log_level=os.getenv("QUANTDEV_LOG_LEVEL", "INFO"),
            deep_research_base_url=os.getenv("DEEP_RESEARCH_BASE_URL", "").rstrip("/"),
            deep_research_api_key=os.getenv("DEEP_RESEARCH_API_KEY", ""),
            market_data_sdk=os.getenv("MARKET_DATA_SDK", "tushare").lower(),
            tushare_token=os.getenv("TINYSHARE_TOKEN")
            or os.getenv("TUSHARE_TOKEN", ""),
            tushare_financial_vip=os.getenv(
                "TUSHARE_FINANCIAL_VIP", "false"
            ).lower()
            == "true",
            tushare_call_interval_seconds=float(
                os.getenv("TUSHARE_CALL_INTERVAL_SECONDS", "0.35")
            ),
            tushare_default_indices=tuple(
                item.strip()
                for item in os.getenv(
                    "TUSHARE_DEFAULT_INDICES",
                    "000300.SH,000905.SH,000852.SH",
                ).split(",")
                if item.strip()
            ),
            broker_mode=os.getenv("QUANTDEV_BROKER_MODE", "disabled").lower(),
            broker_adapter=os.getenv("QUANTDEV_BROKER_ADAPTER", "").strip(),
            live_trading_enabled=os.getenv(
                "QUANTDEV_LIVE_TRADING_ENABLED", "false"
            ).lower()
            == "true",
            require_order_approval=os.getenv(
                "QUANTDEV_REQUIRE_ORDER_APPROVAL", "true"
            ).lower()
            == "true",
            max_live_order_notional=float(
                os.getenv("QUANTDEV_MAX_LIVE_ORDER_NOTIONAL", "5000")
            ),
            max_daily_loss=float(os.getenv("QUANTDEV_MAX_DAILY_LOSS", "1000")),
            paper_enforce_session=os.getenv(
                "QUANTDEV_PAPER_ENFORCE_SESSION", "true"
            ).lower()
            == "true",
            paper_quote_mode=os.getenv(
                "QUANTDEV_PAPER_QUOTE_MODE", "realtime"
            ).lower(),
            quote_max_age_seconds=int(
                os.getenv("QUANTDEV_QUOTE_MAX_AGE_SECONDS", "15")
            ),
            calendar_fail_closed=os.getenv(
                "QUANTDEV_CALENDAR_FAIL_CLOSED", "true"
            ).lower()
            == "true",
            auto_kill_on_reconciliation_break=os.getenv(
                "QUANTDEV_AUTO_KILL_ON_RECON_BREAK", "true"
            ).lower()
            == "true",
            runner_poll_seconds=int(
                os.getenv("QUANTDEV_RUNNER_POLL_SECONDS", "5")
            ),
            runner_lease_seconds=int(
                os.getenv("QUANTDEV_RUNNER_LEASE_SECONDS", "60")
            ),
            approval_ttl_seconds=int(
                os.getenv("QUANTDEV_APPROVAL_TTL_SECONDS", "300")
            ),
            reconciliation_max_age_minutes=int(
                os.getenv("QUANTDEV_RECONCILIATION_MAX_AGE_MINUTES", "1440")
            ),
            quote_max_spread_bps=float(
                os.getenv("QUANTDEV_QUOTE_MAX_SPREAD_BPS", "100")
            ),
            read_api_key=os.getenv("QUANTDEV_READ_API_KEY", ""),
            admin_api_key=os.getenv("QUANTDEV_ADMIN_API_KEY", ""),
            admin_allow_insecure=os.getenv(
                "QUANTDEV_ALLOW_INSECURE_ADMIN", "false"
            ).lower()
            == "true",
            alert_webhook_url=os.getenv("QUANTDEV_ALERT_WEBHOOK_URL", "").strip(),
            require_dual_approval=os.getenv(
                "QUANTDEV_REQUIRE_DUAL_APPROVAL", "true"
            ).lower()
            == "true",
            reconciliation_interval_seconds=int(
                os.getenv("QUANTDEV_RECONCILIATION_INTERVAL_SECONDS", "300")
            ),
            reconciliation_cash_tolerance_cents=int(
                os.getenv("QUANTDEV_RECONCILIATION_CASH_TOLERANCE_CENTS", "0")
            ),
            operator_keys=_parse_operator_keys(
                os.getenv("QUANTDEV_OPERATOR_KEYS", "")
            ),
            broker_dry_run=os.getenv("QUANTDEV_BROKER_DRY_RUN", "true").lower()
            == "true",
        )


settings = Settings.from_env()
