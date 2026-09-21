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


def income_inputs(**overrides):
    inputs = {
        "currency": "USD", "initial_wealth": 1000, "years": 3,
        "annual_income_need": 50, "annual_need_growth": 0, "annual_dividend_yield": 0.04,
        "annual_fee": 0, "annual_tax_drag": 0, "simulations": 1, "seed": 1,
        "return_model": {"type": "bootstrap", "basis": "nominal",
                         "historical_annual_returns": [-0.2, 0.1, 0.3]},
    }
    inputs.update(overrides)
    return inputs


def test_income_without_taxes_does_not_fake_a_strategy_comparison():
    out = run("income", income_inputs(), {})
    result = out["result"]
    # identical by construction without taxes, so only one strategy is reported
    assert set(result["strategies"]) == {"total_return_sales"}
    assert result["strategy_comparison"] is None
    assert any("identical without taxes" in w for w in out["warnings"])
    assert result["sequence_risk"]["median_terminal_wealth_with_low_returns_first"]["amount"] < \
        result["sequence_risk"]["median_terminal_wealth_with_high_returns_first"]["amount"]


def test_income_taxes_make_dividend_and_total_return_strategies_differ():
    taxes = {"dividend_tax_rate": 0.3, "capital_gains_tax_rate": 0.15,
             "initial_cost_basis": 1000, "total_return_dividend_yield": 0.0}
    out = run("income", income_inputs(
        years=10, annual_dividend_yield=0.05, simulations=1, taxes=taxes, spending_timing="end",
        return_model={"type": "parametric", "basis": "nominal", "annual_return": 0.07, "annual_volatility": 0}),
        {})
    strategies = out["result"]["strategies"]
    dividend, total = strategies["dividend_cash"], strategies["total_return_sales"]
    # dividends are taxed every year at 30%; the zero-yield portfolio only realizes gains it sells, at 15%
    assert dividend["mean_taxes_paid"]["amount"] > total["mean_taxes_paid"]["amount"]
    assert dividend["terminal_wealth_percentiles"]["p50"]["amount"] < total["terminal_wealth_percentiles"]["p50"]["amount"]
    # the deferral advantage is partly a deferred liability, shown after liquidation tax
    assert total["terminal_wealth_after_liquidation_tax_percentiles"]["p50"]["amount"] < \
        total["terminal_wealth_percentiles"]["p50"]["amount"]
    assert dividend["mean_dividends_used"]["amount"] > 0 and total["mean_dividends_used"]["amount"] == 0
    # exact first-year check for the dividend strategy (end-of-year spending):
    # 1000 grows to 1070 of which 50 is dividend: 15 dividend tax, 35 net used, and the other
    # 15 is raised by selling shares worth 1020 on basis 1000, grossed up for the gains tax
    one = run("income", income_inputs(
        years=1, annual_dividend_yield=0.05, taxes=taxes, spending_timing="end",
        return_model={"type": "parametric", "basis": "nominal", "annual_return": 0.07, "annual_volatility": 0}),
        {})["result"]["strategies"]["dividend_cash"]
    rate = 0.15 * (1 - 1000 / 1020)
    assert one["mean_taxes_paid"]["amount"] == pytest.approx(15 + 15 * rate / (1 - rate), abs=0.01)
    assert one["mean_sales"]["amount"] == pytest.approx(15 / (1 - rate), abs=0.01)
    with pytest.raises(ValueError, match="double count"):
        run("income", income_inputs(taxes=taxes, annual_tax_drag=0.01), {})


def test_income_spending_timing_defaults_to_start_of_year_and_matches_closed_form():
    model = {"type": "parametric", "basis": "nominal", "annual_return": 0.06, "annual_volatility": 0}
    base = income_inputs(initial_wealth=1_000_000, years=30, annual_income_need=60000,
                         annual_need_growth=0.03, annual_fee=0.005, return_model=model)
    start = run("income", base, {})
    end = run("income", {**base, "spending_timing": "end"}, {})
    assert start["result"]["spending_timing"] == "start"
    assert any("start of each year" in a for a in start["assumptions"])
    w_end = w_start = 1e6
    for year in range(30):
        need = 60000 * 1.03 ** year
        w_end = max(0, w_end * 1.06 * 0.995 - need)
        w_start = max(0, (w_start - need) * 1.06 * 0.995)
    assert end["result"]["strategies"]["total_return_sales"]["terminal_wealth_percentiles"]["p50"]["amount"] == \
        pytest.approx(w_end, abs=0.01)
    assert start["result"]["strategies"]["total_return_sales"]["terminal_wealth_percentiles"]["p50"]["amount"] == \
        pytest.approx(w_start, abs=0.01)


def test_income_real_basis_needs_inflation_and_reports_real_outcomes():
    model = {"type": "parametric", "basis": "real", "annual_return": 0.03, "annual_volatility": 0}
    blocked = run("income", income_inputs(return_model=model), {})
    assert blocked["status"] == "needs_input"
    out = run("income", income_inputs(return_model=model, annual_inflation=0.02, annual_income_need=0,
                                      years=2), {})["result"]["strategies"]["total_return_sales"]
    nominal = 1000 * (1.03 * 1.02) ** 2
    assert out["terminal_wealth_percentiles"]["p50"]["amount"] == pytest.approx(nominal, abs=0.01)
    assert out["real_terminal_wealth_percentiles"]["p50"]["amount"] == pytest.approx(1000 * 1.03 ** 2, abs=0.01)


