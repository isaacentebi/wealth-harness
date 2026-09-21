"""Retirement engine: hand-computed formulas, fail-closed parameters, Modalidad 40, SS breakevens, withdrawal order."""
from __future__ import annotations

import pytest

from wealth import retirement as R
from wealth.catalog import CATALOG
from wealth.service import WealthService


def P(**overrides):
    return R._Params({"parameters": overrides})


# --- parameter table --------------------------------------------------------

def test_parameter_table_statuses_and_unverified_values_are_empty():
    for row in R.parameter_inventory():
        assert row["status"] in {"verified", "statutory", "needs_verification"}, row
        assert row["source"], row
        entry = R.PARAMETERS[row["key"]][row["period"]]
        if row["status"] == "needs_verification":
            assert entry["value"] is None and entry["verify_with"], row
        if row["status"] == "verified":
            assert entry["checked_on"] == "2026-09-21", row
    assert R.PARAMETERS["salario_minimo_general_daily_mxn"][2026]["value"] == "315.04"
    assert R.PARAMETERS["us_402g_deferral_limit"][2026]["value"] == "24500"
    assert R.PARAMETERS["us_hsa_family_limit"][2026]["value"] == "8750"
    # The 2020-reform schedule ends at 11.875% for 4.01 UMA and up in 2030.
    assert R.PARAMETERS["ceav_employer_schedule"]["any"]["value"]["by_year_percent"][2030][-1] == "11.875"


def test_unverified_parameter_is_never_used_and_override_needs_a_source():
    params = P()
    assert params.number("cuota_social_daily_mxn") is None
    assert params.missing and params.needs_verification[0]["key"] == "cuota_social_daily_mxn"
    assert P(cuota_social_daily_mxn={"value": 5, "source": "IMSS table Q3"}).number("cuota_social_daily_mxn") == 5
    with pytest.raises(ValueError):
        P(cuota_social_daily_mxn={"value": 5}).number("cuota_social_daily_mxn")


def test_mexico_fails_closed_for_a_year_without_a_verified_minimum_wage():
    report = R.retirement_mx({"as_of": "2027-03-01", "birth_year": 1966, "first_cotizacion_date": "1990-01-01",
                              "weeks_cotizadas": 1300, "ley73": {"average_daily_salary_mxn": 600, "dependants": {}},
                              "target_monthly_spending_mxn": 30000})
    assert report["status"] == "needs_input"
    assert any("salario_minimo_general_daily_mxn" in m for m in report["missing"])
    assert "ley73" not in report["result"]


def test_rmd_age_for_1959_fails_closed_unless_supplied():
    assert R.rmd_age(1958, P()) == 73 and R.rmd_age(1960, P()) == 75
    params = P()
    assert R.rmd_age(1959, params) is None and params.needs_verification
    assert R.rmd_age(1959, P(rmd_applicable_age={"value": 73, "source": "final regs"})) == 73


def test_us_2025_limits_fail_closed():
    report = R.retirement_us({"contributions": {"tax_year": 2025, "birth_year": 1980}})
    assert report["status"] == "needs_input"
    assert report["result"]["contributions"] is None


# --- Mexico -----------------------------------------------------------------

def test_imss_regime_cutoff():
    assert R.imss_regime("1997-06-30")["regime"] == "ley73"
    assert R.imss_regime("1997-07-01")["regime"] == "ley97"


def test_increments_follow_art_167_fraction_rule():
    # IMSS worked examples: 1,100 weeks -> 12 increments; 2,501 weeks -> 38.5.
    assert R.ley73_increments(1100) == 12
    assert R.ley73_increments(2501) == 38.5
    assert R.ley73_increments(512) == 0 and R.ley73_increments(513) == 0.5 and R.ley73_increments(527) == 1
    assert R.ley73_increments(400) == 0


def test_average_salary_last_250_weeks():
    history = [{"weeks": 200, "daily_salary_mxn": 1000}, {"weeks": 100, "daily_salary_mxn": 500}]
    assert R.average_salary_last_250_weeks(history) == pytest.approx((200 * 1000 + 50 * 500) / 250)


