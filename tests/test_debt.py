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


def test_mx_card_vs_cetes_is_prepay(service):
    result = run(service, EXAMPLES["mx_card_prepay_vs_cetes"])["result"]
    assert result["verdict"] == "prepay" and result["confidence"] == "high"
    assert result["investing"]["risk_free"]["name"] == "CETES 28 days"
    # CETES: real interest taxed at the marginal rate, 6.25% - 30% x (6.25% - 4%).
    assert result["investing"]["after_tax"]["risk_free"] == pytest.approx(0.05575, abs=1e-4)
    assert result["investing"]["gains_tax"] == 0.1  # Art. 129


def test_a_20_percent_card_with_a_starter_reserve_is_paid_first_and_the_reserve_continues(service):
    between = run(service, {**EXAMPLES["mx_card_prepay_vs_cetes"], "reserve": {"months": 1.5, "target_months": 3}})
    result = between["result"]
    assert result["verdict"] == "prepay" and result["prepay_recommended"] is True
    assert result["reserve"] == {"status": "below_target", "in_parallel": True, "rule": "starter (one month)",
                                 "months": 1.5, "target_months": 3}
    assert "in parallel" in result["verdict_text"]["en"] and "en paralelo" in result["verdict_text"]["es"]
    assert any("keep building the reserve" in w for w in between["warnings"])


def test_a_20_percent_card_below_one_month_of_reserve_waits_for_the_starter(service):
    short = run(service, {**EXAMPLES["mx_card_prepay_vs_cetes"], "reserve": {"months": 0.5, "target_months": 3}})
    result = short["result"]
    assert result["reserve"]["status"] == "below_starter"
    assert result["verdict"] == "build_reserve_first" and result["prepay_recommended"] is False
    assert "después de juntar un mes de reserva, esta deuda es tu mejor inversión" in result["verdict_text"]["es"]
    unknown = run(service, {k: v for k, v in EXAMPLES["mx_card_prepay_vs_cetes"].items() if k != "reserve"})
    assert "one month of essentials" in unknown["result"]["condition"]
    assert "reserve" in [m["key"] for m in unknown["missing"]]


def test_low_rate_debt_keeps_the_full_reserve_first_rule(service):
    car = {"id": "car", "kind": "auto", "balance": 85000, "annual_rate": 0.14, "monthly_payment": 3200, "currency": "MXN"}
    inputs = {"mode": "prepay_vs_invest", "as_of": AS_OF, "debt": car, "extra_monthly": 2000, "marginal_rate": 0.3}
    short = run(service, {**inputs, "reserve": {"months": 2, "target_months": 3}})["result"]
    assert short["verdict"] == "build_reserve_first" and short["reserve"]["rule"] == "full target"
    mortgage = run(service, {**EXAMPLES["us_mortgage_prepay_vs_vti"], "reserve": {"months": 5, "target_months": 6}})
    assert mortgage["result"]["verdict"] == "build_reserve_first"
    assert any("below its target" in w for w in mortgage["warnings"])
    full = run(service, {**inputs, "reserve": {"months": 3, "target_months": 3}})["result"]
    assert full["verdict"] != "build_reserve_first"


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
    # 60,000 of cash against 30,000 of essentials a month: 2 of 3 months, past the one-month starter reserve.
    assert prepay["result"]["reserve"]["status"] == "below_target" and prepay["result"]["reserve"]["in_parallel"]
    assert prepay["result"]["verdict"] == "prepay" and prepay["result"]["jurisdiction"] == "MX"


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


def _found(*extra, card=None):
    picture = _picture(*extra)
    if card is not None:
        picture["facts"] = [f for f in picture["facts"] if f["key"] != "liability.card"] + [_said("liability.card", card)]
    return proactive.evaluate(build(picture, None, AS_OF), None, {"facts": []}, AS_OF)


def _kinds(found):
    return {i["kind"]: i for i in found["candidates"]}


def _cash(amount):
    return _said("cash.nu", {"amount": amount, "currency": "MXN", "institution": "Nu"})


