"""Choosing the production auto-resolve threshold FROM THE CURVE (never by feel)."""

from collections.abc import Sequence

from evalharness.metrics import CurvePoint

DEFAULT_MIN_PRECISION = 0.98
DEFAULT_MIN_SUPPORT = 20


def select_threshold(
    curve: Sequence[CurvePoint],
    *,
    min_precision: float = DEFAULT_MIN_PRECISION,
    min_support: int = DEFAULT_MIN_SUPPORT,
) -> CurvePoint | None:
    """Highest-recall threshold whose precision is at least ``min_precision``.

    ``min_support`` stops a tiny, lucky subset of high-confidence cases from qualifying: at least
    that many cases must be auto-resolved at the threshold. Recall never rises with the threshold;
    among thresholds with EQUAL recall (identical automation) the highest wins — same behaviour,
    more safety margin. Returns None when nothing qualifies — the honest answer then is "do not
    auto-resolve yet" (a threshold of 1.0), not a lower bar.
    """
    qualifying = [
        p
        for p in curve
        if p.precision is not None and p.precision >= min_precision and p.auto_count >= min_support
    ]
    if not qualifying:
        return None
    return max(qualifying, key=lambda p: (p.recall or 0.0, p.threshold))
