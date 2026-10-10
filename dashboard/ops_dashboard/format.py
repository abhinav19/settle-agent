"""Pure presentation helpers (no Streamlit, no I/O) so they are trivially testable."""

from datetime import UTC, datetime
from typing import Any


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def humanize_age(created_at: str, now: datetime | None = None) -> str:
    """'3m', '2h 05m', '1d 4h' — compact age for the queue."""
    seconds = max(0, int(((now or datetime.now(UTC)) - parse_ts(created_at)).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def pct(value: float | None, digits: int = 0) -> str:
    return "—" if value is None else f"{value * 100:.{digits}f}%"


def trace_url(template: str | None, trace_id: str | None) -> str | None:
    """Build a link to the tracing UI from ``TRACE_URL_TEMPLATE`` (e.g. .../trace/{trace_id})."""
    if not template or not trace_id or "{trace_id}" not in template:
        return None
    return template.replace("{trace_id}", trace_id)


def queue_rows(items: list[dict[str, Any]], now: datetime | None = None) -> list[dict[str, Any]]:
    """Flatten case summaries into table rows."""
    rows = []
    for c in items:
        rows.append(
            {
                "Status": c["status"],
                "Age": humanize_age(c["created_at"], now),
                "Reason": c["reason_code"],
                "Trade": c["trade_id"],
                "Category": c["category"] or "—",
                "Confidence": pct(c["confidence"]),
                "Fix": (
                    f"{c['fix_action']}{' ✎' if c['requires_write'] else ''}"
                    if c["fix_action"]
                    else "—"
                ),
                "Case": c["id"],
            }
        )
    return rows


def case_label(c: dict[str, Any]) -> str:
    conf = pct(c["confidence"])
    return f"{c['trade_id']} · {c['reason_code']} · {c['status']} · {conf} · {c['id'][:8]}"


def usd(value: float | None) -> str:
    """Money with enough precision for per-case LLM costs (often fractions of a cent)."""
    if value is None:
        return "—"
    return f"${value:,.4f}" if abs(value) < 1 else f"${value:,.2f}"


def millis(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.0f} ms" if value < 1000 else f"{value / 1000:.1f} s"
