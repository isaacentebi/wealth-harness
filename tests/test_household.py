import pytest

from wealth.household import run, validate_household


def household(**updates):
    value = {
        "currency": "USD",
        "as_of": "2026-09-20",
        "complete": True,
        "people": [{"id": "p1", "name": "Ana"}],
        "accounts": [{"id": "a1", "owner_id": "p1", "type": "taxable", "currency": "USD"}],
        "positions": [],
        "lots": [],
        "liabilities": [],
        "external_assets": [],
        "income_exposures": [],
        "fx": [],
        "fund_holdings": [],
    }
    value.update(updates)
    return value


def test_json_import_preserves_account_owner_instrument_and_real_lot():
    source = household(
        positions=[{"id": "pos-1", "account_id": "a1", "instrument_id": "isin:abc", "symbol": "ABC", "quantity": "2", "value": "250", "currency": "USD"}],
        lots=[{"id": "lot-1", "account_id": "a1", "instrument_id": "isin:abc", "quantity": "2", "acquired_on": "2024-03-02", "cost_basis": "180.50", "currency": "USD"}],
    )
    packet = run("import", {"format": "json", "data": source, "source": "broker export"}, {})

    imported = packet["household"]
    assert packet["status"] == "ready"
    assert imported["accounts"][0]["owner_id"] == "p1"
    assert imported["positions"][0]["instrument_id"] == "isin:abc"
    assert imported["lots"][0] == source["lots"][0]
    assert len(imported["lots"]) == 1


def test_csv_import_uses_explicit_mapping_and_defaults_without_inventing_lots():
    csv_data = "kind,identifier,owner,acct_type,ccy\npeople,p1,,,\naccounts,a1,p1,ira,USD\n"
    packet = run("import", {
        "format": "csv",
        "data": csv_data,
        "mapping": {
            "record_type": "kind",
            "people": {"id": "identifier"},
            "accounts": {"id": "identifier", "owner_id": "owner", "type": "acct_type", "currency": "ccy"},
        },
        "defaults": {"household": {"currency": "USD", "as_of": "2026-09-20", "complete": True, "unknown_sections": []}},
    }, {})

    assert packet["status"] == "ready"
    assert packet["household"]["accounts"] == [{"id": "a1", "owner_id": "p1", "type": "ira", "currency": "USD"}]
    assert packet["household"]["lots"] == []


def test_reconciliation_rejects_orphan_and_nonmatching_lots():
    bad = household(
        positions=[{"id": "pos-1", "account_id": "a1", "instrument_id": "x", "symbol": "X", "quantity": 2, "value": 20, "currency": "USD"}],
        lots=[{"id": "lot-1", "account_id": "a1", "instrument_id": "x", "quantity": 1, "acquired_on": "2024-01-01", "cost_basis": 8, "currency": "USD"}],
    )
    with pytest.raises(ValueError, match="lot quantities do not reconcile"):
        validate_household(bad)

    bad["lots"][0].update(instrument_id="other", quantity=2)
    with pytest.raises(ValueError, match="no matching account/instrument position"):
        validate_household(bad)

    bad = household(positions=[{"id": "short", "account_id": "a1", "instrument_id": "x", "symbol": "X", "quantity": -1, "value": -20, "currency": "USD"}])
    with pytest.raises(ValueError, match="nonnegative"):
        validate_household(bad)

    with pytest.raises(ValueError, match="cannot be in the future"):
        validate_household(household(as_of="2999-01-01"))


