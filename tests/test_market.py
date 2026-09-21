from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from wealth import legacy, market


def price_rows(n: int = 320) -> list[dict]:
    dates = pd.bdate_range("2024-01-02", periods=n)
    rng = np.random.default_rng(17)
    common = rng.normal(0.0003, 0.008, n)
    returns = {
        "AAA": common + rng.normal(0.0001, 0.006, n),
        "BBB": 0.4 * common + rng.normal(0.0002, 0.004, n),
        "CCC": -0.15 * common + rng.normal(0.00015, 0.009, n),
        "SPY": common,
    }
    prices = {symbol: 100 * np.exp(np.cumsum(values)) for symbol, values in returns.items()}
    return [{"date": str(day.date()), **{symbol: float(values[i]) for symbol, values in prices.items()}}
            for i, day in enumerate(dates)]


def supplied_prices() -> dict:
    return {"currency": "USD", "source": "custodian export fixture", "rows": price_rows()}


def household() -> dict:
    return {
        "currency": "USD", "scope": "Taxable account T-1", "complete": True,
        "positions": [
            {"account_id": "T-1", "symbol": "AAA", "value": 500, "currency": "USD", "asset_class": "equity"},
            {"account_id": "T-1", "symbol": "BBB", "value": 300, "currency": "USD", "asset_class": "bond"},
            {"account_id": "T-1", "symbol": "USD", "value": 200, "currency": "USD", "asset_class": "cash"},
        ],
    }


def test_analyze_inline_prices_is_offline_and_preserves_stored_cash(monkeypatch):
    monkeypatch.setattr(legacy, "_load_prices", lambda *a, **k: pytest.fail("network path used"))
    prices = supplied_prices()
    out = market.run("analyze", {"benchmark": "SPY", "prices": prices},
                     {"household": household()})
    assert out["status"] == "ready"
    assert out["result"]["scope"] == "Taxable account T-1"
    assert out["result"]["portfolio"]["weights"]["CASH::USD"] == pytest.approx(0.2)
    assert out["result"]["currency"] == "USD"
    assert out["sources"] == [{
        "kind": "supplied_rows", "ref": "custodian export fixture", "currency": "USD",
        "window": {"start": "2024-01-02", "end": "2025-03-24", "n_prices": 320},
    }]
    assert out["result"]["window"]["start"] == "2024-01-03"
    incomplete = household()
    del incomplete["positions"][0]["value"]
    blocked = market.run("analyze", {"benchmark": "SPY", "prices": prices},
                         {"household": incomplete})
    assert blocked["status"] == "needs_input"
    assert blocked["missing"] == ["context.household.positions[0].value"]
    factor_index = pd.to_datetime([row["date"] for row in prices["rows"]])
    factor_rng = np.random.default_rng(41)
    factor_rows = pd.DataFrame({
        "Mkt-RF": factor_rng.normal(0.0002, 0.008, len(factor_index)),
        "SMB": factor_rng.normal(0.0001, 0.004, len(factor_index)),
        "HML": factor_rng.normal(-0.0001, 0.004, len(factor_index)),
        "RF": np.full(len(factor_index), 0.00005),
    }, index=factor_index)
    monkeypatch.setattr(legacy, "_load_factors", lambda model=3: factor_rows)
    factors = market.run("factors", {"model": 3, "prices": prices}, {"household": household()})
    assert factors["status"] == "ready"
    assert set(factors["result"]["assets"]) == {"AAA", "BBB"}
    assert factors["result"]["excluded_cash"] == ["CASH::USD"]
    assert factors["sources"][-1]["ref"] == "Ken French US daily 3-factor library"
    monkeypatch.setattr(legacy, "_load_factors",
                        lambda model=3: (_ for _ in ()).throw(OSError("provider unavailable")))
    unavailable = market.run("factors", {"model": 3, "prices": prices}, {"household": household()})
    assert unavailable["status"] == "needs_input"
    assert unavailable["missing"] == ["factor data coverage for requested assets"]
    assert unavailable["result"]["assets"] == {}


