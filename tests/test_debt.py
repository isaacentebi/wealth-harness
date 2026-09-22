"""The debt engine (task ``debt``): amortization, prepay vs invest, refinance offers, strategies, views, proactive."""
from __future__ import annotations

import copy
from decimal import Decimal

import pytest

from wealth import proactive, views
from wealth.catalog import CATALOG
from wealth.debt import effective_annual
from wealth.service import WealthService
from wealth.situation.model import build

AS_OF = "2026-09-21"
EXAMPLES = CATALOG["debt"]["variants"]


@pytest.fixture()
def service(tmp_path):
    return WealthService(tmp_path / "debt.sqlite3")


def run(service, inputs, **kw):
    report = service.run("debt", inputs, **kw)
    assert report["status"] in {"ready", "partial"}, (report["status"], report["missing"])
    return report


def card(**extra):
    return {"id": "tarjeta", "kind": "card", "balance": 30000, "annual_rate": 0.45, "monthly_payment": 2500,
            "currency": "MXN", **extra}


def _leaves(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from _leaves(item)
    elif isinstance(value, list):
        for item in value:
            yield from _leaves(item)
    elif not isinstance(value, bool) and value is not None:
        yield value


def _spec_numbers(spec):
    data, kind = spec["data"], spec["kind"]
    raw = lambda v: [] if v["t"] in {"text", "date"} else [v["lo"], v["hi"]] if v["t"] == "range" else [v["v"]]  # noqa: E731
    if kind == "ticket":
        return [n for row in data["rows"] + ([data["total"]] if data["total"] else []) for n in raw(row["value"])]
    if kind == "series":
        return [p["y"] for p in data["points"]]
    if kind == "comparison":
        return [n for m in data["metrics"] for v in m["values"] for n in raw(v)]
    return []


# ------------------------------------------------------------------ amortize


def test_mx_card_at_60_percent_cat_amortizes_on_the_tasa_plus_iva(service):
    report = run(service, CATALOG["debt"]["example"])
    [debt] = report["result"]["debts"]
    # 45% tasa: 3.75% a month plus 16% IVA = 4.35% a month, 66.7% a year compounded; the CAT (60%) is not the rate.
    assert debt["effective_annual_rate"] == pytest.approx(float(effective_annual(Decimal("0.0435"))), abs=1e-6)
    assert debt["monthly"][0] == {"month": 1, "date": "2026-10", "payment": 2500, "interest": 1125, "iva": 180,
                                  "principal": 1195, "balance": 28805}
    assert debt["months"] == 18 and debt["payoff_date"] == "2028-03"
    assert debt["interest_cost"] == pytest.approx(debt["total_interest"] + debt["total_iva"], abs=0.01)
    assert sum(y["principal"] for y in debt["yearly"]) == pytest.approx(30000, abs=0.05)
    assert debt["yearly"][-1]["end_balance"] == 0 and len(debt["yearly"]) == 2
    assert "CAT 60% vs tasa 45%" in debt["cat_vs_tasa"] and "leaves out IVA" in debt["cat_vs_tasa"]
    # Banxico's minimum: 1.5% of the balance plus interest and IVA, never below 1.25% of the 60,000 limit.
    assert debt["minimum_rule"]["floor"] == 750
    minimum = debt["if_minimum_only"]
    assert minimum["months"] > 60 and minimum["extra_cost"] > 40000
    assert {s["title"] for s in report["sources"]} >= {"Banco de Mexico, Costo Anual Total (CAT)",
                                                       "Banco de Mexico, Circular 13/2011 (pago minimo de tarjetas de credito)"}
    assert any("IVA of 16%" in a for a in report["assumptions"])


def test_cat_alone_is_an_estimate_at_its_monthly_equivalent(service):
    report = run(service, EXAMPLES["mx_card_cat_only"])
    [debt] = report["result"]["debts"]
    assert debt["rate_basis"] == "cat_estimate" and debt["estimate"] is True
    assert debt["monthly"][0]["interest"] == pytest.approx(30000 * (1.6 ** (1 / 12) - 1), abs=0.01)
    assert any("only the CAT is known" in a for a in report["assumptions"])


def test_fixed_rate_mortgage_from_the_remaining_term_matches_the_annuity(service):
    loan = {"id": "home", "kind": "mortgage", "balance": 200000, "annual_rate": 0.06, "remaining_term_months": 360,
            "currency": "USD"}
    [debt] = run(service, {"mode": "amortize", "as_of": AS_OF, "liabilities": [loan], "monthly_rows": "all"})["result"]["debts"]
    assert debt["monthly_payment"] == pytest.approx(1199.10, abs=0.01) and debt["payment_basis"] == "from remaining term"
    assert debt["months"] == 360 and debt["total_interest"] == pytest.approx(231676.38, abs=0.5)
    assert len(debt["monthly"]) == 360 and debt["monthly_rows_omitted"] == 0 and len(debt["yearly"]) == 30
    assert debt["total_iva"] is None and "escrow" in debt["note"]


def test_a_card_without_a_payment_or_rule_is_missing_never_zero(service):
    report = service.run("debt", {"mode": "amortize", "liabilities": [{"id": "c", "kind": "card", "balance": 5000,
                                                                        "annual_rate": 0.3, "currency": "MXN"}]})
    assert report["status"] == "needs_input" and report["missing"][0]["key"] == "liability.c.monthly_payment"
    rule = service.run("debt", {"mode": "amortize", "liabilities": [{"id": "c", "kind": "card", "balance": 5000,
                                "annual_rate": 0.3, "currency": "USD", "minimum_payment": {"percent_of_balance": 0.01}}]})
    assert rule["status"] == "needs_input" and rule["missing"][0]["key"] == "liability.c.minimum_payment.floor"


def test_infonavit_in_uma_is_indexed_every_february_and_marked_an_estimate(service):
    report = run(service, EXAMPLES["infonavit_uma"])
    [debt] = report["result"]["debts"]
    assert debt["estimate"] is True and debt["denomination"] == "UMA"
    assert debt["unit_value_mxn"] == pytest.approx(117.31 * 30.4, abs=0.01)  # INEGI 2026, monthly = daily x 30.4
    assert "INEGI" in debt["unit_value_source"]
    february = next(r for r in debt["monthly"] if r["date"] == "2027-02")
    january = next(r for r in debt["monthly"] if r["date"] == "2027-01")
    assert february["unit_value"] == pytest.approx(january["unit_value"] * 1.04, abs=0.01)
    assert debt["indexation"] > 0 and debt["months"] and debt["status"] == "ready"
    assert any("an estimate" in w for w in report["warnings"])
    assert any(a.startswith("infonavit: the UMA is assumed to rise 4") for a in report["assumptions"])


def test_infonavit_balance_left_after_30_years_is_cancelled(service):
    credit = {"id": "infonavit", "kind": "infonavit", "denomination": "VSM", "balance_units": 200, "unit_value_mxn": 9500,
              "annual_rate": 0.08, "monthly_payment_units": 1.4, "months_paid": 300, "unit_growth_annual": 0.05}
    [debt] = run(service, {"mode": "amortize", "as_of": AS_OF, "liabilities": [credit]})["result"]["debts"]
    assert debt["months"] == 60 and debt["forgiven_at_30_years"] > 0
    missing = service.run("debt", {"mode": "amortize", "liabilities": [{**credit, "unit_value_mxn": None}]})
    assert missing["status"] == "needs_input"
    assert "liability.infonavit.unit_value_mxn" in [m["key"] for m in missing["missing"]]


# ------------------------------------------------------------------ prepay vs invest


def test_us_mortgage_at_6_5_vs_vti_without_itemizing_is_prepay(service):
    report = run(service, EXAMPLES["us_mortgage_prepay_vs_vti"])
    result = report["result"]
    assert result["guaranteed"]["certainty"] == "guaranteed" and result["investing"]["certainty"] == "uncertain"
    assert result["guaranteed"]["after_tax_rate"] == pytest.approx(0.067, abs=1e-4)  # 6.5% monthly, no deduction
    assert result["verdict"] == "prepay" and result["confidence"] == "high" and result["prepay_recommended"]
    assert 0.065 < result["breakeven"]["pre_tax_return"] < 0.08
    base = result["scenarios"]["base"]
    assert base["net_worth_difference"] == pytest.approx(base["net_worth_if_prepay"] - base["net_worth_if_invest"], abs=0.02)
    assert base["debt_paid_off_month_if_prepay"] < base["debt_paid_off_month_if_invest"]
    # T-bill interest is taxed federally: 4% x (1 - 24%).
    assert result["investing"]["after_tax"]["risk_free"] == pytest.approx(0.0304, abs=1e-4)


def test_itemizing_lowers_the_mortgage_rate_and_an_unknown_answer_is_a_range(service):
    inputs = copy.deepcopy(EXAMPLES["us_mortgage_prepay_vs_vti"])
    itemized = run(service, {**inputs, "itemizes": True})["result"]
    assert itemized["guaranteed"]["after_tax_rate"] == pytest.approx(float(effective_annual(Decimal("0.065") * Decimal("0.76") / 12)), abs=1e-4)
    assert itemized["verdict"] in {"close_call", "invest"}
    unknown = service.run("debt", {k: v for k, v in inputs.items() if k != "itemizes"})
    assert unknown["status"] == "partial" and "itemizes" in [m["key"] for m in unknown["missing"]]
    low, high = unknown["result"]["guaranteed"]["after_tax_rate_range"]
    assert low < high and len(unknown["result"]["cases"]) == 2
    assert unknown["result"]["verdict"] == "depends" and unknown["result"]["decided_by"] == ["itemizes"]


def test_breakeven_return_makes_both_paths_equal(service):
    inputs = copy.deepcopy(EXAMPLES["us_mortgage_prepay_vs_vti"])
    first = run(service, inputs)["result"]["breakeven"]["pre_tax_return"]
    at = run(service, {**inputs, "expected_return": {"conservative": first, "base": first, "source": "breakeven"}})
    assert abs(at["result"]["scenarios"]["base"]["net_worth_difference"]) < 200  # of ~300k: the return is rounded


def test_mx_card_vs_cetes_is_prepay_and_the_reserve_comes_first(service):
    result = run(service, EXAMPLES["mx_card_prepay_vs_cetes"])["result"]
    assert result["verdict"] == "prepay" and result["confidence"] == "high"
    assert result["investing"]["risk_free"]["name"] == "CETES 28 days"
    # CETES: real interest taxed at the marginal rate, 6.25% - 30% x (6.25% - 4%).
    assert result["investing"]["after_tax"]["risk_free"] == pytest.approx(0.05575, abs=1e-4)
    assert result["investing"]["gains_tax"] == 0.1  # Art. 129
    short = run(service, {**EXAMPLES["mx_card_prepay_vs_cetes"], "reserve": {"months": 1, "target_months": 3}})
    assert short["result"]["verdict"] == "build_reserve_first" and short["result"]["prepay_recommended"] is False
    assert any("reserve is below its target" in w for w in short["warnings"])
    unknown = run(service, {k: v for k, v in EXAMPLES["mx_card_prepay_vs_cetes"].items() if k != "reserve"})
    assert unknown["result"]["condition"] and "reserve" in [m["key"] for m in unknown["missing"]]


def test_mx_mortgage_reuses_the_real_interest_deduction(service):
    inputs = {"mode": "prepay_vs_invest", "as_of": AS_OF, "extra_monthly": 5000, "marginal_rate": 0.30,
              "inflation": 0.04, "reserve": {"months": 6, "target_months": 6},
              "debt": {"id": "hipoteca", "kind": "mortgage", "balance": 2000000, "annual_rate": 0.11,
                       "monthly_payment": 22000, "currency": "MXN"}}
    plain = run(service, inputs)["result"]
    deducted = run(service, {**inputs, "mx_mortgage": {"casa_habitacion": True, "financial_system_lender": True,
                                                        "credit_udis": 300000, "within_global_cap": True}})["result"]
    # Only real interest (11% - 4%) is deductible: 7/11 of the interest, at 30%.
    assert deducted["cases"][0]["deduction_share"] == pytest.approx(7 / 11, abs=1e-4)
    monthly = Decimal("0.11") / 12 * (1 - Decimal("0.30") * Decimal(7) / Decimal(11))
    assert deducted["guaranteed"]["after_tax_rate"] == pytest.approx(float(effective_annual(monthly)), abs=1e-4)
    assert plain["guaranteed"]["after_tax_rate_range"] is not None  # unconfirmed deduction: both cases
    assert "Art. 151" in deducted["guaranteed"]["tax"]


def test_us_without_a_t_bill_rate_asks_for_it(service):
    inputs = {k: v for k, v in EXAMPLES["us_mortgage_prepay_vs_vti"].items() if k != "risk_free"}
    report = service.run("debt", inputs)
    assert report["status"] == "partial" and "risk_free" in [m["key"] for m in report["missing"]]
    assert report["result"]["scenarios"]["risk_free"]["net_worth_difference"] is None


def test_lump_sum_prepays_at_once(service):
    result = run(service, {**EXAMPLES["mx_card_prepay_vs_cetes"], "extra_monthly": 0, "lump_sum": 20000})["result"]
    assert result["scenarios"]["base"]["debt_paid_off_month_if_prepay"] < result["debt"]["months_left_at_current_payment"]


# ------------------------------------------------------------------ refinance


def test_car_loan_refinance_saves_interest_after_fees_and_breaks_even(service):
    result = run(service, EXAMPLES["car_loan_refinance"])["result"]
    assert result["interest_saved"] == pytest.approx(result["current"]["interest"] - result["offer"]["total_cost"], abs=0.02)
    assert result["interest_saved"] > 0 and result["worth_it"] is True
    assert 1 <= result["breakeven_month"] <= 12 and result["offer"]["months"] == 48


def test_balance_transfer_promo_ending_before_payoff_is_the_risk(service):
    report = run(service, {**EXAMPLES["card_balance_transfer"],
                           "offer": {**EXAMPLES["card_balance_transfer"]["offer"], "deferred_interest": True}})
    risk = report["result"]["risk"]
    assert report["result"]["fees"] == 240 and report["result"]["offer"]["starting_balance"] == 8240
    assert risk["promo_ends_before_payoff"] is True and risk["balance_at_promo_end"] == 3740
    assert risk["payment_to_clear_within_promo"] == pytest.approx(8240 / 15, abs=0.01)
    assert risk["interest_after_promo"] > 0 and risk["deferred_interest_if_not_cleared"] > 0
    assert any("promo ends in month 15" in w for w in report["warnings"])
    assert any("Deferred interest" in w for w in report["warnings"])


def test_a_worse_offer_never_breaks_even(service):
    result = run(service, {"mode": "refinance", "as_of": AS_OF, "liabilities": EXAMPLES["car_loan_refinance"]["liabilities"],
                           "offer": {"annual_rate": 0.12, "fee": 500}})["result"]
    assert result["interest_saved"] < 0 and result["breakeven_month"] is None and result["worth_it"] is False


# ------------------------------------------------------------------ strategies


def test_avalanche_snowball_hybrid_with_the_difference_in_money(service):
    result = run(service, EXAMPLES["strategies"])["result"]
    rows = {r["strategy"]: r for r in result["strategies"]}
    assert result["best"] == "avalanche"
    assert rows["avalanche"]["interest"] <= min(rows["snowball"]["interest"], rows["hybrid"]["interest"])
    assert result["interest_difference"]["snowball_minus_avalanche"] == pytest.approx(
        rows["snowball"]["interest"] - rows["avalanche"]["interest"], abs=0.01)
    assert rows["snowball"]["order"][0] == "personal" and rows["avalanche"]["order"][0] == "tarjeta"
    assert result["first_debt_cleared_months"]["snowball"] < result["first_debt_cleared_months"]["avalanche"]
    for series in result["balance_series"].values():
        assert series[0]["y"] == 121000 and series[-1]["y"] == 0
    assert result["minimums_only"]["interest_saved_by_budget"] > 0


def test_a_budget_below_the_minimums_asks_for_more(service):
    report = service.run("debt", {**EXAMPLES["strategies"], "monthly_amount": 1000})
    assert report["status"] == "needs_input" and report["missing"][0]["key"] == "monthly_amount"


# ------------------------------------------------------------------ stored debts, views


def test_stored_debts_their_extras_and_the_reserve_come_from_the_picture(service):
    service.create("ana", "Ana")
    source = {"kind": "user", "ref": "chat", "observed_on": AS_OF}
    facts = {"client.profile": {"residence": {"country": "MX"}, "tax_residence": ["MX"]},
             "income.salary": {"amount": 85000, "currency": "MXN", "frequency": "monthly", "net": True},
             "spending.monthly": {"total": 45000, "essential": 30000, "currency": "MXN"},
             "cash.nu": {"amount": 60000, "currency": "MXN", "institution": "Nu"}, "reserve": {"target_months": 3},
             "liability.tarjeta": {"kind": "card", "balance": 30000, "annual_rate": 0.45, "cat": 0.6, "currency": "MXN",
                                   "payment": 2500, "payment_frequency": "monthly", "lender": "BBVA"},
             "liability.infonavit": {"kind": "mortgage", "lender": "Infonavit", "denomination": "UMA", "balance": 600000,
                                     "annual_rate": 0.09, "payment": 6500, "payment_frequency": "monthly", "currency": "MXN"}}
    service.remember("ana", [{"key": k, "value": v, "source": source, "confidence": "reported"} for k, v in facts.items()], 0)
    report = run(service, {"mode": "amortize", "as_of": AS_OF}, client_id="ana")
    debts = {d["id"]: d for d in report["result"]["debts"]}
    assert debts["tarjeta"]["cat"] == 0.6 and debts["infonavit"]["kind"] == "infonavit" and debts["infonavit"]["estimate"]
    assert len(report["evidence_ids"]) >= 2
    prepay = run(service, {"mode": "prepay_vs_invest", "as_of": AS_OF, "debt": "tarjeta", "extra_monthly": 2000,
                           "marginal_rate": 0.3}, client_id="ana")
    # 60,000 of cash against 30,000 of essentials a month: 2 of 3 months, so the reserve comes first.
    assert prepay["result"]["reserve"]["status"] == "below_target"
    assert prepay["result"]["verdict"] == "build_reserve_first" and prepay["result"]["jurisdiction"] == "MX"


@pytest.mark.parametrize("name", ["example", *EXAMPLES])
def test_every_mode_draws_views_from_envelope_numbers_only(service, name):
    inputs = CATALOG["debt"]["example"] if name == "example" else EXAMPLES[name]
    report = run(service, inputs)
    specs = views.views_for("debt", report)
    assert len(specs) == 2 and [s["id"] for s in specs] == [v["id"] for v in report["views"]]
    leaves = {str(v) for v in _leaves(report)}
    for spec in specs:
        views.validate(spec)
        assert all(str(n) in leaves for n in _spec_numbers(spec) if n is not None), spec["id"]
        for lang in ("en", "es"):
            assert views.render_svg(spec, lang).endswith("</svg>")
    kinds = {s["kind"] for s in specs}
    expected = {"amortize": {"series", "ticket"}, "prepay_vs_invest": {"ticket", "comparison"},
                "refinance": {"ticket", "comparison"}, "strategies": {"comparison", "series"}}
    assert kinds == expected[inputs["mode"]]


# ------------------------------------------------------------------ proactive


def _said(key, value):
    return {"key": key, "value": value, "status": "active", "confidence": "reported", "id": f"id-{key}",
            "source": {"kind": "user", "ref": "chat", "observed_on": "2026-09-10"}}


def _picture(*extra):
    base = [_said("client.profile", {"reporting_currency": "MXN"}),
            _said("income.salary", {"amount": 85000, "currency": "MXN", "frequency": "monthly", "net": True}),
            _said("spending.monthly", {"total": 45000, "essential": 30000, "currency": "MXN"}),
            _said("liability.card", {"kind": "card", "balance": 18000, "annual_rate": 0.42, "currency": "MXN",
                                     "payment": 1200, "payment_frequency": "monthly"})]
    return {"client": {"revision": 1}, "facts": [*base, *extra]}


def test_high_interest_item_says_interest_saved_per_month_sooner():
    found = proactive.evaluate(build(_picture(), None, AS_OF), None, {"facts": []}, AS_OF)
    item = next(i for i in found["candidates"] if i["kind"] == "high_interest_debt")
    data = item["data"]
    assert data["months_sooner"] > 0
    assert data["interest_saved_per_month_sooner"] == pytest.approx(data["interest_saved"] / data["months_sooner"], abs=0.01)
    assert "for each of the" in item["why"]["en"] and "por cada uno de los" in item["why"]["es"]


def test_high_interest_item_is_silent_while_the_reserve_is_below_target():
    short = _picture(_said("cash.nu", {"amount": 30000, "currency": "MXN", "institution": "Nu"}),
                     _said("reserve", {"target_months": 3}))
    found = proactive.evaluate(build(short, None, AS_OF), None, {"facts": []}, AS_OF)
    assert "high_interest_debt" not in {i["kind"] for i in found["candidates"]}
    assert "reserve_low" in {i["kind"] for i in found["candidates"]}
    full = _picture(_said("cash.nu", {"amount": 120000, "currency": "MXN", "institution": "Nu"}),
                    _said("reserve", {"target_months": 3}))
    found = proactive.evaluate(build(full, None, AS_OF), None, {"facts": []}, AS_OF)
    assert "high_interest_debt" in {i["kind"] for i in found["candidates"]}