def test_a_growing_balance_is_always_shown_with_the_payment_that_stops_it():
    growing = {"kind": "card", "balance": 40000, "annual_rate": 0.60, "currency": "MXN", "payment": 1500,
               "payment_frequency": "monthly"}
    # No reserve at all: still shown, because 60% + IVA on 40,000 is 2,320 a month and 1,500 does not cover it.
    item = _kinds(_found(_cash(5000), _said("reserve", {"target_months": 3}), card=growing))["high_interest_debt"]
    assert item["severity"] == "act" and item["data"]["balance_growing"] is True
    assert item["data"]["payment_to_stop_growth"] == pytest.approx(40000 * 0.05 * 1.16, abs=0.01)
    assert item["title"]["es"].startswith("Tu tarjeta crece cada mes") and "$2,320" in item["why"]["es"]
    assert "stops the growth" in item["why"]["en"]


def test_a_20_percent_card_waits_only_below_one_month_of_reserve():
    # 15,000 of cash against 30,000 of essentials: half a month, below the starter reserve.
    found = _kinds(_found(_cash(15000), _said("reserve", {"target_months": 3})))
    assert "high_interest_debt" not in found
    reserve = found["reserve_low"]
    assert "Después de juntar un mes de reserva, esta tarjeta es tu mejor inversión." in reserve["why"]["es"]
    assert reserve["data"]["debt_after_starter_reserve"] == ["card"]
    # With no reserve target there is no reserve item to carry it: the card is shown, worded the same way.
    alone = _kinds(_found(_cash(15000)))["high_interest_debt"]
    assert alone["why"]["es"] == "Después de juntar un mes de reserva, esta tarjeta es tu mejor inversión."


def test_a_20_percent_card_with_a_starter_reserve_comes_first_and_the_reserve_continues():
    # 60,000 against 30,000 of essentials: 2 of 3 months, so the card comes first and the reserve keeps growing.
    found = _kinds(_found(_cash(60000), _said("reserve", {"target_months": 3})))
    item = found["high_interest_debt"]
    assert item["data"]["reserve_in_parallel"] is True and "en paralelo" in item["why"]["es"]
    assert "reserve_low" in found
    full = _kinds(_found(_cash(120000), _said("reserve", {"target_months": 3})))["high_interest_debt"]
    assert full["data"]["reserve_in_parallel"] is False and "en paralelo" not in full["why"]["es"]


def test_advice_to_prepay_low_rate_debt_keeps_the_full_reserve_rule():
    from datetime import date
    car = _said("liability.car", {"kind": "auto", "balance": 85000, "annual_rate": 0.13, "currency": "MXN",
                                  "payment": 3200, "payment_frequency": "monthly"})
    picture = _picture(_cash(60000), _said("reserve", {"target_months": 3}), car)
    sit = build(picture, None, AS_OF)
    run_ = proactive._Run(sit, None, picture, date.fromisoformat(AS_OF), {"MX"})
    assert proactive._advice_target(run_, {"text": "Pon el extra al coche", "related": ["liability.car"]}) is None
    assert proactive._advice_target(run_, {"text": "Pon el extra a la tarjeta", "related": ["liability.card"]}) == \
        ("to the card", "a la tarjeta")


# ------------------------------------------------------------------ audit fixes


def test_proactive_high_interest_uses_the_engine_with_iva():
    from datetime import date
    from wealth import debt
    found = proactive.evaluate(build(_picture(_cash(120000), _said("reserve", {"target_months": 3})), None, AS_OF),
                               None, {"facts": []}, AS_OF)
    data = next(i for i in found["candidates"] if i["kind"] == "high_interest_debt")["data"]
    # 18,000 at 42% + IVA: 1,925 a month clears it in 12 months; 1,200 takes 24 months and 10,321 of interest and IVA.
    assert round(data["monthly_to_clear"]) == 1925
    assert data["months_at_current_payment"] == 24 and data["interest_at_current_payment"] == pytest.approx(10320.54, abs=0.01)
    assert round(data["interest_saved"]) == 5225 and data["months_sooner"] == 12
    engine = debt.run({"mode": "amortize", "monthly_rows": 0},
                      [{"id": "card", "kind": "card", "balance": 18000, "annual_rate": 0.42,
                        "monthly_payment": data["monthly_to_clear"], "currency": "MXN"}],
                      date.fromisoformat(AS_OF))["result"]["debts"][0]
    assert engine["months"] == 12 and engine["interest_cost"] == pytest.approx(data["interest_if_cleared"], abs=0.01)