def test_compare_uses_one_sample_and_reports_directional_differences():
    out = market.run("compare", {
        "currency": "USD", "benchmark": "SPY", "prices": supplied_prices(),
        "current_weights": {"AAA": 0.8, "BBB": 0.2},
        "proposed_weights": {"AAA": 0.2, "BBB": 0.8},
    }, {})
    assert out["status"] == "ready"
    result = out["result"]
    assert result["current"]["provenance"]["price_sha256"] == result["proposed"]["provenance"]["price_sha256"]
    assert result["difference"]["ann_vol"] < 0
    assert result["currency"] == "USD"


def test_stress_supports_explicit_shocks_and_dated_historical_windows():
    out = market.run("stress", {
        "currency": "USD", "weights": {"AAA": 0.8, "BBB": 0.2},
        "prices": supplied_prices(),
        "scenarios": [
            {"name": "manual equity shock", "shocks": {"AAA": -0.10, "BBB": 0.02}},
            {"name": "fixture month", "start": "2024-02-01", "end": "2024-02-29"},
        ],
    }, {})
    assert out["status"] == "ready"
    assert out["result"]["scenarios"][0]["portfolio_return"] == pytest.approx(-0.076)
    historical = out["result"]["scenarios"][1]
    assert historical["kind"] == "historical_window"
    assert historical["window"]["observed_start"] == "2024-02-01"
    assert historical["window"]["observed_end"] == "2024-02-29"


@pytest.mark.parametrize("method", ["equal", "invvol", "minvar", "riskparity", "hrp", "cvar", "black_litterman"])
def test_all_constructors_share_constraints_baseline_and_cash_policy(method):
    expanded = household()
    expanded["positions"].insert(2, {"account_id": "T-1", "symbol": "CCC", "value": 200,
                                      "currency": "USD", "asset_class": "equity"})
    expanded["positions"][-1]["value"] = 100
    request = {
        "method": method, "max_weight": 0.6, "prices": supplied_prices(), "confidence": 0.9,
    }
    if method == "black_litterman":
        request.update({
            "prior_returns": {"AAA": 0.05, "BBB": 0.05, "CCC": 0.05},
            "views": [{"weights": {"AAA": 1.0, "BBB": -1.0},
                       "expected_return": 0.04, "confidence": 0.75}],
            "tau": 0.05, "risk_aversion": 3.0,
            "validation": {
                "train_days": 120, "test_days": 40, "transaction_cost_bps": 10,
                "belief_schedule": [{
                    "effective_on": "2024-01-01",
                    "prior_returns": {"AAA": 0.05, "BBB": 0.05, "CCC": 0.05},
                    "views": [{"weights": {"AAA": 1.0, "BBB": -1.0},
                               "expected_return": 0.04, "confidence": 0.75}],
                }, {
                    "effective_on": "2024-09-01",
                    "prior_returns": {"AAA": 0.05, "BBB": 0.05, "CCC": 0.05},
                    "views": [{"weights": {"AAA": 1.0, "BBB": -1.0},
                               "expected_return": -0.03, "confidence": 0.75}],
                }],
            },
        })
    out = market.run("construct", request, {"household": expanded})
    assert out["status"] == "ready"
    result = out["result"]
    assert sum(result["weights"].values()) == pytest.approx(1.0)
    assert max(result["weights"].values()) <= 0.6 + 1e-7
    stored_cash_weight = 100 / 1100
    assert result["weights"]["CASH::USD"] == pytest.approx(stored_cash_weight)
    assert result["equal_weight_baseline"]["weights"]["CASH::USD"] == pytest.approx(stored_cash_weight)
    if method == "black_litterman":
        model = result["model"]
        assert model["posterior_returns"]["AAA"] > model["posterior_returns"]["BBB"]
        validation = result["validation"]
        assert len(validation["blocks"]) == 4
        assert validation["transaction_cost_bps"] == 10
        assert validation["blocks"][0]["method_weights"]["CASH::USD"] == pytest.approx(stored_cash_weight)
        assert validation["blocks"][0]["belief_effective_on"] == "2024-01-01"
        assert [block["belief_effective_on"] for block in validation["blocks"]] == [
            "2024-01-01", "2024-01-01", "2024-09-01", "2024-09-01"]
        assert validation["blocks"][0]["train_window"]["end"] < validation["blocks"][0]["test_window"]["start"]
        assert validation["interpretation"].endswith("does not prove future advantage")
        too_long = dict(request)
        too_long["validation"] = {"train_days": 300, "test_days": 40, "transaction_cost_bps": 0}
        blocked = market.run("construct", too_long, {"household": expanded})
        assert blocked["status"] == "needs_input"
        assert blocked["missing"] == ["validation requires at least 340 return observations; received 319"]
        degenerate = dict(request)
        degenerate.pop("validation")
        degenerate["views"] = [{"weights": {}, "expected_return": 0.04, "confidence": 0.75}]
        with pytest.raises(ValueError, match="degenerate zero loadings"):
            market.run("construct", degenerate, {"household": expanded})
        no_schedule = dict(request)
        no_schedule["validation"] = {"train_days": 120, "test_days": 40, "transaction_cost_bps": 0}
        blocked_beliefs = market.run("construct", no_schedule, {"household": expanded})
        assert blocked_beliefs["status"] == "needs_input"
        assert blocked_beliefs["missing"] == [
            "validation.belief_schedule is required for Black-Litterman walk-forward validation"]


