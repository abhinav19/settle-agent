from datetime import UTC, datetime

import pytest

from ops_dashboard.format import case_label, humanize_age, millis, pct, queue_rows, trace_url, usd

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("created", "expected"),
    [
        ("2026-10-10T11:59:30+00:00", "30s"),
        ("2026-10-10T11:57:00Z", "3m"),
        ("2026-10-10T09:55:00+00:00", "2h 05m"),
        ("2026-10-09T08:00:00+00:00", "1d 4h"),
        ("2026-10-10T13:00:00+00:00", "0s"),  # clock skew never goes negative
    ],
)
def test_humanize_age(created: str, expected: str) -> None:
    assert humanize_age(created, NOW) == expected


def test_pct() -> None:
    assert pct(0.974) == "97%" and pct(0.974, 1) == "97.4%" and pct(None) == "—"


def test_trace_url() -> None:
    assert trace_url("http://j/trace/{trace_id}", "abc") == "http://j/trace/abc"
    assert trace_url(None, "abc") is None
    assert trace_url("http://j/trace", "abc") is None  # template without the placeholder
    assert trace_url("http://j/trace/{trace_id}", None) is None


CASE = {
    "id": "0123456789abcdef",
    "status": "AWAITING_APPROVAL",
    "reason_code": "SSIM",
    "trade_id": "T-1",
    "category": "STATIC_DATA",
    "confidence": 0.95,
    "fix_action": "AMEND_SSI",
    "requires_write": True,
    "created_at": "2026-10-10T11:00:00+00:00",
}


def test_queue_rows_flatten_cases() -> None:
    [row] = queue_rows([CASE], NOW)
    assert row["Age"] == "1h 00m" and row["Confidence"] == "95%" and row["Fix"] == "AMEND_SSI ✎"
    assert row["Case"] == CASE["id"] and row["Category"] == "STATIC_DATA"


def test_queue_rows_handle_uninvestigated_cases() -> None:
    bare = {
        **CASE,
        "category": None,
        "confidence": None,
        "fix_action": None,
        "requires_write": None,
    }
    [row] = queue_rows([bare], NOW)
    assert (row["Category"], row["Confidence"], row["Fix"]) == ("—", "—", "—")


def test_case_label_is_compact_and_identifying() -> None:
    assert case_label(CASE) == "T-1 · SSIM · AWAITING_APPROVAL · 95% · 01234567"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "—"),
        (0.0, "$0.0000"),
        (0.0642, "$0.0642"),
        (0.999, "$0.9990"),
        (1.0, "$1.00"),
        (1234.5, "$1,234.50"),
    ],
)
def test_usd(value: float | None, expected: str) -> None:
    assert usd(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "—"),
        (0, "0 ms"),
        (850, "850 ms"),
        (999.4, "999 ms"),
        (1000, "1.0 s"),
        (4200, "4.2 s"),
    ],
)
def test_millis(value: float | None, expected: str) -> None:
    assert millis(value) == expected