def test_project_reports_nominal_and_real_and_counts_inflation_once():
    real_model = {"type": "parametric", "basis": "real", "annual_return": 0.04, "annual_volatility": 0}
    out = run("project", projection(return_model=real_model, annual_inflation=0.03, simulations=1,
                                    end_date="2028-01-01"), {})
    result = out["result"]
    assert result["terminal_wealth_percentiles"]["p50"]["amount"] == pytest.approx(100 * (1.04 * 1.03) ** 2, abs=0.01)
    assert result["real_terminal_wealth_percentiles"]["p50"]["amount"] == pytest.approx(100 * 1.04 ** 2, abs=0.01)
    # an indexed withdrawal of 100 today's money on a real 0% return costs exactly today's 100 in real terms
    flat_real = {"type": "parametric", "basis": "real", "annual_return": 0, "annual_volatility": 0}
    exact = run("project", projection(
        initial_wealth=200, return_model=flat_real, annual_inflation=0.05, simulations=1,
        end_date="2027-01-01", cashflows=[{"id": "g", "date": "2027-01-01", "amount": 100,
                                           "type": "withdrawal", "inflation_indexed": True, "hard_goal": True}]),
        {})["result"]
    assert exact["real_terminal_wealth_percentiles"]["p50"]["amount"] == pytest.approx(100, abs=0.01)
    unstated = run("project", projection(), {})
    assert any("basis was not stated" in w for w in unstated["warnings"])


def test_tax_drag_hits_gains_not_wealth_in_loss_years():
    loss = run("project", projection(
        simulations=1, end_date="2027-01-01", annual_tax_drag=0.02,
        return_model={"type": "parametric", "basis": "nominal", "annual_return": -0.1, "annual_volatility": 0}), {})
    assert loss["result"]["terminal_wealth_percentiles"]["p50"]["amount"] == pytest.approx(90.0)
    gain = run("project", projection(
        simulations=1, end_date="2027-01-01", annual_tax_drag=0.02,
        return_model={"type": "parametric", "basis": "nominal", "annual_return": 0.1, "annual_volatility": 0}), {})
    assert gain["result"]["terminal_wealth_percentiles"]["p50"]["amount"] == pytest.approx(108.0)
    small = run("project", projection(
        simulations=1, end_date="2027-01-01", annual_tax_drag=0.05,
        return_model={"type": "parametric", "basis": "nominal", "annual_return": 0.01, "annual_volatility": 0}), {})
    assert small["result"]["terminal_wealth_percentiles"]["p50"]["amount"] == pytest.approx(100.0)


def test_block_bootstrap_and_student_t_are_seeded_and_documented():
    import random

    from wealth import planning

    history = [0.1, -0.2, 0.3, 0.05]
    model = planning._return_model({"type": "block_bootstrap", "basis": "real", "block_length": 4,
                                    "historical_annual_returns": history}, "m", [])
    draws, _ = planning._draw_returns(model, 8, random.Random(3))
    # a full-length block is a rotation of the history, so adjacent-year order is preserved
    doubled = history + history
    assert any(draws[:4] == doubled[i:i + 4] for i in range(4))
    assert "block length 4" in model.describe()
    with pytest.raises(ValueError, match="block_length"):
        planning._return_model({"type": "block_bootstrap", "basis": "real", "block_length": 5,
                                "historical_annual_returns": history}, "m", [])
    t_model = planning._return_model({"type": "student_t", "basis": "nominal", "annual_return": 0.06,
                                      "annual_volatility": 0.15, "degrees_of_freedom": 4}, "m", [])
    sample, _ = planning._draw_returns(t_model, 40000, random.Random(5))
    mean = sum(sample) / len(sample)
    std = (sum((x - mean) ** 2 for x in sample) / len(sample)) ** 0.5
    assert mean == pytest.approx(0.06, abs=0.005)
    assert std == pytest.approx(0.15, rel=0.1)
    kurtosis = sum((x - mean) ** 4 for x in sample) / len(sample) / std ** 4
    assert kurtosis > 4  # fat tails relative to the normal's 3
    with pytest.raises(ValueError, match="degrees_of_freedom"):
        planning._return_model({"type": "student_t", "basis": "nominal", "annual_return": 0.06,
                                "annual_volatility": 0.15, "degrees_of_freedom": 2}, "m", [])
    first = run("project", projection(simulations=50, return_model={
        "type": "student_t", "basis": "nominal", "annual_return": 0.05, "annual_volatility": 0.2,
        "degrees_of_freedom": 5}), {})
    assert first == run("project", projection(simulations=50, return_model={
        "type": "student_t", "basis": "nominal", "annual_return": 0.05, "annual_volatility": 0.2,
        "degrees_of_freedom": 5}), {})


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