def test_high_interest_is_decided_on_the_effective_rate_with_iva():
    card18 = {"kind": "card", "balance": 18000, "annual_rate": 0.18, "currency": "MXN", "payment": 1200,
              "payment_frequency": "monthly"}  # 18% x 1.16 compounded monthly is about 23% a year
    item = _kinds(_found(_cash(120000), card=card18))["high_interest_debt"]
    assert item["data"]["effective_annual_rate"] > 0.2


def test_infonavit_without_months_paid_is_partial_not_zero(service):
    credit = {"id": "infonavit", "kind": "infonavit", "denomination": "UMA", "balance_units": 250, "annual_rate": 0.10,
              "monthly_payment_units": 2.0}
    report = service.run("debt", {"mode": "amortize", "as_of": AS_OF, "liabilities": [credit]})
    assert report["status"] == "partial"
    assert "liability.infonavit.months_paid" in [m["key"] for m in report["missing"]]
    [debt] = report["result"]["debts"]
    assert debt["projection"] == "partial" and debt["forgiven_at_30_years"] is None and debt["months_paid"] is None
    dated = run(service, {"mode": "amortize", "as_of": AS_OF, "liabilities": [{**credit, "origination_date": "2011-09-01"}]})
    [debt] = dated["result"]["debts"]
    assert dated["status"] == "ready" and debt["months_paid"] == 180 and debt["months"] == 180
    assert debt["forgiven_at_30_years"] > 0
    excluded = run(service, {"mode": "amortize", "as_of": AS_OF,
                             "liabilities": [{**credit, "months_paid": 240, "liberation_eligible": False}]})
    assert excluded["result"]["debts"][0]["forgiven_at_30_years"] is None


def test_a_payment_from_a_term_includes_iva(service):
    loan = {"id": "p", "kind": "personal", "balance": 100000, "annual_rate": 0.30, "remaining_term_months": 24,
            "currency": "MXN"}
    [debt] = run(service, {"mode": "amortize", "as_of": AS_OF, "liabilities": [loan]})["result"]["debts"]
    assert debt["monthly_payment"] == pytest.approx(5841.32, abs=0.01) and debt["months"] == 24


def test_refinance_compares_the_same_payment_and_a_longer_term(service):
    car = EXAMPLES["car_loan_refinance"]["liabilities"]
    report = run(service, {"mode": "refinance", "as_of": AS_OF, "liabilities": car,
                           "offer": {"kind": "refinance", "annual_rate": 0.085, "fee": 0, "term_months": 72}})
    longer = report["result"]
    assert longer["interest_saved"] < 0 and longer["breakeven_month"] is None
    assert longer["same_payment"]["monthly_payment"] == 520 and longer["same_payment"]["interest_saved"] > 0
    assert longer["verdict"] == "take_offer_keep_payment"
    assert "longer term lowers the payment but costs more" in longer["verdict_text"]["en"]
    for spec in views.views_for("debt", report):
        views.validate(spec)
    worse = run(service, {"mode": "refinance", "as_of": AS_OF, "liabilities": car,
                          "offer": {"kind": "refinance", "annual_rate": 0.12, "fee": 500, "term_months": 72}})["result"]
    assert worse["verdict"] == "keep" and worse["worth_it"] is False and worse["breakeven_month"] is None
    mx = [{"id": "a", "kind": "card", "balance": 50000, "annual_rate": 0.45, "monthly_payment": 4000, "currency": "MXN"}]
    consolidated = run(service, {"mode": "refinance", "as_of": AS_OF, "liabilities": mx,
                                 "offer": {"kind": "consolidation", "annual_rate": 0.25, "term_months": 24}})
    assert consolidated["result"]["offer"]["months"] == 24  # the term's payment carries IVA
    assert any("sin comisión indicada" in a for a in consolidated["assumptions"])


def test_mx_gains_tax_is_on_the_real_gain_and_depends_on_the_channel(service):
    base = {**EXAMPLES["mx_card_prepay_vs_cetes"], "horizon_months": 120}
    report = run(service, base)
    investing = report["result"]["investing"]
    assert investing["gains_tax"] == 0.1 and investing["gains_tax_basis"].startswith("real gain")
    assert any(a.startswith("LISR Art. 129") for a in report["assumptions"])
    grown, cost = 1.10 ** 10, 1.04 ** 10
    assert investing["after_tax"]["base"] == pytest.approx((grown - (grown - cost) * 0.1) ** 0.1 - 1, abs=0.0001)
    foreign = run(service, {**base, "investment_channel": "foreign_broker"})["result"]["investing"]
    assert foreign["gains_tax"] == 0.3 and "marginal" in foreign["gains_tax_rule"]


