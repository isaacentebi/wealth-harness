from __future__ import annotations

from decimal import Decimal

import pytest

from wealth import estate


NRA = {"us_citizen": False, "green_card": False, "us_domiciled": False}


def _assets():
    return [
        {"id": "voo", "type": "us_domiciled_fund", "value_usd": 300000, "custody": "us_broker"},
        {"id": "aapl-sic", "type": "us_stock", "value_usd": 200000, "custody": "sic"},
        {"id": "cspx", "type": "non_us_domiciled_fund", "value_usd": 400000},
        {"id": "deposit", "type": "us_bank_deposit", "value_usd": 50000},
        {"id": "tsy", "type": "us_debt_portfolio_interest", "value_usd": 25000},
        {"id": "fondo", "type": "mexican_fund", "value_usd": 100000},
    ]


def test_tentative_tax_schedule():
    assert estate.tentative_tax(Decimal(0)) == 0
    assert estate.tentative_tax(Decimal(60000)) == Decimal(13000)
    assert estate.tentative_tax(Decimal(1000000)) == Decimal(345800)
    assert estate.tentative_tax(Decimal(2000000)) == Decimal(745800)


def test_nra_exposure_counts_only_us_situs_and_flags_sic():
    report = estate.us_estate_exposure({"year": 2026, "decedent": NRA, "assets": _assets(), "estimated_deductions_usd": 100000})
    assert report["status"] == "ready", report["missing"]
    exposure = report["result"]["exposure"]
    assert exposure["us_situs_total_usd"] == "500000.00"
    assert exposure["of_which_held_via_sic_usd"] == "200000.00"
    assert exposure["estimated_tax_range_usd"] == {"low": "108800.00", "high": "142800.00"}
    assert exposure["form_706na_likely_required"] is True
    rows = {row["id"]: row for row in report["result"]["assets"]}
    assert rows["cspx"]["us_situs"] is False and rows["deposit"]["us_situs"] is False
    assert "uncertain" in rows["aapl-sic"]["situs_note"]
    assert estate.CONSULT_FLAG in report["result"]["flags"]
    assert any("no US-Mexico estate" in w for w in report["warnings"])
    structures = [alt["structure"] for alt in report["result"]["alternatives"]]
    assert any("UCITS" in s for s in structures) and any("Mexican-domiciled" in s for s in structures)


def test_nra_below_exemption_has_no_tax():
    report = estate.us_estate_exposure({"year": 2026, "decedent": NRA, "assets": [{"id": "x", "type": "us_stock", "value_usd": 60000}]})
    assert report["result"]["exposure"]["estimated_tax_range_usd"] == {"low": "0.00", "high": "0.00"}
    assert report["result"]["exposure"]["form_706na_likely_required"] is False


def test_us_citizen_worldwide_with_verified_exclusion_and_pfic_warning():
    report = estate.us_estate_exposure({"year": 2026, "decedent": {"us_citizen": True}, "adjusted_taxable_gifts_usd": 0,
                                        "assets": [{"id": "w", "type": "mexican_real_estate", "value_usd": 20000000},
                                                   {"id": "cash", "type": "us_brokerage_cash", "value_usd": 0}]})
    assert report["status"] == "ready", report["missing"]
    assert report["result"]["exposure"]["estimated_tax_range_usd"]["high"] == "2000000.00"
    assert report["result"]["parameters_used"][0]["value"] == "15000000"
    assert any("PFIC" in w for w in report["warnings"])
    assert len(report["result"]["alternatives"]) == 2


def test_us_person_fails_closed_without_gifts_or_unverified_exclusion():
    no_gifts = estate.us_estate_exposure({"year": 2026, "decedent": {"us_citizen": True}, "assets": _assets()})
    assert no_gifts["status"] == "needs_input"
    assert any(m.startswith("adjusted_taxable_gifts_usd") for m in no_gifts["missing"])
    unverified = estate.us_estate_exposure({"year": 2027, "decedent": {"us_citizen": True}, "adjusted_taxable_gifts_usd": 0, "assets": _assets()})
    assert unverified["status"] == "needs_input"
    assert any("basic_exclusion_amount_usd" in m for m in unverified["missing"])
    supplied = estate.us_estate_exposure({"year": 2027, "decedent": {"us_citizen": True}, "adjusted_taxable_gifts_usd": 0, "assets": _assets(),
                                          "parameters": {"basic_exclusion_amount_usd": {"value": 15400000, "source": "IRS Rev. Proc. for 2027"}}})
    assert supplied["status"] == "ready"
    assert supplied["result"]["exposure"]["estimated_tax_range_usd"]["high"] == "0.00"


def test_domicile_and_asset_types_are_not_guessed():
    assert estate.us_estate_exposure({})["status"] == "needs_input"
    green = estate.us_estate_exposure({"year": 2026, "decedent": {"us_citizen": False, "green_card": True}, "assets": _assets()})
    assert green["status"] == "needs_input"
    assert any(m.startswith("decedent.us_domiciled") for m in green["missing"])
    report = estate.us_estate_exposure({"year": 2026, "decedent": NRA, "assets": [
        {"id": "a", "type": "crypto", "value_usd": 1}, {"id": "b", "type": "us_brokerage_cash", "value_usd": 1}, {"id": "c", "type": "us_stock"}]})
    assert report["status"] == "partial"
    assert len(report["missing"]) == 3
    with pytest.raises(ValueError):
        estate.us_estate_exposure({"year": 2026, "decedent": {"us_citizen": "no"}, "assets": _assets()})


def test_non_citizen_spouse_marital_deduction_not_assumed():
    report = estate.us_estate_exposure({"year": 2026, "decedent": {"us_citizen": True, "spouse_us_citizen": False}, "adjusted_taxable_gifts_usd": 0,
                                        "marital_deduction_usd": 5000000, "assets": [{"id": "w", "type": "foreign_stock", "value_usd": 20000000}]})
    assert report["result"]["exposure"]["estimated_tax_range_usd"]["high"] == "2000000.00"
    assert any("QDOT" in w for w in report["warnings"])


def test_run_dispatch():
    with pytest.raises(ValueError):
        estate.run("tax", {}, {})
    assert estate.run("estate", {}, {})["status"] == "needs_input"
