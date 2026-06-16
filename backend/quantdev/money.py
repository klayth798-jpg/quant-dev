"""金额定点数工具：用 Decimal 做精确到“分”的金额运算，杜绝 IEEE-754 浮点漂移。

设计约定：
  - 对外/存储仍以“元”为单位，但任何金额在写库或比较前都量化到 0.01（分）。
  - 任何涉及费率（佣金 / 印花税 / 滑点）的乘法都先走 Decimal，再量化到分，
    避免浮点误差随成交笔数累积。
  - 对账等比较一律换算成整数“分”做精确比较，消除浮点假差异（false break）。

float 进入 Decimal 前一律经 str()，避免把二进制误差直接带进定点运算。
"""
from decimal import ROUND_HALF_UP, Decimal
from typing import Union

Number = Union[int, float, str, Decimal]

_CENT = Decimal("0.01")


def to_decimal(value: Number) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def quantize_yuan(value: Number) -> Decimal:
    """量化到“分”（四舍五入到 0.01），返回 Decimal。"""
    return to_decimal(value).quantize(_CENT, rounding=ROUND_HALF_UP)


def round_money(value: Number) -> float:
    """量化到“分”并返回 float（元），用于写库 / 接口返回。"""
    return float(quantize_yuan(value))


def to_cents(value: Number) -> int:
    """换算成整数“分”，用于精确比较与对账。"""
    return int(quantize_yuan(value) * 100)


def cents_to_yuan(cents: int) -> float:
    return float((Decimal(int(cents)) / 100).quantize(_CENT))


def money_equal(left: Number, right: Number) -> bool:
    """在“分”精度上比较两个金额是否相等（无浮点假差异）。"""
    return to_cents(left) == to_cents(right)
