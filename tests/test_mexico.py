from __future__ import annotations

from decimal import Decimal

import pytest

from wealth import mexico


TARIFF_2026 = mexico.PARAMETERS["isr_annual_tariff"][2026]["value"]


# --- parameter table -------------------------------------------------------

def test_parameter_table_marks_status_and_never_uses_unverified_values():
    for key, years in mexico.PARAMETERS.items():
        for year, entry in years.items():
            assert entry["status"] in {"verified", "statutory", "needs_verification"}, (key, year)
            assert entry["source"], (key, year)
            if entry["status"] == "needs_verification":
                assert entry["value"] is None, (key, year)
                assert entry["verify_with"]
            if entry["status"] == "verified":
                assert entry["checked_on"] == "2026-09-21"
    assert mexico.PARAMETERS["interest_annual_retention_rate"][2026]["value"] == "0.0090"
    assert mexico.PARAMETERS["uma_annual_mxn"][2026]["value"] == "42794.64"


def test_isr_annual_tax_uses_fixed_fee_plus_rate_on_excess():
    assert mexico.isr_annual_tax(0, TARIFF_2026) == 0
    assert mexico.isr_annual_tax(500000, TARIFF_2026).quantize(Decimal("0.01")) == Decimal("85773.86")
    # Continuity at a bracket boundary.
    top_of_first = mexico.isr_annual_tax(Decimal("10135.11"), TARIFF_2026)
    assert abs(top_of_first - Decimal("194.59")) < Decimal("0.01")
    with pytest.raises(ValueError):
        mexico.isr_annual_tax(100, [{"lower": "10", "fixed": "0", "rate_percent": "1"}, {"lower": "5", "fixed": "0", "rate_percent": "2"}])


# --- holdings ---------------------------------------------------------------

def test_classify_holdings_liquidity_udi_and_pfic():
    report = mexico.classify_holdings({
        "us_person": True,
        "holdings": [
            {"id": "c1", "type": "CETES", "value_mxn": 100000},
            {"id": "u1", "type": "udibonos", "udis": 1000, "udi_value": "8.5", "udi_date": "2026-09-18", "udi_source": "Banxico SIE"},
            {"id": "a1", "type": "afore", "value_mxn": 300000},
            {"id": "p1", "type": "ppr", "value_mxn": 50000},
            {"id": "f1", "type": "mutual_fund_debt", "value_mxn": 20000},
            {"id": "s1", "type": "sic", "value": 1000, "currency": "USD", "fx_to_mxn": "18.5", "fx_source": "Banxico FIX 2026-09-18"},
        ],
    })
    assert report["status"] == "ready"
    rows = {row["id"]: row for row in report["result"]["holdings"]}
    assert rows["u1"]["value_mxn"] == "8500.00"
    assert rows["a1"]["liquidity"]["class"] == "locked_until_retirement"
    assert rows["p1"]["liquidity"]["class"] == "locked_until_age_65"
    assert "early_withdrawal_penalty" in rows["p1"]["liquidity"]
    assert "pfic_warning" in rows["f1"]
    assert rows["s1"]["tax_regime"] == "article_129" and "us_estate_note" in rows["s1"]
    assert report["result"]["locked_or_restricted_mxn"] == "350000.00"
    assert report["result"]["liquid_or_exchange_mxn"] == "147000.00"


def test_classify_holdings_fails_closed_on_unknowns():
    assert mexico.classify_holdings({})["status"] == "needs_input"
    report = mexico.classify_holdings({"holdings": [
        {"id": "x", "type": "crypto"},
        {"id": "u", "type": "udibonos", "udis": 10},
        {"id": "n", "value_mxn": 5},
        {"id": "f", "type": "mutual_fund_equity", "value_mxn": 1},
    ]})
    assert report["status"] == "partial"
    joined = " ".join(report["missing"])
    assert "holdings[0].type" in joined and "holdings[1].udi_value" in joined and "holdings[2].type" in joined
    assert any(m.startswith("us_person") for m in report["missing"])
    rows = {row["id"]: row for row in report["result"]["holdings"]}
    assert rows["u"]["value_mxn"] is None


def test_classify_holdings_from_household_and_non_mxn_needs_fx():
    household = {"positions": [
        {"id": "pos1", "mx_instrument_type": "fibra", "value": 1000, "currency": "MXN"},
        {"id": "pos2", "value": 1000, "currency": "MXN"},
        {"id": "pos3", "mx_instrument_type": "foreign_brokerage", "value": 10, "currency": "USD"},
    ]}
    report = mexico.run("mx_holdings", {"us_person": False}, {"household": household})
    assert report["status"] == "partial"
    assert "holdings[1].type" in report["missing"]
    assert "holdings[2].fx_to_mxn" in report["missing"]
    assert any("stored household" in a for a in report["assumptions"])