def test_mx_unknown_marginal_is_labelled_as_a_range(service):
    inputs = {k: v for k, v in EXAMPLES["mx_card_prepay_vs_cetes"].items() if k != "marginal_rate"}
    report = run(service, inputs)
    investing = report["result"]["investing"]
    assert investing["marginal_rate_known"] is False
    assert "rango según tu tasa marginal" in investing["marginal_label"]["es"]
    for spec in views.views_for("debt", report):
        views.validate(spec)


def test_single_debt_amortize_draws_its_balance_with_the_interest_paid(service):
    report = run(service, {"mode": "amortize", "as_of": AS_OF, "debt": card()})
    specs = views.views_for("debt", report)
    series = next(s for s in specs if s["kind"] == "series")
    views.validate(series)
    [debt] = report["result"]["debts"]
    assert len(series["data"]["points"]) == len(debt["balance_series"]) and series["data"]["points"][-1]["y"] == 0
    assert series["caption"]["en"] == "Interest and IVA paid: $13,350 over 18 months"
    assert series["caption"]["es"].startswith("Intereses e IVA pagados: $13,350")
    grows = run(service, {"mode": "amortize", "as_of": AS_OF, "debt": card(monthly_payment=500)})
    assert next(s for s in views.views_for("debt", grows) if s["kind"] == "series")["caption"]["en"] == \
        "Not repaid at this payment"


def test_payment_frequency_maturity_and_top_level_credit_limit(service):
    semimonthly = {"id": "sm", "kind": "auto", "balance": 10000, "annual_rate": 0.1, "monthly_payment": 250,
                   "payment_frequency": "semimonthly", "currency": "USD"}
    soon = {"id": "mat", "kind": "auto", "balance": 1000, "annual_rate": 0.1, "maturity": "2026-09-30", "currency": "USD"}
    limit = {"id": "t2", "kind": "card", "balance": 30000, "annual_rate": 0.45, "credit_limit": 60000, "currency": "MXN",
             "minimum_payment": {"percent_of_balance": 0.015, "percent_of_limit": 0.0125}}
    debts = {d["id"]: d for d in run(service, {"mode": "amortize", "as_of": AS_OF,
                                               "liabilities": [semimonthly, soon, limit]})["result"]["debts"]}
    assert debts["sm"]["monthly_payment"] == 500
    assert debts["mat"]["months"] == 1
    assert debts["t2"]["minimum_rule"]["floor"] == 750


def test_strategies_minimum_is_capped_at_balance_plus_interest(service):
    debts = [{"id": "c1", "kind": "card", "balance": 500, "annual_rate": 0.30, "monthly_payment": 800, "currency": "USD"},
             {"id": "c2", "kind": "card", "balance": 9000, "annual_rate": 0.25, "monthly_payment": 200, "currency": "USD"}]
    result = run(service, {"mode": "strategies", "as_of": AS_OF, "monthly_amount": 900, "liabilities": debts})["result"]
    assert result["minimum_payments"] == pytest.approx(712.5, abs=0.01)


def test_us_student_loan_and_standard_deduction_hurdle(service):
    loan = {"id": "sl", "kind": "student", "balance": 40000, "annual_rate": 0.065, "monthly_payment": 450, "currency": "USD"}
    common = {"mode": "prepay_vs_invest", "as_of": AS_OF, "extra_monthly": 200, "marginal_rate": 0.24,
              "reserve": {"months": 6, "target_months": 6}}
    with_deduction = run(service, {**common, "debt": loan})
    without = run(service, {**common, "debt": loan, "student_loan_deduction": False})
    assert with_deduction["result"]["guaranteed"]["after_tax_rate"] < without["result"]["guaranteed"]["after_tax_rate"]
    assert any("phases out" in a for a in with_deduction["assumptions"])
    mortgage = EXAMPLES["us_mortgage_prepay_vs_vti"]
    itemized = run(service, {**mortgage, "itemizes": True})
    hurdle = run(service, {**mortgage, "itemizes": True, "standard_deduction": 30000, "other_itemized_deductions": 10000})
    assert any("standard deduction" in a for a in itemized["assumptions"])
    assert hurdle["result"]["guaranteed"]["after_tax_rate"] > itemized["result"]["guaranteed"]["after_tax_rate"]