def test_exposure_nav_lookthrough_overlap_targets_and_unknown_residual():
    source = household(
        positions=[
            {"id": "fund-pos", "account_id": "a1", "instrument_id": "fund", "symbol": "FND", "quantity": 10, "value": 100, "currency": "USD", "asset_class": "fund"},
            {"id": "stock-pos", "account_id": "a1", "instrument_id": "stock", "symbol": "STK", "quantity": 5, "value": 50, "currency": "USD", "asset_class": "equity", "sector": "tech"},
        ],
        external_assets=[{"id": "home", "name": "Home", "value": 200, "currency": "USD", "asset_class": "real_estate", "liquid": False}],
        liabilities=[{"id": "mortgage", "value": 80, "currency": "USD"}, {"id": "unknown-debt", "value": None, "currency": "USD"}],
        income_exposures=[
            {"id": "salary", "description": "Technology employer", "sector": "tech", "country": "US", "currency": "USD", "annual_amount": 120000},
            {"id": "consulting", "description": "Independent consulting", "currency": "USD"},
        ],
        fund_holdings=[
            {"instrument_id": "fund", "as_of": "2026-09-20", "source": "issuer factsheet", "holdings": [
                {"instrument_id": "stock", "weight": "0.6", "symbol": "STK", "asset_class": "equity", "sector": "tech"},
                {"instrument_id": "bond", "weight": "0.2", "symbol": "BND", "asset_class": "fixed_income", "sector": "government"},
                {"instrument_id": "nested", "weight": "0.1", "symbol": "NEST", "asset_class": "fund"},
            ]},
            {"instrument_id": "nested", "as_of": "2026-09-20", "source": "issuer factsheet", "holdings": [
                {"instrument_id": "fund", "weight": "1", "symbol": "FND", "asset_class": "fund"},
            ]},
        ],
    )
    packet = run("exposure", {"household": source, "evaluation_date": "2026-09-20", "targets": {
        "asset_class": {"equity": "0.4"}, "sector": {"tech": "0.4"},
    }}, {})
    result = packet["result"]

    assert packet["status"] == "partial"
    assert result["known_assets"] == "350"
    assert result["known_nav"] == "270"
    assert result["liquid_capital"] == "150"
    assert result["coverage"] == {
        "household_complete": True,
        "household_age_days": 0,
        "household_stale": False,
        "excluded_value_records": 0,
        "unknown_liabilities": 1,
        "unknown_sections": [],
        "nonliquid_position_records": 0,
        "unattributed_value_records": 2,
        "income_exposure_gaps": 3,
        "lookthrough_complete": False,
        "rejected_fx_quotes": [],
        "weight_denominator": "known_assets",
    }
    assert result["overlap"] == [{
        "left_position_id": "fund-pos", "right_position_id": "stock-pos",
        "overlap_weight": "0.6", "shared_instruments": ["stock"],
    }]
    assert result["targets"][0]["status"] == "underweight"
    sector_target = next(row for row in result["targets"] if row["dimension"] == "sector")
    assert sector_target["status"] == "indeterminate"
    assert sector_target["measured_weight"] == "0.3142857142857142857142857143"
    assert sector_target["difference"] is None
    assert sector_target["difference_range"] is not None
    instruments = {row["name"]: row["value"] for row in result["exposures"]["instrument"]}
    assert instruments["unknown:residual:fund"] == "10"
    assert instruments["unknown:cycle:fund"] == "10"
    assert result["income_exposures"][0]["annual_amount"] == {"amount": "120000", "currency": "USD"}
    assert result["income_exposures"][1]["annual_amount"] is None
    assert "annual_amount is unknown" in result["income_exposures"][1]["gaps"]
    salary_links = {link["dimension"]: link for link in result["economic_links"][0]["links"]}
    assert salary_links["sector"]["known_asset_value"] == "110"
    assert salary_links["sector"]["weight_basis"] == "known_assets"
    assert "correlation or causation" in result["economic_links"][0]["interpretation"]


def test_stale_fx_and_fund_data_are_excluded_or_disclosed_as_partial():
    source = household(
        positions=[{"id": "fund-pos", "account_id": "a1", "instrument_id": "fund", "symbol": "FND", "quantity": 1, "value": 100, "currency": "EUR", "asset_class": "fund"}],
        fx=[{"from": "EUR", "to": "USD", "rate": "1.2", "as_of": "2026-09-01", "source": "ECB"}],
        fund_holdings=[{"instrument_id": "fund", "as_of": "2026-01-01", "source": "old factsheet", "holdings": [{"instrument_id": "stock", "weight": 1}]}],
    )
    packet = run("exposure", {"household": source, "evaluation_date": "2026-09-20", "targets": {"asset_class": {"equity": "0.4"}}}, {})

    assert packet["status"] == "partial"
    assert packet["result"]["known_assets"] == "0"
    assert packet["result"]["coverage"]["excluded_value_records"] == 1
    target = packet["result"]["targets"][0]
    assert target["status"] == "indeterminate"
    assert target["measured_weight"] is None
    assert target["possible_weight"] == {"minimum": "0", "maximum": "1"}
    assert target["difference_range"] == {"minimum": "-0.4", "maximum": "0.6"}
    assert target["uncertainty"] == ["unbounded_missing_assets"]
    assert any("stale FX" in warning for warning in packet["warnings"])
    assert any("Fund holdings" in warning and "stale" in warning for warning in packet["warnings"])


