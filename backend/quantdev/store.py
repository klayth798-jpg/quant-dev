"""数据访问门面：Store 由各子域 repository 组合而成。

历史代码以 from quantdev.store import store/new_id/utc_now/decode_json 访问，
这里保持该调用面不变；具体 SQL 已按子域拆分到 quantdev/repositories/ 下。
"""
from quantdev.repositories.base import decode_json, new_id, utc_now
from quantdev.repositories.calendar import CalendarRepository
from quantdev.repositories.lease import LeaseRepository
from quantdev.repositories.market_data import MarketDataRepository
from quantdev.repositories.order import OrderRepository
from quantdev.repositories.quote import QuoteRepository
from quantdev.repositories.reconciliation import ReconciliationRepository
from quantdev.repositories.research import ResearchRepository
from quantdev.repositories.sync import SyncRepository
from quantdev.repositories.system import SystemRepository
from quantdev.repositories.task import TaskRepository
from quantdev.repositories.verification import VerificationRepository

__all__ = [
    "store",
    "Store",
    "new_id",
    "utc_now",
    "decode_json",
]


class Store(
    MarketDataRepository,
    SyncRepository,
    ResearchRepository,
    CalendarRepository,
    QuoteRepository,
    LeaseRepository,
    SystemRepository,
    OrderRepository,
    ReconciliationRepository,
    VerificationRepository,
    TaskRepository,
):
    """组合各子域 repository 的数据访问门面。"""


store = Store()