def test_ley73_matches_imss_worked_vejez_example():
    # IMSS example: salary 888.26, SM 52.59, 2,501 weeks, spouse + 2 children -> 32,186.62 monthly (own-right weeks exceed the cap).
    out = R.ley73_pension(888.26, 2501, 65, 52.59, dependants={"spouse": True, "children_under_16": 2}, params=P())
    assert out["art167_group"].startswith("6.01")
    assert out["annual_cuantia_at_65_mxn"] == pytest.approx(347963.63, abs=0.05)
    assert out["monthly_pension_mxn"] == pytest.approx(32186.62, abs=0.05)
    assert out["cap_monthly_mxn"] == pytest.approx(29989.87, abs=0.05)


def test_ley73_hand_case_with_assistance_cesantia_and_minimum():
    # 500/day, SM 315.04 -> 1.59x -> 49.23% + 1.430%; 1,000 weeks -> 10 increments.
    out = R.ley73_pension(500, 1000, 65, 315.04, dependants={}, params=P())
    annual = 500 * 365 * (0.4923 + 0.0143 * 10)
    assert out["monthly_pension_mxn"] == pytest.approx(annual / 12 * 1.11 * 1.15, abs=0.01)
    at60 = R.ley73_pension(500, 1000, 60, 315.04, dependants={}, params=P())
    assert at60["cesantia_percent"] == 75
    # 75% of the cuantia (9,250.02 with factor and assistance) falls under the minimum (one salario minimo x 1.11).
    assert annual / 12 * 0.75 * 1.11 * 1.15 < 315.04 * 365 / 12 * 1.11
    assert at60["minimum_applied"] and at60["monthly_pension_mxn"] == pytest.approx(315.04 * 365 / 12 * 1.11, abs=0.01)
    at62 = R.ley73_pension(1000, 1000, 62, 315.04, dependants={}, params=P())
    annual_1000 = 1000 * 365 * (0.2560 + 0.02096 * 10)
    assert at62["cesantia_percent"] == 85
    assert at62["monthly_pension_mxn"] == pytest.approx(annual_1000 / 12 * 0.85 * 1.11 * 1.15, abs=0.01)
    low = R.ley73_pension(320, 520, 60, 315.04, dependants={}, params=P())
    assert low["minimum_applied"] and low["monthly_pension_mxn"] == pytest.approx(315.04 * 365 / 12 * 1.11, abs=0.01)
    assert R.ley73_pension(500, 499, 65, 315.04, dependants={}, params=P())["eligible"] is False


def _ley73_inputs(**mod):
    return {"as_of": "2026-09-21", "age": 60, "first_cotizacion_date": "1990-03-01", "weeks_cotizadas": 1000,
            "retirement_age": 65, "target_monthly_spending_mxn": 30000,
            "ley73": {"average_daily_salary_mxn": 500, "dependants": {}, "modalidad40": {"daily_salary_mxn": 1000, "years": 5, **mod}}}


def test_modalidad40_break_even_hand_case():
    report = R.retirement_mx(_ley73_inputs())
    assert report["status"] == "ready", report["missing"]
    mod = report["result"]["ley73"]["modalidad40"]
    rates = [0.02 + e + 0.01125 + 0.02375 + 0.01425 for e in (0.07513, 0.08603, 0.09694, 0.10784, 0.11875)]
    assert mod["contribution_rates_by_year"][0]["rate"] == pytest.approx(0.14438)
    cost = sum(1000 * 365 * r for r in rates)
    assert mod["total_cost_mxn"] == pytest.approx(cost, abs=0.05)
    # 260 weeks >= 250, so the new average is the Modalidad 40 salary; weeks 1,260 -> 760 extra = 14 x 52 + 32 -> 15
    # increments; 1000/315.04 = 3.17x -> 25.60% and 2.096%.
    new_annual = 1000 * 365 * (0.2560 + 0.02096 * 15)
    with_mod = new_annual / 12 * 1.11 * 1.15
    without = 500 * 365 * (0.4923 + 0.0143 * 10) / 12 * 1.11 * 1.15
    assert mod["pension_with_mxn"] == pytest.approx(with_mod, abs=0.02)
    assert mod["pension_without_mxn"] == pytest.approx(without, abs=0.02)
    gain = (with_mod - without) * 13
    assert mod["payback_years"] == pytest.approx(cost / gain, abs=0.01)
    assert mod["break_even_age"] == pytest.approx(65 + cost / gain, abs=0.01)
    # IRR solves NPV = 0 for the costs then the level increase.
    for scenario in mod["by_longevity"]:
        years = int(scenario["death_age"] - 65)
        flows = [-1000 * 365 * r for r in rates] + [gain] * years
        irr = scenario["real_irr_percent"] / 100
        assert abs(sum(f / (1 + irr) ** t for t, f in enumerate(flows))) < 50