def test_unknown_sections_survive_round_trip_and_retirement_is_not_liquid_capital():
    partial_source = {
        "currency": "USD", "as_of": "2026-09-20", "complete": True,
        "people": [{"id": "p1"}],
        "accounts": [
            {"id": "taxable", "owner_id": "p1", "type": "brokerage", "currency": "USD"},
            {"id": "retirement", "owner_id": "p1", "type": "ira", "currency": "USD"},
            {"id": "restricted", "owner_id": "p1", "type": "brokerage", "currency": "USD", "restrictions": ["pledged collateral"]},
        ],
        "positions": [
            {"id": "available", "account_id": "taxable", "instrument_id": "cash", "symbol": "CASH", "quantity": 100, "value": 100, "currency": "USD", "asset_class": "cash"},
            {"id": "locked", "account_id": "retirement", "instrument_id": "stock", "symbol": "STK", "quantity": 1, "value": 100, "currency": "USD", "asset_class": "equity"},
            {"id": "pledged", "account_id": "restricted", "instrument_id": "bond", "symbol": "BND", "quantity": 1, "value": 100, "currency": "USD", "asset_class": "fixed_income"},
        ],
    }
    imported = run("import", {"household": partial_source}, {})
    assert imported["status"] == "partial"
    assert "liabilities" in imported["household"]["unknown_sections"]

    exposed = run("exposure", {"evaluation_date": "2026-09-20"}, {"household": imported["household"]})
    assert exposed["status"] == "partial"
    assert exposed["result"]["liquid_capital"] == "100"
    assert exposed["result"]["coverage"]["nonliquid_position_records"] == 2
    assert "liabilities" in exposed["result"]["coverage"]["unknown_sections"]

    uncertain_assets = household(
        unknown_sections=["positions"],
        positions=[{"id": "cash", "account_id": "a1", "instrument_id": "cash", "symbol": "CASH", "quantity": 100, "value": 100, "currency": "USD", "asset_class": "cash"}],
    )
    uncertain_target = run("exposure", {
        "household": uncertain_assets, "evaluation_date": "2026-09-20", "targets": {"asset_class": {"equity": "0.4"}},
    }, {})["result"]["targets"][0]
    assert uncertain_target["status"] == "indeterminate"
    assert uncertain_target["measured_weight"] == "0"
    assert uncertain_target["measured_weight_basis"] == "known_assets"
    assert "actual_weight" not in uncertain_target and "measured_known_assets_weight" not in uncertain_target
    assert uncertain_target["possible_weight"] == {"minimum": "0", "maximum": "1"}

    incomplete = household(complete=False, positions=uncertain_assets["positions"])
    assert run("exposure", {
        "household": incomplete, "evaluation_date": "2026-09-20", "targets": {"asset_class": {"equity": "0.4"}},
    }, {})["result"]["targets"][0]["status"] == "indeterminate"

    reconciled = imported["household"]
    reconciled["unknown_sections"] = []
    assert run("exposure", {"household": reconciled, "evaluation_date": "2026-09-20"}, {})["status"] == "ready"


# --- Canonical vocabulary, FX, ownership, liquidity and freshness -----------------

from wealth.household import account_tax_treatment, canonical_account_type, normalize_symbol  # noqa: E402

EVAL = {"evaluation_date": "2026-09-20"}


def _position(pid, account, instrument, value, currency="USD", **extra):
    return {"id": pid, "account_id": account, "instrument_id": instrument, "symbol": instrument,
            "quantity": 1, "value": value, "currency": currency, **extra}


def test_account_type_vocabulary_is_shared_and_taxable_brokerage_is_liquid():
    assert canonical_account_type("taxable_brokerage") == canonical_account_type("Brokerage") == "taxable"
    assert canonical_account_type("401(k)") == "employer_plan"
    assert canonical_account_type("Roth IRA") == "roth_ira"
    assert canonical_account_type("afore") == "mx_afore"
    assert canonical_account_type("mystery") is None
    assert account_tax_treatment("taxable_brokerage") == "taxable"
    assert account_tax_treatment("ira") == "tax_deferred"
    assert account_tax_treatment("mystery") == "unknown"

    source = household(
        accounts=[
            {"id": "a1", "owner_id": "p1", "type": "taxable_brokerage", "currency": "USD"},
            {"id": "odd", "owner_id": "p1", "type": "mystery", "currency": "USD"},
        ],
        positions=[_position("x", "a1", "AAPL", 100), _position("y", "odd", "MSFT", 50)],
    )
    packet = run("exposure", {"household": source, **EVAL}, {})
    assert packet["result"]["liquid_capital"] == "100"
    assert packet["result"]["liquidity"]["value_by_reason"]["account_type_default:taxable"] == "100"
    assert packet["result"]["liquidity"]["value_by_reason"]["unknown_account_type"] == "50"
    assert any("outside the canonical vocabulary" in warning for warning in packet["warnings"])


