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


def test_us_related_ira_replacement_disallows_only_matched_shares_permanently_and_open_window_is_provisional():
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
    assert row["scenario_treatment"] == "loss_partially_disallowed_wash_sale"
    match = row["wash_sale"]["replacements"][0]
    assert match["account_type"] == "traditional_ira" and match["permanent"] is True and match["shares"] == "10"
    # Only 10 of 100 loss shares were replaced: $100 of the $1,000 loss is disallowed, permanently (IRA).
    assert row["wash_sale"]["permanently_disallowed_loss"] == "100.00"
    assert packet["result"]["included_scenario_gain_or_loss"]["long_term"] == "-900.00"
    assert packet["result"]["wash_sale"]["replacement_basis_adjustments"] == []
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
    assert packet["missing"] == ["us_return_facts (bracket engine) or rates (marginal override)"]


def test_mexico_article_129_uses_only_verified_mxn_adjusted_basis_and_ten_year_carry():
    lot = _lot("mx", "BMV-X", "2023-01-01", 18000, currency="MXN")
    household = _household(lot, as_of="2026-09-15")
    household["accounts"][0]["currency"] = "MXN"
    packet = tax.run(
        "tax",
        {
            "jurisdiction": "MX_ARTICLE_129",
            "household": household,
            "as_of": "2026-09-15",
            "sale_date": "2026-09-15",
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
            {"jurisdiction": "MX", "household": mx, "sale_date": "2026-03-31", "article_129_sales": [sale, {**sale, "quantity": 50}]},
            {},
        )


# --- Wash-sale share matching, bracket engine, lot selection and harvesting --------

from decimal import Decimal  # noqa: E402

from wealth import us_tax_parameters as params  # noqa: E402

SALE = "2026-03-20"
AS_OF = "2026-04-25"


def _multi_household(*lots, as_of=AS_OF):
    household = _household(*lots, as_of=as_of)
    for position in household["positions"]:
        position["quantity"] = sum(lot["quantity"] for lot in lots if lot["instrument_id"] == position["instrument_id"])
    return household


def _sized(lot_id, instrument, acquired, basis, quantity):
    return {**_lot(lot_id, instrument, acquired, basis), "quantity": quantity}


def _us(household, *, prices, **extra):
    inputs = {
        "jurisdiction": "US", "household": household, "sale_date": SALE, "as_of": AS_OF,
        "prices": [{"instrument_id": key, "price": value, "currency": "USD", "as_of": SALE, "source": "quote"} for key, value in prices.items()],
        "wash_sale_coverage": {**_coverage(), "purchases_from": "2026-01-01", "purchases_through": AS_OF},
        "us_tax_facts": _us_facts(SALE), "filing_status": "single",
    }
    inputs.update(extra)
    return tax.run("tax", inputs, {})


def _return_facts(ordinary=100000, **overrides):
    facts = {"tax_year": 2026, "complete": True, "ordinary_taxable_income": ordinary, "qualified_dividends": 0,
             "magi_excluding_capital_gains": ordinary, "net_investment_income_excluding_capital_gains": 0}
    facts.update(overrides)
    return facts


def test_same_day_acquisition_is_short_term_not_an_error():
    household = _multi_household(_sized("old", "X", "2025-01-02", 1000, 50), _sized("new", "X", SALE, 1000, 50))
    packet = _us(household, prices={"X": 8}, rates={"ordinary": 0.37, "long_term": 0.2})
    rows = {row["lot_id"]: row for row in packet["result"]["candidates"]}
    assert rows["new"]["character"] == "short_term"
    assert rows["old"]["character"] == "long_term"
    # Both lots are sold, so neither replaces the other.
    assert packet["result"]["wash_sale"]["disallowed_loss"] == "0.00"


def test_household_lot_in_window_is_a_replacement_with_share_matching_basis_and_tacking():
    household = _multi_household(_sized("old", "X", "2025-01-02", 2000, 100), _sized("recent", "X", "2026-03-10", 280, 40))
    packet = _us(household, prices={"X": 8}, mode="rebalance", sales=[{"lot_id": "old", "quantity": 100}],
                 rates={"ordinary": 0.37, "long_term": 0.2})
    row = packet["result"]["candidates"][0]
    assert row["gain_or_loss"] == "-1200.00"
    assert row["wash_sale"]["matched_shares"] == "40"
    assert row["wash_sale"]["disallowed_loss"] == "480.00"
    assert row["recognized_gain_or_loss"] == "-720.00"
    assert row["scenario_treatment"] == "loss_partially_disallowed_wash_sale"
    assert packet["result"]["included_scenario_gain_or_loss"]["long_term"] == "-720.00"
    adjustment = packet["result"]["wash_sale"]["replacement_basis_adjustments"][0]
    assert adjustment == {
        "replacement": "lot:recent", "account_id": "taxable", "instrument_id": "X", "shares": "40",
        "basis_increase": "480.00", "holding_period_tacked_days": 442,
        "adjusted_holding_period_start": "2024-12-23", "from_lot_id": "old",
    }

    # Substantially identical purchases after the sale fill the remaining shares in acquisition order.
    packet = _us(household, prices={"X": 8}, mode="rebalance", sales=[{"lot_id": "old", "quantity": 100}],
                 purchases=[{"account_id": "taxable", "instrument_id": "Y", "trade_date": "2026-04-01", "quantity": 100}],
                 substantially_identical_groups=[{"id": "same-index", "instrument_ids": ["X", "Y"]}],
                 rates={"ordinary": 0.37, "long_term": 0.2})
    row = packet["result"]["candidates"][0]
    assert [match["shares"] for match in row["wash_sale"]["replacements"]] == ["40", "60"]
    assert row["recognized_gain_or_loss"] == "0.00"
    assert row["scenario_treatment"] == "loss_disallowed_wash_sale"


def test_purchase_that_created_the_sold_lot_is_not_its_own_replacement():
    household = _multi_household(_sized("L", "X", "2026-03-05", 1000, 100))
    purchase = {"account_id": "taxable", "instrument_id": "X", "trade_date": "2026-03-05", "quantity": 100}
    for linked in (purchase, {**purchase, "lot_id": "L", "quantity": 60}):
        packet = _us(household, prices={"X": 8}, mode="rebalance", sales=[{"lot_id": "L", "quantity": 100}],
                     purchases=[linked], rates={"ordinary": 0.37, "long_term": 0.2})
        row = packet["result"]["candidates"][0]
        assert row["recognized_gain_or_loss"] == "-200.00"
        assert row["scenario_treatment"] == "included"
        assert packet["result"]["wash_sale"]["linked_purchases"][0]["lot_id"] == "L"
    with pytest.raises(ValueError, match="does not match"):
        _us(household, prices={"X": 8}, mode="rebalance", sales=[{"lot_id": "L", "quantity": 100}],
            purchases=[{**purchase, "trade_date": "2026-03-06", "lot_id": "L"}])


def test_unsold_remainder_of_the_loss_lot_is_not_a_replacement():
    household = _multi_household(_sized("L", "X", "2026-03-05", 1000, 100))
    packet = _us(household, prices={"X": 8}, mode="rebalance", sales=[{"lot_id": "L", "quantity": 50}],
                 rates={"ordinary": 0.37, "long_term": 0.2})
    row = packet["result"]["candidates"][0]
    assert row["recognized_gain_or_loss"] == "-100.00"
    assert row["wash_sale"]["matched_shares"] == "0"


@pytest.mark.parametrize(("year", "status", "income", "expected"), [
    (2025, "single", "626350", "188769.75"),
    (2025, "head_of_household", "626350", "187031.50"),
    (2025, "married_filing_jointly", "751600", "202154.50"),
    (2025, "married_filing_separately", "375800", "101077.25"),
    (2026, "single", "640600", "192979.25"),
    (2026, "head_of_household", "640600", "191171.00"),
    (2026, "married_filing_jointly", "768700", "206583.50"),
    (2026, "qualifying_surviving_spouse", "768700", "206583.50"),
    (2026, "married_filing_separately", "384350", "103291.75"),
])
def test_bracket_tables_reproduce_the_revenue_procedure_base_amounts(year, status, income, expected):
    table = params.parameters(year)
    assert params.ordinary_tax(table, status, Decimal(income)) == Decimal(expected)
    assert table["source"]["url"].startswith("https://www.irs.gov/pub/irs-drop/rp-")


def test_preferential_rates_stack_on_ordinary_income_and_niit_uses_magi_threshold():
    table = params.parameters(2026)
    stacked = params.federal_tax(
        table, "single", ordinary_income=Decimal(40000), qualified_dividends=Decimal(0),
        net_short_term_gain=Decimal(0), net_capital_gain=Decimal(20000), capital_loss_deduction=Decimal(0),
        magi_excluding_capital_gains=Decimal(40000), nii_excluding_capital_gains=Decimal(0),
    )
    # 9,450 fills the 0% band to 49,450; the remaining 10,550 is taxed at 15%.
    assert stacked["preferential_at_0"] == Decimal(9450)
    assert stacked["preferential_tax"] == Decimal("1582.50")
    assert stacked["ordinary_tax"] == Decimal(4552)
    assert stacked["niit"] == 0

    niit = params.federal_tax(
        table, "married_filing_jointly", ordinary_income=Decimal(280000), qualified_dividends=Decimal(0),
        net_short_term_gain=Decimal(0), net_capital_gain=Decimal(10000), capital_loss_deduction=Decimal(0),
        magi_excluding_capital_gains=Decimal(300000), nii_excluding_capital_gains=Decimal(20000),
    )
    assert niit["niit"] == Decimal("1140.000")  # 3.8% x min(30,000 NII, 60,000 over the 250,000 threshold)


def test_bracket_engine_scenario_and_fail_closed_for_unsupported_years():
    household = _multi_household(_sized("L", "X", "2025-01-02", 5000, 100))
    packet = _us(household, prices={"X": 10}, mode="rebalance", sales=[{"lot_id": "L", "quantity": 100}],
                 us_tax_facts=_us_facts(SALE, long_term_gains=10000), us_return_facts=_return_facts(150000))
    estimate = packet["result"]["incremental_tax_estimate"]
    assert packet["status"] == "ready"
    assert estimate["incremental_tax"] == "-600.00"  # 4,000 less long-term gain at 15%
    assert estimate["tax_year"] == 2026
    assert "Rev. Proc. 2025-32" in estimate["parameters_source"]["title"]
    assert estimate["marginal_rates_before_scenario"]["long_term_gain"] == "0.15"
    assert "2026 sales" in packet["sources"][0]["version"]

    old = _multi_household(_sized("L", "X", "2023-01-02", 5000, 100), as_of="2024-06-03")
    packet = tax.run("tax", {
        "jurisdiction": "US", "household": old, "sale_date": "2024-06-03", "as_of": "2024-07-10",
        "mode": "rebalance", "sales": [{"lot_id": "L", "quantity": 100}],
        "prices": [{"instrument_id": "X", "price": 10, "currency": "USD", "as_of": "2024-06-03", "source": "q"}],
        "us_tax_facts": _us_facts("2024-06-03"), "filing_status": "single",
        "us_return_facts": _return_facts(tax_year=2024),
    }, {})
    assert packet["result"]["incremental_tax_estimate"] is None
    assert "verified U.S. federal parameters for tax year 2024 (or rates as an explicit marginal override)" in packet["missing"]


def test_loss_limit_and_carryforward_preserve_character():
    household = _multi_household(_sized("st", "S", "2025-12-01", 6000, 100), _sized("lt", "L", "2024-01-02", 3000, 100))
    packet = _us(household, prices={"S": 10, "L": 10}, mode="rebalance",
                 sales=[{"lot_id": "st", "quantity": 100}, {"lot_id": "lt", "quantity": 100}],
                 us_return_facts=_return_facts())
    after = packet["result"]["netting"]["after_scenario"]
    assert after["ordinary_income_loss_deduction"] == "3000.00"
    assert after["short_term_carryforward"] == "2000.00"
    assert after["long_term_carryforward"] == "2000.00"
    assert packet["result"]["incremental_tax_estimate"]["incremental_tax"] == "-660.00"  # 3,000 at 22%


def _selection_household():
    return _multi_household(
        _sized("A", "X", "2024-01-02", 500, 100),    # long-term gain of 5/share at 10
        _sized("B", "X", "2025-12-01", 1500, 100),   # short-term loss of 5/share
        _sized("C", "X", "2025-06-01", 900, 100),    # short-term gain of 1/share
    )


def test_lot_selection_compares_methods_and_tax_minimiser_wins():
    packet = _us(_selection_household(), prices={"X": 10}, mode="lot_selection",
                 target={"instrument_id": "X", "proceeds": 1000}, specific_lots=[{"lot_id": "C", "quantity": 100}],
                 us_tax_facts=_us_facts(SALE, short_term_gains=2000), us_return_facts=_return_facts())
    methods = packet["result"]["methods"]
    assert [lot["lot_id"] for lot in methods["fifo"]["lots"]] == ["A"]
    assert [lot["lot_id"] for lot in methods["lifo"]["lots"]] == ["B"]
    assert [lot["lot_id"] for lot in methods["hifo"]["lots"]] == ["B"]
    assert methods["fifo"]["incremental_tax"] == "75.00"        # 500 LTCG at 15%
    assert methods["specific_id"]["incremental_tax"] == "22.00"  # 100 STCG at 22%
    assert methods["tax_min"]["incremental_tax"] == "-110.00"    # 500 ST loss against ST gains at 22%
    assert [lot["lot_id"] for lot in methods["tax_min"]["lots"]] == ["B"]
    assert packet["result"]["lowest_tax_method"] == "lifo"
    assert packet["result"]["tax_min_search"]["exhaustive_vertex_search"] is True
    assert packet["result"]["target"]["quantity"] == "100"

    # A post-sale purchase of the same security washes the loss lot's benefit; the selector prices that in.
    washed = _us(_selection_household(), prices={"X": 10}, mode="lot_selection",
                 target={"instrument_id": "X", "quantity": 100}, methods=["hifo", "tax_min"],
                 purchases=[{"account_id": "taxable", "instrument_id": "X", "trade_date": "2026-03-25", "quantity": 100}],
                 us_tax_facts=_us_facts(SALE, short_term_gains=2000), us_return_facts=_return_facts())
    assert washed["result"]["methods"]["hifo"]["wash_sale_disallowed_loss"] == "500.00"
    assert Decimal(washed["result"]["methods"]["tax_min"]["incremental_tax"]) <= Decimal(washed["result"]["methods"]["hifo"]["incremental_tax"])
    # The washed loss produces no current benefit (0.00) but still beats realizing gains elsewhere.
    assert washed["result"]["methods"]["tax_min"]["incremental_tax"] == "0.00"

    with pytest.raises(ValueError, match="shares; 300"):
        _us(_selection_household(), prices={"X": 10}, mode="lot_selection", target={"instrument_id": "X", "quantity": 301})


def test_year_end_harvest_report_ranks_losses_and_finds_where_benefit_is_exhausted():
    household = _multi_household(
        _sized("l1", "S", "2025-12-01", 4000, 100),
        _sized("l2", "L", "2024-01-02", 4000, 100),
        _sized("l3", "W", "2024-01-02", 2000, 100),
        _sized("g1", "G", "2025-04-01", 500, 100),
    )
    packet = _us(household, prices={"S": 10, "L": 10, "W": 10, "G": 10}, mode="harvest_report",
                 us_tax_facts=_us_facts(SALE, short_term_gains=1000), us_return_facts=_return_facts())
    report = packet["result"]
    assert [row["lot_id"] for row in report["candidates"]] == ["l1", "l2", "l3"]
    assert report["candidates"][0]["standalone_tax_reduction"] == "660.00"
    assert report["candidates"][0]["tax_reduction_per_dollar_of_loss"] == "0.22"
    assert report["candidates"][0]["repurchase_not_before"] == "2026-04-20"
    steps = report["cumulative_plan"]
    assert [step["marginal_tax_reduction"] for step in steps] == ["660.00", "220.00", "0.00"]
    assert steps[1]["carryforward_after"] == {"short_term": "0.00", "long_term": "2000.00"}
    assert report["current_year_benefit_exhausted_at_lot"] == "l3"
    assert report["last_trade_date_for_tax_year"] == "2026-12-31"
    assert report["short_term_gains_turning_long_term"][0]["lot_id"] == "g1"
    assert report["short_term_gains_turning_long_term"][0]["long_term_from"] == "2026-04-02"


def test_mexico_tax_year_follows_sale_date_not_as_of():
    lot = _lot("mx", "BMV-X", "2023-01-01", 18000, currency="MXN")
    household = _household(lot, as_of="2026-09-15")
    household["accounts"][0]["currency"] = "MXN"
    sale = {
        "lot_id": "mx", "quantity": 100, "proceeds_mxn": 12000, "article_129_adjusted_basis_mxn": 18000,
        "basis_source": "broker", "proceeds_source": "broker",
        "article_129_eligibility": {
            "individual_taxpayer": True, "eligible_security": True, "eligible_venue": True,
            "eligible_acquisition": True, "no_exclusion_applies": True, "source": "broker",
            "security_scope": "BMV-X", "venue": "BMV", "acquisition_scope": "mx",
        },
    }
    base = {"jurisdiction": "MX", "household": household, "as_of": "2027-01-10", "article_129_sales": [sale],
            "article_129_realized_gain_or_loss_mxn": 0,
            "article_129_loss_carryforwards": [{"origin_year": 2016, "available_updated_mxn": 100, "updated_through": "2025-12"}]}
    missing = tax.run("tax", base, {})
    assert missing["status"] == "needs_input"
    assert missing["missing"] == ["article_129_sales[0].sale_date"]
    packet = tax.run("tax", {**base, "article_129_sales": [{**sale, "sale_date": "2026-09-15"}]}, {})
    assert packet["result"]["tax_year"] == 2026
    assert packet["result"]["loss_carryforwards"][0]["eligible_this_year"] is True
    assert packet["result"]["sales"][0]["nominal_household_basis_mxn"] == "18000.00"
    assert packet["result"]["sales"][0]["acquired_on"] == "2023-01-01"
