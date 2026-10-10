"""Shared monetary scaling using exact Decimal values."""

from __future__ import annotations

from typing import Iterable, TypeVar
from researchx.contracts.models import Amount
from decimal import Decimal


TreeT = TypeVar("TreeT", dict[str, object], list[dict[str, object]])


UNIT_SCALE = {
    "元": Decimal(1),
    "千元": Decimal(1000),
    "万元": Decimal(10000),
    "百万元": Decimal(1000000),
    "亿元": Decimal(100000000),
    "十亿元": Decimal(1000000000),
}


def amount_value(amount: Amount | None) -> Decimal | None:
    return (
        None
        if amount is None or amount.value is None
        else amount.value * UNIT_SCALE[amount.unit]
        if amount.unit is not None
        else None
    )


def restore_decimal_fields(value: TreeT, paths: Iterable[str]) -> TreeT:
    """Restore explicitly declared numeric paths after a JSON round trip.

    Plugins supply their own paths. Other strings, including source text, stay literal.
    """

    def restore(node: object, parts: list[str]) -> None:
        key, *remaining = parts
        if isinstance(node, list):
            fields: list[object] = list(range(len(node)))
        elif isinstance(node, dict):
            fields = list(node) if key == "*" else [key]
        else:
            raise ValueError("Decimal path must traverse a JSON object or array")
        for field in fields:
            if isinstance(node, dict):
                if field not in node:
                    continue
                item = node[field]
            else:
                if not isinstance(field, int):
                    raise ValueError("Array index must be an integer")
                item = node[field]
            if remaining:
                if item is not None:
                    restore(item, remaining)
            elif item is not None:
                number = Decimal(str(item))
                if not number.is_finite():
                    raise ValueError("计算数值须为有限值")
                if isinstance(node, list):
                    if not isinstance(field, int):
                        raise ValueError("Array index must be an integer")
                    node[field] = number
                else:
                    node[field] = number

    for path in paths:
        restore(value, path.split("."))
    return value
