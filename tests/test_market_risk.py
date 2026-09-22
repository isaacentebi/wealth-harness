"""Stress with beta propagation and FX shocks, VaR/CVaR of the current book, and the truncation boundary."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from wealth import legacy, market
from wealth.service import WealthService


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def offline(*args, **kwargs):
        raise OSError("network access is disabled in tests")
    monkeypatch.setattr(legacy, "_load_factors", offline)
    monkeypatch.setattr(legacy, "_load_prices", lambda *a, **k: pytest.fail("live price path used"))
    monkeypatch.setattr(legacy, "_fetch_one", offline)


def _rows(n=300, beta=0.5, seed=3, symbols=("SPY", "BBB"), start="2024-01-02"):
    """SPY random walk; BBB moves exactly ``beta`` times SPY each day."""
    rng = np.random.default_rng(seed)
    r = rng.normal(0.0004, 0.01, n)
    spy = 100 * np.cumprod(1 + r)
    bbb = 50 * np.cumprod(1 + beta * r)
    dates = pd.bdate_range(start, periods=n)
    return [{"date": str(d.date()), symbols[0]: float(spy[i]), symbols[1]: float(bbb[i])} for i, d in enumerate(dates)]


# ------------------------------------------------------------------ stress


def test_partial_shock_propagates_by_a_history_beta():
    out = market.run("stress", {"currency": "USD", "weights": {"SPY": 0.6, "BBB": 0.4},
                                "prices": {"currency": "USD", "source": "fixture", "rows": _rows()},
                                "scenarios": [{"name": "equity -25%", "shocks": {"SPY": -0.25}}]}, {})
    assert out["status"] == "ready"
    row = out["result"]["scenarios"][0]
    assert row["asset_returns"]["BBB"] == pytest.approx(-0.125, abs=1e-4)
    assert row["portfolio_return"] == pytest.approx(0.6 * -0.25 + 0.4 * -0.125, abs=1e-4)
    detail = row["propagation"]["assets"]["BBB"]
    assert detail["basis"] == "history" and detail["n_returns"] == 299 and detail["r_squared"] == pytest.approx(1)
    assert row["propagation"]["assets"]["SPY"] == {"basis": "shocked"}
    assert any("OLS" in a for a in out["assumptions"])


def test_full_shocks_are_unchanged_and_too_little_history_falls_back_to_the_asset_class():
    full = market.run("stress", {"currency": "USD", "weights": {"SPY": 0.5, "BBB": 0.5},
                                 "scenarios": [{"name": "s", "shocks": {"SPY": -0.2, "BBB": 0.02}}]}, {})
    assert full["result"]["scenarios"][0]["portfolio_return"] == pytest.approx(-0.09)
    assert "propagation" not in full["result"]["scenarios"][0]
    short = market.run("stress", {"currency": "USD", "weights": {"SPY": 0.5, "BND": 0.5},
                                  "asset_classes": {"BND": "bond"},
                                  "prices": {"currency": "USD", "source": "fixture",
                                             "rows": _rows(30, symbols=("SPY", "BND"))},
                                  "scenarios": [{"name": "s", "shocks": {"SPY": -0.2}}]}, {})
    detail = short["result"]["scenarios"][0]["propagation"]["assets"]["BND"]
    assert detail["basis"] == "asset_class_default" and detail["beta"] == 0.1
    assert "29 overlapping daily returns" in detail["history"]
    assert short["result"]["scenarios"][0]["portfolio_return"] == pytest.approx(0.5 * -0.2 + 0.5 * -0.02)
    assert any("asset-class beta" in a for a in short["assumptions"])


def test_household_asset_classes_drive_defaults_and_unknown_stays_unknown():
    household = {"currency": "USD", "complete": True, "positions": [
        {"account_id": "a", "symbol": "VTI", "value": 600, "currency": "USD", "asset_class": "equity"},
        {"account_id": "a", "symbol": "BND", "value": 300, "currency": "USD", "asset_class": "bond"},
        {"account_id": "a", "symbol": "XYZ", "value": 50, "currency": "USD"},
        {"account_id": "a", "symbol": "USD", "value": 50, "currency": "USD", "asset_class": "cash"}]}
    out = market.run("stress", {"scenarios": [{"name": "SPY -30%", "shocks": {"SPY": -0.3}}]},
                     {"household": household})
    row = out["result"]["scenarios"][0]
    assert row["asset_returns"]["VTI"] == pytest.approx(-0.3) and row["asset_returns"]["BND"] == pytest.approx(-0.03)
    assert row["asset_returns"]["CASH::USD"] == 0.0
    assert row["asset_returns"]["XYZ"] is None and row["portfolio_return"] is None
    assert row["portfolio_return_reason"] == "unknown return for XYZ" and row["portfolio_change"] is None
    assert out["status"] == "partial" and any("XYZ" in m for m in out["missing"])
    explicit = market.run("stress", {"scenarios": [{"name": "SPY -30%", "shocks": {"SPY": -0.3}}],
                                     "betas": {"XYZ": 2.0}, "beta_source": "broker risk report"},
                          {"household": household})
    row = explicit["result"]["scenarios"][0]
    assert row["asset_returns"]["XYZ"] == pytest.approx(-0.6)
    assert row["portfolio_change"] == pytest.approx(1000 * row["portfolio_return"], abs=0.01)
    assert explicit["status"] == "ready"


def test_class_defaults_need_an_equity_factor_and_losses_floor_at_all_of_it():
    out = market.run("stress", {"currency": "USD", "weights": {"BND": 0.5, "VTI": 0.5},
                                "asset_classes": {"VTI": "equity", "BND": "bond"},
                                "scenarios": [{"name": "bonds", "shocks": {"BND": -0.1}}]}, {})
    assert out["result"]["scenarios"][0]["asset_returns"]["VTI"] is None
    crash = market.run("stress", {"currency": "USD", "weights": {"SPY": 0.5, "COIN": 0.5},
                                  "betas": {"COIN": 3}, "beta_source": "assumed",
                                  "scenarios": [{"name": "crash", "shocks": {"SPY": -0.5}}]}, {})
    row = crash["result"]["scenarios"][0]
    assert row["asset_returns"]["COIN"] == -1.0
    assert row["propagation"]["assets"]["COIN"]["clipped_from"] == pytest.approx(-1.5)


def test_usdmxn_shock_reaches_the_peso_value_of_dollar_assets():
    household = {"currency": "MXN", "complete": True, "positions": [
        {"account_id": "a", "symbol": "SPY", "value": 600000, "currency": "MXN", "asset_class": "equity",
         "native_currency": "USD"},
        {"account_id": "a", "symbol": "NAFTRAC.MX", "value": 200000, "currency": "MXN", "asset_class": "equity",
         "native_currency": "MXN"},
        {"account_id": "a", "symbol": "MXN", "value": 200000, "currency": "MXN", "asset_class": "cash"}]}
    out = market.run("stress", {"scenarios": [
        {"name": "US selloff, peso weakens", "shocks": {"SPY": -0.2, "NAFTRAC.MX": -0.1}, "fx_shocks": {"USDMXN": 0.15}},
        {"name": "peso only", "fx_shocks": {"USD/MXN": -0.1}}]}, {"household": household})
    first, second = out["result"]["scenarios"]
    assert first["asset_returns"]["SPY"] == pytest.approx(0.8 * 1.15 - 1)  # -8% in pesos
    assert first["local_returns"]["SPY"] == -0.2
    assert first["asset_returns"]["NAFTRAC.MX"] == pytest.approx(-0.1)
    assert first["asset_returns"]["CASH::MXN"] == 0.0
    assert first["portfolio_return"] == pytest.approx(0.6 * -0.08 + 0.2 * -0.1)
    assert first["fx"]["assets"]["SPY"] == {"currency": "USD", "pair": "USDMXN"}
    assert second["asset_returns"]["SPY"] == pytest.approx(-0.1) and second["asset_returns"]["NAFTRAC.MX"] == 0.0
    assert second["portfolio_return"] == pytest.approx(-0.06) and "held" in second["fx"]["note"]
    assert out["status"] == "ready"


def test_fx_shock_without_the_asset_currency_is_unknown_not_assumed():
    out = market.run("stress", {"currency": "MXN", "weights": {"VOO": 1.0},
                                "scenarios": [{"name": "fx", "shocks": {"VOO": 0.0}, "fx_shocks": {"USDMXN": 0.2}}]}, {})
    row = out["result"]["scenarios"][0]
    assert row["portfolio_return"] is None and row["fx"]["unknown_currency"] == ["VOO"]
    known = market.run("stress", {"currency": "MXN", "weights": {"VOO": 1.0}, "asset_currencies": {"VOO": "USD"},
                                  "scenarios": [{"name": "fx", "shocks": {"VOO": 0.0}, "fx_shocks": {"USDMXN": 0.2}}]},
                       {})
    assert known["result"]["scenarios"][0]["portfolio_return"] == pytest.approx(0.2)
    with pytest.raises(ValueError, match="currency pairs"):
        market.run("stress", {"currency": "MXN", "weights": {"VOO": 1.0},
                              "scenarios": [{"name": "fx", "fx_shocks": {"USD": 0.2}}]}, {})


class _Provider:
    """Stands in for the price cache: returns fixed history, never fetches."""

    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def history(self, symbols, start, end, *, adjusted=False, budget=None):
        self.calls.append(sorted(symbols))
        series = {s: {"symbol": s, "currency": "USD", "source": "cache fixture", "first": self.rows[0]["date"],
                      "last": self.rows[-1]["date"], "stale": False,
                      "points": [{"date": r["date"], "price": str(r[s])} for r in self.rows]}
                  for s in symbols if s in self.rows[0]}
        return {"series": series, "missing": [{"symbol": s, "reason": "offline and nothing cached"}
                                              for s in symbols if s not in self.rows[0]],
                "offline": True, "pending": []}


def test_service_estimates_betas_from_the_price_cache(tmp_path):
    provider = _Provider(_rows(200, beta=1.4))
    service = WealthService(tmp_path / "w.sqlite3", prices=provider)
    report = service.run("stress", inputs={"currency": "USD", "weights": {"BBB": 1.0},
                                           "scenarios": [{"name": "SPY -10%", "shocks": {"SPY": -0.1}}]})
    assert provider.calls == [["BBB", "SPY"]]
    row = report["result"]["scenarios"][0]
    assert row["asset_returns"]["BBB"] == pytest.approx(-0.14, abs=1e-4)
    assert row["propagation"]["assets"]["BBB"]["source"] == "price cache (adjusted closes) for betas"
    assert report["market_data"]["offline"] is True
    assert report["views"]


# ------------------------------------------------------------------ VaR / CVaR


def _book(n, seed=5):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2023-01-02", periods=n)
    common = rng.normal(0.0003, 0.01, n)
    rows = [{"date": str(d.date()),
             "AAA": float(100 * np.prod(1 + common[:i + 1] + 0.002 * np.sin(np.arange(i + 1)))),
             "BBB": float(100 * np.prod(1 + 0.3 * common[:i + 1]))} for i, d in enumerate(dates)]
    return {"currency": "USD", "source": "fixture", "rows": rows}


def test_analyze_reports_historical_and_parametric_var_with_contributions():
    household = {"currency": "USD", "complete": True, "positions": [
        {"account_id": "a", "symbol": "AAA", "value": 70000, "currency": "USD", "asset_class": "equity"},
        {"account_id": "a", "symbol": "BBB", "value": 30000, "currency": "USD", "asset_class": "equity"}]}
    prices = _book(600)
    out = market.run("analyze", {"benchmark": "AAA", "prices": prices}, {"household": household})
    tail = out["result"]["tail_risk"]
    px = pd.DataFrame(prices["rows"]).set_index("date")
    rets = px.pct_change().dropna().to_numpy() @ np.array([0.7, 0.3])
    losses = -rets
    one_day = tail["historical"]["1d"]
    assert one_day["var"] == pytest.approx(np.quantile(losses, 0.95), abs=1e-6)
    assert one_day["cvar"] >= one_day["var"] > 0
    assert one_day["cvar_amount"] == pytest.approx(one_day["cvar"] * 100000, abs=0.1)
    assert tail["historical"]["1m"]["var"] > one_day["var"] and tail["historical"]["1m"]["n_windows"] == 579
    assert sum(p["historical_cvar_1d"] for p in tail["by_position"]) == pytest.approx(one_day["cvar"], abs=1e-5)
    param = tail["parametric"]["1d"]
    assert param["var"] == pytest.approx(1.6448536 * rets.std(ddof=1) - rets.mean(), rel=1e-4)
    assert sum(p["parametric_var_1d"] for p in tail["by_position"]) == pytest.approx(param["var"], abs=1e-5)
    assert tail["parametric"]["1m"]["var"] > param["var"]
    assert any("normal" in a for a in out["assumptions"])


def test_short_samples_say_why_the_historical_estimate_is_missing():
    out = market.run("analyze", {"currency": "USD", "weights": {"AAA": 0.5, "BBB": 0.5}, "benchmark": "AAA",
                                 "prices": _book(320)}, {})
    tail = out["result"]["tail_risk"]
    assert tail["historical"]["1d"]["var"] is not None
    assert tail["historical"]["1m"]["var"] is None and "500 daily returns" in tail["historical"]["1m"]["reason"]
    assert tail["parametric"]["1m"]["var"] is not None
    assert tail["historical"]["1d"]["var_amount"] is None and "total value" in tail["amounts_reason"]
    tiny = market.run("analyze", {"currency": "USD", "weights": {"AAA": 1.0}, "benchmark": "AAA",
                                  "prices": _book(40)}, {})["result"]["tail_risk"]
    assert tiny["parametric"]["1d"]["var"] is None and tiny["by_position"] == []


# ------------------------------------------------------------------ truncation boundary


@pytest.mark.parametrize("n, warned", [(252 * 5, False), (252 * 5 - 21, False), (252 * 5 - 22, True)])
def test_truncated_sample_warning_boundary(n, warned):
    px = pd.DataFrame({"SPY": 1.0}, index=pd.bdate_range(end="2026-09-18", periods=n))
    warnings: list[str] = []
    market._common_window(px, 5, warnings)
    assert any(w.startswith("TRUNCATED SAMPLE") for w in warnings) is warned
