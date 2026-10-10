"""security-master: static instrument data. Pure and deterministic over a Dataset."""

from pydantic import BaseModel

from _shared.errors import ErrorCode, ToolFailure
from _shared.models import Dataset, Instrument
from _shared.validation import ISIN_RE, require


class InstrumentInfo(BaseModel):
    isin: str
    name: str
    market: str
    currency: str
    lot_size: int
    status: str


class SecurityMaster:
    def __init__(self, dataset: Dataset) -> None:
        self._by_isin: dict[str, Instrument] = {i.isin: i for i in dataset.instruments}

    def get_instrument(self, isin: str) -> InstrumentInfo:
        require(ISIN_RE, isin, "isin")
        inst = self._by_isin.get(isin)
        if inst is None:
            raise ToolFailure(ErrorCode.NOT_FOUND, f"instrument {isin} not found")
        return InstrumentInfo(**inst.model_dump())
