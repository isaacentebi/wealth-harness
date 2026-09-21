from __future__ import annotations

import pytest

from wealth.research import TASKS, run


AS_OF = "2026-09-20"
SOURCE = {
    "id": "S1", "title": "Issuer annual report", "url": "https://example.com/report",
    "as_of": "2026-06-30", "kind": "filing",
}
THESIS = {
    "bullish": [{"claim": "Recurring revenue expanded.", "source_ids": ["S1"]}],
    "bearish": [{"claim": "Operating margin compressed.", "source_ids": ["S1"]}],
    "disconfirming": [{"claim": "Renewal data would refute the durability case if it weakens.", "source_ids": ["S1"]}],
}


def valid_household(positions, fund_holdings=(), **extra):
    """A canonical household (research validates context households)."""
    value = {
        "currency": "USD", "as_of": AS_OF, "complete": True, "people": [{"id": "p1"}],
        "accounts": [{"id": "a1", "owner_id": "p1", "type": "taxable", "currency": "USD"}],
        "positions": [{"quantity": 1, **position} for position in positions], "lots": [], "liabilities": [],
        "external_assets": [], "income_exposures": [], "fx": [], "fund_holdings": list(fund_holdings),
    }
    value.update(extra)
    return value


def company_inputs():
    return {
        "symbol": "ACME", "as_of": AS_OF, "sources": [SOURCE],
        "business_facts": [{"label": "business", "value": "Industrial software", "source_ids": ["S1"]}],
        "statements": [
            {"period_end": "2026-06-30", "period_type": "FY", "currency": "USD", "source_ids": ["S1"], "metrics": {
                "revenue": 120, "gross_profit": 72, "operating_income": 18, "net_income": 12,
                "total_assets": 200, "shareholders_equity": 80, "cash": 20, "total_debt": 50,
                "ebitda": 25, "current_assets": 60, "current_liabilities": 30,
                "operating_cash_flow": 24, "capital_expenditure": -6,
            }},
            {"period_end": "2025-06-30", "period_type": "FY", "currency": "USD", "source_ids": ["S1"], "metrics": {"revenue": 100}},
        ],
        "thesis_evidence": THESIS,
    }


def test_company_research_computes_metrics_case_deltas_and_household_links():
    previous = {"financial_metrics": {"operating_margin": {"value": "0.1", "unit": "ratio"}}}
    household = valid_household(
        [
            {"id": "pos1", "account_id": "a1", "instrument_id": "acme", "symbol": "ACME", "quantity": 4, "value": 100, "currency": "USD"},
            {"id": "fund-pos", "account_id": "a1", "instrument_id": "fund", "symbol": "FND", "quantity": 2, "value": 200, "currency": "USD", "asset_class": "fund"},
        ],
        [{
            "instrument_id": "fund", "as_of": "2026-09-01", "source": "factsheet",
            "holdings": [
                {"instrument_id": "acme", "symbol": "ACME", "weight": "0.25", "asset_class": "equity"},
                {"instrument_id": "other", "symbol": "OTHER", "weight": "0.5", "asset_class": "equity"},
            ],
        }],
    )
    packet = run("research", company_inputs(), {
        "research.ACME": previous, "household": household,
        "thesis.ACME": {"summary": "Durable recurring revenue, monitored against renewal rates."},
    })
    assert TASKS == ("research", "value")
    assert packet["status"] == "partial"
    assert packet["result"]["financial_metrics"]["revenue_growth"]["value"] == "0.2"
    assert packet["result"]["financial_metrics"]["free_cash_flow_proxy"]["value"] == "18"
    assert packet["result"]["financial_metrics"]["net_debt_to_ebitda"]["value"] == "1.2"
    assert packet["result"]["prior_case"]["deltas"] == [
        {"metric": "operating_margin", "prior": "0.1", "current": "0.15", "change": "0.05"}
    ]
    links = packet["result"]["household_links"]
    assert links[0]["owner_id"] == "p1"
    indirect = next(link for link in links if link["exposure_type"] == "fund_lookthrough")
    assert indirect["path"] == ["fund", "acme"]
    assert indirect["weight"] == "0.25"
    assert indirect["weighted_value"] == "50"
    assert packet["result"]["household_link_coverage"]["status"] == "partial"
    coverage = packet["result"]["household_link_coverage"]
    assert "residual unreported weight" in coverage["warnings"][0]
    assert coverage["unknown_exposure"] == [{"position_id": "fund-pos", "kind": "residual", "path": ["fund"], "weight": "0.25", "value_reporting": "50"}]
    assert coverage["totals_reporting"] == {
        "direct_value": "100", "fund_lookthrough_value": "50", "total_linked_value": "150",
        "unknown_lookthrough_value": "50", "positions_excluded_for_fx": 0,
    }
    assert packet["result"]["remembered_thesis"]["summary"].startswith("Durable")
    assert packet["result"]["thesis_evidence"]["disconfirming"][0]["claim"].startswith("Renewal")


