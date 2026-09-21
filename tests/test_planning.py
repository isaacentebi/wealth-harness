from __future__ import annotations

import pytest

from wealth.planning import run


def projection(**overrides):
    inputs = {
        "currency": "USD", "as_of": "2026-01-01", "end_date": "2028-01-01",
        "initial_wealth": 100, "simulations": 10, "seed": 7,
        "return_model": {"type": "parametric", "annual_return": 0, "annual_volatility": 0},
        "annual_inflation": 0, "annual_fee": 0, "annual_tax_drag": 0,
        "cashflows": [], "recurring_cashflows": [],
    }
    inputs.update(overrides)
    return inputs


def test_project_separates_fulfilled_withdrawals_from_positive_terminal_wealth():
    result = run("project", projection(cashflows=[
        {"id": "home", "date": "2026-06-01", "amount": 120, "type": "withdrawal",
         "inflation_indexed": False, "hard_goal": True},
        {"id": "later-income", "date": "2027-01-01", "amount": 200, "type": "contribution",
         "inflation_indexed": False, "hard_goal": False},
    ]), {})
    assert result["status"] == "ready"
    assert result["result"]["hard_goal_funded_probability_percent"] == {"home": 0.0}
    assert result["result"]["all_withdrawals_fulfilled_probability_percent"] == 0.0
    assert result["result"]["positive_terminal_wealth_probability_percent"] == 100.0
    assert result["result"]["terminal_wealth_percentiles"]["p50"]["amount"] == 200.0

    exact = run("project", projection(
        end_date="2027-01-01", simulations=1,
        cashflows=[{"id": "goal", "date": "2027-01-01", "amount": 100,
                    "type": "withdrawal", "inflation_indexed": False, "hard_goal": True}],
    ), {})["result"]
    assert exact["hard_goal_funded_probability_percent"] == {"goal": 100.0}
    assert exact["all_withdrawals_fulfilled_probability_percent"] == 100.0
    assert exact["positive_terminal_wealth_probability_percent"] == 0.0


def test_project_bootstrap_is_seeded_and_inflation_fees_and_tax_drag_change_math():
    case = projection(
        initial_wealth=1000, simulations=40, seed=11,
        return_model={"type": "bootstrap", "historical_annual_returns": [-0.2, 0.1, 0.25]},
        annual_inflation=0.03, annual_fee=0.01, annual_tax_drag=0.02,
        cashflows=[{"id": "goal", "date": "2027-01-01", "amount": 900,
                    "type": "withdrawal", "inflation_indexed": True, "hard_goal": True}],
    )
    first = run("project", case, {})
    second = run("project", case, {})
    assert first == second
    probability = first["result"]["hard_goal_funded_probability_percent"]["goal"]
    assert 0 < probability < 100
    assert first["result"]["terminal_wealth_percentiles"]["p10"]["amount"] >= 0

    exact = run("project", projection(
        return_model={"type": "parametric", "annual_return": 0.1, "annual_volatility": 0},
        simulations=1,
    ), {})
    assert exact["result"]["terminal_wealth_percentiles"]["p50"]["amount"] == 121.0


def test_income_dividend_is_not_double_counted_and_sequence_risk_is_reported():
    inputs = {
        "currency": "USD", "initial_wealth": 1000, "years": 3,
        "annual_income_need": 50, "annual_need_growth": 0, "annual_dividend_yield": 0.04,
        "annual_fee": 0, "annual_tax_drag": 0, "simulations": 1, "seed": 1,
        "return_model": {"type": "bootstrap", "historical_annual_returns": [-0.2, 0.1, 0.3]},
    }
    result = run("income", inputs, {})["result"]
    dividend = result["strategies"]["dividend_cash"]
    sales = result["strategies"]["total_return_sales"]
    assert dividend["terminal_wealth_percentiles"] == sales["terminal_wealth_percentiles"]
    assert dividend["mean_sales"]["amount"] < sales["mean_sales"]["amount"]
    assert result["sequence_risk"]["median_terminal_wealth_with_low_returns_first"]["amount"] < result["sequence_risk"]["median_terminal_wealth_with_high_returns_first"]["amount"]


def test_ladder_uses_only_cashflows_available_by_each_liability_and_reports_gaps():
    result = run("ladder", {
        "currency": "USD", "as_of": "2026-01-01",
        "reserve": {"currency": "USD", "amount": 20},
        "liabilities": [
            {"id": "tuition-1", "date": "2026-06-01", "amount": 100, "currency": "USD"},
            {"id": "tuition-2", "date": "2027-06-01", "amount": 100, "currency": "USD"},
        ],
        "assets": [{"id": "bond-a", "currency": "USD", "cashflows": [
            {"date": "2026-05-01", "amount": 60}, {"date": "2027-05-01", "amount": 80},
            {"date": "2028-01-01", "amount": 100},
        ]}],
    }, {})["result"]
    assert [row["gap_on_due_date"]["amount"] for row in result["coverage"]] == [20.0, 40.0]
    assert result["coverage"][1]["arrears_paid_before_current_liability"]["amount"] == 20.0
    assert result["cumulative_on_due_date_gap"]["amount"] == 60.0
    assert result["later_cashflows_applied_to_arrears"]["amount"] == 40.0
    assert result["ending_outstanding_arrears"]["amount"] == 0.0
    assert result["uncommitted_later_cashflows"]["amount"] == 60.0

    arrears = run("ladder", {
        "currency": "USD", "as_of": "2026-01-01",
        "reserve": {"currency": "USD", "amount": 0},
        "liabilities": [
            {"id": "old", "date": "2026-02-01", "amount": 100, "currency": "USD"},
            {"id": "later", "date": "2026-04-01", "amount": 100, "currency": "USD"},
        ],
        "assets": [{"id": "inflow", "currency": "USD", "cashflows": [
            {"date": "2026-03-01", "amount": 150},
        ]}],
    }, {})["result"]
    assert arrears["coverage"][1]["arrears_paid_before_current_liability"]["amount"] == 100.0
    assert arrears["coverage"][1]["funded_on_due_date"]["amount"] == 50.0
    assert arrears["ending_outstanding_arrears"]["amount"] == 50.0
    assert arrears["ending_reserve_after_liabilities"]["amount"] == 0.0
    assert arrears["uncommitted_later_cashflows"]["amount"] == 0


def test_missing_assumptions_and_implicit_fx_are_rejected():
    assert run("project", {"currency": "USD"}, {})["status"] == "needs_input"
    with pytest.raises(ValueError, match="requires modeled_fx"):
        run("ladder", {
            "currency": "USD", "as_of": "2026-01-01",
            "reserve": {"currency": "USD", "amount": 0},
            "liabilities": [{"id": "g", "date": "2027-01-01", "amount": 100, "currency": "EUR"}],
            "assets": [],
        }, {})
    converted = run("ladder", {
        "currency": "USD", "as_of": "2026-01-01",
        "reserve": {"currency": "USD", "amount": 0},
        "modeled_fx": [{"from": "EUR", "to": "USD", "rate": 1.2}],
        "liabilities": [{"id": "g", "date": "2027-01-01", "amount": 100, "currency": "EUR"}],
        "assets": [],
    }, {})
    assert converted["result"]["ending_outstanding_arrears"]["amount"] == 120.0
