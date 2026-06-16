from datetime import date, datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class FactorEvaluateRequest(BaseModel):
    forward_days: int = Field(default=5, ge=1, le=60)
    neutralize: bool = False


class BacktestRequest(BaseModel):
    name: str = Field(default="多因子选股回测", min_length=1, max_length=80)
    factor_id: str = "momentum_20"
    initial_capital: float = Field(default=1_000_000, gt=10_000)
    top_n: int = Field(default=3, ge=1, le=20)
    rebalance_days: int = Field(default=5, ge=1, le=60)
    commission_rate: float = Field(default=0.0003, ge=0, le=0.01)
    stamp_duty_rate: float = Field(default=0.0005, ge=0, le=0.01)
    slippage_bps: float = Field(default=5, ge=0, le=100)
    universe_indices: Optional[List[str]] = None
    buffer_multiple: float = Field(default=1.0, ge=1.0, le=3.0)
    exclude_st: bool = False
    apply_price_limit: bool = False
    neutralize: bool = False


class PositionInput(BaseModel):
    symbol: str
    weight: float = Field(ge=0, le=1)


class RiskCheckRequest(BaseModel):
    positions: List[PositionInput]
    proposed_turnover: float = Field(default=0, ge=0)
    current_drawdown: float = Field(default=0, ge=0)
    order_notional_weight: float = Field(default=0, ge=0)


class AgentResearchRequest(BaseModel):
    question: str = Field(min_length=2, max_length=1000)
    factor_id: Optional[str] = None
    backtest_run_id: Optional[str] = None


class PaperOrderRequest(BaseModel):
    client_order_id: str = Field(min_length=8, max_length=80)
    symbol: str = Field(min_length=6, max_length=20)
    side: str
    quantity: int = Field(gt=0)
    order_type: str = "market"
    limit_price: Optional[float] = Field(default=None, gt=0)
    strategy_run_id: Optional[str] = Field(default=None, max_length=80)
    order_intent_id: Optional[str] = Field(default=None, max_length=80)


class TushareSyncRequest(BaseModel):
    start_date: date
    end_date: date
    sync_daily: bool = True
    sync_financials: bool = True
    sync_indices: bool = True
    indices: Optional[List[str]] = None
    financial_symbols: Optional[List[str]] = None
    max_standard_financial_symbols: int = Field(default=100, ge=1, le=1000)


class ApiMessage(BaseModel):
    message: str
    data: Dict[str, Any] = {}


class KillSwitchRequest(BaseModel):
    active: bool
    reason: str = Field(default="", max_length=200)


class QuoteUpdateRequest(BaseModel):
    symbol: str = Field(min_length=6, max_length=20)
    price: float = Field(gt=0)
    bid: Optional[float] = Field(default=None, gt=0)
    ask: Optional[float] = Field(default=None, gt=0)
    quote_time: datetime
    source: str = Field(default="paper-feed", min_length=2, max_length=40)
    status: str = Field(default="tradable", min_length=2, max_length=20)


class StrategyConfigRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    factor_id: str = Field(default="momentum_20", min_length=2, max_length=80)
    top_n: int = Field(default=5, ge=1, le=20)
    rebalance_days: int = Field(default=5, ge=1, le=60)
    universe_indices: Optional[List[str]] = None
    neutralize: bool = False
    order_type: str = "market"
    max_order_notional: float = Field(default=5000, gt=0)
    enabled: bool = False


class StrategyRunRequest(BaseModel):
    strategy_id: str = Field(min_length=4, max_length=80)
    force: bool = False


class StrategyEnableRequest(BaseModel):
    enabled: bool


class IntentApprovalRequest(BaseModel):
    reason: str = Field(default="", max_length=200)


class IntentRejectRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=200)