def test_fund_research_calculates_reported_concentration_and_residual():
    inputs = {
        "symbol": "FUND", "entity_type": "fund", "as_of": AS_OF,
        "sources": [{**SOURCE, "kind": "fund", "as_of": "2026-09-01"}],
        "fund_facts": [{"label": "expense_ratio", "value": "0.002", "source_ids": ["S1"]}],
        "fund_holdings": [
            {"symbol": "AAA", "weight": "0.4", "source_ids": ["S1"]},
            {"symbol": "BBB", "weight": "0.35", "source_ids": ["S1"]},
        ],
        "thesis_evidence": {"bullish": [], "bearish": [], "disconfirming": []},
    }
    packet = run("research", inputs, {})
    assert packet["status"] == "ready"
    assert packet["result"]["fund_metrics"] == {
        "reported_holdings_weight": "0.75", "top_10_concentration": "0.75", "residual_unreported_weight": "0.25"
    }


def test_dcf_scenario_has_explicit_enterprise_to_equity_bridge():
    packet = run("value", {
        "symbol": "ACME", "as_of": AS_OF, "sources": [SOURCE],
        "scenarios": [{
            "name": "base", "kind": "dcf_fcff", "currency": "USD", "valuation_date": AS_OF,
            "forecast": [{"period": 1, "free_cash_flow": 10}, {"period": 2, "free_cash_flow": 11}],
            "discount_rate": "0.10", "terminal_growth": "0.02", "net_debt": 20,
            "shares_outstanding": 10, "current_price": 9, "source_ids": ["S1"],
        }],
    }, {})
    scenario = packet["result"]["scenarios"][0]
    assert packet["status"] == "ready"
    assert scenario["basis"] == "enterprise_value"
    assert float(scenario["enterprise_value"]) == pytest.approx(134.090909)
    assert float(scenario["equity_value"]) == pytest.approx(114.090909)
    assert float(scenario["implied_value_per_share"]) == pytest.approx(11.409091)
    assert scenario["interpretation"] == "scenario, not prediction"


def test_multiples_enforces_basis_and_reports_bridge():
    packet = run("value", {
        "symbol": "ACME", "as_of": AS_OF, "sources": [SOURCE],
        "scenarios": [{
            "name": "peer", "kind": "multiples", "currency": "USD", "valuation_date": AS_OF,
            "basis": "enterprise_value", "metric_name": "EBITDA", "metric_value": 25,
            "multiple": 8, "net_debt": 30, "shares_outstanding": 10, "source_ids": ["S1"],
        }],
    }, {})
    assert packet["result"]["scenarios"][0]["enterprise_value"] == "200"
    assert packet["result"]["scenarios"][0]["equity_value"] == "170"
    with pytest.raises(ValueError, match="net_debt is not used"):
        run("value", {
            "symbol": "ACME", "as_of": AS_OF, "sources": [SOURCE],
            "scenarios": [{
                "name": "bad", "kind": "multiples", "currency": "USD", "valuation_date": AS_OF,
                "basis": "equity_value", "metric_name": "net_income", "metric_value": 10,
                "multiple": 12, "net_debt": 5, "shares_outstanding": 10, "source_ids": ["S1"],
            }],
        }, {})


def test_offline_default_and_stale_sources_return_actionable_status():
    offline = run("research", {"symbol": "ACME", "as_of": AS_OF}, {})
    assert offline["status"] == "needs_input"
    assert offline["missing"][0]["key"] == "sources"

    stale = company_inputs()
    stale["sources"] = [{**SOURCE, "as_of": "2020-01-01", "max_age_days": 365}]
    packet = run("research", stale, {})
    assert packet["status"] == "partial"
    assert any(item["reason"] == "stale" for item in packet["missing"])


