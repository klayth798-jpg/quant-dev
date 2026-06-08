from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict


@dataclass(frozen=True)
class BrokerOrder:
    client_order_id: str
    symbol: str
    side: str
    quantity: int
    order_type: str = "limit"
    limit_price: float = 0.0


class BrokerAdapter(ABC):
    @abstractmethod
    def submit_order(self, order: BrokerOrder) -> Dict[str, str]:
        raise NotImplementedError

    @abstractmethod
    def cancel_order(self, broker_order_id: str) -> Dict[str, str]:
        raise NotImplementedError


class DisabledLiveBroker(BrokerAdapter):
    def submit_order(self, order: BrokerOrder) -> Dict[str, str]:
        raise PermissionError("实盘 Broker 未启用，订单不会被发送")

    def cancel_order(self, broker_order_id: str) -> Dict[str, str]:
        raise PermissionError("实盘 Broker 未启用，订单不会被发送")

