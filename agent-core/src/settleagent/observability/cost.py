"""Budget tracking: running spend, per-case alerts, and threshold alerts (80% / 100%).

The database remains the source of truth; this tracker is seeded from it at startup and then
updated per case, so alerts fire in-process without a query per case.
"""

import logging
from dataclasses import dataclass
from uuid import UUID

from settleagent.observability.telemetry import NOOP, Telemetry

log = logging.getLogger(__name__)


def _usd(x: float) -> str:
    return f"${x:,.4f}" if x < 1 else f"${x:,.2f}"


@dataclass(frozen=True)
class BudgetAlert:
    kind: str  # "budget_threshold" | "case_cost"
    detail: str
    threshold: float | None = None


class CostTracker:
    def __init__(
        self,
        ceiling_usd: float,
        *,
        per_case_alert_usd: float = 1.0,
        initial_total_usd: float = 0.0,
        thresholds: tuple[float, ...] = (0.8, 1.0),
        telemetry: Telemetry = NOOP,
    ) -> None:
        if ceiling_usd <= 0:
            raise ValueError("ceiling_usd must be positive")
        self._ceiling = ceiling_usd
        self._per_case = per_case_alert_usd
        self._total = initial_total_usd
        self._thresholds = sorted(thresholds)
        self._telemetry = telemetry
        self._fired = {t for t in self._thresholds if self._total >= t * ceiling_usd}

    def seed(self, total_usd: float) -> None:
        """Set the spend-to-date from the database at startup (thresholds already passed
        are marked fired, so a restart does not re-alert)."""
        self._total = total_usd
        self._fired = {t for t in self._thresholds if self._total >= t * self._ceiling}

    def sync(self, db_total_usd: float) -> None:
        """Adopt the database's spend if it is higher (other replicas / earlier runs spent too).

        Never lowers the local figure, and never re-fires alerts that already fired.
        """
        if db_total_usd > self._total:
            self._total = db_total_usd
            self._fired |= {t for t in self._thresholds if self._total >= t * self._ceiling}

    @property
    def ceiling_usd(self) -> float:
        return self._ceiling

    @property
    def exceeded(self) -> bool:
        """True once spend has reached the ceiling: new investigations must not call the LLM."""
        return self._total >= self._ceiling

    @property
    def total_usd(self) -> float:
        return self._total

    @property
    def fraction_used(self) -> float:
        return self._total / self._ceiling

    def record(self, case_id: UUID, cost_usd: float) -> list[BudgetAlert]:
        """Add a finished case's cost; return (and log/count) any alerts it triggers."""
        self._total += cost_usd
        alerts: list[BudgetAlert] = []
        if cost_usd > self._per_case:
            alerts.append(
                BudgetAlert(
                    "case_cost", f"case cost {_usd(cost_usd)} exceeds {_usd(self._per_case)}"
                )
            )
        for t in self._thresholds:
            if t not in self._fired and self._total >= t * self._ceiling:
                self._fired.add(t)  # each threshold alerts once
                alerts.append(
                    BudgetAlert(
                        "budget_threshold",
                        f"spend {_usd(self._total)} reached {t:.0%} of the "
                        f"{_usd(self._ceiling)} budget",
                        t,
                    )
                )
        for a in alerts:
            log.warning(
                "BUDGET ALERT: %s",
                a.detail,
                extra={
                    "alert": a.kind,
                    "total_usd": round(self._total, 6),
                    "case_cost_usd": cost_usd,
                },
            )
            self._telemetry.budget_alerts.add(
                1, {"kind": a.kind, "threshold": a.threshold if a.threshold is not None else 0.0}
            )
        return alerts