def _mxn_household(fx):
    return household(
        accounts=[{"id": "a1", "owner_id": "p1", "type": "brokerage", "currency": "MXN"}],
        positions=[_position("x", "a1", "WALMEX", 1800, "MXN")],
        fx=fx,
    )


def test_fx_reciprocal_quotes_are_order_independent_and_inconsistent_pairs_rejected():
    consistent = [
        {"from": "MXN", "to": "USD", "rate": "0.0556", "as_of": "2026-09-20", "source": "s"},
        {"from": "USD", "to": "MXN", "rate": "18", "as_of": "2026-09-20", "source": "s"},
    ]
    forward = run("exposure", {"household": _mxn_household(consistent), **EVAL}, {})["result"]
    backward = run("exposure", {"household": _mxn_household(list(reversed(consistent))), **EVAL}, {})["result"]
    assert forward["known_assets"] == backward["known_assets"] == "100.08"
    assert forward["fx_used"][0]["meaning"] == "1 MXN = 0.0556 USD"
    assert forward["fx_used"][0]["quoted_as"] == "MXN->USD"

    inconsistent = [
        {"from": "MXN", "to": "USD", "rate": "0.05", "as_of": "2026-09-20", "source": "s"},
        {"from": "USD", "to": "MXN", "rate": "18", "as_of": "2026-09-20", "source": "s"},
    ]
    with pytest.raises(ValueError, match="inconsistent reciprocal FX"):
        validate_household(_mxn_household(inconsistent))


def test_inverted_fx_quote_is_excluded_until_direction_is_verified():
    inverted = [{"from": "MXN", "to": "USD", "rate": "18", "as_of": "2026-09-20", "source": "s"}]
    packet = run("exposure", {"household": _mxn_household(inverted), **EVAL}, {})
    assert packet["status"] == "partial"
    assert packet["result"]["known_assets"] == "0"
    assert packet["result"]["coverage"]["rejected_fx_quotes"] == ["MXN->USD"]
    assert any("looks inverted" in warning for warning in packet["warnings"])
    assert any("implausible (possibly inverted)" in warning for warning in packet["warnings"])

    correct = [{"from": "USD", "to": "MXN", "rate": "18", "as_of": "2026-09-20", "source": "s"}]
    assert run("exposure", {"household": _mxn_household(correct), **EVAL}, {})["result"]["known_assets"] == "100"

    forced = [{**inverted[0], "direction_verified": True}]
    assert run("exposure", {"household": _mxn_household(forced), **EVAL}, {})["result"]["known_assets"] == "32400"


def test_ownership_shares_attribute_without_double_counting():
    source = household(
        people=[{"id": "p1"}, {"id": "p2"}],
        accounts=[
            {"id": "joint", "owner_id": "p1", "type": "taxable", "currency": "USD",
             "ownership": {"form": "joint_tenancy", "owners": [{"person_id": "p1", "share": 0.5}, {"person_id": "p2", "share": 0.5}]}},
            {"id": "mx", "owner_id": "p2", "type": "taxable", "currency": "USD",
             "ownership": {"form": "sociedad_conyugal", "owners": [{"person_id": "p1"}, {"person_id": "p2"}]}},
            {"id": "tic", "owner_id": "p1", "type": "taxable", "currency": "USD",
             "ownership": {"form": "tenancy_in_common", "owners": [{"person_id": "p1", "share": 0.7}, {"person_id": "p2", "share": 0.3}]}},
        ],
        positions=[_position("a", "joint", "AAPL", 100), _position("b", "mx", "WALMEX", 200), _position("c", "tic", "MSFT", 100)],
        external_assets=[
            {"id": "home", "name": "Home", "value": 1000, "currency": "USD", "liquid": False, "owner_id": "p2"},
            {"id": "art", "name": "Painting", "value": 50, "currency": "USD", "liquid": False},
        ],
        liabilities=[{"id": "mortgage", "value": 400, "currency": "USD",
                      "ownership": {"form": "community_property", "owners": [{"person_id": "p1"}, {"person_id": "p2"}]}}],
    )
    packet = run("exposure", {"household": source, **EVAL}, {})
    people = {row["person_id"]: row for row in packet["result"]["people"]}
    assert people["p1"]["attributed_assets"] == "220"   # 50 + 100 + 70
    assert people["p2"]["attributed_assets"] == "1180"  # 50 + 100 + 30 + 1000
    assert people["unattributed"]["attributed_assets"] == "50"
    assert people["p1"]["attributed_liabilities"] == people["p2"]["attributed_liabilities"] == "200"
    person_total = sum(float(row["value"]) for row in packet["result"]["exposures"]["person"])
    assert person_total == float(packet["result"]["known_assets"]) == 1450
    assert packet["result"]["coverage"]["unattributed_value_records"] == 1
    assert any("Equal economic shares presumed for sociedad_conyugal" in note for note in packet["assumptions"])

    bad = household(people=[{"id": "p1"}, {"id": "p2"}], accounts=[{
        "id": "a1", "owner_id": "p1", "type": "taxable", "currency": "USD",
        "ownership": {"form": "joint_tenancy", "owners": [{"person_id": "p1", "share": 0.6}, {"person_id": "p2", "share": 0.6}]},
    }])
    with pytest.raises(ValueError, match="sum to 1"):
        validate_household(bad)
    bad["accounts"][0]["ownership"] = {"form": "tenancy_in_common", "owners": [{"person_id": "p1"}, {"person_id": "p2"}]}
    with pytest.raises(ValueError, match="requires explicit shares"):
        validate_household(bad)
    bad["accounts"][0]["ownership"] = {"form": "joint_tenancy", "owners": [{"person_id": "p1", "pct": 50}]}
    with pytest.raises(ValueError, match="uses 'share'"):
        validate_household(bad)