# --- real interest -----------------------------------------------------------

def _account(**overrides):
    account = {"id": "cetes", "institution": "Cetesdirecto", "nominal_interest_mxn": 10000, "average_daily_balance_mxn": 100000,
               "days": 365, "inpc_first_month": "100", "inpc_last_month": "104", "retention_withheld_mxn": 900}
    account.update(overrides)
    return account


def test_real_interest_2026_with_tariff_and_retention_credit():
    report = mexico.real_interest({"tax_year": 2026, "accounts": [_account(constancia_real_interest_mxn=6000)], "taxable_income_before_mxn": 500000})
    assert report["status"] == "ready", report["missing"]
    totals = report["result"]["totals"]
    assert totals["inflation_adjustment_mxn"] == "4000.00"
    assert totals["real_interest_mxn"] == "6000.00"
    account = report["result"]["accounts"][0]
    assert account["expected_retention_estimate_mxn"] == "900.00"
    assert account["constancia_reconciliation"]["matches"] is True
    effect = report["result"]["annual_isr_effect"]
    assert effect["isr_change_mxn"] == "1411.20"  # 6,000 at 23.52%
    assert effect["net_isr_after_retention_mxn"] == "511.20"
    assert report["result"]["retention_rate"] == "0.009"
    assert any(p["key"] == "isr_annual_tariff" for p in report["result"]["parameters_used"])


def test_real_interest_loss_and_marginal_rate():
    report = mexico.real_interest({"tax_year": 2026, "marginal_rate": "0.30",
                                   "accounts": [_account(nominal_interest_mxn=1000, retention_withheld_mxn=900)]})
    totals = report["result"]["totals"]
    assert totals["real_interest_mxn"] == "0.00"
    assert totals["real_interest_loss_mxn"] == "3000.00"
    assert report["result"]["annual_isr_effect"]["net_isr_after_retention_mxn"] == "-900.00"
    assert any("Chapters I" in w for w in report["warnings"])


def test_real_interest_udi_adjustment_and_reconciliation_mismatch():
    report = mexico.real_interest({"tax_year": 2026, "marginal_rate": "0.1",
                                   "accounts": [_account(udi_adjustment_mxn=2000, inflation_factor="0.04", constancia_real_interest_mxn=6000, retention_withheld_mxn=100)]})
    assert report["result"]["totals"]["real_interest_mxn"] == "8000.00"
    assert report["result"]["accounts"][0]["constancia_reconciliation"]["matches"] is False
    assert any("constancia" in w for w in report["warnings"])


def test_real_interest_fails_closed_for_unverified_year_unless_rate_supplied():
    closed = mexico.real_interest({"tax_year": 2025, "accounts": [_account()], "marginal_rate": "0.3"})
    assert closed["status"] == "needs_input"
    assert any("interest_annual_retention_rate" in m for m in closed["missing"])
    assert closed["result"]["parameters_needing_verification"][0]["key"] == "interest_annual_retention_rate"
    opened = mexico.real_interest({"tax_year": 2025, "accounts": [_account()], "marginal_rate": "0.3",
                                   "parameters": {"interest_annual_retention_rate": {"value": "0.005", "source": "LIF 2025 art. 21 DOF"}}})
    assert opened["status"] == "ready"
    assert opened["result"]["parameters_used"][0]["status"] == "caller_supplied"


def test_real_interest_missing_inputs_are_not_zero():
    assert mexico.real_interest({})["status"] == "needs_input"
    report = mexico.real_interest({"tax_year": 2026, "accounts": [_account(), {"id": "b"}]})
    assert report["status"] == "partial"
    assert "taxable_income_before_mxn or marginal_rate" in report["missing"]
    assert any(m.startswith("accounts[1].nominal_interest_mxn") for m in report["missing"])
    no_retention = dict(_account())
    del no_retention["retention_withheld_mxn"]
    partial = mexico.real_interest({"tax_year": 2026, "accounts": [no_retention], "marginal_rate": "0.3"})
    assert partial["status"] == "partial"
    assert partial["result"]["totals"]["retention_credited_mxn"] is None
    with pytest.raises(ValueError):
        mexico.real_interest({"tax_year": 2026, "accounts": [_account(days=0)]})


# --- deductions ----------------------------------------------------------------

def _deduction_inputs(**overrides):
    inputs = {"tax_year": 2026, "total_income_mxn": 1000000, "accumulable_income_mxn": 900000,
              "deductions": {"general_mxn": 200000, "retirement_151v_mxn": 50000, "art185_mxn": 0},
              "proposed_ppr_contribution_mxn": 100000, "proposed_art185_mxn": 200000, "taxable_income_before_mxn": 700000}
    inputs.update(overrides)
    return inputs


