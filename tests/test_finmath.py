"""Shared maths (wealth.finmath) and the callers that must agree on it."""
from __future__ import annotations

import math
from datetime import date
from decimal import Decimal

import pytest

from wealth import finmath
from wealth.ledger.derive import FxTable as LedgerFx
from wealth.ledger.performance import xirr as ledger_xirr
from wealth.profile import xirr as profile_xirr
from wealth.situation.model import _FX

# name, flows, expected annual rate (derived by hand), or None
XIRR_TABLE = [
    # -1000 now, +1100 in a year: (1+r) = 1.1
    ("two-flow 10%", [("2025-01-01", -1000), ("2026-01-01", 1100)], 0.10),
    # -100 + 230/(1+r) - 132/(1+r)^2 = 0  ->  (1+r) = 1.1 or 1.2; the root nearest zero is shown
    ("multi-root 10%/20%", [("2025-01-01", -100), ("2026-01-01", 230), ("2027-01-01", -132)], 0.10),
    # 1/1000 back after a year
    ("-99.9% in a year", [("2025-01-01", -1000), ("2026-01-01", 1)], -0.999),
    # half back after 30 days: 0.5^(365/30) - 1
    ("-50% in 30 days", [("2025-01-01", -1000), ("2025-01-31", 500)], 0.5 ** (365 / 30) - 1),
    # 100x in a year
    ("+100x in a year", [("2025-01-01", -100), ("2026-01-01", 10000)], 99.0),
    # no time passes: no rate
    ("same-day", [("2025-01-01", -1000), ("2025-01-01", 1100)], None),
    # a zero flow changes nothing
    ("zero flow included", [("2025-01-01", 0), ("2025-06-01", -1000), ("2026-06-01", 1100)], 0.10),
    ("unsorted input", [("2026-01-01", 1100), ("2025-01-01", -1000)], 0.10),
    # no sign change: no rate
    ("all contributions", [("2025-01-01", -1000), ("2026-01-01", -10)], None),
]


@pytest.mark.parametrize("name,flows,expected", XIRR_TABLE, ids=[row[0] for row in XIRR_TABLE])
def test_ledger_and_profile_xirr_agree_with_the_hand_table(name, flows, expected):
    ledger = ledger_xirr([(d, Decimal(str(a))) for d, a in flows])
    profile = profile_xirr([(date.fromisoformat(d), float(a)) for d, a in flows])
    if expected is None:
        assert ledger is None and profile is None
    else:
        assert ledger == pytest.approx(expected, rel=1e-9, abs=1e-12)
        assert profile == pytest.approx(expected, rel=1e-9, abs=1e-12)
        assert ledger == pytest.approx(profile, rel=1e-12, abs=1e-15)


def test_xirr_reports_every_root_when_there_is_more_than_one():
    solved = finmath.xirr_solve([("2025-01-01", -100), ("2026-01-01", 230), ("2027-01-01", -132)])
    assert solved.roots == pytest.approx([0.10, 0.20], abs=1e-9)
    assert solved.rate == pytest.approx(0.10, abs=1e-9)
    assert "not unique" in solved.warning and "10.00%" in solved.warning and "20.00%" in solved.warning


def test_xirr_expands_its_bracket_beyond_1000_percent():
    # 1000x in a year: rate 999, above the initial 10 (1,000 %) bracket
    assert finmath.xirr([("2025-01-01", -1), ("2026-01-01", 1000)]) == pytest.approx(999, rel=1e-9)


def test_annualize_never_annualizes_short_or_total_losses():
    assert finmath.annualize(0.21, 730) == pytest.approx(0.1, abs=1e-12)          # 1.21^(1/2) - 1
    assert finmath.annualize(Decimal("0.21"), 730) == Decimal(str((1.21) ** 0.5 - 1))
    assert finmath.annualize(0.05, 200) is None
    assert finmath.annualize(-1, 400) is None and finmath.annualize(None, 400) is None


def test_month_arithmetic():
    assert finmath.add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert finmath.add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)
    assert finmath.add_months(date(2026, 11, 15), 3, day_of_month=31) == date(2027, 2, 28)
    assert finmath.add_months(date(2026, 3, 1), -3) == date(2025, 12, 1)
    assert finmath.months_between(date(2026, 9, 21), date(2027, 1, 1)) == 4
    assert finmath.month_ends("2025-01-01", "2025-04-30") == ["2025-01-31", "2025-02-28", "2025-03-31"]
    assert finmath.month_ends("2025-01-31", "2025-03-31") == ["2025-02-28"]
    assert finmath.month_keys("2025-11-15", "2026-02-01") == ["2025-11", "2025-12", "2026-01", "2026-02"]


def test_frequency_factors_and_holding_period():
    assert finmath.per_month(Decimal(1200), "annual") == Decimal(100)
    assert finmath.per_month(Decimal(1000), "biweekly") == Decimal(26000) / Decimal(12)
    assert finmath.per_month(Decimal(300), "quarterly") == Decimal(100)
    assert finmath.per_month(Decimal(1), "fortnightly-ish") is None
    # One year and a day is long term (the tax engine's rule), a leap day included.
    assert finmath.holding_character("2024-02-29", "2025-02-28") == "short"
    assert finmath.holding_character("2024-02-29", "2025-03-01") == "long"
    assert finmath.holding_character("2025-01-02", "2026-01-02") == "short"
    assert finmath.holding_character(None, "2026-01-02") == "unknown"


def test_essential_rule_counts_unknown_as_essential():
    assert finmath.essential_spending(Decimal(100), Decimal(40)) == Decimal(140)
    assert "essential" in finmath.ESSENTIAL_RULE


def test_fx_uses_the_newest_of_the_pair_and_its_inverse_in_both_callers():
    # Repro fx1: a 2023 USD/MXN rate of 17 used to beat a 2026 MXN/USD rate of 0.05 (20 MXN per USD).
    situation = _FX(date(2026, 9, 21))
    situation.add("USD", "MXN", "17", "2023-01-02", "ledger")
    situation.add("MXN", "USD", "0.05", "2026-09-18", "statement 2026-09-18")
    assert situation.convert(Decimal(1000), "USD", "MXN") == Decimal(20000)
    assert situation.listing() == [{"pair": "MXN/USD", "rate": 0.05, "date": "2026-09-18", "source": "statement 2026-09-18"}]
    rows = [{"base": "USD", "quote": "MXN", "rate": "17", "date": "2023-01-02"},
            {"base": "MXN", "quote": "USD", "rate": "0.05", "date": "2026-09-18"}]
    assert LedgerFx(rows).convert(Decimal(1000), "USD", "MXN", "2026-09-21") == Decimal(20000)
    # Too old is unknown, never the stale rate, never 1 or 0.
    old = _FX(date(2026, 9, 21), max_age_days=7)
    old.add("USD", "MXN", "17", "2026-09-01", "statement")
    assert old.convert(Decimal(1000), "USD", "MXN") is None
    assert finmath.FxTable(rows, max_age_days=7).rate("USD", "MXN", "2026-09-30") is None
    # The direct pair wins a tie.
    tie = finmath.FxTable([{"base": "USD", "quote": "MXN", "rate": "18", "date": "2026-09-18"},
                           {"base": "MXN", "quote": "USD", "rate": "0.05", "date": "2026-09-18"}])
    assert tie.rate("USD", "MXN", "2026-09-18") == Decimal(18)
    assert math.isclose(float(tie.rate("MXN", "USD", "2026-09-18")), 0.05)
