from __future__ import annotations

import pytest

from wealth import tax


def _household(*lots, as_of="2026-04-15", complete=True):
    instruments = {lot["instrument_id"] for lot in lots}
    return {
        "currency": "USD",
        "as_of": as_of,
        "complete": complete,
        "people": [{"id": "p1"}, {"id": "spouse"}],
        "accounts": [
            {"id": "taxable", "owner_id": "p1", "tax_unit": "joint", "type": "taxable", "currency": "USD"},
            {"id": "spouse-ira", "owner_id": "spouse", "tax_unit": "joint", "type": "ira", "currency": "USD"},
        ],
        "positions": [
            {"id": f"pos-{instrument}", "account_id": "taxable", "instrument_id": instrument, "symbol": instrument, "quantity": 100, "value": 1000, "currency": "USD"}
            for instrument in instruments
        ],
        "lots": list(lots),
    }


def _lot(lot_id, instrument, acquired, basis, *, currency="USD"):
    return {
        "id": lot_id,
        "account_id": "taxable",
        "instrument_id": instrument,
        "quantity": 100,
        "acquired_on": acquired,
        "cost_basis": basis,
        "currency": currency,
    }


def _coverage(sale_date="2026-02-28"):
    return {
        "accounts_complete": True,
        "identity_mapping_complete": True,
        "automatic_reinvestment_reviewed": True,
        "spouse_accounts_reviewed": True,
        "controlled_accounts_reviewed": True,
        "purchases_from": "2026-01-01",
        "purchases_through": "2026-04-15",
    }


def _us_facts(sale_date="2026-02-28", **overrides):
    facts = {
        "short_term_gains": 0,
        "short_term_losses": 0,
        "long_term_gains": 0,
        "long_term_losses": 0,
        "short_term_loss_carryover": 0,
        "long_term_loss_carryover": 0,
        "ordinary_income_loss_deduction_available": 3000,
        "complete": True,
        "as_of": sale_date,
    }
    facts.update(overrides)
    return facts


def test_us_calendar_character_and_schedule_d_incremental_netting():
    household = _household(
        _lot("st", "ST", "2025-02-28", 2500),
        _lot("lt", "LT", "2025-02-27", 1500),
    )
    packet = tax.run(
        "tax",
        {
            "jurisdiction": "US",
            "household": household,
            "sale_date": "2026-02-28",
            "as_of": "2026-04-15",
            "mode": "rebalance",
            "sales": [{"lot_id": "st", "quantity": 100}, {"lot_id": "lt", "quantity": 100}],
            "prices": [
                {"instrument_id": "ST", "price": 10, "currency": "USD", "as_of": "2026-02-28", "source": "broker quote"},
                {"instrument_id": "LT", "price": 10, "currency": "USD", "as_of": "2026-02-28", "source": "broker quote"},
            ],
            "us_tax_facts": _us_facts(short_term_gains=1000, long_term_gains=2000),
            "filing_status": "single",
            "rates": {"ordinary": "0.37", "long_term": "0.15"},
            "wash_sale_coverage": _coverage(),
        },
        {},
    )

    assert packet["status"] == "ready"
    assert [row["character"] for row in packet["result"]["candidates"]] == ["short_term", "long_term"]
    assert packet["result"]["netting"]["after_scenario"]["net_long_term"] == "1000.00"
    assert packet["result"]["incremental_tax_estimate"]["incremental_tax"] == "-520.00"
    assert "guaranteed savings" in packet["result"]["incremental_tax_estimate"]["method"]


def test_us_related_ira_mapping_conflict_excludes_entire_lot_and_open_window_is_provisional():
    household = _household(_lot("loss", "FUND-A", "2024-01-01", 2000), as_of="2026-03-01")
    packet = tax.run(
        "tax",
        {
            "jurisdiction": "US",
            "household": household,
            "sale_date": "2026-03-01",
            "as_of": "2026-03-01",
            "prices": [{"instrument_id": "FUND-A", "price": 10, "currency": "USD", "as_of": "2026-03-01", "source": "broker"}],
            "purchases": [{"account_id": "spouse-ira", "instrument_id": "FUND-B", "trade_date": "2026-02-20", "quantity": 10}],
            "substantially_identical_groups": [{"id": "same-index", "instrument_ids": ["FUND-A", "FUND-B"]}],
            "wash_sale_coverage": {**_coverage("2026-03-01"), "purchases_through": "2026-03-01"},
        },
        {},
    )

    row = packet["result"]["candidates"][0]
    assert packet["status"] == "partial"
    assert packet["result"]["review_status"] == "provisional"
    assert packet["result"]["execution_ready"] is False
    assert row["scenario_treatment"] == "excluded_entire_lot_conservatively"
    assert row["wash_conflicts"][0]["account_type"] == "ira"
    assert packet["result"]["included_scenario_gain_or_loss"]["long_term"] == "0.00"
    assert packet["result"]["netting"] is None
    assert "us_tax_facts.short_term_gains" in packet["missing"]
    assert "filing_status" in packet["missing"]
    assert any("future replacement-purchase window" in reason for reason in packet["result"]["provisional_reasons"])


