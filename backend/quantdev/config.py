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


@dataclass(frozen=True)
class Settings:
    environment: str
    data_mode: str
    database_path: Path
    log_level: str
    deep_research_base_url: str
    deep_research_api_key: str
    market_data_sdk: str
    tushare_token: str
    tushare_financial_vip: bool
    tushare_call_interval_seconds: float
    tushare_default_indices: tuple

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            environment=os.getenv("QUANTDEV_ENV", "development"),
            data_mode=os.getenv("QUANTDEV_DATA_MODE", "demo").lower(),
            database_path=_resolve_path(
                os.getenv("QUANTDEV_DATABASE_PATH", "./data/quantdev.db")
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
        )


settings = Settings.from_env()
