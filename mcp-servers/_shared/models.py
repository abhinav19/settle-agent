"""Reference-data and scenario models shared by the generator and the MCP servers."""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Instrument(_Model):
    isin: str
    name: str
    market: str
    currency: str
    lot_size: int
    status: Literal["ACTIVE", "SUSPENDED", "MATURED"]


class SsiRecord(_Model):
    ssi_id: str
    bic: str
    currency: str
    account: str
    agent_bic: str
    status: Literal["ACTIVE", "SUPERSEDED"]
    effective_from: date


class Trade(_Model):
    trade_id: str
    isin: str
    counterparty_bic: str
    market: str
    side: Literal["BUY", "SELL"]
    trade_date: date
    settlement_date: date
    quantity: Decimal
    cash_amount: Decimal
    currency: str
    instructed_at: datetime
    cutoff_at: datetime
    securities_available: Decimal
    cash_available: Decimal
    ssi_id: str


class CounterpartyInstruction(_Model):
    trade_id: str
    status: Literal["MATCHED", "MISSING", "MISMATCH"]
    submitted_at: datetime | None = None
    quantity: Decimal | None = None
    cash_amount: Decimal | None = None
    settlement_date: date | None = None
    mismatch_fields: tuple[str, ...] = ()


class Calendar(_Model):
    market: str
    holidays: dict[str, str]  # ISO date -> holiday name; weekends are implicit


class Scenario(_Model):
    """A labelled failure: the event plus ground truth (used by tests and the eval harness)."""

    scenario_id: str
    difficulty: Literal["clear", "ambiguous", "underdetermined"]
    event: dict[str, str]
    expected_category: str
    secondary_category: str | None = None
    expected_fix_action: str | None = None
    expected_requires_write: bool | None = None
    auto_resolvable: bool
    note: str


class Dataset(_Model):
    seed: int
    instruments: tuple[Instrument, ...]
    ssis: tuple[SsiRecord, ...]
    trades: tuple[Trade, ...]
    counterparty_instructions: tuple[CounterpartyInstruction, ...]
    calendars: tuple[Calendar, ...]
    scenarios: tuple[Scenario, ...]