def test_us_missing_rates_still_returns_verified_losses_without_inventing_savings():
    household = _household(_lot("loss", "X", "2020-01-01", 2000))
    packet = tax.run(
        "tax",
        {
            "jurisdiction": "US",
            "household": household,
            "sale_date": "2026-02-28",
            "as_of": "2026-04-15",
            "prices": [{"instrument_id": "X", "price": 10, "currency": "USD", "as_of": "2026-02-28", "source": "broker"}],
            "wash_sale_coverage": _coverage(),
            "us_tax_facts": _us_facts(),
            "filing_status": "single",
        },
        {},
    )

    assert packet["status"] == "partial"
    assert packet["result"]["candidates"][0]["gain_or_loss"] == "-1000.00"
    assert packet["result"]["incremental_tax_estimate"] is None
    assert packet["missing"] == ["rates.ordinary", "rates.long_term"]


def test_mexico_article_129_uses_only_verified_mxn_adjusted_basis_and_ten_year_carry():
    lot = _lot("mx", "BMV-X", "2023-01-01", 18000, currency="MXN")
    household = _household(lot, as_of="2026-12-31")
    household["accounts"][0]["currency"] = "MXN"
    packet = tax.run(
        "tax",
        {
            "jurisdiction": "MX_ARTICLE_129",
            "household": household,
            "as_of": "2026-12-31",
            "article_129_sales": [{
                "lot_id": "mx", "quantity": 100, "proceeds_mxn": 12000,
                "article_129_adjusted_basis_mxn": 18000,
                "basis_source": "broker Article 129 annual statement",
                "proceeds_source": "broker execution statement",
                "article_129_eligibility": {
                    "individual_taxpayer": True,
                    "eligible_security": True,
                    "eligible_venue": True,
                    "eligible_acquisition": True,
                    "no_exclusion_applies": True,
                    "source": "broker tax statement and taxpayer records",
                    "security_scope": "BMV-X",
                    "venue": "Bolsa Mexicana de Valores",
                    "acquisition_scope": "mx",
                },
            }],
            "article_129_realized_gain_or_loss_mxn": 10000,
            "article_129_loss_carryforwards": [
                {"origin_year": 2025, "available_updated_mxn": 2000, "updated_through": "2025-12",},
                {"origin_year": 2015, "available_updated_mxn": 9999, "updated_through": "2025-12",},
            ],
        },
        {},
    )

    assert packet["status"] == "ready"
    assert packet["result"]["netting"]["before_scenario"]["taxable_gain_mxn"] == "8000.00"
    assert packet["result"]["netting"]["after_scenario"]["taxable_gain_mxn"] == "2000.00"
    assert packet["result"]["incremental_tax_estimate"]["incremental_tax"] == "-600.00"
    assert packet["result"]["loss_carryforwards"][1]["eligible_this_year"] is False
    assert "no U.S." in packet["result"]["scope"]
    assert packet["sources"][0]["url"].endswith("/LISR.pdf")


def test_rejects_implicit_fx_and_requires_article_129_eligibility():
    usd = _household(_lot("usd", "X", "2024-01-01", 1000))
    with pytest.raises(ValueError, match="no FX is inferred"):
        tax.run(
            "tax",
            {
                "jurisdiction": "US", "household": usd, "sale_date": "2026-02-28", "as_of": "2026-04-15",
                "prices": [{"instrument_id": "X", "price": 9, "currency": "MXN", "as_of": "2026-02-28", "source": "quote"}],
                "wash_sale_coverage": _coverage(),
                "us_tax_facts": _us_facts(),
                "filing_status": "single",
            },
            {},
        )

    with pytest.raises(ValueError, match="verified sale-date quote"):
        tax.run(
            "tax",
            {
                "jurisdiction": "US", "household": usd, "sale_date": "2026-02-28", "as_of": "2026-04-15",
                "prices": [{"instrument_id": "X", "price": 9, "currency": "USD", "as_of": "2026-02-27", "source": "stale quote"}],
                "wash_sale_coverage": _coverage(), "us_tax_facts": _us_facts(), "filing_status": "single",
            },
            {},
        )

    mx = _household(_lot("mx", "M", "2024-01-01", 1000, currency="MXN"))
    packet = tax.run("tax", {"jurisdiction": "MX", "household": mx}, {})
    assert packet["status"] == "needs_input"
    assert packet["missing"] == ["article_129_sales"]

    eligibility = {
        "individual_taxpayer": True, "eligible_security": True, "eligible_venue": True,
        "eligible_acquisition": True, "no_exclusion_applies": True, "source": "broker and taxpayer records",
        "security_scope": "M", "venue": "BMV", "acquisition_scope": "mx",
    }
    sale = {
        "lot_id": "mx", "quantity": 60, "proceeds_mxn": 500,
        "article_129_adjusted_basis_mxn": 600, "basis_source": "broker", "proceeds_source": "broker",
        "article_129_eligibility": eligibility,
    }
    with pytest.raises(ValueError, match="duplicate Article 129 sale row"):
        tax.run(
            "tax",
            {"jurisdiction": "MX", "household": mx, "as_of": "2026-12-31", "article_129_sales": [sale, {**sale, "quantity": 50}]},
            {},
        )