def test_personal_deductions_caps_and_ppr_saving():
    report = mexico.personal_deductions(_deduction_inputs())
    assert report["status"] == "ready", report["missing"]
    caps = report["result"]["caps"]
    assert caps["five_annual_umas_mxn"] == "213973.20"
    assert caps["global_cap_mxn"] == "150000.00"
    assert caps["art151_v_cap_mxn"] == "90000.00"
    assert report["result"]["allowed"]["general_mxn"] == "150000.00"
    assert report["result"]["remaining_room"]["art151_v_mxn"] == "40000.00"
    ppr = report["result"]["contribution_scenarios"]["art151_v"]
    assert ppr["deductible_mxn"] == "40000.00" and ppr["non_deductible_excess_mxn"] == "60000.00"
    expected = mexico.isr_annual_tax(700000, TARIFF_2026) - mexico.isr_annual_tax(660000, TARIFF_2026)
    assert ppr["estimated_isr_saving_mxn"] == format(expected.quantize(Decimal("0.01")), "f")
    assert ppr["deadline"] == "2026-12-31"
    art185 = report["result"]["contribution_scenarios"]["art185"]
    assert art185["deductible_mxn"] == "152000.00"
    assert "Deferral" in art185["tradeoff"]


def test_personal_deductions_fail_closed():
    assert mexico.personal_deductions({})["status"] == "needs_input"
    missing_existing = mexico.personal_deductions(_deduction_inputs(deductions={"general_mxn": 1}))
    assert missing_existing["status"] == "needs_input"
    assert "deductions.retirement_151v_mxn" in missing_existing["missing"]
    unverified = mexico.personal_deductions(_deduction_inputs(tax_year=2025))
    assert unverified["status"] == "needs_input"
    assert any("uma_annual_mxn" in m for m in unverified["missing"])
    supplied = mexico.personal_deductions(_deduction_inputs(tax_year=2025, parameters={"uma_annual_mxn": {"value": "41273.52", "source": "INEGI DOF 10-01-2025"}}))
    assert supplied["status"] == "ready"
    old_tariff = mexico.personal_deductions(_deduction_inputs(tax_year=2024, parameters={"uma_annual_mxn": {"value": "39606.36", "source": "INEGI DOF 2024"}}))
    assert old_tariff["status"] == "needs_input"
    assert any("isr_annual_tariff" in m for m in old_tariff["missing"])
    no_rate = mexico.personal_deductions(_deduction_inputs(taxable_income_before_mxn=None))
    assert no_rate["status"] == "partial" and "taxable_income_before_mxn or marginal_rate" in no_rate["missing"]
    with pytest.raises(ValueError):
        mexico.personal_deductions(_deduction_inputs(accumulable_income_mxn=2000000))


def test_parameter_override_requires_source_and_warns_on_conflict():
    with pytest.raises(ValueError):
        mexico.personal_deductions(_deduction_inputs(parameters={"uma_annual_mxn": {"value": "1"}}))
    report = mexico.personal_deductions(_deduction_inputs(parameters={"uma_annual_mxn": {"value": "40000", "source": "test"}}))
    assert any("differs" in w for w in report["warnings"])


# --- foreign securities ----------------------------------------------------------

def test_foreign_securities_gain_with_fx_and_dividend_credit():
    report = mexico.foreign_securities({
        "tax_year": 2026, "taxable_income_before_mxn": 700000,
        "sales": [{"id": "not-in-sic", "currency": "USD", "proceeds": 12000, "fx_sale": "18", "cost": 10000, "fx_acquisition": "20",
                   "acquired_on": "2020-01-10", "sold_on": "2026-05-01", "sic_listed": False}],
        "dividends": [{"id": "d1", "currency": "USD", "gross": 1000, "withheld": 100, "fx": "18", "paid_on": "2026-03-31",
                       "source_country": "US", "w8ben_on_file": True}],
    })
    assert report["status"] == "ready", report["missing"]
    sale = report["result"]["sales"][0]
    assert sale["gain_or_loss_mxn"] == "16000.00"  # 216,000 - 200,000
    assert sale["components"]["local_currency_gain_at_sale_fx_mxn"] == "36000.00"
    assert sale["components"]["fx_effect_on_cost_mxn"] == "-20000.00"
    assert report["result"]["totals"]["additional_10pct_dividend_tax_mxn"] == "1800.00"
    credit = report["result"]["foreign_tax_credit"]
    assert credit["estimated_credit_mxn"] == "1800.00"
    assert mexico.CONSULT_FLAG in report["result"]["flags"]
    assert report["result"]["execution_ready"] is False
    assert any("annualization" in w for w in report["warnings"])


