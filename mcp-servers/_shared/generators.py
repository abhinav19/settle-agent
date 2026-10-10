"""Seeded generator for internally consistent synthetic reference data and labelled scenarios.

Everything is derived from one ``random.Random(seed)`` and fixed constants, so the same
``(seed, n)`` always yields a byte-identical dataset. Each scenario's event references an
ISIN, BIC, trade and SSI that resolve in the generated reference data, and carries ground
truth (category, fix, whether it is safely auto-resolvable) for tests and the eval harness.
"""

import random
import string
import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Final

from _shared.models import (
    Calendar,
    CounterpartyInstruction,
    Dataset,
    Instrument,
    Scenario,
    SsiRecord,
    Trade,
)

CODES: Final = ("LACK", "MONY", "NMAT", "CMIS", "SSIM", "DENO", "LATE", "CALD")
CATEGORY: Final = {
    "LACK": "INVENTORY",
    "MONY": "FUNDING",
    "NMAT": "MATCHING",
    "CMIS": "COUNTERPARTY",
    "SSIM": "STATIC_DATA",
    "DENO": "QUANTITY",
    "LATE": "TIMING",
    "CALD": "CALENDAR",
}
# code -> (fix action, requires_write) for a clearly diagnosed failure
FIX: Final = {
    "LACK": ("PARTIAL_SETTLE", True),
    "MONY": ("RESUBMIT", True),
    "NMAT": ("CHASE_COUNTERPARTY", False),
    "CMIS": ("CHASE_COUNTERPARTY", False),
    "SSIM": ("AMEND_SSI", True),
    "DENO": ("RESUBMIT", True),
    "LATE": ("REBOOK_SETTLEMENT_DATE", True),
    "CALD": ("NO_ACTION", False),
}
MARKETS: Final = {
    "XNYS": ("US", "USD"),
    "XLON": ("GB", "GBP"),
    "XETR": ("DE", "EUR"),
    "XPAR": ("FR", "EUR"),
    "XTKS": ("JP", "JPY"),
}
CURRENCIES: Final = ("USD", "GBP", "EUR", "JPY")
WINDOW_START: Final = date(2026, 9, 1)
WINDOW_END: Final = date(2026, 12, 31)
DIFFICULTY_MIX: Final = (("clear", 0.60), ("ambiguous", 0.25), ("underdetermined", 0.15))

_ADJ = ("Apex", "Borealis", "Cobalt", "Delta", "Everest", "Falcon", "Granite", "Harbor")
_NOUN = ("Energy", "Foods", "Logistics", "Materials", "Networks", "Pharma", "Retail", "Systems")
_ALNUM = string.ascii_uppercase + string.digits


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n = n * 2 - 9 if n * 2 > 9 else n * 2
        total += n
    return total % 10 == 0


def isin_with_check_digit(body11: str) -> str:
    """Append the Luhn check digit to an 11-character ISIN body."""
    for d in range(10):
        candidate = f"{body11}{d}"
        if _luhn_ok("".join(str(int(c, 36)) for c in candidate)):
            return candidate
    raise AssertionError("unreachable: a Luhn digit always exists")


def is_business_day(calendar: Calendar, day: date) -> bool:
    return day.weekday() < 5 and day.isoformat() not in calendar.holidays


def next_business_day(calendar: Calendar, day: date) -> date:
    nxt = day + timedelta(days=1)
    while not is_business_day(calendar, nxt):
        nxt += timedelta(days=1)
    return nxt


def _previous_business_day(calendar: Calendar, day: date) -> date:
    prv = day - timedelta(days=1)
    while not is_business_day(calendar, prv):
        prv -= timedelta(days=1)
    return prv


def _make_instruments(rng: random.Random, count: int) -> list[Instrument]:
    out: list[Instrument] = []
    seen: set[str] = set()
    markets = sorted(MARKETS)
    while len(out) < count:
        market = markets[len(out) % len(markets)]
        country, ccy = MARKETS[market]
        body = country + "".join(rng.choice(_ALNUM) for _ in range(9))
        isin = isin_with_check_digit(body)
        if isin in seen:
            continue
        seen.add(isin)
        out.append(
            Instrument(
                isin=isin,
                name=f"{rng.choice(_ADJ)} {rng.choice(_NOUN)} {len(out):02d}",
                market=market,
                currency=ccy,
                lot_size=rng.choice((1, 1, 10, 100, 1000)),
                status="ACTIVE",
            )
        )
    return sorted(out, key=lambda i: i.isin)


def _make_bics(rng: random.Random, count: int) -> list[str]:
    bics: set[str] = set()
    while len(bics) < count:
        bank = "".join(rng.choice(string.ascii_uppercase) for _ in range(4))
        country = rng.choice(("US", "GB", "DE", "FR", "JP"))
        bics.add(f"{bank}{country}{rng.choice(('FF', '2L', 'XX'))}")
    return sorted(bics)


