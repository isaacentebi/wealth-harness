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


def test_art135_definitive_option_tests_real_interest():
    # Nominal 105,000 exceeds the 100,000 limit, but real interest (105,000 - 4% of 250,000 = 95,000) does not.
    report = mexico.real_interest({"tax_year": 2026, "marginal_rate": "0.30",
                                   "accounts": [_account(nominal_interest_mxn=105000, average_daily_balance_mxn=250000)]})
    option = report["result"]["definitive_retention_option"]
    assert option["tested_amount_mxn"] == "95000.00"
    assert option["within_limit"] is True and "basis_note" not in option
    # An account without inflation inputs falls back to nominal, with a note.
    no_inpc = {"id": "bank", "nominal_interest_mxn": 20000, "average_daily_balance_mxn": 1, "days": 365}
    mixed = mexico.real_interest({"tax_year": 2026, "marginal_rate": "0.30",
                                  "accounts": [_account(nominal_interest_mxn=105000, average_daily_balance_mxn=250000), no_inpc]})
    option = mixed["result"]["definitive_retention_option"]
    assert option["tested_amount_mxn"] == "115000.00"
    assert option["within_limit"] is False
    assert "bank" in option["basis_note"] and "nominal" in option["basis_note"]
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
    # Art. 185 shares the Art. 151 global cap (150,000), which general deductions (200,000) already fill.
    art185 = report["result"]["contribution_scenarios"]["art185"]
    assert art185["deductible_mxn"] == "0.00"
    assert art185["non_deductible_excess_mxn"] == "200000.00"
    assert art185["estimated_isr_saving_mxn"] == "0.00"
    assert report["result"]["remaining_room"]["art185_mxn"] == "0.00"
    assert report["result"]["remaining_room"]["global_cap_mxn"] == "0.00"
    assert "Deferral" in art185["tradeoff"]


def test_art185_is_limited_by_remaining_global_cap_room():
    # 150,000 global cap - 50,000 general = 100,000 room, below the 152,000 Art. 185 limit; fr. V is outside the cap.
    report = mexico.personal_deductions(_deduction_inputs(deductions={"general_mxn": 50000, "retirement_151v_mxn": 50000, "art185_mxn": 0}))
    assert report["result"]["remaining_room"]["art185_mxn"] == "100000.00"
    art185 = report["result"]["contribution_scenarios"]["art185"]
    assert art185["deductible_mxn"] == "100000.00" and art185["non_deductible_excess_mxn"] == "100000.00"
    expected = mexico.isr_annual_tax(700000, TARIFF_2026) - mexico.isr_annual_tax(600000, TARIFF_2026)
    assert art185["estimated_isr_saving_mxn"] == format(expected.quantize(Decimal("0.01")), "f")
    # Existing Art. 185 deposits consume the shared room too.
    existing = mexico.personal_deductions(_deduction_inputs(deductions={"general_mxn": 50000, "retirement_151v_mxn": 0, "art185_mxn": 30000}))
    assert existing["result"]["allowed"]["art185_mxn"] == "30000.00"
    assert existing["result"]["remaining_room"]["art185_mxn"] == "70000.00"
    assert existing["result"]["remaining_room"]["global_cap_mxn"] == "70000.00"


_MORTGAGE = {"casa_habitacion": True, "financial_system_lender": True, "real_interest_paid_mxn": 80000,
             "credit_udis": 400000}


def test_mortgage_real_interest_counts_inside_the_global_cap():
    # 150,000 cap: 50,000 general + 80,000 real mortgage interest fit; the Art. 185 room shrinks to 20,000.
    report = mexico.personal_deductions(_deduction_inputs(
        deductions={"general_mxn": 50000, "retirement_151v_mxn": 0, "art185_mxn": 0}, mortgage=_MORTGAGE))
    assert report["status"] == "ready", report["missing"]
    result = report["result"]
    assert result["allowed"]["general_mxn"] == "50000.00"
    assert result["allowed"]["mortgage_real_interest_mxn"] == "80000.00"
    assert result["remaining_room"]["art185_mxn"] == "20000.00"
    assert result["mortgage_interest"]["deductible_before_cap_mxn"] == "80000.00"
    assert result["mortgage_interest"]["cut_by_global_cap_mxn"] == "0.00"
    assert any("fraccion IV" in s["title"] for s in report["sources"])
    # With 100,000 of general deductions the cap leaves 50,000 for the interest.
    capped = mexico.personal_deductions(_deduction_inputs(
        deductions={"general_mxn": 100000, "retirement_151v_mxn": 0, "art185_mxn": 0}, mortgage=_MORTGAGE))
    assert capped["result"]["allowed"]["mortgage_real_interest_mxn"] == "50000.00"
    assert capped["result"]["mortgage_interest"]["cut_by_global_cap_mxn"] == "30000.00"
    assert capped["result"]["allowed"]["total_personal_deductions_mxn"] == "150000.00"