def test_illiquid_assets_are_not_liquid_capital_regardless_of_account():
    source = household(
        accounts=[{"id": "a1", "owner_id": "p1", "type": "brokerage", "currency": "USD"}],
        positions=[
            _position("pe", "a1", "PE-FUND-III", 1000, asset_class="private_equity"),
            _position("reit", "a1", "VNQ", 100, asset_class="real_estate", liquid=True),
            _position("locked", "a1", "IPO", 100, lockup_until="2026-12-01"),
            _position("unlisted", "a1", "PRIVCO", 100, listed=False),
            _position("hedge", "a1", "HF", 100, asset_class="hedge_fund", redemption={"frequency": "quarterly", "notice_days": 45}),
            _position("interval", "a1", "INTERVAL", 100, redemption={"frequency": "daily", "notice_days": 2, "gate": "0.25"}),
            _position("stock", "a1", "AAPL", 100),
        ],
    )
    result = run("exposure", {"household": source, **EVAL}, {})["result"]
    assert result["liquid_capital"] == "225"  # REIT 100 + AAPL 100 + 25% gate on 100
    reasons = result["liquidity"]["value_by_reason"]
    assert reasons["illiquid_asset_class"] == "1000"
    assert reasons["lockup"] == "100"
    assert reasons["unlisted"] == "100"
    assert reasons["redemption_terms_exceed_horizon"] == "100"
    assert reasons["redemption_gated"] == "100"
    # A horizon long enough for quarterly redemption plus notice makes the hedge fund count.
    longer = run("exposure", {"household": source, "evaluation_date": "2026-09-20", "liquidity_horizon_days": 140}, {})["result"]
    assert longer["liquid_capital"] == "325"


def test_freshness_is_measured_at_evaluation_date_and_stale_households_are_flagged():
    source = _mxn_household([{"from": "USD", "to": "MXN", "rate": "18", "as_of": "2026-06-01", "source": "s"}])
    source["as_of"] = "2026-06-01"
    source["fund_holdings"] = [{"instrument_id": "WALMEX", "as_of": "2026-06-01", "source": "f", "holdings": [{"instrument_id": "x", "weight": 1}]}]
    at_snapshot = run("exposure", {"household": source, "evaluation_date": "2026-06-01"}, {})
    assert at_snapshot["status"] == "ready"
    later = run("exposure", {"household": source, "evaluation_date": "2026-09-20"}, {})
    coverage = later["result"]["coverage"]
    assert later["status"] == "partial"
    assert coverage["household_stale"] is True and coverage["household_age_days"] == 111
    assert later["result"]["known_assets"] == "0"  # FX is 111 days old at evaluation
    assert any("Household snapshot is stale" in warning for warning in later["warnings"])
    assert any("Fund holdings for WALMEX are stale" in warning for warning in later["warnings"])
    with pytest.raises(ValueError, match="cannot precede"):
        run("exposure", {"household": source, "evaluation_date": "2026-05-01"}, {})


def test_normalize_symbol_handles_share_classes_and_bmv_conventions():
    assert {normalize_symbol(value) for value in ("BRK.B", "BRK-B", "brk/b", "BRK B", "BRK.B US Equity")} == {"BRK-B"}
    assert {normalize_symbol(value) for value in ("WALMEX*", "WALMEX.MX", "WALMEX* MM", "BMV:WALMEX")} == {"WALMEX"}
    assert normalize_symbol("GFNORTEO.MX") == "GFNORTEO"
    assert normalize_symbol("") is None
