"""Shared base for every contract model."""

from pydantic import BaseModel, ConfigDict


class DomainModel(BaseModel):
    """Strict, frozen, reject-on-extra base for all schemas that cross a boundary."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
