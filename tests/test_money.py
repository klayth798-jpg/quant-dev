from quantdev.money import (
    cents_to_yuan,
    money_equal,
    quantize_yuan,
    round_money,
    to_cents,
)
from quantdev.services.execution import paper_execution_service


def test_quantize_eliminates_float_drift():
    # 0.1 + 0.2 的经典浮点误差被量化到分后消除。
    assert round_money(0.1 + 0.2) == 0.3
    assert to_cents(0.1 + 0.2) == 30
    assert money_equal(0.1 + 0.2, 0.3)
    assert str(quantize_yuan(0.1 + 0.2)) == "0.30"


def test_cents_roundtrip():
    for cents in (0, 1, 99, 100, 123456, 100000000):
        assert to_cents(cents_to_yuan(cents)) == cents


def test_half_up_rounding():
    # 四舍五入到分（ROUND_HALF_UP，而非 Python round 的银行家舍入）。
    assert to_cents(0.125) == 13  # 0.125 -> 0.13
    assert to_cents(2.675) == 268  # 经典浮点 round(2.675,2)=2.67 的反例


def test_paper_account_cash_has_no_subcent_residue():
    """多笔成交后账户现金始终是精确的“分”，不积累浮点尾差。"""
    account = paper_execution_service.get_account()
    symbol = "600519.SH"
    for index in range(6):
        side = "buy" if index % 2 == 0 else "sell"
        try:
            paper_execution_service.submit(
                _order(f"money-drift-{index:04d}", symbol, side)
            )
        except ValueError:
            # 资金/持仓不足等被拒属正常，跳过即可，重点是账户金额精度。
            pass
    account = paper_execution_service.get_account()
    cash = account["cash"]
    # 现金等于其量化到分的值（无 0.30000000000000004 之类尾差）。
    assert round_money(cash) == cash
    assert to_cents(cash) == round(cash * 100)


def _order(client_id, symbol, side):
    from quantdev.models import PaperOrderRequest

    return PaperOrderRequest(
        client_order_id=client_id,
        symbol=symbol,
        side=side,
        quantity=100,
        order_type="market",
    )
