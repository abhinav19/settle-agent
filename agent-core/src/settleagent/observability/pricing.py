"""Token -> USD. Prices are USD per million tokens (first-party Claude API list prices).

Unknown models are priced at the most expensive known rate so a budget is never silently
under-counted; callers can tell via ``is_known_model``.
"""

from decimal import ROUND_HALF_UP, Decimal
from typing import Final

_M: Final = Decimal(1_000_000)

PRICES_PER_MTOK: Final[dict[str, tuple[Decimal, Decimal]]] = {
    # model id: (input, output)
    "claude-fable-5-1": (Decimal(10), Decimal(50)),
    "claude-fable-5": (Decimal(10), Decimal(50)),
    "claude-mythos-5-1": (Decimal(10), Decimal(50)),
    "claude-opus-5-5": (Decimal(4), Decimal(20)),
    "claude-opus-5": (Decimal(5), Decimal(25)),
    "claude-opus-4-8": (Decimal(5), Decimal(25)),
    "claude-opus-4-7": (Decimal(5), Decimal(25)),
    "claude-opus-4-6": (Decimal(5), Decimal(25)),
    "claude-sonnet-5-5": (Decimal(2), Decimal(10)),
    "claude-sonnet-5": (Decimal(2), Decimal(10)),
    "claude-sonnet-4-6": (Decimal(3), Decimal(15)),
    "claude-haiku-4-5": (Decimal(1), Decimal(5)),
}
CONSERVATIVE: Final = (Decimal(10), Decimal(50))


def _lookup(model: str) -> tuple[Decimal, Decimal] | None:
    if model in PRICES_PER_MTOK:
        return PRICES_PER_MTOK[model]
    # dated snapshots, e.g. claude-haiku-4-5-20251001 -> claude-haiku-4-5 (longest prefix wins)
    for known in sorted(PRICES_PER_MTOK, key=len, reverse=True):
        if model.startswith(known + "-"):
            return PRICES_PER_MTOK[known]
    return None


def is_known_model(model: str) -> bool:
    return _lookup(model) is not None


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """USD cost of one call/run, rounded to micro-dollars."""
    if input_tokens < 0 or output_tokens < 0:
        raise ValueError("token counts cannot be negative")
    in_price, out_price = _lookup(model) or CONSERVATIVE
    total = (Decimal(input_tokens) * in_price + Decimal(output_tokens) * out_price) / _M
    return float(total.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP))