def test_modalidad40_salary_is_capped_at_25_uma():
    report = R.retirement_mx(_ley73_inputs(daily_salary_mxn=10000))
    assert report["result"]["ley73"]["modalidad40"]["daily_salary_mxn"] == pytest.approx(25 * 117.31)
    with pytest.raises(ValueError):
        R.retirement_mx(_ley73_inputs(years=6))  # would run past the pension age


def test_ley97_weeks_schedule_and_pension_garantizada():
    params = P()
    assert R.ley97_weeks_required(2021, params) == 750
    assert R.ley97_weeks_required(2026, params) == 875
    assert R.ley97_weeks_required(2031, params) == 1000
    assert R.ley97_weeks_required(2040, params) == 1000
    warnings: list[str] = []
    pg = R.pension_garantizada(2026, 60, 875, 1.5, params, warnings)
    assert pg["monthly_dec2020_mxn"] == 2622 and pg["monthly_mxn"] is None  # INPC factor unverified
    pg = R.pension_garantizada(2026, 65, 2000, 5.2, P(pension_garantizada_inpc_factor={"value": 1.3, "source": "INEGI"}), warnings)
    assert pg["monthly_dec2020_mxn"] == 8241 and pg["monthly_mxn"] == pytest.approx(8241 * 1.3)
    assert R.pension_garantizada(2026, 62, 850, 3.0, params, warnings)["eligible"] is False


def test_ley97_projection_hand_case():
    # One year: SBC 500 = 4.26 UMA -> 2026 top band 7.513%; zero return and fee.
    report = R.retirement_mx({"as_of": "2026-09-21", "age": 64, "first_cotizacion_date": "2000-01-01", "weeks_cotizadas": 1000,
                              "retirement_age": 65, "target_monthly_spending_mxn": 20000,
                              "ley97": {"sbc_daily_mxn": 500, "afore_balance_mxn": 100000, "real_return": 0.0, "fee": 0,
                                        "annuity_real_rate": 0.0, "average_career_sbc_daily_mxn": 300},
                              "longevity_ages": 85})
    ley97 = report["result"]["ley97"]
    contribution = 500 * 365 * (0.02 + 0.07513 + 0.01125)
    assert ley97["contributions_by_year"][0]["contribution_mxn"] == pytest.approx(contribution, abs=0.01)
    scenario = ley97["scenarios"][0]
    assert scenario["balance_at_retirement_mxn"] == pytest.approx(100000 + contribution, abs=0.01)
    assert scenario["programmed_withdrawal_monthly_mxn"]["low"] == pytest.approx((100000 + contribution) / 20 / 12, abs=0.01)
    assert ley97["meets_weeks"] and ley97["weeks_required"] == 900
    # Unverified INPC factor: the guarantee is not valued and it is listed as missing.
    assert report["status"] == "partial"
    assert any("pension_garantizada_inpc_factor" in m for m in report["missing"])


def test_ley97_negativa_when_weeks_short():
    report = R.retirement_mx({"as_of": "2026-09-21", "age": 64, "first_cotizacion_date": "2010-01-01", "weeks_cotizadas": 500,
                              "retirement_age": 65, "target_monthly_spending_mxn": 20000,
                              "ley97": {"sbc_daily_mxn": 500, "afore_balance_mxn": 100000, "average_career_sbc_daily_mxn": 300}})
    scenario = report["result"]["ley97"]["scenarios"][0]
    assert scenario["estimated_pension_monthly_mxn"] is None and "negativa" in scenario["outcome"]


def test_voluntary_contributions_link_to_deductions():
    report = R.retirement_mx({**_ley73_inputs(), "voluntary": {"long_term_monthly_mxn": 5000, "accumulable_income_mxn": 500000}})
    vol = report["result"]["voluntary"]
    assert vol["long_term"]["art151v_cap_mxn"] == 50000.0  # 10% of 500,000 < 5 UMA (213,973.20)
    assert vol["long_term"]["deductible_mxn"] == 50000.0
    assert vol["related_task"]["task"] == "mx_deductions"