def test_validation_rejects_unknown_sources_and_invalid_rates():
    inputs = company_inputs()
    inputs["business_facts"][0]["source_ids"] = ["MISSING"]
    with pytest.raises(ValueError, match="unknown source"):
        run("research", inputs, {})
    with pytest.raises(ValueError, match="must exceed terminal_growth"):
        run("value", {
            "symbol": "ACME", "as_of": AS_OF, "sources": [SOURCE],
            "scenarios": [{
                "name": "bad", "kind": "dcf_fcff", "currency": "USD", "valuation_date": AS_OF,
                "forecast": [{"period": 1, "free_cash_flow": 10}], "discount_rate": "0.02",
                "terminal_growth": "0.03", "net_debt": 0, "shares_outstanding": 1,
                "source_ids": ["S1"],
            }],
        }, {})


def test_temporal_consistency_rejects_future_packet_facts_and_statements():
    with pytest.raises(ValueError, match="as_of cannot be in the future"):
        run("research", {"symbol": "ACME", "as_of": "2999-01-01"}, {})

    for field in ("business_facts", "fund_facts"):
        inputs = company_inputs()
        inputs[field] = [{"label": "future", "value": 1, "as_of": "2027-01-01", "source_ids": ["S1"]}]
        with pytest.raises(ValueError, match=f"{field}.*cannot be after packet as_of"):
            run("research", inputs, {})

    inputs = company_inputs()
    inputs["statements"][0]["period_end"] = "2027-01-01"
    with pytest.raises(ValueError, match="period_end cannot be after packet as_of"):
        run("research", inputs, {})

    with pytest.raises(ValueError, match="as_of cannot be in the future"):
        run("value", {"symbol": "ACME", "as_of": "2999-01-01"}, {})


def test_household_lookthrough_discloses_stale_cycle_and_unknown_exposure():
    household = valid_household(
        [
            {"id": "stale-pos", "account_id": "a1", "instrument_id": "stale", "symbol": "OLD", "value": 100, "currency": "USD", "asset_class": "fund"},
            {"id": "cycle-pos", "account_id": "a1", "instrument_id": "one", "symbol": "ONE", "value": 100, "currency": "USD", "asset_class": "fund"},
        ],
        [
            {"instrument_id": "stale", "as_of": "2026-01-01", "source": "old", "holdings": [{"instrument_id": "acme", "symbol": "ACME", "weight": 1}]},
            {"instrument_id": "one", "as_of": AS_OF, "source": "current", "holdings": [{"instrument_id": "two", "symbol": "TWO", "weight": 1, "asset_class": "fund"}]},
            {"instrument_id": "two", "as_of": AS_OF, "source": "current", "holdings": [{"instrument_id": "one", "symbol": "ONE", "weight": 1, "asset_class": "fund"}]},
        ],
    )
    packet = run("research", company_inputs(), {"household": household})
    coverage = packet["result"]["household_link_coverage"]
    assert packet["result"]["household_links"] == []
    assert coverage["status"] == "partial"
    assert coverage["lookthrough_complete"] is False
    assert any("stale" in warning for warning in coverage["warnings"])
    assert any("cycle" in warning for warning in coverage["warnings"])


# --- Valuation integrity, period types, live stamping and household matching ------

import sys  # noqa: E402
import types  # noqa: E402
from datetime import date, datetime, timezone  # noqa: E402

from wealth import research  # noqa: E402


def _value(*scenarios):
    return run("value", {"symbol": "ACME", "as_of": AS_OF, "sources": [SOURCE], "scenarios": list(scenarios)}, {})


def _dcf(**extra):
    scenario = {
        "name": "base", "kind": "dcf_fcff", "currency": "USD", "valuation_date": AS_OF,
        "forecast": [{"period": 1, "free_cash_flow": 10}, {"period": 2, "free_cash_flow": 11}],
        "discount_rate": "0.10", "terminal_growth": "0.02", "net_debt": 20,
        "shares_outstanding": 10, "source_ids": ["S1"],
    }
    scenario.update(extra)
    return scenario


def test_non_positive_multiple_metric_is_not_meaningful_and_not_ready():
    packet = _value({
        "name": "m", "kind": "multiples", "currency": "USD", "valuation_date": AS_OF, "basis": "enterprise_value",
        "metric_name": "ebitda", "metric_value": -50, "multiple": 12, "net_debt": 100, "shares_outstanding": 10,
        "source_ids": ["S1"],
    })
    scenario = packet["result"]["scenarios"][0]
    assert packet["status"] == "partial"
    assert scenario["status"] == "not_meaningful"
    assert scenario["implied_value_per_share"] is None
    assert packet["missing"][0]["reason"] == "not_meaningful"
    with pytest.raises(ValueError, match="multiple must be positive"):
        _value({"name": "z", "kind": "multiples", "currency": "USD", "valuation_date": AS_OF, "basis": "equity_value",
                "metric_name": "earnings", "metric_value": 5, "multiple": 0, "shares_outstanding": 1, "source_ids": ["S1"]})


