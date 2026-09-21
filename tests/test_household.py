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
    packet = run("exposure", {"household": source, "targets": {
        "asset_class": {"equity": "0.4"}, "sector": {"tech": "0.4"},
    }}, {})
    result = packet["result"]

    assert packet["status"] == "partial"
    assert result["known_assets"] == "350"
    assert result["known_nav"] == "270"
    assert result["liquid_capital"] == "150"
    assert result["coverage"] == {
        "household_complete": True,
        "excluded_value_records": 0,
        "unknown_liabilities": 1,
        "unknown_sections": [],
        "nonliquid_position_records": 0,
        "income_exposure_gaps": 3,
        "lookthrough_complete": False,
        "weight_denominator": "known_assets",
    }
    assert result["overlap"] == [{
        "left_position_id": "fund-pos", "right_position_id": "stock-pos",
        "overlap_weight": "0.6", "shared_instruments": ["stock"],
    }]
    assert result["targets"][0]["status"] == "underweight"
    sector_target = next(row for row in result["targets"] if row["dimension"] == "sector")
    assert sector_target["status"] == "indeterminate"
    assert sector_target["actual_weight"] is None
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
    packet = run("exposure", {"household": source, "targets": {"asset_class": {"equity": "0.4"}}}, {})

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

    exposed = run("exposure", {}, {"household": imported["household"]})
    assert exposed["status"] == "partial"
    assert exposed["result"]["liquid_capital"] == "100"
    assert exposed["result"]["coverage"]["nonliquid_position_records"] == 2
    assert "liabilities" in exposed["result"]["coverage"]["unknown_sections"]

    uncertain_assets = household(
        unknown_sections=["positions"],
        positions=[{"id": "cash", "account_id": "a1", "instrument_id": "cash", "symbol": "CASH", "quantity": 100, "value": 100, "currency": "USD", "asset_class": "cash"}],
    )
    uncertain_target = run("exposure", {
        "household": uncertain_assets, "targets": {"asset_class": {"equity": "0.4"}},
    }, {})["result"]["targets"][0]
    assert uncertain_target["status"] == "indeterminate"
    assert uncertain_target["measured_weight"] == "0"
    assert uncertain_target["measured_known_assets_weight"] == "0"
    assert uncertain_target["actual_weight"] is None
    assert uncertain_target["possible_weight"] == {"minimum": "0", "maximum": "1"}

    incomplete = household(complete=False, positions=uncertain_assets["positions"])
    assert run("exposure", {
        "household": incomplete, "targets": {"asset_class": {"equity": "0.4"}},
    }, {})["result"]["targets"][0]["status"] == "indeterminate"

    reconciled = imported["household"]
    reconciled["unknown_sections"] = []
    assert run("exposure", {"household": reconciled}, {})["status"] == "ready"