def test_mexico_gap_in_real_pesos():
    report = R.retirement_mx(_ley73_inputs())
    gap = report["result"]["gap"]
    pension = report["result"]["ley73"]["pension_at_retirement_age"]["monthly_pension_mxn"]
    assert gap["monthly_gap_mxn"]["high"] == pytest.approx(30000 - pension, abs=0.01)


def test_regime_is_required():
    assert R.retirement_mx({"weeks_cotizadas": 100})["status"] == "needs_input"


# --- United States ----------------------------------------------------------

def test_social_security_reduction_credits_and_breakevens():
    report = R.retirement_us({"social_security": {"pia_monthly_usd": 1000, "birth_year": 1962}})
    ss = report["result"]["social_security"]
    by_age = {c["claim_age"]: c["monthly_benefit_usd"] for c in ss["claims"]}
    assert by_age[62] == 700 and by_age[64] == 800 and by_age[65] == pytest.approx(866.67) and by_age[67] == 1000
    assert by_age[70] == 1240
    breakevens = {(b["earlier"], b["later"]): b["breakeven_age"] for b in ss["breakevens"]}
    assert breakevens[(62, 70)] == pytest.approx((1240 * 70 - 700 * 62) / 540, abs=0.01)
    assert breakevens[(62, 67.0)] == pytest.approx(78.67, abs=0.01)
    assert breakevens[(67.0, 70)] == pytest.approx(82.5, abs=0.01)
    # Longer lives favor later claiming at a zero discount rate.
    best = {row["death_age"]: row["best_claim_age"] for row in ss["lifetime_value"]}
    assert best[78] < 67 and best[95] == 70


def test_social_security_fra_for_1957_and_spousal():
    params = P()
    assert R.full_retirement_age_months(1957, params) == 66 * 12 + 6
    assert R.benefit_factor(-42, params) == pytest.approx(1 - 36 * 5 / 900 - 6 * 5 / 1200)
    report = R.retirement_us({"social_security": {"pia_monthly_usd": 2000, "birth_year": 1960,
                                                  "spouse": {"birth_year": 1960, "own_pia_monthly_usd": 400}}})
    spousal = report["result"]["social_security"]["spousal"]
    assert spousal["spousal_excess_at_spouse_fra_usd"] == 600
    rows = {r["spouse_claim_age"]: r["spousal_excess_monthly_usd"] for r in spousal["by_spouse_claim_age"]}
    assert rows[62] == pytest.approx(600 * (1 - 36 * 25 / 3600 - 24 * 5 / 1200), abs=0.01)  # 65%
    assert rows[70] == 600  # no delayed credits on spousal benefits


def test_contribution_limits_2026():
    out = R.retirement_us({"contributions": {"tax_year": 2026, "birth_year": 1964, "prior_year_fica_wages_usd": 160000,
                                             "hdhp_coverage": "self"}})["result"]["contributions"]
    plan = out["employer_plan_401k_403b_457"]
    assert plan["elective_deferral_usd"] == 24500 and plan["catch_up_usd"] == 11250 and plan["catch_up_must_be_roth"] is True
    assert out["ira"]["limit_usd"] == 8600 and out["hsa"]["limit_usd"] == 5400
    young = R.retirement_us({"contributions": {"tax_year": 2026, "birth_year": 1990}})["result"]["contributions"]
    assert young["employer_plan_401k_403b_457"]["catch_up_usd"] == 0 and young["ira"]["limit_usd"] == 7500
    fifty = R.retirement_us({"contributions": {"tax_year": 2026, "birth_year": 1975, "prior_year_fica_wages_usd": 100000}})["result"]["contributions"]
    assert fifty["employer_plan_401k_403b_457"]["catch_up_usd"] == 8000
    assert fifty["employer_plan_401k_403b_457"]["catch_up_must_be_roth"] is False


def test_rmd_amount_uses_uniform_table():
    out = R.retirement_us({"rmd": {"birth_year": 1951, "age": 75, "prior_year_end_balance_usd": 246000}})["result"]["rmd"]
    assert out["rmd_start_age"] == 73 and out["rmd_usd"] == 10000 and out["first_rmd_deadline"] == "2025-04-01"


def _withdrawals(**extra):
    base = {"balances": {"taxable": 100000, "taxable_basis": 100000, "tax_deferred": 100000, "roth": 100000},
            "annual_spending_usd": 30000, "other_ordinary_income_usd": 0, "start_age": 60, "years": 3,
            "filing_status": "single", "real_return": 0.0, "birth_year": 1966}
    base.update(extra)
    return R.retirement_us({"withdrawals": base})["result"]["withdrawals"]


