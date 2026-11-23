from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP

BYTES_PER_GB = Decimal(1024) ** 3
Q = Decimal("0.000001")


def D(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def qgb(value) -> Decimal:
    return D(value).quantize(Q, rounding=ROUND_HALF_UP)


def dec_str(value) -> str:
    text = format(qgb(value), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def floor_money(qty, price: int) -> int:
    return int((D(qty) * Decimal(int(price))).to_integral_value(rounding=ROUND_FLOOR))


def gb_to_bytes(gb) -> int:
    return int((D(gb) * BYTES_PER_GB).to_integral_value(rounding=ROUND_HALF_UP))


def bytes_to_gb(num: int) -> Decimal:
    return qgb(Decimal(int(num)) / BYTES_PER_GB)


@dataclass(frozen=True)
class Charge:
    need_gb: Decimal
    need_days: int
    gb_from_credit: Decimal
    days_from_credit: int
    gb_paid: Decimal
    days_paid: int
    toman: int


def quote_charge(
    need_gb,
    need_days: int,
    credit_gb,
    credit_days: int,
    price_per_gb: int,
    price_per_day: int,
) -> Charge:
    needed_gb = qgb(max(D(0), D(need_gb)))
    needed_days = max(0, int(need_days))
    have_gb = qgb(max(D(0), D(credit_gb)))
    have_days = max(0, int(credit_days))
    gb_from = qgb(min(have_gb, needed_gb))
    days_from = min(have_days, needed_days)
    gb_paid = qgb(needed_gb - gb_from)
    days_paid = needed_days - days_from
    toman = floor_money(gb_paid, price_per_gb) + floor_money(Decimal(days_paid), price_per_day)
    return Charge(
        need_gb=needed_gb,
        need_days=needed_days,
        gb_from_credit=gb_from,
        days_from_credit=days_from,
        gb_paid=gb_paid,
        days_paid=days_paid,
        toman=toman,
    )


def refund_decrease(old_cap, new_cap, old_days_left: int, new_days_left: int) -> tuple[Decimal, int]:
    gb = qgb(max(D(0), D(old_cap) - D(new_cap)))
    days = max(0, int(old_days_left) - int(new_days_left))
    return gb, days


def refund_delete(cap, used_gb, days_left_value: int) -> tuple[Decimal, int]:
    gb = qgb(max(D(0), D(cap) - D(used_gb)))
    return gb, max(0, int(days_left_value))
