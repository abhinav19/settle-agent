"""holiday-calendar: market business days. Pure and deterministic over a Dataset."""

from datetime import date

from pydantic import BaseModel

from _shared.errors import ErrorCode, ToolFailure
from _shared.generators import is_business_day, next_business_day
from _shared.models import Calendar, Dataset
from _shared.validation import MARKET_RE, require, require_date


class BusinessDay(BaseModel):
    market: str
    date: date
    is_business_day: bool
    reason: str | None  # WEEKEND | HOLIDAY | None
    holiday_name: str | None


class NextBusinessDay(BaseModel):
    market: str
    after: date
    next_business_day: date


class HolidayCalendar:
    def __init__(self, dataset: Dataset) -> None:
        self._cal: dict[str, Calendar] = {c.market: c for c in dataset.calendars}

    def _calendar(self, market: str) -> Calendar:
        require(MARKET_RE, market, "market")
        cal = self._cal.get(market)
        if cal is None:
            raise ToolFailure(ErrorCode.NOT_FOUND, f"unknown market {market}")
        return cal

    def is_business_day(self, market: str, day: str) -> BusinessDay:
        cal = self._calendar(market)
        d = require_date(day, "date")
        name = cal.holidays.get(d.isoformat())
        reason = "WEEKEND" if d.weekday() >= 5 else ("HOLIDAY" if name else None)
        return BusinessDay(
            market=market,
            date=d,
            is_business_day=is_business_day(cal, d),
            reason=reason,
            holiday_name=name,
        )

    def next_business_day(self, market: str, day: str) -> NextBusinessDay:
        cal = self._calendar(market)
        d = require_date(day, "date")
        return NextBusinessDay(market=market, after=d, next_business_day=next_business_day(cal, d))
