import math
from typing import Dict, Iterable, List, Sequence, Tuple


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def sample_std(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    center = mean(values)
    return math.sqrt(sum((value - center) ** 2 for value in values) / (len(values) - 1))


def annualized_return(equity: Sequence[float], periods_per_year: int = 252) -> float:
    if len(equity) < 2 or equity[0] <= 0 or equity[-1] <= 0:
        return 0.0
    years = (len(equity) - 1) / periods_per_year
    return (equity[-1] / equity[0]) ** (1 / years) - 1 if years > 0 else 0.0


def returns_from_equity(equity: Sequence[float]) -> List[float]:
    result = []
    for previous, current in zip(equity, equity[1:]):
        result.append(current / previous - 1 if previous else 0.0)
    return result


def max_drawdown(equity: Sequence[float]) -> float:
    peak = 0.0
    worst = 0.0
    for value in equity:
        peak = max(peak, value)
        if peak:
            worst = min(worst, value / peak - 1)
    return worst


def sharpe_ratio(daily_returns: Sequence[float], risk_free_rate: float = 0.02) -> float:
    volatility = sample_std(daily_returns)
    if volatility == 0:
        return 0.0
    excess_daily = mean(daily_returns) - risk_free_rate / 252
    return excess_daily / volatility * math.sqrt(252)


def ranks(values: Sequence[float]) -> List[float]:
    ordered = sorted(enumerate(values), key=lambda item: item[1])
    result = [0.0] * len(values)
    cursor = 0
    while cursor < len(ordered):
        end = cursor
        while end + 1 < len(ordered) and ordered[end + 1][1] == ordered[cursor][1]:
            end += 1
        average_rank = (cursor + end) / 2 + 1
        for index in range(cursor, end + 1):
            result[ordered[index][0]] = average_rank
        cursor = end + 1
    return result


def pearson(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or len(left) < 2:
        return 0.0
    left_mean = mean(left)
    right_mean = mean(right)
    numerator = sum(
        (left_value - left_mean) * (right_value - right_mean)
        for left_value, right_value in zip(left, right)
    )
    left_scale = math.sqrt(sum((value - left_mean) ** 2 for value in left))
    right_scale = math.sqrt(sum((value - right_mean) ** 2 for value in right))
    denominator = left_scale * right_scale
    return numerator / denominator if denominator else 0.0


def spearman(left: Sequence[float], right: Sequence[float]) -> float:
    return pearson(ranks(left), ranks(right))


def summarize_equity(equity: Sequence[float]) -> Dict[str, float]:
    daily_returns = returns_from_equity(equity)
    total_return = equity[-1] / equity[0] - 1 if len(equity) > 1 and equity[0] else 0.0
    annual_return = annualized_return(equity)
    drawdown = max_drawdown(equity)
    volatility = sample_std(daily_returns) * math.sqrt(252)
    return {
        "total_return": round(total_return, 6),
        "annualized_return": round(annual_return, 6),
        "annualized_volatility": round(volatility, 6),
        "sharpe_ratio": round(sharpe_ratio(daily_returns), 4),
        "max_drawdown": round(drawdown, 6),
        "calmar_ratio": round(annual_return / abs(drawdown), 4) if drawdown else 0.0,
        "positive_day_ratio": round(
            sum(1 for value in daily_returns if value > 0) / len(daily_returns), 4
        )
        if daily_returns
        else 0.0,
    }


def group_by_date(
    rows: Iterable[Dict[str, object]],
) -> Tuple[List[str], Dict[str, List[Dict[str, object]]]]:
    grouped: Dict[str, List[Dict[str, object]]] = {}
    for row in rows:
        grouped.setdefault(str(row["trade_date"]), []).append(row)
    dates = sorted(grouped)
    return dates, grouped