def test_dcf_reports_terminal_share_and_rejects_growth_at_or_above_discount_rate():
    packet = _value(_dcf(discount_rate="0.08", terminal_growth="0.079", forecast=[{"period": 1, "free_cash_flow": 100}]))
    scenario = packet["result"]["scenarios"][0]
    assert float(scenario["terminal_value_share_of_enterprise_value"]) > 0.99
    assert any("terminal value is 99.9% of enterprise value" in warning for warning in packet["warnings"])
    assert any("less than one percentage point" in warning for warning in packet["warnings"])
    with pytest.raises(ValueError, match="must exceed terminal_growth"):
        _value(_dcf(discount_rate="0.08", terminal_growth="0.08"))


def test_equity_bridge_and_treasury_stock_dilution():
    packet = _value(_dcf(
        forecast=[{"period": 1, "free_cash_flow": 100}], discount_rate="0.10", terminal_growth="0",
        net_debt=100, minority_interest=50, preferred_equity=25, lease_liabilities=25, non_operating_assets=200,
        shares_outstanding=10, options=[{"count": 2, "strike": 50}], rsus=0,
    ))
    scenario = packet["result"]["scenarios"][0]
    # EV = 100/1.1 + (100/0.10)/1.1 = 1000; equity = 1000 - 100 - 50 - 25 - 25 + 200 = 1000.
    assert float(scenario["enterprise_value"]) == pytest.approx(1000)
    assert float(scenario["equity_value"]) == pytest.approx(1000)
    assert scenario["bridge_items_not_supplied"] == []
    # Treasury stock method at the implied value: p = 1000 / (10 + 2(1 - 50/p)) -> p = 1100/12.
    assert float(scenario["implied_value_per_share"]) == pytest.approx(1100 / 12)
    assert float(scenario["diluted_shares"]) == pytest.approx(10 + 2 * (1 - 50 / (1100 / 12)))

    with pytest.raises(ValueError, match="double count"):
        _value(_dcf(lease_liabilities=5, leases_included_in_net_debt=True))
    bare = _value(_dcf())
    assert bare["result"]["scenarios"][0]["bridge_items_not_supplied"] == [
        "minority_interest", "preferred_equity", "lease_liabilities", "non_operating_assets", "dilutive_securities",
    ]


def test_current_price_currency_must_match_scenario_currency():
    unmatched = _value(_dcf(current_price=9, current_price_currency="MXN"))
    scenario = unmatched["result"]["scenarios"][0]
    assert "upside_downside" not in scenario
    assert any("no implicit FX" in warning for warning in unmatched["warnings"])
    unstated = _value(_dcf(current_price=9))["result"]["scenarios"][0]
    assert "upside_downside" not in unstated
    matched = _value(_dcf(current_price=9, current_price_currency="USD"))["result"]["scenarios"][0]
    assert float(matched["upside_downside"]) == pytest.approx(11.409091 / 9 - 1, rel=1e-6)


def test_stub_period_and_mid_year_convention():
    packet = _value(_dcf(
        valuation_date="2026-06-30", first_period_end="2026-12-31", stub_fcf_basis="full_period", mid_year_convention=True,
        forecast=[{"period": 1, "free_cash_flow": 100}, {"period": 2, "free_cash_flow": 110}], net_debt=0,
    ))
    scenario = packet["result"]["scenarios"][0]
    stub = 184 / 365
    expected = (
        100 * stub / 1.1 ** (stub / 2)
        + 110 / 1.1 ** (stub + 0.5)
        + (110 * 1.02 / 0.08) / 1.1 ** (stub + 0.5)
    )
    assert float(scenario["enterprise_value"]) == pytest.approx(expected, rel=1e-9)
    assert scenario["timing"]["stub_fcf_basis"] == "full_period"
    with pytest.raises(ValueError, match="stub_fcf_basis"):
        _value(_dcf(first_period_end="2026-12-31"))


def _statement(end, kind, **metrics):
    return {"period_end": end, "period_type": kind, "currency": "USD", "source_ids": ["S1"], "metrics": metrics}


