from collections import Counter
from datetime import date

from _shared.generators import CODES, generate_dataset, isin_with_check_digit
from _shared.models import Dataset
from _shared.store import load_dataset, save_dataset
from holiday_calendar.service import HolidayCalendar
from security_master.service import SecurityMaster
from ssi_store.service import SsiStore
from trade_store.service import TradeStore


def test_generation_is_deterministic(dataset: Dataset) -> None:
    assert generate_dataset(42, 300) == dataset


def test_different_seed_differs(dataset: Dataset) -> None:
    assert generate_dataset(7, 300) != dataset


def test_save_load_roundtrip_is_byte_stable(dataset: Dataset, tmp_path) -> None:  # type: ignore[no-untyped-def]
    p1 = save_dataset(dataset, tmp_path / "a")
    p2 = save_dataset(load_dataset(tmp_path / "a"), tmp_path / "b")
    assert p1.read_bytes() == p2.read_bytes()


def test_committed_dataset_matches_generator(dataset: Dataset) -> None:
    """data/synthetic must be what `make seed` produces (guards against stale data)."""
    assert load_dataset() == dataset


def test_mix_and_coverage(dataset: Dataset) -> None:
    assert len(dataset.scenarios) == 300
    assert Counter(s.difficulty for s in dataset.scenarios) == {
        "clear": 180,
        "ambiguous": 75,
        "underdetermined": 45,
    }
    assert {s.event["reason_code"] for s in dataset.scenarios} == set(CODES)
    for code in CODES:
        per = [s for s in dataset.scenarios if s.event["reason_code"] == code]
        assert {s.difficulty for s in per} == {"clear", "ambiguous", "underdetermined"}


def test_only_clear_cases_are_auto_resolvable(dataset: Dataset) -> None:
    for s in dataset.scenarios:
        assert s.auto_resolvable == (s.difficulty == "clear")
        assert (s.expected_fix_action is not None) == s.auto_resolvable
        assert (s.expected_category == "UNDETERMINED") == (s.difficulty == "underdetermined")


def test_isins_have_valid_check_digits(dataset: Dataset) -> None:
    for inst in dataset.instruments:
        assert isin_with_check_digit(inst.isin[:11]) == inst.isin


def test_events_resolve_in_reference_data(dataset: Dataset) -> None:
    sm, ssi, tr = SecurityMaster(dataset), SsiStore(dataset), TradeStore(dataset)
    for s in dataset.scenarios:
        ev = s.event
        sm.get_instrument(ev["isin"])
        ssi.get_ssi(ev["counterparty_bic"], ev["currency"])
        if s.note != "trade absent from trade store":
            assert tr.get_trade(ev["trade_id"]).isin == ev["isin"]


def test_event_ids_unique(dataset: Dataset) -> None:
    ids = [s.event["event_id"] for s in dataset.scenarios]
    assert len(set(ids)) == len(ids)


def test_ground_truth_is_discoverable_through_tools(dataset: Dataset) -> None:
    """For every clear scenario, the data the tools expose actually shows the labelled fault."""
    sm, ssi, tr = SecurityMaster(dataset), SsiStore(dataset), TradeStore(dataset)
    cal = HolidayCalendar(dataset)
    for s in (x for x in dataset.scenarios if x.difficulty == "clear"):
        code, tid = s.event["reason_code"], s.event["trade_id"]
        t = tr.get_trade(tid)
        cp = tr.get_counterparty_instruction(tid)
        if code == "LACK":
            assert t.securities_available < t.quantity
        elif code == "MONY":
            assert t.cash_available < t.cash_amount
        elif code == "NMAT":
            assert cp.status == "MISMATCH" and cp.mismatch_fields
        elif code == "CMIS":
            assert cp.status == "MISSING"
        elif code == "SSIM":
            assert not ssi.get_ssi_for_trade(tid).is_current
        elif code == "DENO":
            assert t.quantity % sm.get_instrument(t.isin).lot_size != 0
        elif code == "LATE":
            assert t.instructed_at > t.cutoff_at
        elif code == "CALD":
            assert not cal.is_business_day(t.market, t.settlement_date.isoformat()).is_business_day


def test_clear_scenarios_have_no_unrelated_faults(dataset: Dataset) -> None:
    """A clear scenario's only fault is the reported one (otherwise it would be ambiguous)."""
    sm, ssi, tr = SecurityMaster(dataset), SsiStore(dataset), TradeStore(dataset)
    cal = HolidayCalendar(dataset)
    for s in (x for x in dataset.scenarios if x.difficulty == "clear"):
        code, tid = s.event["reason_code"], s.event["trade_id"]
        t, cp = tr.get_trade(tid), tr.get_counterparty_instruction(tid)
        faults = set()
        if t.securities_available < t.quantity:
            faults.add("LACK")
        if t.cash_available < t.cash_amount:
            faults.add("MONY")
        if cp.status == "MISMATCH":
            faults.add("NMAT")
        if cp.status == "MISSING":
            faults.add("CMIS")
        if not ssi.get_ssi_for_trade(tid).is_current:
            faults.add("SSIM")
        if t.quantity % sm.get_instrument(t.isin).lot_size != 0:
            faults.add("DENO")
        if t.instructed_at > t.cutoff_at:
            faults.add("LATE")
        if not cal.is_business_day(t.market, t.settlement_date.isoformat()).is_business_day:
            faults.add("CALD")
        assert faults == {code}, (tid, faults)


def test_settlement_dates_within_window(dataset: Dataset) -> None:
    for t in dataset.trades:
        assert date(2026, 9, 1) <= t.settlement_date <= date(2026, 12, 31)
