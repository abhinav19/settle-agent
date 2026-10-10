"""ssi-store: standing settlement instructions. Pure and deterministic over a Dataset."""

from datetime import date

from pydantic import BaseModel

from _shared.errors import ErrorCode, ToolFailure
from _shared.models import Dataset, SsiRecord
from _shared.validation import BIC_RE, CCY_RE, require, require_id


class Ssi(BaseModel):
    ssi_id: str
    bic: str
    currency: str
    account: str
    agent_bic: str
    status: str
    effective_from: date


class TradeSsi(BaseModel):
    trade_id: str
    instructed_ssi: Ssi
    current_ssi: Ssi
    is_current: bool


def _ssi(r: SsiRecord) -> Ssi:
    return Ssi(**r.model_dump())


class SsiStore:
    def __init__(self, dataset: Dataset) -> None:
        self._by_id = {s.ssi_id: s for s in dataset.ssis}
        self._active = {(s.bic, s.currency): s for s in dataset.ssis if s.status == "ACTIVE"}
        self._trade_ssi = {t.trade_id: t.ssi_id for t in dataset.trades}

    def get_ssi(self, bic: str, currency: str) -> Ssi:
        require(BIC_RE, bic, "bic")
        require(CCY_RE, currency, "currency")
        rec = self._active.get((bic, currency))
        if rec is None:
            raise ToolFailure(ErrorCode.NOT_FOUND, f"no active SSI for {bic}/{currency}")
        return _ssi(rec)

    def get_ssi_for_trade(self, trade_id: str) -> TradeSsi:
        require_id(trade_id, "trade_id")
        ssi_id = self._trade_ssi.get(trade_id)
        if ssi_id is None:
            raise ToolFailure(ErrorCode.NOT_FOUND, f"no SSI recorded for trade {trade_id}")
        instructed = self._by_id[ssi_id]
        current = self._active[(instructed.bic, instructed.currency)]
        return TradeSsi(
            trade_id=trade_id,
            instructed_ssi=_ssi(instructed),
            current_ssi=_ssi(current),
            is_current=instructed.ssi_id == current.ssi_id,
        )
