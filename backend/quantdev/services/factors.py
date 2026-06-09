import math
from typing import Any, Dict, Iterable, List, Optional, Tuple

from quantdev.analytics import mean, sample_std, spearman
from quantdev.store import store

# 合成因子配置：factor_id -> [(基础因子, 权重), ...]
# 合成时每个基础因子先做截面去极值+标准化，再加权求和，
# 使不同量纲的因子可比、可叠加。
COMPOSITE_FACTORS: Dict[str, List[Tuple[str, float]]] = {
    "composite_multi": [
        ("low_volatility_20", 0.6),
        ("reversal_5", 0.4),
    ],
}


class FactorService:
    minimum_observations = 21

    @staticmethod
    def _winsorize_zscore(
        values: Dict[str, float], limit: float = 3.0
    ) -> Dict[str, float]:
        """对一个截面（同一交易日全部标的）的因子值做去极值+标准化。

        - 去极值：按中位数 ± limit×MAD 截断极端值（对离群更稳健）。
        - 标准化：减均值除以样本标准差，得到可跨因子相加的 z-score。
        """
        if len(values) < 2:
            return {symbol: 0.0 for symbol in values}
        symbols = list(values)
        raw = [values[symbol] for symbol in symbols]
        ordered = sorted(raw)
        mid = len(ordered) // 2
        median = (
            ordered[mid]
            if len(ordered) % 2
            else (ordered[mid - 1] + ordered[mid]) / 2
        )
        abs_dev = sorted(abs(value - median) for value in raw)
        mad = (
            abs_dev[mid]
            if len(abs_dev) % 2
            else (abs_dev[mid - 1] + abs_dev[mid]) / 2
        )
        if mad > 0:
            lower = median - limit * 1.4826 * mad
            upper = median + limit * 1.4826 * mad
            raw = [min(max(value, lower), upper) for value in raw]
        center = mean(raw)
        scale = sample_std(raw)
        if scale == 0:
            return {symbol: 0.0 for symbol in symbols}
        return {
            symbol: (value - center) / scale
            for symbol, value in zip(symbols, raw)
        }

    @staticmethod
    def group_price_rows(
        rows: Iterable[Dict[str, Any]],
    ) -> Dict[str, List[Dict[str, Any]]]:
        result: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            result.setdefault(row["symbol"], []).append(row)
        return result

    def _series_by_symbol(
        self, snapshot_id: str
    ) -> Dict[str, List[Dict[str, Any]]]:
        return self.group_price_rows(store.list_prices(snapshot_id=snapshot_id))

    @staticmethod
    def _adjusted_close(row: Dict[str, Any]) -> float:
        return float(row["close"]) * float(row.get("adj_factor") or 1.0)

    @staticmethod
    def _neutralize_cross_section(
        values: Dict[str, float],
        industry: Dict[str, str],
        log_cap: Dict[str, float],
    ) -> Dict[str, float]:
        """对单个交易日截面做行业+市值中性化。

        - 行业中性化：减去各行业组内均值，消除行业整体暴露。
        - 市值中性化：对 log(总市值) 做一元线性回归取残差，剥离规模因子暴露。
        无市值数据的标的仅做行业去均值。
        """
        if len(values) < 2:
            return dict(values)
        # 行业去均值。
        groups: Dict[str, List[str]] = {}
        for symbol in values:
            groups.setdefault(industry.get(symbol, "未分类"), []).append(symbol)
        demeaned: Dict[str, float] = {}
        for symbols in groups.values():
            group_mean = mean([values[symbol] for symbol in symbols])
            for symbol in symbols:
                demeaned[symbol] = values[symbol] - group_mean
        # 对 log 市值回归取残差。
        paired = [
            (log_cap[symbol], demeaned[symbol])
            for symbol in demeaned
            if symbol in log_cap and math.isfinite(log_cap[symbol])
        ]
        if len(paired) < 2:
            return demeaned
        xs = [item[0] for item in paired]
        ys = [item[1] for item in paired]
        x_mean = mean(xs)
        y_mean = mean(ys)
        denom = sum((x - x_mean) ** 2 for x in xs)
        if denom <= 0:
            return demeaned
        beta = sum((x - x_mean) * (y - y_mean) for x, y in paired) / denom
        alpha = y_mean - beta * x_mean
        result = dict(demeaned)
        for symbol in demeaned:
            cap = log_cap.get(symbol)
            if cap is not None and math.isfinite(cap):
                result[symbol] = demeaned[symbol] - (alpha + beta * cap)
        return result

    def _neutralize_panel(
        self, panel: Dict[str, Dict[str, float]]
    ) -> Dict[str, Dict[str, float]]:
        """对整张因子面板逐交易日做行业+市值中性化。"""
        industry = store.industry_map()
        cap_panel = store.market_cap_panel()
        log_cap_panel: Dict[str, Dict[str, float]] = {}
        for trade_date, caps in cap_panel.items():
            log_cap_panel[trade_date] = {
                symbol: math.log(cap) for symbol, cap in caps.items() if cap > 0
            }
        neutralized: Dict[str, Dict[str, float]] = {}
        for trade_date, cross_section in panel.items():
            log_cap = log_cap_panel.get(trade_date, {})
            neutralized[trade_date] = self._neutralize_cross_section(
                cross_section, industry, log_cap
            )
        return neutralized

    def _factor_value(
        self, factor_id: str, rows: List[Dict[str, Any]], index: int
    ) -> Optional[float]:
        if factor_id == "momentum_20":
            if index < 20:
                return None
            return (
                self._adjusted_close(rows[index])
                / self._adjusted_close(rows[index - 20])
                - 1
            )
        if factor_id == "reversal_5":
            if index < 5:
                return None
            return -(
                self._adjusted_close(rows[index])
                / self._adjusted_close(rows[index - 5])
                - 1
            )
        if factor_id == "low_volatility_20":
            if index < 20:
                return None
            daily_returns = [
                self._adjusted_close(rows[cursor])
                / self._adjusted_close(rows[cursor - 1])
                - 1
                for cursor in range(index - 19, index + 1)
            ]
            return -sample_std(daily_returns)
        if factor_id == "volume_ratio_20":
            if index < 19:
                return None
            short_volume = mean([row["volume"] for row in rows[index - 4 : index + 1]])
            long_volume = mean([row["volume"] for row in rows[index - 19 : index + 1]])
            return short_volume / long_volume - 1 if long_volume else None
        raise ValueError("未知因子: {}".format(factor_id))

    def build_panel(
        self,
        factor_id: str,
        snapshot_id: str,
        neutralize: bool = False,
        series: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    ) -> Dict[str, Dict[str, float]]:
        price_series = series or self._series_by_symbol(snapshot_id)
        if factor_id in COMPOSITE_FACTORS:
            panel = self._build_composite_panel(factor_id, price_series)
        else:
            if not store.get_factor(factor_id):
                raise ValueError("因子不存在: {}".format(factor_id))
            panel = {}
            for symbol, rows in price_series.items():
                for index, row in enumerate(rows):
                    value = self._factor_value(factor_id, rows, index)
                    if value is not None and math.isfinite(value):
                        panel.setdefault(row["trade_date"], {})[symbol] = value
        if neutralize:
            panel = self._neutralize_panel(panel)
        return panel

    def _build_composite_panel(
        self,
        factor_id: str,
        series: Dict[str, List[Dict[str, Any]]],
    ) -> Dict[str, Dict[str, float]]:
        """合成因子面板：各基础因子按交易日截面去极值+标准化后加权求和。"""
        components = COMPOSITE_FACTORS[factor_id]
        # 先算出每个基础因子的原始面板。
        base_panels: Dict[str, Dict[str, Dict[str, float]]] = {}
        for base_id, _weight in components:
            base_panel: Dict[str, Dict[str, float]] = {}
            for symbol, rows in series.items():
                for index, row in enumerate(rows):
                    value = self._factor_value(base_id, rows, index)
                    if value is not None and math.isfinite(value):
                        base_panel.setdefault(row["trade_date"], {})[symbol] = value
            base_panels[base_id] = base_panel
        # 逐交易日：每个基础因子截面标准化后加权累加。
        panel: Dict[str, Dict[str, float]] = {}
        all_dates = set()
        for base_panel in base_panels.values():
            all_dates.update(base_panel)
        for trade_date in all_dates:
            component_sections = [
                base_panels[base_id].get(trade_date, {})
                for base_id, _weight in components
            ]
            if not component_sections or any(not section for section in component_sections):
                continue
            common_symbols = set(component_sections[0])
            for section in component_sections[1:]:
                common_symbols.intersection_update(section)
            if not common_symbols:
                continue
            scores: Dict[str, float] = {}
            for base_id, weight in components:
                cross_section = {
                    symbol: base_panels[base_id][trade_date][symbol]
                    for symbol in common_symbols
                }
                standardized = self._winsorize_zscore(cross_section)
                for symbol, zscore in standardized.items():
                    scores[symbol] = scores.get(symbol, 0.0) + weight * zscore
            if scores:
                panel[trade_date] = scores
        return panel

    def evaluate(
        self, factor_id: str, forward_days: int = 5, neutralize: bool = False
    ) -> Dict[str, Any]:
        snapshot_id = store.latest_snapshot_id()
        if not snapshot_id:
            raise ValueError("没有可用的数据快照")
        series = self._series_by_symbol(snapshot_id)
        panel = self.build_panel(
            factor_id,
            snapshot_id,
            neutralize=neutralize,
            series=series,
        )
        forward_returns: Dict[Tuple[str, str], float] = {}
        for symbol, rows in series.items():
            for index, row in enumerate(rows[:-forward_days]):
                forward_returns[(row["trade_date"], symbol)] = (
                    self._adjusted_close(rows[index + forward_days])
                    / self._adjusted_close(row)
                    - 1
                )

        daily_ic: List[float] = []
        long_short_returns: List[float] = []
        observations = 0
        for trade_date in sorted(panel):
            pairs = [
                (value, forward_returns[(trade_date, symbol)])
                for symbol, value in panel[trade_date].items()
                if (trade_date, symbol) in forward_returns
            ]
            if len(pairs) < 4:
                continue
            factors = [pair[0] for pair in pairs]
            returns = [pair[1] for pair in pairs]
            daily_ic.append(spearman(factors, returns))
            ordered = sorted(pairs, key=lambda pair: pair[0])
            bucket_size = max(1, len(ordered) // 3)
            bottom = mean([pair[1] for pair in ordered[:bucket_size]])
            top = mean([pair[1] for pair in ordered[-bucket_size:]])
            long_short_returns.append(top - bottom)
            observations += len(pairs)

        ic_mean = mean(daily_ic)
        ic_std = sample_std(daily_ic)
        periodicity = 252 / forward_days
        annualized_long_short = mean(long_short_returns) * periodicity
        metrics = {
            "ic_mean": round(ic_mean, 6),
            "ic_std": round(ic_std, 6),
            "ic_ir": round(ic_mean / ic_std * math.sqrt(periodicity), 4) if ic_std else 0.0,
            "positive_ic_ratio": round(
                sum(1 for value in daily_ic if value > 0) / len(daily_ic), 4
            )
            if daily_ic
            else 0.0,
            "annualized_long_short_return": round(annualized_long_short, 6),
            "observations": observations,
            "trading_days": len(daily_ic),
            "forward_days": forward_days,
        }
        return store.save_factor_run(
            factor_id=factor_id,
            snapshot_id=snapshot_id,
            parameters={"forward_days": forward_days, "neutralize": neutralize},
            metrics=metrics,
        )


factor_service = FactorService()