def _make_calendars(rng: random.Random) -> list[Calendar]:
    weekdays = [
        WINDOW_START + timedelta(days=d)
        for d in range((WINDOW_END - WINDOW_START).days + 1)
        if (WINDOW_START + timedelta(days=d)).weekday() < 5
    ]
    cals = []
    for market in sorted(MARKETS):
        picks = sorted(rng.sample(weekdays, 8))
        cals.append(
            Calendar(
                market=market,
                holidays={
                    d.isoformat(): f"{market} Synthetic Holiday {n + 1}"
                    for n, d in enumerate(picks)
                },
            )
        )
    return cals


def _make_ssis(rng: random.Random, bics: list[str]) -> list[SsiRecord]:
    ssis = []
    for bic in bics:
        for ccy in CURRENCIES:
            agent = rng.choice(bics)
            ssis.append(
                SsiRecord(
                    ssi_id=f"SSI-{bic}-{ccy}-1",
                    bic=bic,
                    currency=ccy,
                    account=f"ACC{rng.randint(10**7, 10**8 - 1)}",
                    agent_bic=agent,
                    status="SUPERSEDED",
                    effective_from=date(2026, 1, 15),
                )
            )
            ssis.append(
                SsiRecord(
                    ssi_id=f"SSI-{bic}-{ccy}-2",
                    bic=bic,
                    currency=ccy,
                    account=f"ACC{rng.randint(10**7, 10**8 - 1)}",
                    agent_bic=agent,
                    status="ACTIVE",
                    effective_from=date(2026, 7, 1) + timedelta(days=rng.randint(0, 60)),
                )
            )
    return ssis


def _difficulties(rng: random.Random, n: int) -> list[str]:
    counts = [round(n * share) for _, share in DIFFICULTY_MIX]
    counts[0] += n - sum(counts)
    pool = [name for (name, _), c in zip(DIFFICULTY_MIX, counts, strict=True) for _ in range(c)]
    rng.shuffle(pool)
    return pool


def _raw_message(t: dict[str, Any], code: str) -> str:
    return (
        ":16R:GENL\n"
        f":20C::SEME//{t['trade_id']}\n"
        ":23G:INST\n"
        ":16S:GENL\n"
        ":16R:TRADDET\n"
        f":98A::SETT//{t['settlement_date'].strftime('%Y%m%d')}\n"
        f":35B:ISIN {t['isin']}\n"
        ":16S:TRADDET\n"
        ":16R:SETTRAN\n"
        f":36B::SETT//UNIT/{t['quantity']}\n"
        f":19A::SETT//{t['currency']}{t['cash_amount']}\n"
        f":95P::REAG//{t['counterparty_bic']}\n"
        ":16S:SETTRAN\n"
        ":16R:STAT\n"
        ":25D::IPRC//PENF\n"
        f":24B::PENF//{code}\n"
        ":16S:STAT\n"
        "-- SYNTHETIC MESSAGE (not a real MT548) --"
    )


