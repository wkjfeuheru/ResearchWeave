"""Shared monetary scaling using exact Decimal values."""

from decimal import Decimal

UNIT_SCALE = {
    "元": Decimal(1),
    "千元": Decimal(1000),
    "万元": Decimal(10000),
    "百万元": Decimal(1000000),
    "亿元": Decimal(100000000),
    "十亿元": Decimal(1000000000),
}


def amount_value(amount):
    return (
        None if amount is None or amount.value is None else amount.value * UNIT_SCALE[amount.unit]
    )


def restore_decimal_fields(value, paths):
    """Restore explicitly declared numeric paths after a JSON round trip.

    Plugins supply their own paths. Other strings, including source text, stay literal.
    """

    def restore(node, parts):
        key, *remaining = parts
        keys = range(len(node)) if isinstance(node, list) else node.keys() if key == "*" else [key]
        for field in keys:
            if isinstance(node, dict) and field not in node:
                continue
            item = node[field]
            if remaining:
                if item is not None:
                    restore(item, remaining)
            elif item is not None:
                number = Decimal(str(item))
                if not number.is_finite():
                    raise ValueError("计算数值须为有限值")
                node[field] = number

    for path in paths:
        restore(value, path.split("."))
    return value
