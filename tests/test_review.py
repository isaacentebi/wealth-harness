"""Quarterly review and fee audit (wealth/review.py)."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

import pytest

from wealth import review
from wealth.catalog import CATALOG, _MX_REVIEW_EXAMPLE
from wealth.ledger import performance, price_table
from wealth.service import WealthService


def _run(inputs, snapshot=None, history=None):
    return review.run_task("quarterly_review", inputs, snapshot or {"facts": [], "decisions": []}, None,
                           "2026-09-21", fact_history=history)


def _mx(**changes):
    inputs = deepcopy(_MX_REVIEW_EXAMPLE)
    inputs.update(changes)
    return inputs


def D(value):
    return None if value is None else Decimal(value)


# ------------------------------------------------------------------ net worth decomposition


def test_decomposition_identity_holds_within_a_cent():
    report = _run(_mx())
    data = report["result"]["sections"]["net_worth"]["data"]
    start, end = D(data["start"]), D(data["end"])
    contributions, growth = D(data["contributions"]["net"]), D(data["growth"]["total"])
    assert abs(start + contributions + growth - end) <= Decimal("0.01")
    assert D(data["identity"]["residual"]) == 0
    parts = data["growth"]
    assert D(parts["investment_income"]) + D(parts["fees_and_withholding"]) + D(parts["market"]) == growth
    flows = data["contributions"]
    assert D(flows["earned_income"]) == Decimal("180000") and D(flows["spending"]) == Decimal("-99000")
    assert D(flows["earned_income"]) + D(flows["spending"]) + D(flows["other_flows"]) + \
        D(flows["opening_balances_recorded"]) == contributions
    # Own-account transfers (BBVA -> GBM) are internal: they are not contributions.
    assert D(flows["other_flows"]) == 0


def test_unknown_price_leaves_values_unknown_not_zero():
    inputs = _mx()
    inputs["prices"] = {k: v for k, v in inputs["prices"].items() if k != "VOO"}
    report = _run(inputs)
    section = report["result"]["sections"]["net_worth"]
    assert section["status"] == "partial" and report["status"] == "partial"
    assert section["data"]["end"] is None and section["data"]["growth"]["total"] is None
    assert section["data"]["identity"]["residual"] is None
    assert any(m["key"].startswith("price VOO") or "VOO" in m["key"] for m in section["missing"])
    alloc = report["result"]["sections"]["allocation"]["data"]
    assert alloc["portfolio_value"] is None and all(s["outside_band"] is None for s in alloc["sleeves"])
    assert "VOO" in " ".join(report["result"]["narrative_inputs"]["unknown"])


# ------------------------------------------------------------------ performance


def _simple_ledger():
    source = {"kind": "document", "ref": "broker statement"}
    return {
        "accounts": [{"id": "brk", "institution": "Broker", "type": "brokerage", "currency": "USD"}],
        "instruments": [{"id": "X", "symbol": "X", "currency": "USD", "listing_currency": "USD", "asset_class": "equity"}],
        "entries": [
            {"id": "e1", "account_id": "brk", "kind": "opening_balance", "date": "2026-03-31", "instrument_id": "X",
             "quantity": "100", "cost_basis": "900", "acquired_on": "2025-01-02", "currency": "USD",
             "confidence": "reported", "source": source},
            {"id": "e2", "account_id": "brk", "kind": "deposit", "date": "2026-05-01", "amount": "500",
             "currency": "USD", "confidence": "reported", "source": source},
        ],
        "fx": [], "assertions": [], "labels": [], "category_rules": [],
    }


def test_performance_matches_the_ledger_and_a_hand_computed_twr():
    ledger = _simple_ledger()
    prices = {"X": [{"date": "2026-03-31", "price": "10"}, {"date": "2026-05-01", "price": "11"},
                    {"date": "2026-06-30", "price": "12"}]}
    report = _run({"period_start": "2026-04-01", "period_end": "2026-06-30", "currency": "USD", "ledger": ledger,
                   "prices": prices})
    total = report["result"]["sections"]["performance"]["data"]["total"]
    # (1100 before the deposit / 1000) x (1700 / 1600) - 1
    assert total["twr_period"] == "0.168750"
    direct = performance(ledger, "2026-03-31", "2026-06-30", "USD", price_table(prices))
    assert total["xirr_annual"] == direct["result"]["xirr_annual"]
    assert total["start_value"] == "1000.00" and total["end_value"] == "1700.00"
    nw = report["result"]["sections"]["net_worth"]["data"]
    assert nw["contributions"]["other_flows"] == "500.00" and nw["growth"]["market"] == "200.00"


def test_benchmarks_are_weighted_by_ips_targets_and_60_40():
    report = _run(_mx())
    bench = report["result"]["sections"]["performance"]["data"]["benchmarks"]
    equity = Decimal("0.042")
    fixed = Decimal(str((1 + 0.075) ** (91 / 365) - 1))
    cash = Decimal(str((1 + 0.07) ** (91 / 365) - 1))
    expected = Decimal("0.6") * equity + Decimal("0.35") * fixed + Decimal("0.05") * cash
    assert abs(Decimal(bench["ips_benchmark"]["period_return"]) - expected) < Decimal("0.000001")
    assert bench["global_60_40"]["period_return"] == "0.028800"
    twr = Decimal(report["result"]["sections"]["performance"]["data"]["total"]["twr_period"])
    assert Decimal(bench["global_60_40"]["excess_twr"]) == twr - Decimal("0.0288")
    no_bench = _run(_mx(benchmarks={}))
    section = no_bench["result"]["sections"]["performance"]
    assert section["data"]["benchmarks"]["global_60_40"]["period_return"] is None
    assert any(m["key"].startswith("benchmarks.") for m in section["missing"])


# ------------------------------------------------------------------ cash flow, goals, dca, taxes


def test_cash_flow_compares_with_the_prior_quarter():
    data = _run(_mx())["result"]["sections"]["cash_flow"]["data"]
    assert data["current"]["savings_rate"] == "0.4500" and data["prior"]["savings_rate"] == "0.5000"
    assert data["change"]["spending"] == "9000.00"
    assert [t["category"] for t in data["top_categories"]] == ["housing", "groceries", "dining"]
    assert data["top_categories"][2]["change"] == "6000.00"


def test_goal_progress_dca_and_mexican_tax():
    sections = _run(_mx())["result"]["sections"]
    goal = sections["goals"]["data"]["goals"][0]
    assert goal["funded"] == "757678.00" and goal["funded_pct"] == "0.0947" and goal["status"] == "on_track"
    plan = sections["dca"]["data"]["plans"][0]
    assert plan["counts"] == {"on_time": 2, "skipped": 1} and plan["missed"] == ["2026-06-01"]
    taxes = sections["taxes"]["data"]
    assert taxes["period"]["gains"]["art129_10pct"] == "1000.00"
    assert taxes["period"]["estimated_tax"]["total"] == "100.00"
    unknown_listing = _run(_mx(sic_listed={}, tax={"jurisdiction": "MX"}))["result"]["sections"]["taxes"]
    assert unknown_listing["data"]["period"]["estimated_tax"]["total"] == "100.00"  # only a CETES sale this quarter


def test_foreign_sale_without_sic_status_is_unknown_not_zero():
    inputs = _mx()
    ledger = deepcopy(inputs["ledger"])
    ledger["entries"].append({"id": "sell-voo", "account_id": "ibkr", "kind": "sell", "date": "2026-06-20",
                              "amount": "2020", "currency": "USD", "instrument_id": "VOO", "quantity": "4",
                              "confidence": "reported", "source": {"kind": "document", "ref": "ibkr"}})
    ledger["fx"].append({"date": "2026-06-20", "base": "USD", "quote": "MXN", "rate": "18.05", "source": "fictional"})
    report = _run(_mx(ledger=ledger, sic_listed={}))
    taxes = report["result"]["sections"]["taxes"]
    assert taxes["data"]["period"]["estimated_tax"]["total"] is None
    assert any(m["key"] == "sic_listed.VOO" for m in taxes["missing"])


# ------------------------------------------------------------------ decision journal and history


def test_decision_journal_reports_what_was_decided_why_and_what_happened():
    snapshot = {"client": {"id": "c", "revision": 3}, "facts": [], "decisions": [
        {"id": "d1", "title": "Invertir 10,000 al mes en CSPXN", "rationale": "Plan de retiro; IPS balanceado.",
         "status": "accepted", "needs_review": False, "review_reasons": [],
         "created_at": "2026-03-30T10:00:00Z", "updated_at": "2026-04-01T09:00:00Z"},
        {"id": "d2", "title": "Vender VOO", "rationale": "Situs sucesorio.", "status": "proposed",
         "needs_review": True, "review_reasons": ["goals changed"],
         "created_at": "2026-05-20T10:00:00Z", "updated_at": "2026-05-20T10:00:00Z"},
        {"id": "d3", "title": "Old idea", "rationale": "x", "status": "proposed",
         "created_at": "2026-01-10T10:00:00Z", "updated_at": "2026-01-10T10:00:00Z"},
        {"id": "d4", "title": "Old and done", "rationale": "x", "status": "dismissed",
         "created_at": "2026-01-10T10:00:00Z", "updated_at": "2026-01-11T10:00:00Z"},
    ]}
    inputs = _mx()
    inputs.pop("facts")
    data = _run(inputs, snapshot)["result"]["sections"]["decisions"]["data"]
    rows = {d["id"]: d for d in data["decisions"]}
    assert set(rows) == {"d1", "d2"}
    assert rows["d1"]["why"].startswith("Plan de retiro") and rows["d1"]["decided_on"] == "2026-04-01"
    happened = rows["d1"]["what_happened"]
    assert happened["trades"] == 3 and happened["instruments"] == ["CETES", "CSPXN"]
    assert happened["net_invested"] == "-29700.00"  # two buys (10,000 + 10,300) minus the CETES sale (50,000)
    assert rows["d2"]["decided_on"] is None and rows["d2"]["needs_review"] is True
    assert data["counts"] == {"accepted": 1, "proposed": 1}
    assert data["still_open_from_before"] == [{"id": "d3", "title": "Old idea", "proposed_on": "2026-01-10"}]


def test_changes_timeline_uses_revision_history():
    history = {"income.salary": [
        {"key": "income.salary", "value": {"amount": 55000}, "revision": 1, "recorded_at": "2026-01-05T00:00:00Z",
         "valid_from": "2026-01-01", "status": "replaced", "source": {"kind": "user"}},
        {"key": "income.salary", "value": {"amount": 60000}, "revision": 4, "recorded_at": "2026-05-02T00:00:00Z",
         "valid_from": "2026-05-01", "status": "active", "source": {"kind": "user"}},
    ]}
    changes = _run(_mx(), history=history)["result"]["sections"]["changes"]["data"]
    assert changes["basis"] == "full revision history"
    assert changes["timeline"] == [{"date": "2026-05-01", "key": "income.salary", "action": "update",
                                    "from": '{"amount": 55000}', "to": '{"amount": 60000}',
                                    "valid_from": "2026-05-01", "source_kind": "user"}]


# ------------------------------------------------------------------ next quarter


def test_next_decisions_rank_reserve_first_then_policy_then_efficiency():
    report = _run(_mx())
    section = report["result"]["sections"]["next_quarter"]["data"]
    kinds = [c["kind"] for c in section["candidates"]]
    assert kinds == ["reserve", "drift", "harvest", "ppr_headroom", "dca"]
    assert [c["kind"] for c in section["two_decisions"]] == ["reserve", "drift"]
    assert [c["rank"] for c in section["candidates"]] == [1, 2, 3, 4, 5]
    ppr = next(c for c in section["candidates"] if c["kind"] == "ppr_headroom")
    assert ppr["data"]["room_mxn"] == "52000.00" and ppr["value"] == "15600.00" and ppr["tier"] == 4
    harvest = next(c for c in section["candidates"] if c["kind"] == "harvest")
    # 20 VOO at 505 USD x 18.00 = 181,800 MXN against 11,000 USD x 20.30 = 223,300 MXN of cost.
    assert harvest["data"]["total_loss"] == "41500.00" and harvest["value"] == "4150.00"
    assert report["result"]["narrative_inputs"]["next_quarter"][0]["kind"] == "reserve"


def test_year_end_deadline_lifts_ppr_above_policy_items():
    inputs = _mx(period_start="2026-07-01", period_end="2026-09-30")
    for series, price in (("CSPXN", "10600"), ("CETES", "10.18"), ("VOO", "510")):
        inputs["prices"][series] = inputs["prices"][series] + [{"date": "2026-09-30", "price": price}]
    inputs["ledger"] = deepcopy(inputs["ledger"])
    inputs["ledger"]["fx"].append({"date": "2026-09-30", "base": "USD", "quote": "MXN", "rate": "18.1", "source": "fictional"})
    inputs["benchmarks"] = {}
    kinds = [c["kind"] for c in _run(inputs)["result"]["sections"]["next_quarter"]["data"]["candidates"]]
    assert kinds[:3] == ["reserve", "ppr_headroom", "drift"]


def test_no_reserve_gap_means_no_reserve_candidate():
    inputs = _mx()
    inputs["facts"] = [f if f["key"] != "cash.nu" else
                       {"key": "cash.nu", "value": {"amount": 400000, "currency": "MXN", "purpose": "reserve",
                                                    "institution": "Nu"}} for f in inputs["facts"]]
    top = _run(inputs)["result"]["sections"]["next_quarter"]["data"]["two_decisions"]
    assert [c["kind"] for c in top] == ["drift", "harvest"]


# ------------------------------------------------------------------ fee audit


def test_parse_rate_units_and_ambiguity():
    assert review.parse_rate({"value": "0.07", "unit": "percent", "source": "s"}, "f")[0] == Decimal("0.0007")
    assert review.parse_rate({"value": "7", "unit": "bps", "source": "s"}, "f")[0] == Decimal("0.0007")
    assert review.parse_rate("0.0007", "f", "s")[0] == Decimal("0.0007")
    rate, _, problem = review.parse_rate("0.2", "f", "s")
    assert rate is None and problem["reason"] == "ambiguous"
    rate, _, problem = review.parse_rate("0.0007", "f")
    assert rate is None and problem["reason"] == "unsourced"
    with pytest.raises(ValueError):
        review.parse_rate({"value": "150", "unit": "percent", "source": "s"}, "f")


def test_afore_commission_is_dated_and_fails_closed():
    rate, source, problem = review.afore_rate("Afore XXI Banorte", 2026)
    assert rate == Decimal("0.0054") and problem is None and source["checked_on"] == "2026-09-21"
    assert review.afore_rate("PensionISSSTE", 2026)[0] == Decimal("0.0052")
    assert review.afore_rate("Afore Inventada", 2026)[0] is None
    assert review.afore_rate("Profuturo", 2025)[2]["key"] == "afore.commission@2025"


def test_fee_maths_bps_top_sources_and_compounding():
    report = review.run_task("fee_audit", CATALOG["fee_audit"]["variants"]["holdings_only"],
                             {"facts": []}, None, "2026-09-21")
    result = report["result"]
    # 0.45% x 100,000 + 1% x 105,000 advisory; idle cash 5,000 x 0..4.5%
    assert result["annual_cost"]["low"] == "1500.00" and result["annual_cost"]["high"] == "1725.00"
    assert result["annual_cost"]["bps_low"] == "142.9"
    assert [t["id"] for t in result["top_sources"]] == ["advisory", "fund_expenses", "cash_drag"]
    equivalent = result["cheaper_equivalents"][0]
    assert equivalent["equivalent"] == "VTI" and equivalent["annual_difference"] == "420.00"
    gap = result["compounding"]["fee_gap"]
    g, base = 420 / 105000, 105000
    low = base * (1.04 ** 10) - base * ((1.04 - g) ** 10)
    high = base * (1.07 ** 10) - base * ((1.07 - g) ** 10)
    assert Decimal(gap["10y"]["low"]) == Decimal(str(low)).quantize(Decimal("0.01"))
    assert Decimal(gap["10y"]["high"]) == Decimal(str(high)).quantize(Decimal("0.01"))
    assert Decimal(gap["20y"]["high"]) > Decimal(gap["10y"]["high"])
    assert result["annual_cost"]["complete"] is False  # no ledger: commissions unknown, not zero
    assert any(m["key"] == "ledger" for m in report["missing"])


def test_fee_audit_from_ledger_counts_commission_iva_afore_and_flags_estate_situs():
    report = review.run_task("fee_audit", CATALOG["fee_audit"]["example"], {"facts": []}, None, "2026-09-21")
    components = {c["id"]: c for c in report["result"]["components"]}
    assert components["trading"]["commission"] == "50.00" and components["trading"]["iva"] == "8.00"
    assert components["afore"]["annual_low"] == "2430.00"
    situs = [s for s in report["result"]["cheaper_equivalents"] if s.get("kind") == "estate_situs"]
    assert situs and situs[0]["holding"] == "VOO" and "US-situs" in situs[0]["note"]
    assert report["result"]["compounding"]["fee_gap"] is None and "unknown, not zero" in \
        report["result"]["compounding"]["fee_gap_note"]
    # Assessed value includes the AFORE balance its commission is charged on.
    assert report["result"]["assessed_value"] == "1207678.00"


def test_unsourced_or_ambiguous_expense_ratio_is_unknown_not_zero():
    inputs = deepcopy(CATALOG["fee_audit"]["variants"]["holdings_only"])
    inputs["instruments"]["FUNDX"]["expense_ratio"] = "0.45"  # percent or decimal? unknown
    report = review.run_task("fee_audit", inputs, {"facts": []}, None, "2026-09-21")
    fund = next(c for c in report["result"]["components"] if c["id"] == "fund_expenses")
    assert fund["complete"] is False and fund["detail"][0]["annual_cost"] is None
    assert report["result"]["annual_cost"]["complete"] is False
    assert report["result"]["compounding"]["fee_gap"] is None


def test_afore_with_unknown_name_fails_closed():
    inputs = deepcopy(CATALOG["fee_audit"]["variants"]["holdings_only"])
    inputs["afore"] = [{"name": "Mi Afore", "balance": 300000}]
    report = review.run_task("fee_audit", inputs, {"facts": []}, None, "2026-09-21")
    afore = next(c for c in report["result"]["components"] if c["id"] == "afore")
    assert afore["complete"] is False and afore["detail"][0]["annual_cost"] is None
    assert any(m["key"] == "afore.name" for m in report["missing"])


# ------------------------------------------------------------------ service


def test_service_runs_both_tasks_for_a_client_with_history(tmp_path):
    service = WealthService(tmp_path / "review.sqlite3")
    service.create("ana", "Ana")
    source = {"kind": "user", "ref": "chat", "observed_on": "2026-09-10"}
    service.remember("ana", [
        {"key": "client.profile", "value": {"residence": {"country": "MX"}, "tax_residence": ["MX"]}, "source": source},
        {"key": "income.salary", "value": {"amount": 55000, "currency": "MXN", "frequency": "monthly", "kind": "salary"},
         "source": source, "valid_from": "2026-01-01"},
    ])
    revision = service.situation("ana")["revision"]
    service.remember("ana", [
        {"key": "income.salary", "value": {"amount": 60000, "currency": "MXN", "frequency": "monthly", "kind": "salary"},
         "source": {"kind": "user", "ref": "chat", "observed_on": "2026-09-15"}, "valid_from": "2026-05-01"},
    ], expected_revision=revision)
    inputs = _mx()
    inputs.pop("facts")
    report = service.run("quarterly_review", inputs=inputs, client_id="ana")
    assert report["status"] in {"ready", "partial"}
    timeline = report["result"]["sections"]["changes"]["data"]["timeline"]
    assert [(t["key"], t["action"], t["date"]) for t in timeline] == [("income.salary", "update", "2026-05-01")]
    assert report["evidence_ids"]
    audit = service.run("fee_audit", inputs=CATALOG["fee_audit"]["example"], client_id="ana")
    assert audit["status"] in {"ready", "partial"} and audit["result"]["annual_cost"]["low"]


def test_missing_period_or_ledger_needs_input():
    assert _run({})["status"] == "needs_input"
    assert _run({"period_start": "2026-04-01", "period_end": "2026-06-30", "ledger": {"accounts": []}})["status"] == "needs_input"
    with pytest.raises(ValueError):
        _run({"period_start": "2026-04-01", "period_end": "2026-06-30", "bogus": 1})