def test_mortgage_above_750000_udis_keeps_the_proportional_interest():
    # 8,500,000 MXN of credit at 8.5 MXN per UDI = 1,000,000 UDIs; 750,000 / 1,000,000 of the interest counts.
    mortgage = {**{k: v for k, v in _MORTGAGE.items() if k != "credit_udis"}, "credit_amount_mxn": 8500000,
                "udi_value_at_origination": "8.5", "udi_source": "Banxico SIE, UDI on the contract date"}
    report = mexico.personal_deductions(_deduction_inputs(
        deductions={"general_mxn": 0, "retirement_151v_mxn": 0, "art185_mxn": 0}, mortgage=mortgage))
    block = report["result"]["mortgage_interest"]
    assert block["credit_udis"] == "1000000.00" and block["deductible_before_cap_mxn"] == "60000.00"  # 80,000 x 0.75
    assert any("750,000 UDIs" in w for w in report["warnings"])


def test_mortgage_unknowns_stay_null_with_a_reason():
    report = mexico.personal_deductions(_deduction_inputs(mortgage={"real_interest_paid_mxn": 80000}))
    block = report["result"]["mortgage_interest"]
    assert block["deductible_before_cap_mxn"] is None and block["reason"]
    assert report["result"]["allowed"]["mortgage_real_interest_mxn"] is None
    assert report["status"] == "partial"
    assert {"mortgage.casa_habitacion=true", "mortgage.financial_system_lender=true"} <= set(report["missing"])
    assert any(m.startswith("mortgage.credit_udis") for m in report["missing"])
    # The lender's constancia figure is enough on its own once the home and lender are confirmed.
    constancia = mexico.personal_deductions(_deduction_inputs(mortgage={
        "casa_habitacion": True, "financial_system_lender": True, "constancia_deductible_real_interest_mxn": 42000}))
    assert constancia["result"]["mortgage_interest"]["deductible_before_cap_mxn"] == "42000.00"
    assert not [m for m in constancia["missing"] if m.startswith("mortgage")]


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
    # Without the year's other Art. 129 results and carryforwards, only the scenario-only figure is shown.
    assert totals["article_129_tax_before_other_results_mxn"] == "7200.00"
    assert totals["article_129_tax_mxn"] is None
    assert report["status"] == "partial"
    assert "article_129_realized_gain_or_loss_mxn" in report["missing"]
    assert "article_129_loss_carryforwards" in report["missing"]
    assert report["result"]["net_estimated_mexican_tax_mxn"] is None
    assert "article_129_incremental_tax_mxn" in report["result"]["unknown_tax_components"]
    assert totals["progressive_net_gain_or_loss_mxn"] == "36000.00"  # only the non-SIC sale is progressive income
    assert any("contested" in w for w in report["warnings"])
    assert any("constancia" in w for w in report["warnings"])
    missing = mexico.foreign_securities({"tax_year": 2026, "marginal_rate": "0.3", "sales": [{**sale, "id": "x"}]})
    assert "sales[0].sic_listed" in missing["missing"]


def test_sic_sale_nets_against_other_article_129_results_and_carryforwards():
    sale = {"id": "aapl-ibkr", "currency": "USD", "proceeds": 12000, "fx_sale": "18", "cost": 10000, "fx_acquisition": "18",
            "acquired_on": "2022-01-10", "sold_on": "2026-05-01", "sic_listed": True}  # 36,000 MXN gain
    report = mexico.foreign_securities({"tax_year": 2026, "marginal_rate": "0.3", "sales": [sale],
                                        "article_129_realized_gain_or_loss_mxn": -10000,
                                        "article_129_loss_carryforwards": [
                                            {"origin_year": 2020, "available_updated_mxn": 6000, "updated_through": "2026-04"},
                                            {"origin_year": 2014, "available_updated_mxn": 50000, "updated_through": "2026-04"}]})
    assert report["status"] == "ready", report["missing"]
    totals = report["result"]["totals"]
    assert totals["article_129_tax_before_other_results_mxn"] == "3600.00"
    assert totals["article_129_tax_mxn"] == "2000.00"  # (36,000 - 10,000 - 6,000) x 10%; the 2014 loss has expired
    netting = report["result"]["article_129_netting"]
    assert netting["after_scenario"]["carry_used_mxn"] == "6000.00"
    assert [c["eligible_this_year"] for c in netting["loss_carryforwards"]] == [True, False]
    assert report["result"]["known_tax_components"]["article_129_incremental_tax_mxn"] == "2000.00"
    assert report["result"]["net_estimated_mexican_tax_mxn"] == "2000.00"


def test_dividend_paid_on_is_parsed_and_scoped_to_tax_year():
    dividend = {"id": "d1", "currency": "USD", "gross": 100, "withheld": 10, "fx": 18, "source_country": "US", "w8ben_on_file": True}
    with pytest.raises(ValueError, match="paid_on"):
        mexico.foreign_securities({"tax_year": 2026, "marginal_rate": "0.3", "dividends": [{**dividend, "paid_on": "2026-13-01"}]})
    report = mexico.foreign_securities({"tax_year": 2026, "marginal_rate": "0.3", "dividends": [
        {**dividend, "paid_on": "2025-12-31"}, {**dividend, "id": "d2", "paid_on": "2026-06-30"}]})
    assert [row["id"] for row in report["result"]["dividends"]] == ["d2"]
    assert report["result"]["dividends"][0]["paid_on"] == "2026-06-30"
    assert report["result"]["totals"]["dividends_gross_mxn"] == "1800.00"
    assert any("d1" in w and "outside tax_year" in w for w in report["warnings"])


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
