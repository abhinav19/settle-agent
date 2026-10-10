"""trade-store: trades and counterparty instructions. Pure and deterministic over a Dataset."""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel

from _shared.errors import ErrorCode, ToolFailure
from _shared.models import CounterpartyInstruction, Dataset, Trade
from _shared.validation import require_id


class TradeInfo(BaseModel):
    trade_id: str
    isin: str
    counterparty_bic: str
    market: str
    side: str
    trade_date: date
    settlement_date: date
    quantity: Decimal
    cash_amount: Decimal
    currency: str
    instructed_at: datetime
    cutoff_at: datetime
    securities_available: Decimal
    cash_available: Decimal


class CounterpartyInstructionInfo(BaseModel):
    trade_id: str
    status: str  # MATCHED | MISSING | MISMATCH
    submitted_at: datetime | None
    quantity: Decimal | None
    cash_amount: Decimal | None
    settlement_date: date | None
    mismatch_fields: list[str]


class TradeStore:
    def __init__(self, dataset: Dataset) -> None:
        self._trades: dict[str, Trade] = {t.trade_id: t for t in dataset.trades}
        self._cps: dict[str, CounterpartyInstruction] = {
            c.trade_id: c for c in dataset.counterparty_instructions
        }

    def get_trade(self, trade_id: str) -> TradeInfo:
        require_id(trade_id, "trade_id")
        t = self._trades.get(trade_id)
        if t is None:
            raise ToolFailure(ErrorCode.NOT_FOUND, f"trade {trade_id} not found")
        return TradeInfo(**t.model_dump(exclude={"ssi_id"}))

    def get_counterparty_instruction(self, trade_id: str) -> CounterpartyInstructionInfo:
        require_id(trade_id, "trade_id")
        c = self._cps.get(trade_id)
        if c is None:
            raise ToolFailure(ErrorCode.NOT_FOUND, f"no counterparty record for trade {trade_id}")
        data = c.model_dump()
        data["mismatch_fields"] = list(c.mismatch_fields)
        return CounterpartyInstructionInfo(**data)
