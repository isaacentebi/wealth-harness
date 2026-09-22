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


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Every provider adapter fails loudly unless a test injects fixture data."""
    def offline(*args, **kwargs):
        raise OSError("network access is disabled in tests")
    monkeypatch.setattr(legacy, "_load_factors", offline)
    monkeypatch.setattr(legacy, "_load_prices", lambda *a, **k: pytest.fail("live price path used"))
    monkeypatch.setattr(legacy, "_fetch_one", offline)


def rf_factors(index, daily_rf=0.00015):
    return pd.DataFrame({"Mkt-RF": 0.0, "SMB": 0.0, "HML": 0.0, "RF": daily_rf},
                        index=pd.DatetimeIndex(index))


def test_analyze_inline_prices_is_offline_and_preserves_stored_cash(monkeypatch):
    monkeypatch.setattr(legacy, "_load_prices", lambda *a, **k: pytest.fail("network path used"))
    prices = supplied_prices()
    dates = pd.to_datetime([row["date"] for row in prices["rows"]])
    monkeypatch.setattr(legacy, "_load_factors", lambda model=3: rf_factors(dates))
    offline = market.run("analyze", {"benchmark": "SPY", "prices": prices}, {"household": household()})
    assert offline["result"]["portfolio"]["sharpe"] is None
    assert any("keep this run offline" in w for w in offline["warnings"])
    out = market.run("analyze", {"benchmark": "SPY", "prices": prices, "risk_free": "ken_french"},
                     {"household": household()})
    assert out["status"] == "ready"
    assert out["result"]["scope"] == "Taxable account T-1"
    assert out["result"]["portfolio"]["weights"]["CASH::USD"] == pytest.approx(0.2)
    assert out["result"]["currency"] == "USD"
    assert out["sources"] == [{
        "kind": "supplied_rows", "ref": "custodian export fixture", "currency": "USD",
        "window": {"start": "2024-01-02", "end": "2025-03-24", "n_prices": 320},
    }, {"kind": "risk_free", "ref": "Ken French US daily RF (one-month T-bill)", "currency": "USD"}]
    assert out["result"]["window"]["start"] == "2024-01-03"
    assert out["result"]["benchmark"] == {"symbol": "SPY", "basis": "explicit"}
    # cash earns the risk-free rate instead of 0%
    cash = out["result"]["assets"]["CASH::USD"]
    assert cash["ann_return"] == pytest.approx((1.00015) ** 252 - 1, abs=1e-4)
    assert out["result"]["rf_annual"] == pytest.approx(0.00015 * 252, abs=1e-6)
    assert out["result"]["portfolio"]["sharpe"] is not None
    assert out["result"]["portfolio"]["alpha"] is not None
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
        # 319 returns = 120 train + 4 full 40-day tests + a final 39-day partial block
        assert len(validation["blocks"]) == 5
        assert [block["partial"] for block in validation["blocks"]] == [False] * 4 + [True]
        assert validation["blocks"][-1]["test_window"]["n_days"] == 39
        assert validation["partial_final_block"] is True
        assert validation["strategies"]["method"]["n_days"] == 319 - 120
        assert validation["transaction_cost_bps"] == 10
        block0 = validation["blocks"][0]["strategies"]
        assert block0["method"]["weights"]["CASH::USD"] == pytest.approx(stored_cash_weight)
        assert set(block0) == {"method", "equal_weight", "inverse_volatility"}
        assert validation["blocks"][0]["belief_effective_on"] == "2024-01-01"
        assert [block["belief_effective_on"] for block in validation["blocks"]] == [
            "2024-01-01", "2024-01-01", "2024-09-01", "2024-09-01", "2024-09-01"]
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
    first = validation["blocks"][0]["strategies"]
    assert first["method"]["turnover"] == pytest.approx(0.4)
    assert first["equal_weight"]["turnover"] == pytest.approx(0.4)
    assert first["method"]["cost_return"] == pytest.approx(0.004)
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


# --------------------------------------------------------------------------
# audit regressions
# --------------------------------------------------------------------------
def audit_returns(n: int = 1000) -> pd.DataFrame:
    rng = np.random.default_rng(0)
    return pd.DataFrame({"A": rng.normal(0.0002, 0.004, n), "B": rng.normal(0.0012, 0.03, n),
                         "C": rng.normal(0.0005, 0.01, n)}, index=pd.bdate_range("2020-01-01", periods=n))


def test_cvar_cagr_floor_is_met_in_sample_and_arithmetic_floor_is_labelled():
    rets = audit_returns()
    # The audit case: a 15% floor used to return a 10.6% CAGR portfolio. The best
    # in-sample CAGR here is ~13% (asset B alone), so 15% is now refused.
    with pytest.raises(ValueError, match="not attainable"):
        market._cvar(rets, 1.0, 0.95, min_cagr=0.15)
    weights, detail = market._cvar(rets, 1.0, 0.95, min_cagr=0.11)
    realized = legacy.ann_return((rets * weights).sum(axis=1))
    floor = detail["return_floor"]
    assert floor["kind"] == "in_sample_cagr"
    assert 0.11 - 1e-9 <= realized < 0.11 + 1e-4
    assert floor["realized_in_sample_cagr"] == pytest.approx(realized)
    assert floor["realized_arithmetic_annual_mean"] > floor["realized_in_sample_cagr"]
    _, arithmetic = market._cvar(rets, 1.0, 0.95, min_arithmetic=0.15)
    arith_floor = arithmetic["return_floor"]
    assert arith_floor["kind"] == "arithmetic_annual_mean"
    assert arith_floor["realized_arithmetic_annual_mean"] >= 0.15 - 1e-9
    # an arithmetic floor is not a CAGR floor: variance drag leaves realized CAGR below it
    assert arith_floor["realized_in_sample_cagr"] < 0.15
    with pytest.raises(ValueError, match="not both"):
        market._cvar(rets, 1.0, 0.95, min_cagr=0.1, min_arithmetic=0.1)


def two_asset_prices(currency: str = "USD", extra: tuple[str, ...] = ()) -> dict:
    idx = pd.bdate_range("2023-01-02", periods=300)
    rng = np.random.default_rng(1)
    symbols = ("AAPL", "BND", *extra)
    px = {s: 100 * np.cumprod(1 + rng.normal(0.0005, 0.01 + 0.005 * i, len(idx)))
          for i, s in enumerate(symbols)}
    rows = [{"date": str(d.date()), **{s: float(px[s][i]) for s in symbols}} for i, d in enumerate(idx)]
    return {"currency": currency, "source": "fixture", "rows": rows}


def test_benchmark_is_explicit_or_documented_default_never_the_first_holding(monkeypatch):
    prices = two_asset_prices(extra=("VTI",))
    dates = pd.to_datetime([row["date"] for row in prices["rows"]])
    monkeypatch.setattr(legacy, "_load_factors", lambda model=3: rf_factors(dates))
    request = {"weights": {"AAPL": 0.4, "BND": 0.6}, "currency": "USD", "prices": prices,
               "risk_free": "ken_french"}
    out = market.run("analyze", request, {})
    assert out["result"]["bench"] == "VTI"
    assert out["result"]["benchmark"] == {"symbol": "VTI", "basis": "currency_default"}
    assert any("documented USD default" in a for a in out["assumptions"])
    assert out["result"]["portfolio"]["sharpe"] is not None
    # default benchmark absent from supplied prices: no beta, stated, and never AAPL
    out = market.run("analyze", {**request, "prices": two_asset_prices()}, {})
    assert out["result"]["bench"] is None
    assert out["result"]["portfolio"]["beta"] is None
    assert any("Default benchmark VTI" in w for w in out["warnings"])
    # a currency without a documented default never falls back to a holding
    mxn = market.run("analyze", {"weights": {"AAPL": 0.4, "BND": 0.6}, "currency": "MXN",
                                 "prices": two_asset_prices("MXN")}, {})
    assert mxn["result"]["bench"] is None
    assert mxn["result"]["benchmark"]["basis"] == "none"


def test_non_usd_risk_free_is_omitted_with_reason_or_explicitly_supplied():
    request = {"weights": {"AAPL": 0.4, "BND": 0.4, "CASH::MXN": 0.2}, "currency": "MXN",
               "benchmark": "BND", "prices": two_asset_prices("MXN")}
    out = market.run("analyze", request, {})
    assert out["result"]["portfolio"]["sharpe"] is None
    assert out["result"]["portfolio"]["alpha"] is None
    assert any("No built-in MXN risk-free series" in w for w in out["warnings"])
    assert out["result"]["assets"]["CASH::MXN"]["ann_return"] == 0
    supplied = market.run("analyze", {**request, "risk_free": {"annual_rate": 0.10,
                                                               "source": "Banxico 28-day CETES"}}, {})
    result = supplied["result"]
    assert result["portfolio"]["sharpe"] is not None
    assert result["assets"]["CASH::MXN"]["ann_return"] == pytest.approx(0.10, abs=1e-4)
    assert result["methodology"]["risk_free"].startswith("constant 10.0000% annual MXN")
    assert supplied["sources"][-1]["kind"] == "risk_free_assumption"
    omitted = market.run("analyze", {**request, "risk_free": "none"}, {})
    assert omitted["result"]["portfolio"]["sharpe"] is None


def test_partial_weights_are_never_silently_rescaled():
    base = {"currency": "USD", "benchmark": "BND", "prices": two_asset_prices(extra=("MSFT",)),
            "risk_free": "none", "weights": {"AAPL": 0.3, "MSFT": 0.3}}
    blocked = market.run("analyze", base, {})
    assert blocked["status"] == "needs_input"
    assert "weights summing to 1 (received 0.6)" in blocked["missing"][0]
    as_cash = market.run("analyze", {**base, "weights_residual": "cash"}, {})
    assert as_cash["result"]["portfolio"]["weights"] == pytest.approx(
        {"AAPL": 0.3, "MSFT": 0.3, "CASH::USD": 0.4})
    assert any("labelled CASH::USD" in w for w in as_cash["warnings"])
    normalized = market.run("analyze", {**base, "weights_residual": "normalize"}, {})
    assert normalized["result"]["portfolio"]["weights"] == pytest.approx({"AAPL": 0.5, "MSFT": 0.5})
    assert any("rescaled proportionally" in w for w in normalized["warnings"])
    with pytest.raises(ValueError, match="cannot be completed with cash"):
        market.run("analyze", {**base, "weights": {"AAPL": 0.7, "MSFT": 0.6}, "weights_residual": "cash"}, {})
    compare = market.run("compare", {**base, "current_weights": {"AAPL": 1.0},
                                     "proposed_weights": {"AAPL": 0.5}}, {})
    assert compare["status"] == "needs_input"
    assert compare["missing"][0].startswith("proposed_weights summing to 1")
    # Hand-rounded weights (within 1%) are rescaled, with a note; nothing else is.
    rounded = market.run("analyze", {**base, "weights": {"AAPL": 0.4999, "MSFT": 0.4999}}, {})
    assert rounded["result"]["portfolio"]["weights"] == pytest.approx({"AAPL": 0.5, "MSFT": 0.5})
    assert any("within rounding of 1" in w for w in rounded["warnings"])
    assert market.run("analyze", {**base, "weights": {"AAPL": 0.5, "MSFT": 0.489}}, {})["status"] == "needs_input"
    shape = market.run("stress", {"scenarios": [{"name": "crash", "shocks": {"AAPL": -0.3}}]}, {})["missing"][0]
    assert '"weights": {' in shape and "positions: [{symbol, value" in shape and "client_id" in shape
    with pytest.raises(ValueError, match=r"needs shocks \{SYMBOL: return\}.*\['drop'\]"):
        market.run("stress", {"currency": "USD", "weights": {"AAPL": 1.0},
                              "scenarios": [{"name": "crash", "drop": -0.3}]}, {})


def test_an_equity_or_market_shock_is_a_broad_index_shock_propagated_by_beta():
    held = market.run("stress", {"currency": "USD", "weights": {"IVV": 0.5, "BND": 0.3, "CASH::USD": 0.2},
                                 "asset_classes": {"IVV": "equity", "BND": "bond"},
                                 "scenarios": [{"name": "crash", "equity_shock": -0.3}]}, {})
    row = held["result"]["scenarios"][0]
    assert row["propagation"]["factor"] == "IVV" and row["propagation"]["factor_shock"] == -0.3
    assert row["portfolio_return"] == pytest.approx(0.5 * -0.3 + 0.3 * 0.1 * -0.3, abs=1e-9)
    assert any("equity_shock -30% is applied to IVV" in a for a in held["assumptions"])
    # No index fund held: the shock lands on SPY and every holding moves by its class beta to it.
    other = market.run("stress", {"currency": "USD", "weights": {"AAPL": 0.6, "BND": 0.4},
                                  "asset_classes": {"AAPL": "equity", "BND": "bond"},
                                  "scenarios": [{"name": "crash", "market_shock": -0.2}]}, {})
    row = other["result"]["scenarios"][0]
    assert row["propagation"]["factor"] == "SPY"
    assert row["portfolio_return"] == pytest.approx(0.6 * -0.2 + 0.4 * 0.1 * -0.2, abs=1e-9)
    with pytest.raises(ValueError, match="not both"):
        market.run("stress", {"currency": "USD", "weights": {"AAPL": 1.0},
                              "scenarios": [{"name": "x", "equity_shock": -0.3, "market_shock": -0.2}]}, {})


def test_stress_window_and_regime_table_use_the_same_close_to_close_convention(monkeypatch):
    prices = supplied_prices()
    stress = market.run("stress", {
        "currency": "USD", "weights": {"AAA": 1.0}, "prices": prices,
        "scenarios": [{"name": "window", "start": "2024-03-02", "end": "2024-06-28"}],
    }, {})["result"]["scenarios"][0]
    px = pd.DataFrame(prices["rows"]).set_index("date")
    px.index = pd.to_datetime(px.index)
    rets = legacy.daily_returns(px)
    segment = legacy.slice_regime(rets, "2024-03-02", "2024-06-28")
    assert legacy.total_return(segment["AAA"]) == pytest.approx(stress["asset_returns"]["AAA"], abs=1e-12)
    monkeypatch.setattr(legacy, "REGIMES", [("fixture", "2024-03-02", "2024-06-28")])
    row = legacy.regime_table(rets, "SPY", None)[0]
    assert row["start"] == stress["window"]["observed_start"]
    assert row["assets"]["AAA"]["total_return"] == pytest.approx(stress["asset_returns"]["AAA"], abs=1e-4)
    # a window starting on the sample's first close includes no return into that close
    first = legacy.slice_regime(rets, "2024-01-02", "2024-01-10")
    assert first.index.min() == pd.Timestamp("2024-01-03")


def test_walk_forward_reports_partial_block_and_inverse_vol_and_static_baselines():
    out = market.run("construct", {
        "currency": "USD", "tickers": ["AAA", "BBB", "SPY"], "method": "minvar",
        "prices": supplied_prices(),
        "validation": {"train_days": 120, "test_days": 50, "transaction_cost_bps": 5,
                       "static_baseline_weights": {"SPY": 0.6, "BBB": 0.4}},
    }, {})
    validation = out["result"]["validation"]
    assert set(validation["strategies"]) == {"method", "equal_weight", "inverse_volatility", "static_baseline"}
    assert [b["test_window"]["n_days"] for b in validation["blocks"]] == [50, 50, 50, 49]
    assert validation["partial_final_block"] is True
    assert all(v["n_days"] == 199 for v in validation["strategies"].values())
    static = validation["blocks"][0]["strategies"]["static_baseline"]["weights"]
    assert static == pytest.approx({"AAA": 0.0, "BBB": 0.4, "SPY": 0.6})
    with pytest.raises(ValueError, match="outside the construction universe"):
        market.run("construct", {
            "currency": "USD", "tickers": ["AAA", "BBB"], "method": "equal", "prices": supplied_prices(),
            "validation": {"train_days": 120, "test_days": 50, "transaction_cost_bps": 0,
                           "static_baseline_weights": {"SPY": 1.0}},
        }, {})


def test_pairwise_covariance_is_inception_aware_psd_and_labelled():
    prices = supplied_prices()
    rows = [dict(row) for row in prices["rows"]]
    for row in rows[:150]:
        row["CCC"] = None  # CCC launched later
    request = {"currency": "USD", "tickers": ["AAA", "BBB", "CCC"], "method": "minvar",
               "prices": {**prices, "rows": rows}}
    common = market.run("construct", request, {})
    assert common["result"]["window"]["n_days"] == 169
    assert common["warnings"][0].startswith("TRUNCATED SAMPLE")
    pairwise = market.run("construct", {**request, "covariance": "pairwise"}, {})
    cov = pairwise["result"]["covariance"]
    assert cov["asset_first_return"]["AAA"] == "2024-01-03"
    assert cov["minimum_pair_overlap_returns"] == 169
    assert pairwise["warnings"][0].startswith("INCEPTION-AWARE COVARIANCE")
    assert sum(pairwise["result"]["weights"].values()) == pytest.approx(1.0)
    px = pd.DataFrame(rows).set_index("date").astype(float)
    px.index = pd.to_datetime(px.index)
    matrix, _ = market._pairwise_cov(px.pct_change(fill_method=None).iloc[1:])
    assert np.linalg.eigvalsh(matrix).min() > 0
    full = px["AAA"].pct_change().iloc[1:]
    assert matrix[0, 0] == pytest.approx(full.var() * 252)  # full history, not the common window
    with pytest.raises(ValueError, match="common window"):
        market.run("construct", {**request, "covariance": "pairwise", "method": "cvar"}, {})


def test_black_litterman_market_prior_and_posterior_covariance():
    px = pd.DataFrame(price_rows()).set_index("date")[["AAA", "BBB", "CCC"]]
    px.index = pd.to_datetime(px.index)
    rets = legacy.daily_returns(px)
    cov = market._shrunk_cov(rets)
    inputs = {"market_weights": {"AAA": 600, "BBB": 300, "CCC": 100},
              "market_weights_source": "fixture caps", "tau": 0.05, "risk_aversion": 2.5, "views": []}
    weights, detail = market._black_litterman(rets, 1.0, inputs)
    expected_prior = 2.5 * cov @ np.array([0.6, 0.3, 0.1])
    assert list(detail["prior_returns"].values()) == pytest.approx(list(expected_prior))
    assert detail["prior"] == "market_equilibrium"
    # without views the posterior mean is the prior and the covariance widens by tau * Sigma
    assert detail["posterior_returns"] == pytest.approx(detail["prior_returns"])
    assert detail["posterior_volatility"]["AAA"] == pytest.approx(np.sqrt(1.05 * cov[0, 0]))
    # reverse optimization round-trips: with negligible tau the optimizer recovers the market mix
    tiny, _ = market._black_litterman(rets, 1.0, dict(inputs, tau=1e-8))
    assert list(tiny) == pytest.approx([0.6, 0.3, 0.1], abs=1e-3)
    # with no views only the covariance (Sigma + M) differs, and it moves the weights
    assert not np.allclose(weights.to_numpy(), tiny.to_numpy(), atol=1e-3)
    viewed = dict(inputs, views=[{"weights": {"AAA": 1.0}, "expected_return": 0.2, "confidence": 0.5}])
    _, with_view = market._black_litterman(rets, 1.0, viewed)
    assert with_view["posterior_returns"]["AAA"] > detail["posterior_returns"]["AAA"]
    assert with_view["posterior_volatility"]["AAA"] < detail["posterior_volatility"]["AAA"]
    with pytest.raises(ValueError, match="exactly one prior"):
        market._black_litterman(rets, 1.0, dict(inputs, prior_returns={"AAA": 0, "BBB": 0, "CCC": 0}))


def test_rendering_is_a_separate_module_reachable_from_the_engine():
    from wealth import render
    assert legacy._view("card_analyze") is render.card_analyze
    assert not hasattr(legacy, "svg_line")  # presentation no longer lives in the quant engine


def test_shared_helpers_are_the_single_source():
    from wealth import _common, planning, workflows
    assert market._envelope is _common.envelope
    assert planning._money is _common.money and workflows._date is _common.iso_date
    assert _common.historical_cvar([0.0, 1.0, 2.0, 3.0], 0.5) == pytest.approx(2.5)
    assert _common.money(__import__("decimal").Decimal("1.50"), "USD") == {"currency": "USD", "amount": "1.5"}