def test_withdrawal_ordering_taxable_first_vs_proportional():
    out = _withdrawals()
    first = out["strategies"]["taxable_first"]
    assert [y["from_taxable"] for y in first["years"]] == [30000, 30000, 30000]
    assert all(y["from_tax_deferred"] == 0 and y["from_roth"] == 0 and y["federal_tax"] == 0 for y in first["years"])
    prop = out["strategies"]["proportional"]
    assert all(y["from_taxable"] == y["from_tax_deferred"] == y["from_roth"] == 10000 for y in prop["years"])
    assert prop["total_federal_tax_usd"] == 0  # 10,000 of ordinary income is under the 16,100 standard deduction


def test_bracket_filling_roth_conversion_hand_case():
    out = _withdrawals(balances={"taxable": 400000, "taxable_basis": 400000, "tax_deferred": 500000, "roth": 0},
                       annual_spending_usd=20000, years=1)
    year = out["strategies"]["taxable_first_with_roth_conversions"]["years"][0]
    # Single 2026: 12% bracket tops at 50,400 taxable; + 16,100 deduction = 66,500 converted.
    assert year["roth_conversion"] == 66500
    assert year["federal_tax"] == pytest.approx(12400 * 0.10 + (50400 - 12400) * 0.12)
    assert year["from_taxable"] == pytest.approx(20000 + 5800)
    assert out["strategies"]["taxable_first"]["years"][0]["federal_tax"] == 0


def test_withdrawals_include_rmds_and_capital_gains():
    out = _withdrawals(balances={"taxable": 200000, "taxable_basis": 100000, "tax_deferred": 265000, "roth": 0},
                       start_age=73, birth_year=1953, years=1, annual_spending_usd=60000)
    year = out["strategies"]["taxable_first"]["years"][0]
    assert year["rmd"] == pytest.approx(265000 / 26.5)
    assert year["realized_gain"] == pytest.approx(year["from_taxable"] * 0.5)


# --- readiness --------------------------------------------------------------

def test_readiness_formulas():
    report = R.retirement_readiness({"currency": "USD", "current_age": 45, "retirement_age": 65, "plan_to_age": 95,
                                     "target_annual_spending": 80000, "guaranteed_annual_income": 30000,
                                     "current_savings": 100000, "annual_contribution": 10000, "real_return": 0.0,
                                     "withdrawal_rates": [0.04]})
    res = report["result"]
    assert res["required_nest_egg_by_withdrawal_rate"]["0.04"] == 1250000
    assert res["projected_savings_at_retirement_by_return"]["0"] == 300000
    gap = res["gap_matrix"][0]
    assert gap["gap"] == 950000 and gap["extra_monthly_contribution"] == pytest.approx(950000 / 240, abs=0.01)
    assert report["status"] == "partial" and any("return_model" in m for m in report["missing"])
    assert R.monthly_contribution_to_close(1000, 0.0, 0) is None


def test_readiness_monte_carlo_uses_planning():
    report = R.retirement_readiness({"currency": "USD", "current_age": 64, "retirement_age": 65, "plan_to_age": 95,
                                     "target_annual_spending": 40000, "guaranteed_annual_income": 0,
                                     "current_savings": 1000000, "annual_contribution": 0, "real_return": 0.0,
                                     "return_model": {"type": "parametric", "basis": "real", "annual_return": 0.0, "annual_volatility": 0.0},
                                     "annual_inflation": 0.0, "simulations": 100})
    mc = report["result"]["monte_carlo"]
    # 1,000,000 / 40,000 = 25 years < 30: deterministic failure; the 3.5% nest egg (1,142,857) lasts 28.6 years: also fails.
    assert mc["projected_savings"]["probability_of_success_percent"] == 0
    report = R.retirement_readiness({"currency": "USD", "current_age": 64, "retirement_age": 65,
                                     "plan_to_age": 85, "target_annual_spending": 40000, "guaranteed_annual_income": 0,
                                     "current_savings": 1000000, "annual_contribution": 0, "real_return": 0.0,
                                     "return_model": {"type": "parametric", "basis": "real", "annual_return": 0.0, "annual_volatility": 0.0},
                                     "annual_inflation": 0.02, "simulations": 100})
    assert report["result"]["monte_carlo"]["projected_savings"]["probability_of_success_percent"] == 100


