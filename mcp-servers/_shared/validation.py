"""Input validation shared by the services (raises typed INVALID_INPUT)."""

import re
from datetime import date

from _shared.errors import ErrorCode, ToolFailure

ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
BIC_RE = re.compile(r"^[A-Z]{6}[A-Z0-9]{2}([A-Z0-9]{3})?$")
CCY_RE = re.compile(r"^[A-Z]{3}$")
MARKET_RE = re.compile(r"^[A-Z]{4}$")


def require(pattern: re.Pattern[str], value: str, name: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ToolFailure(ErrorCode.INVALID_INPUT, f"malformed {name}: {value!r}")
    return value


def require_id(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 64:
        raise ToolFailure(ErrorCode.INVALID_INPUT, f"malformed {name}: {value!r}")
    return value


def require_date(value: str, name: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ToolFailure(
            ErrorCode.INVALID_INPUT, f"{name} must be an ISO date (YYYY-MM-DD): {value!r}"
        ) from exc