def test_period_types_compare_like_for_like_and_annualise_returns():
    inputs = company_inputs()
    inputs["statements"] = [
        _statement("2026-06-30", "Q", revenue=25, net_income=5, shareholders_equity=100, total_assets=200, ebitda=10, cash=10, total_debt=50),
        _statement("2025-12-31", "FY", revenue=100),
        _statement("2025-06-30", "Q", revenue=20),
    ]
    metrics = run("research", inputs, {})["result"]["financial_metrics"]
    assert metrics["revenue_growth"]["value"] == "0.25"  # Q2 vs Q2, not quarter vs fiscal year
    assert metrics["revenue_growth"]["comparison_period_end"] == "2025-06-30"
    assert metrics["return_on_equity"]["value"] == "0.2"  # 5 x 4 / 100
    assert metrics["return_on_equity"]["annualisation"] == "quarterly flow x 4"
    assert metrics["net_debt_to_ebitda"]["value"] == "1"  # 40 / (10 x 4)

    inputs["statements"] = [_statement("2026-06-30", "Q", revenue=25), _statement("2025-12-31", "FY", revenue=100)]
    packet = run("research", inputs, {})
    assert "revenue_growth" not in packet["result"]["financial_metrics"]
    assert any("no Q period ending about one year before" in gap for gap in packet["result"]["financial_metric_gaps"])

    inputs["statements"] = [{**_statement("2026-06-30", None, net_income=5, shareholders_equity=100)}]
    packet = run("research", inputs, {})
    assert "return_on_equity" not in packet["result"]["financial_metrics"]
    assert any("period_type is unknown" in gap for gap in packet["result"]["financial_metric_gaps"])


def test_live_fetch_is_stamped_with_retrieval_date_and_refuses_historical_as_of(monkeypatch):
    market_time = datetime(2026, 9, 18, 20, 0, tzinfo=timezone.utc).timestamp()

    class Ticker:
        def __init__(self, symbol):
            self.symbol = symbol

        def get_info(self):
            return {"longName": "Fund", "currentPrice": 10, "regularMarketTime": market_time}

        def get_funds_data(self):
            raise RuntimeError("no fund data")

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(Ticker=Ticker))
    monkeypatch.setattr(research, "_today", lambda: date(2026, 9, 20))
    packet = run("research", {"symbol": "FUND", "entity_type": "fund", "live_fetch": True}, {})
    source = packet["sources"][0]
    assert source["as_of"] == "2026-09-20"
    facts = {fact["label"]: fact for fact in packet["result"]["business_facts"]}
    assert facts["price"]["as_of"] == "2026-09-18"
    assert facts["name"]["as_of"] == "2026-09-20"

    historical = run("research", {"symbol": "FUND", "entity_type": "fund", "as_of": "2026-01-02", "live_fetch": True}, {})
    assert any(item["reason"] == "not_point_in_time" for item in historical["missing"])
    assert historical["sources"] == []


def test_household_matching_normalises_tickers_and_totals_in_reporting_currency():
    household = valid_household(
        [
            {"id": "brk", "account_id": "a1", "instrument_id": "US0846707026", "symbol": "BRK.B", "value": 500, "currency": "USD"},
            {"id": "wal", "account_id": "mx", "instrument_id": "WALMEX*", "symbol": "WALMEX*", "value": 1800, "currency": "MXN"},
        ],
        accounts=[
            {"id": "a1", "owner_id": "p1", "type": "taxable", "currency": "USD"},
            {"id": "mx", "owner_id": "p1", "type": "taxable", "currency": "MXN"},
        ],
        fx=[{"from": "USD", "to": "MXN", "rate": "18", "as_of": AS_OF, "source": "Banxico FIX"}],
    )
    brk = run("research", {**company_inputs(), "symbol": "BRK-B"}, {"household": household})["result"]
    assert [link["position_id"] for link in brk["household_links"]] == ["brk"]
    wal = run("research", {**company_inputs(), "symbol": "WALMEX.MX"}, {"household": household})["result"]
    link = wal["household_links"][0]
    assert link["position_id"] == "wal" and link["weighted_value"] == "1800" and link["weighted_value_reporting"] == "100"
    assert wal["household_link_coverage"]["totals_reporting"]["total_linked_value"] == "100"
    assert link["attribution"] == [{"person_id": "p1", "share": "1"}]


def test_invalid_household_context_is_reported_not_guessed():
    packet = run("research", company_inputs(), {"household": {"as_of": AS_OF, "complete": True}})
    coverage = packet["result"]["household_link_coverage"]
    assert coverage["status"] == "unavailable"
    assert "failed validation" in coverage["warnings"][0]
    assert packet["status"] == "partial"