def test_readiness_requires_guaranteed_income():
    report = R.retirement_readiness({"currency": "USD", "current_age": 40})
    assert report["status"] == "needs_input" and "guaranteed_annual_income" in report["missing"]


# --- service ---------------------------------------------------------------

def test_tasks_run_through_the_service(tmp_path):
    service = WealthService(tmp_path / "w.sqlite3")
    for task in ("retirement_mx", "retirement_us", "retirement_readiness"):
        report = service.run(task, inputs=CATALOG[task]["example"])
        assert report["status"] in {"ready", "partial"}, (task, report["missing"])
        assert report["sources"] or task == "retirement_readiness"


def test_birth_year_from_saved_profile():
    report = R.retirement_us({"contributions": {"tax_year": 2026}}, {"client.profile": {"birth_year": 1964}})
    assert report["result"]["contributions"]["age_at_year_end"] == 62
    assert any("client.profile" in a for a in report["assumptions"])


def test_ley73_cesantia_applies_the_percentage_after_assignments_and_the_cap():
    # Arts. 164, 169, 171: assignments and the 100%-of-salary cap act on the vejez pension; cesantia then pays
    # its age percentage of it.  Here the own-right vejez cuantia already exceeds the salary, so the spouse
    # assignment adds nothing and the pension at 60 is 75% of the own-right amount (x 1.11).
    out = R.ley73_pension(1000, 2500, 60, 100, dependants={"spouse": True}, params=P())
    vejez = out["annual_cuantia_at_65_mxn"] / 12
    assert vejez > 1000 * 365 / 12
    assert out["monthly_pension_mxn"] == pytest.approx(vejez * 0.75 * 1.11, abs=0.01)
    # Where the cap does not bind, the order does not change the result: 1.15 x 0.75 x 1.11.
    low = R.ley73_pension(300, 1500, 60, 100, dependants={"spouse": True}, params=P())
    assert low["monthly_pension_mxn"] == pytest.approx(low["annual_cuantia_at_65_mxn"] / 12 * 1.15 * 0.75 * 1.11,
                                                       abs=0.01)
    # A cap that binds on the vejez pension with assignments is then reduced by the cesantia percentage.
    capped = R.ley73_pension(400, 2000, 62, 100, dependants={"spouse": True, "children_under_16": 2}, params=P())
    vejez = capped["annual_cuantia_at_65_mxn"] / 12
    salary = 400 * 365 / 12
    assert vejez < salary < vejez * 1.35
    assert capped["monthly_pension_mxn"] == pytest.approx(salary * 0.85 * 1.11, abs=0.01)


# --- audit findings ---------------------------------------------------------

def _ley73_plain(**ley73):
    return {"as_of": "2026-09-21", "age": 60, "first_cotizacion_date": "1990-01-01", "weeks_cotizadas": 1000,
            "retirement_age": 65, "target_monthly_spending_mxn": 30000, "ley73": {"average_daily_salary_mxn": 600, **ley73}}