def test_infeasible_cap_is_rejected_instead_of_relaxed():
    with pytest.raises(ValueError, match="infeasible maximum weight"):
        market.run("construct", {
            "currency": "USD", "tickers": ["AAA", "BBB", "CCC"], "method": "cvar",
            "max_weight": 0.3, "prices": supplied_prices(),
        }, {})
    validation = market.run("construct", {
        "currency": "USD", "weights": {"AAA": 0.9, "BBB": 0.1}, "method": "equal",
        "max_weight": 1.0, "prices": supplied_prices(),
        "validation": {"train_days": 120, "test_days": 40, "transaction_cost_bps": 100},
    }, {})["result"]["validation"]
    assert validation["blocks"][0]["method_turnover"] == pytest.approx(0.4)
    assert validation["blocks"][0]["equal_turnover"] == pytest.approx(0.4)
    assert validation["blocks"][0]["method_cost_return"] == pytest.approx(0.004)
    with pytest.raises(ValueError, match="cash symbols must match portfolio currency USD"):
        market.run("construct", {
            "currency": "USD", "weights": {"AAA": 0.8, "CASH::MXN": 0.2},
            "method": "equal", "prices": supplied_prices(),
        }, {})


def test_compatibility_launcher_executes_engine_in_patchable_wrapper_globals(monkeypatch, capsys):
    path = Path(__file__).parents[1] / "tools" / "wm.py"
    spec = importlib.util.spec_from_file_location("wm_compat_test", path)
    wrapper = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = wrapper
    spec.loader.exec_module(wrapper)
    px = pd.DataFrame(price_rows(80)).set_index("date")[["AAA", "SPY"]]
    px.index = pd.to_datetime(px.index)
    px.attrs.update(currency="USD", source="patched loader", data_kind="synthetic",
                    risk_free_policy="omit", first_dates={"AAA": "2024-01-02", "SPY": "2024-01-02"})
    monkeypatch.setattr(wrapper, "_load_prices", lambda *a, **k: (px, []))
    monkeypatch.setattr(wrapper, "_ticker_meta", lambda names: {n: {"currency": "USD"} for n in names})
    wrapper.main(["analyze", "AAA", "--bench", "SPY", "--currency", "USD"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["provenance"]["source"] == "patched loader"
    assert wrapper.ROOT == Path(__file__).parents[1]
    assert path.read_text().startswith("# /// script\n")