def test_foreign_securities_flags_withholding_and_fails_closed():
    assert mexico.foreign_securities({"tax_year": 2026})["status"] == "needs_input"
    report = mexico.foreign_securities({"tax_year": 2026, "marginal_rate": "0.3", "dividends": [
        {"id": "d1", "currency": "USD", "gross": 100, "withheld": 30, "fx": 18, "paid_on": "2026-01-01", "source_country": "US", "w8ben_on_file": False},
        {"id": "d2", "currency": "USD", "gross": 100, "withheld": 15, "fx": 18, "paid_on": "2026-01-01", "source_country": "US"},
    ]})
    assert "dividends[1].w8ben_on_file" in report["missing"]
    assert any("30%" in w for w in report["warnings"])
    with pytest.raises(ValueError):
        mexico.foreign_securities({"tax_year": 2026, "sales": [{"id": "s", "currency": "USD", "proceeds": 1, "fx_sale": 1, "cost": 1, "fx_acquisition": 1,
                                                               "acquired_on": "2025-01-01", "sold_on": "2026-01-01", "venue": "sic", "sic_listed": True}]})
    with pytest.raises(ValueError):
        mexico.foreign_securities({"tax_year": 2026, "sales": [{"id": "s", "currency": "USD", "proceeds": 1, "fx_sale": 1, "cost": 1, "fx_acquisition": 1,
                                                               "acquired_on": "2025-01-01", "sold_on": "2025-06-01", "sic_listed": False}]})


def test_sic_listing_not_the_broker_decides_the_ten_percent_rate():
    sale = {"currency": "USD", "proceeds": 12000, "fx_sale": "18", "cost": 10000, "fx_acquisition": "18",
            "acquired_on": "2022-01-10", "sold_on": "2026-05-01"}
    report = mexico.foreign_securities({"tax_year": 2026, "marginal_rate": "0.3", "sales": [
        {**sale, "id": "aapl-ibkr", "sic_listed": True},
        {**sale, "id": "cspx-ibkr", "sic_listed": True, "security_type": "equity_etf"},
        {**sale, "id": "microcap-ibkr", "sic_listed": False},
    ]})
    rows = {row["id"]: row for row in report["result"]["sales"]}
    assert rows["aapl-ibkr"]["regime"] == "article_129"
    assert rows["microcap-ibkr"]["regime"] == "progressive"
    totals = report["result"]["totals"]
    assert totals["article_129_net_gain_or_loss_mxn"] == "72000.00"
    assert totals["article_129_tax_mxn"] == "7200.00"
    assert totals["net_gain_or_loss_mxn"] == "36000.00"  # only the non-SIC sale is progressive income
    assert any("contested" in w for w in report["warnings"])
    assert any("constancia" in w for w in report["warnings"])
    missing = mexico.foreign_securities({"tax_year": 2026, "marginal_rate": "0.3", "sales": [{**sale, "id": "x"}]})
    assert "sales[0].sic_listed" in missing["missing"]


# --- calendar ------------------------------------------------------------------

def test_tax_calendar_items_and_conditional_monthlies():
    report = mexico.tax_calendar({"tax_year": 2026, "as_of": "2026-09-21", "business_or_professional_income": False,
                                  "rental_income": False, "foreign_dividends": True, "us_person": True})
    assert report["status"] == "ready"
    items = {item["id"]: item for item in report["result"]["items"]}
    assert items["mx_annual_return"]["date"] == "2027-04-30"
    assert items["mx_constancias_intereses"]["date"] == "2027-02-15"
    assert items["mx_ppr_151v_contributions"]["days_until"] == 101
    assert items["mx_foreign_dividend_10pct_2026_12"]["date"] == "2027-01-17"
    assert items["us_fbar"]["jurisdiction"] == "US"
    dates = [item["date"] for item in report["result"]["items"]]
    assert dates == sorted(dates)


def test_tax_calendar_needs_year_and_reports_unknown_flags():
    assert mexico.tax_calendar({})["status"] == "needs_input"
    report = mexico.tax_calendar({"tax_year": 2026})
    assert report["status"] == "partial"
    assert len(report["missing"]) == 4
    with pytest.raises(ValueError):
        mexico.tax_calendar({"tax_year": 2026, "us_person": "yes"})


def test_run_dispatch_validates_task():
    with pytest.raises(ValueError):
        mexico.run("tax", {}, {})
    assert mexico.run("mx_calendar", {"tax_year": 2026}, {})["status"] == "partial"