def _has_negative_zero(value):
    if isinstance(value, float):
        return value == 0 and str(value).startswith("-")
    if isinstance(value, dict):
        return any(_has_negative_zero(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_negative_zero(v) for v in value)
    return False


def test_unknown_dependants_default_to_the_15_percent_ayuda_asistencial():
    unknown = R.ley73_pension(500, 1000, 65, 315.04, dependants=None, params=P())
    none = R.ley73_pension(500, 1000, 65, 315.04, dependants={}, params=P())
    assert unknown["family_assignments_share"] == 0.15 and unknown["family_assignments_assumed"] is True
    assert unknown["monthly_pension_mxn"] == none["monthly_pension_mxn"]
    assert none["family_assignments_assumed"] is False
    report = R.retirement_mx(_ley73_plain())
    assert report["result"]["ley73"]["pension_at_retirement_age"]["family_assignments_share"] == 0.15
    assert any("ayuda asistencial" in a for a in report["assumptions"])
    assert any("ley73.dependants" in m for m in report["missing"])


def test_ley73_average_salary_is_capped_at_25_uma():
    ceiling = 25 * 117.31
    report = R.retirement_mx(_ley73_plain(average_daily_salary_mxn=5000, dependants={}))
    pension = report["result"]["ley73"]["pension_at_retirement_age"]
    assert pension["average_daily_salary_mxn"] == pytest.approx(ceiling, abs=0.01)
    assert any("art. 28 ceiling" in w for w in report["warnings"])
    at_cap = R.retirement_mx(_ley73_plain(average_daily_salary_mxn=ceiling, dependants={}))
    assert not any("art. 28 ceiling" in w for w in at_cap["warnings"])


def test_pension_garantizada_is_flagged_as_an_estimate_with_unchanged_values():
    warnings: list[str] = []
    pg = R.pension_garantizada(2026, 60, 875, 1.5, P(), warnings)
    assert pg["estimate"] is True and pg["monthly_dec2020_mxn"] == 2622
    assert any("estimate pending the official IMSS table" in w for w in warnings)


def test_unclosable_readiness_gap_is_none_with_a_reason():
    report = R.retirement_readiness({"currency": "USD", "current_age": 65, "retirement_age": 65, "plan_to_age": 95,
                                     "target_annual_spending": 80000, "guaranteed_annual_income": 30000,
                                     "current_savings": 100000, "annual_contribution": 0, "real_return": [0.0, 0.03]})
    res = report["result"]
    assert res["worst_case_extra_monthly"] is None and res["worst_case_extra_monthly_reason"]
    assert res["best_case_extra_monthly"] is None
    closable = R.retirement_readiness({"currency": "USD", "current_age": 45, "retirement_age": 65, "plan_to_age": 95,
                                       "target_annual_spending": 80000, "guaranteed_annual_income": 30000,
                                       "current_savings": 100000, "annual_contribution": 10000, "real_return": 0.0,
                                       "withdrawal_rates": [0.04]})["result"]
    assert closable["worst_case_extra_monthly"] == pytest.approx(950000 / 240, abs=0.01)
    assert "worst_case_extra_monthly_reason" not in closable


def test_by_age_adds_52_weeks_per_working_year_while_contributing():
    held = R.retirement_mx(_ley73_plain(dependants={}))
    working = R.retirement_mx(_ley73_plain(dependants={}, still_contributing=True))
    for age in range(60, 66):
        direct = R.ley73_pension(600, 1000 + 52 * (age - 60), age, 315.04, dependants={}, params=P())
        assert working["result"]["ley73"]["by_age"][str(age)] == direct["monthly_pension_mxn"]
    assert working["result"]["ley73"]["by_age"]["65"] > held["result"]["ley73"]["by_age"]["65"]
    assert working["result"]["ley73"]["pension_at_retirement_age"]["weeks"] == 1260
    assert held["result"]["ley73"]["pension_at_retirement_age"]["weeks"] == 1000
    assert any("still_contributing" in a for a in held["assumptions"])
    with pytest.raises(ValueError):
        R.retirement_mx(_ley73_plain(dependants={}, still_contributing="yes"))


def test_ley97_after_the_last_known_fee_year_falls_back_or_uses_the_callers_fee():
    base = {"as_of": "2027-03-01", "age": 60, "first_cotizacion_date": "2000-01-01", "weeks_cotizadas": 1000,
            "retirement_age": 65, "target_monthly_spending_mxn": 20000,
            "parameters": {"salario_minimo_general_daily_mxn": {"value": 340, "source": "fictional 2027 figure"},
                           "uma_daily_mxn": {"value": 120, "source": "fictional 2027 figure"}}}
    ley97 = {"sbc_daily_mxn": 500, "afore_balance_mxn": 100000, "average_career_sbc_daily_mxn": 300}
    fallback = R.retirement_mx({**base, "ley97": ley97})
    assert fallback["result"]["ley97"]["fee"] == 0.0054
    assert any("No CONSAR maximum AFORE fee is recorded for 2027" in w for w in fallback["warnings"])
    assert not any("afore_fee_max" in m for m in fallback["missing"])
    supplied = R.retirement_mx({**base, "ley97": {**ley97, "fee": 0.005}})
    assert supplied["result"]["ley97"]["fee"] == 0.005
    assert not any("AFORE fee is recorded" in w for w in supplied["warnings"])


def test_retirement_outputs_have_no_negative_zero():
    assert str(R._r(-0.001)) == "0.0"
    for task in ("retirement_mx", "retirement_us", "retirement_readiness"):
        report = R.run(task, CATALOG[task]["example"], {})
        assert not _has_negative_zero(report["result"]), task
