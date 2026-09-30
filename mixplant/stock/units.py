"""Gram arithmetic shared by every stock record.

The journal may store rounded kilogram readings coming back from the scale,
but the ledger itself only ever moves integer grams so the books can never
gain or lose mass through binary float rounding.
"""

from __future__ import annotations

from ..errors import ValidationError

GRAMS_PER_KG = 1000


def to_grams(kg: float, *, field: str = "kg") -> int:
    """Convert a kilogram value into whole grams."""

    if not isinstance(kg, (int, float)):
        raise ValidationError("stock value must be numeric", field=field, value=kg)
    grams = int(round(float(kg) * GRAMS_PER_KG))
    return grams


def to_kg(grams: int) -> float:
    """Turn whole grams back into kilograms, rounded to the gram."""

    return round(grams / GRAMS_PER_KG, 3)