def generate_dataset(seed: int = 42, n_scenarios: int = 300) -> Dataset:
    """Build the full reference dataset plus ``n_scenarios`` labelled failure scenarios."""
    rng = random.Random(seed)  # noqa: S311 - deterministic synthetic data, not security
    instruments = _make_instruments(rng, 40)
    bics = _make_bics(rng, 12)
    calendars = _make_calendars(rng)
    cal_by_market = {c.market: c for c in calendars}
    ssis = _make_ssis(rng, bics)
    ssi_by_id = {s.ssi_id: s for s in ssis}
    lot_gt1 = [i for i in instruments if i.lot_size > 1]

    trades: list[Trade] = []
    cps: list[CounterpartyInstruction] = []
    scenarios: list[Scenario] = []
    difficulties = _difficulties(rng, n_scenarios)
    underdetermined_seen = 0

    for idx in range(n_scenarios):
        code = CODES[idx % len(CODES)]
        difficulty = difficulties[idx]
        other = None
        if difficulty == "ambiguous":
            conflicts = {"NMAT": "CMIS", "CMIS": "NMAT"}
            other = rng.choice([c for c in CODES if c != code and c != conflicts.get(code)])
        faults = {code} if difficulty == "clear" else ({code, other} if other else set())
        if difficulty == "underdetermined":
            faults = set()

        needs_lot = "DENO" in faults
        inst = rng.choice(lot_gt1 if needs_lot else instruments)
        cal = cal_by_market[inst.market]
        bic = rng.choice(bics)
        settle = date(2026, 9, 10) + timedelta(days=rng.randint(0, 95))
        while not is_business_day(cal, settle):
            settle += timedelta(days=1)
        prev_bd = _previous_business_day(cal, settle)
        trade_date = _previous_business_day(cal, prev_bd)
        k = rng.randint(2, 20)
        qty = inst.lot_size * k
        price = Decimal(rng.randint(500, 50000)) / 100
        instructed = datetime.combine(prev_bd, time(10, 0), tzinfo=UTC)
        cutoff = datetime.combine(prev_bd, time(15, 0), tzinfo=UTC)
        t: dict[str, Any] = {
            "trade_id": f"T-{seed}-{idx:05d}",
            "isin": inst.isin,
            "counterparty_bic": bic,
            "market": inst.market,
            "side": rng.choice(("BUY", "SELL")),
            "trade_date": trade_date,
            "settlement_date": settle,
            "quantity": Decimal(qty),
            "currency": inst.currency,
            "instructed_at": instructed,
            "cutoff_at": cutoff,
            "ssi_id": f"SSI-{bic}-{inst.currency}-2",
        }
        notes: list[str] = []

        if "DENO" in faults:
            t["quantity"] = Decimal(qty + rng.randint(1, inst.lot_size - 1))
            notes.append("quantity breaks lot size")
        t["cash_amount"] = (t["quantity"] * price).quantize(Decimal("0.01"))
        t["securities_available"] = t["quantity"]
        t["cash_available"] = t["cash_amount"]
        if "LACK" in faults:
            lots = int(t["quantity"] // inst.lot_size)
            t["securities_available"] = Decimal(inst.lot_size * rng.randint(1, lots - 1))
            notes.append("securities shortfall")
        if "MONY" in faults:
            t["cash_available"] = (t["cash_amount"] * Decimal(rng.randint(10, 90)) / 100).quantize(
                Decimal("0.01")
            )
            notes.append("cash shortfall")
        if "SSIM" in faults:
            t["ssi_id"] = f"SSI-{bic}-{inst.currency}-1"
            notes.append("trade references superseded SSI")
        if "LATE" in faults:
            t["instructed_at"] = cutoff + timedelta(hours=rng.randint(1, 5))
            notes.append("instructed after cut-off")
        if "CALD" in faults:
            holiday = date.fromisoformat(rng.choice(sorted(cal.holidays)))
            t["settlement_date"] = holiday
            notes.append(f"settlement date {holiday} is a market holiday")

        ci: dict[str, Any] = {
            "trade_id": t["trade_id"],
            "status": "MATCHED",
            "submitted_at": t["instructed_at"] + timedelta(minutes=10),
            "quantity": t["quantity"],
            "cash_amount": t["cash_amount"],
            "settlement_date": t["settlement_date"],
            "mismatch_fields": (),
        }
        if "CMIS" in faults:
            ci.update(
                status="MISSING",
                submitted_at=None,
                quantity=None,
                cash_amount=None,
                settlement_date=None,
            )
            notes.append("counterparty has not instructed")
        if "NMAT" in faults:
            fields = rng.sample(["quantity", "cash_amount", "settlement_date"], rng.randint(1, 2))
            for f in fields:
                if f == "settlement_date":
                    ci[f] = ci[f] + timedelta(days=1)
                else:
                    ci[f] = ci[f] + Decimal(rng.randint(1, 9))
            ci.update(status="MISMATCH", mismatch_fields=tuple(sorted(fields)))
            notes.append(f"counterparty instruction differs on {', '.join(sorted(fields))}")

        if difficulty == "underdetermined":
            note = "no inconsistency found in reference data"
            if underdetermined_seen % 2 == 1:
                note = "trade absent from trade store"
            else:
                trades.append(Trade(**t))
                cps.append(CounterpartyInstruction(**ci))
            underdetermined_seen += 1
        else:
            trades.append(Trade(**t))
            cps.append(CounterpartyInstruction(**ci))
            note = "; ".join(notes)

        event = {
            "schema_version": "1.0",
            "event_id": str(uuid.UUID(int=rng.getrandbits(128), version=4)),
            "emitted_at": datetime.combine(settle, time(8, 0), tzinfo=UTC)
            .replace(minute=idx % 60)
            .isoformat(),
            "trade_id": t["trade_id"],
            "isin": t["isin"],
            "settlement_date": t["settlement_date"].isoformat(),
            "counterparty_bic": bic,
            "reason_code": code,
            "quantity": str(t["quantity"]),
            "cash_amount": str(t["cash_amount"]),
            "currency": t["currency"],
            "raw_message": _raw_message(t, code),
        }
        clear = difficulty == "clear"
        scenarios.append(
            Scenario(
                scenario_id=f"S-{seed}-{idx:05d}",
                difficulty=difficulty,
                event=event,
                expected_category=CATEGORY[code]
                if difficulty != "underdetermined"
                else "UNDETERMINED",
                secondary_category=CATEGORY[other] if other else None,
                expected_fix_action=FIX[code][0] if clear else None,
                expected_requires_write=FIX[code][1] if clear else None,
                auto_resolvable=clear,
                note=note,
            )
        )

    if not all(t.ssi_id in ssi_by_id for t in trades):
        raise RuntimeError("generated a trade referencing an unknown SSI")
    return Dataset(
        seed=seed,
        instruments=tuple(instruments),
        ssis=tuple(ssis),
        trades=tuple(sorted(trades, key=lambda x: x.trade_id)),
        counterparty_instructions=tuple(sorted(cps, key=lambda x: x.trade_id)),
        calendars=tuple(calendars),
        scenarios=tuple(scenarios),
    )
