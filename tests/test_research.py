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


def company_inputs():
    return {
        "symbol": "ACME", "as_of": AS_OF, "sources": [SOURCE],
        "business_facts": [{"label": "business", "value": "Industrial software", "source_ids": ["S1"]}],
        "statements": [
            {"period_end": "2026-06-30", "currency": "USD", "source_ids": ["S1"], "metrics": {
                "revenue": 120, "gross_profit": 72, "operating_income": 18, "net_income": 12,
                "total_assets": 200, "shareholders_equity": 80, "cash": 20, "total_debt": 50,
                "ebitda": 25, "current_assets": 60, "current_liabilities": 30,
                "operating_cash_flow": 24, "capital_expenditure": -6,
            }},
            {"period_end": "2025-06-30", "currency": "USD", "source_ids": ["S1"], "metrics": {"revenue": 100}},
        ],
        "thesis_evidence": THESIS,
    }


def test_company_research_computes_metrics_case_deltas_and_household_links():
    previous = {"financial_metrics": {"operating_margin": {"value": "0.1", "unit": "ratio"}}}
    household = {
        "as_of": AS_OF, "complete": True,
        "accounts": [{"id": "a1", "owner_id": "p1"}],
        "positions": [
            {"id": "pos1", "account_id": "a1", "instrument_id": "acme", "symbol": "ACME", "quantity": 4, "value": 100, "currency": "USD"},
            {"id": "fund-pos", "account_id": "a1", "instrument_id": "fund", "symbol": "FND", "quantity": 2, "value": 200, "currency": "USD", "asset_class": "fund"},
        ],
        "fund_holdings": [{
            "instrument_id": "fund", "as_of": "2026-09-01", "source": "factsheet",
            "holdings": [
                {"instrument_id": "acme", "symbol": "ACME", "weight": "0.25", "asset_class": "equity"},
                {"instrument_id": "other", "symbol": "OTHER", "weight": "0.5", "asset_class": "equity"},
            ],
        }],
    }
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
    assert "residual unreported weight" in packet["result"]["household_link_coverage"]["warnings"][0]
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
    household = {
        "as_of": AS_OF, "complete": True,
        "accounts": [{"id": "a1", "owner_id": "p1"}],
        "positions": [
            {"id": "stale-pos", "account_id": "a1", "instrument_id": "stale", "symbol": "OLD", "value": 100, "currency": "USD", "asset_class": "fund"},
            {"id": "cycle-pos", "account_id": "a1", "instrument_id": "one", "symbol": "ONE", "value": 100, "currency": "USD", "asset_class": "fund"},
        ],
        "fund_holdings": [
            {"instrument_id": "stale", "as_of": "2026-01-01", "source": "old", "holdings": [{"instrument_id": "acme", "symbol": "ACME", "weight": 1}]},
            {"instrument_id": "one", "as_of": AS_OF, "source": "current", "holdings": [{"instrument_id": "two", "symbol": "TWO", "weight": 1, "asset_class": "fund"}]},
            {"instrument_id": "two", "as_of": AS_OF, "source": "current", "holdings": [{"instrument_id": "one", "symbol": "ONE", "weight": 1, "asset_class": "fund"}]},
        ],
    }
    packet = run("research", company_inputs(), {"household": household})
    coverage = packet["result"]["household_link_coverage"]
    assert packet["result"]["household_links"] == []
    assert coverage["status"] == "partial"
    assert coverage["lookthrough_complete"] is False
    assert any("stale" in warning for warning in coverage["warnings"])
    assert any("cycle" in warning for warning in coverage["warnings"])
