"""SIC (Mexico) listings: MXN quote currency, premium/discount, and opt-in merging. Fully offline."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from wealth import legacy, market


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def offline(*args, **kwargs):
        raise OSError("network access is disabled in tests")
    monkeypatch.setattr(legacy, "_load_factors", offline)
    monkeypatch.setattr(legacy, "_fetch_one", offline)
    monkeypatch.setattr(legacy, "_fx_series", offline)
    monkeypatch.setattr(legacy, "_latest_quote", lambda *a, **k: pytest.fail("live quote path used"))
    monkeypatch.setattr(legacy, "_load_prices", lambda *a, **k: pytest.fail("live price path used"))


def rows(symbols, n=120):
    dates = pd.bdate_range("2025-01-02", periods=n)
    rng = np.random.default_rng(5)
    out = []
    paths = {s: 100 * np.exp(np.cumsum(rng.normal(0.0003, 0.01, n))) for s in symbols}
    for i, day in enumerate(dates):
        out.append({"date": str(day.date()), **{s: float(paths[s][i]) for s in symbols}})
    return out


# ---------------------------------------------------------------- currency
def test_mx_suffix_is_mxn_and_other_symbols_keep_provider_currency():
    assert market.is_sic_symbol("AAPL.MX") and market.is_sic_symbol("brkb.mx")
    assert not market.is_sic_symbol("AAPL") and not market.is_sic_symbol(".MX")
    assert market.quote_currency("AAPL.MX") == "MXN"
    assert market.quote_currency("AAPL.MX", "USD") == "MXN"
    assert market.quote_currency("AAPL", "usd") == "USD"
    assert market.quote_currency("SAP.DE") is None


def test_live_path_converts_mx_listing_from_mxn(monkeypatch):
    """The legacy adapter's FX step sees .MX as MXN even when provider metadata is blank."""
    idx = pd.bdate_range("2025-01-02", periods=40)
    raw = pd.DataFrame({"AAPL.MX": np.linspace(4000, 4400, 40), "MSFT": np.linspace(400, 440, 40)}, index=idx)
    monkeypatch.setattr(legacy, "_ticker_meta",
                        lambda tickers: {t: {"currency": None if t.endswith(".MX") else "USD"} for t in tickers})
    fx = pd.Series(20.0, index=idx)
    seen = {}

    def fake_load(tickers, years=5, currency=None, align=True):
        seen["meta"] = legacy._ticker_meta(list(tickers))
        loader = lambda frm, to: (1.0 / fx) if (frm, to) == ("MXN", "USD") else None
        return legacy.to_currency(raw[list(tickers)], currency, [], fx_loader=loader), []

    monkeypatch.setattr(legacy, "_load_prices", fake_load)
    out = market._price_frame({"years": 1}, ["AAPL.MX", "MSFT"], "USD")
    assert seen["meta"]["AAPL.MX"]["currency"] == "MXN"
    assert out.px["AAPL.MX"].iloc[0] == pytest.approx(200.0)
    assert out.px["MSFT"].iloc[0] == pytest.approx(400.0)
    assert out.px.attrs["quote_currencies"]["AAPL.MX"] == "MXN"
    assert any("AAPL.MX" in a and "converted to USD" in a for a in out.assumptions)
    # the patch is scoped to the call
    assert legacy._ticker_meta(["AAPL.MX"])["AAPL.MX"]["currency"] is None


def test_live_path_in_mxn_leaves_mx_listing_unconverted(monkeypatch):
    idx = pd.bdate_range("2025-01-02", periods=40)
    raw = pd.DataFrame({"AAPL.MX": np.linspace(4000, 4400, 40)}, index=idx)
    monkeypatch.setattr(legacy, "_ticker_meta", lambda tickers: {t: {"currency": "USD"} for t in tickers})
    monkeypatch.setattr(legacy, "_load_prices", lambda tickers, years=5, currency=None, align=True: (
        legacy.to_currency(raw[list(tickers)], currency, [], fx_loader=lambda f, t: None), []))
    out = market._price_frame({"years": 1}, ["AAPL.MX"], "MXN")
    assert out.px["AAPL.MX"].iloc[0] == pytest.approx(4000.0)
    assert any("provider reported quote currency USD" in w for w in out.warnings)


def test_supplied_usd_rows_with_mx_symbol_warn_no_implicit_fx():
    prices = {"currency": "USD", "source": "fixture", "rows": rows(["AAPL.MX", "VTI"])}
    out = market.run("analyze", {"currency": "USD", "weights": {"AAPL.MX": 1.0}, "prices": prices,
                                 "risk_free": "none"}, {})
    assert out["status"] == "ready"
    assert any("AAPL.MX quote in MXN" in w for w in out["warnings"])


# ---------------------------------------------------------------- premium
def test_premium_positive():
    out = market.sic_premium("AAPL.MX", "AAPL", sic_price_mxn=4100.0, sic_price_as_of="2026-09-18",
                             home_price=200.0, home_price_as_of="2026-09-18",
                             usdmxn=20.0, usdmxn_as_of="2026-09-18", source="broker screen")
    assert out["status"] == "ready"
    r = out["result"]
    assert r["implied_sic_price_mxn"] == pytest.approx(4000.0)
    assert r["premium"] == pytest.approx(0.025)
    assert r["premium_bps"] == pytest.approx(250.0)
    assert r["direction"] == "premium" and r["same_day"] is True
    assert r["as_of"] == {"sic_price_mxn": "2026-09-18", "home_price": "2026-09-18", "usdmxn": "2026-09-18"}
    assert not out["warnings"]


def test_premium_negative_via_run_task():
    out = market.run("sic_premium", {"sic_symbol": "AAPL.MX", "home_symbol": "AAPL",
                                     "sic_price_mxn": 3960, "sic_price_as_of": "2026-09-18",
                                     "home_price": 200, "home_price_as_of": "2026-09-18",
                                     "usdmxn": 20, "usdmxn_as_of": "2026-09-18",
                                     "price_source": "broker screen"}, {})
    assert out["status"] == "ready"
    assert out["result"]["premium"] == pytest.approx(-0.01)
    assert out["result"]["premium_bps"] == pytest.approx(-100.0)
    assert out["result"]["direction"] == "discount"
    assert set(out) == {"status", "result", "missing", "warnings", "sources", "assumptions"}


def test_missing_inputs_name_the_fields_and_are_not_zero():
    out = market.run("sic_premium", {"sic_symbol": "AAPL.MX", "sic_price_mxn": 4000,
                                     "price_source": "broker screen"}, {})
    assert out["status"] == "needs_input"
    assert out["missing"] == ["home_price", "usdmxn"]
    assert "premium" not in out["result"]
    assert out["result"]["home_symbol"] == "AAPL"
    assert market.run("sic_premium", {}, {})["missing"] == ["sic_symbol"]
    unsourced = market.run("sic_premium", {"sic_symbol": "AAPL.MX", "sic_price_mxn": 4000,
                                           "home_price": 200, "usdmxn": 20}, {})
    assert unsourced["status"] == "needs_input" and unsourced["missing"][0].startswith("price_source")


def test_date_mismatch_warns_but_computes():
    out = market.sic_premium("BRKB.MX", sic_underlyings={"BRKB.MX": "BRK-B"},
                             sic_price_mxn=9500, sic_price_as_of="2026-09-18",
                             home_price=470, home_price_as_of="2026-09-17",
                             usdmxn=20.1, usdmxn_as_of="2026-09-18", source="fixture")
    assert out["result"]["home_symbol"] == "BRK-B"
    assert out["status"] == "ready" and out["result"]["same_day"] is False
    assert any(w.startswith("DATE MISMATCH") and "home_price 2026-09-17" in w for w in out["warnings"])


def test_fetch_missing_uses_injected_adapters(monkeypatch):
    quotes = {"AAPL.MX": (4040.0, "MXN", "2026-09-18"), "AAPL": (200.0, "USD", "2026-09-18")}
    monkeypatch.setattr(legacy, "_latest_quote", lambda symbol, warnings: quotes[symbol])
    monkeypatch.setattr(legacy, "_fx_series",
                        lambda frm, to: pd.Series([19.9, 20.0], index=pd.to_datetime(["2026-09-17", "2026-09-18"])))
    out = market.run("sic_premium", {"sic_symbol": "AAPL.MX", "fetch_missing": True}, {})
    assert out["status"] == "ready"
    assert out["result"]["premium"] == pytest.approx(0.01)
    assert {s["field"] for s in out["sources"]} == {"sic_price_mxn", "home_price", "usdmxn"}


# ---------------------------------------------------------------- combining
def test_combine_flag_merges_weights_with_assumption():
    prices = {"currency": "USD", "source": "fixture", "rows": rows(["AAPL", "BRK-B", "VTI"])}
    base = {"currency": "USD", "prices": prices, "risk_free": "none",
            "weights": {"AAPL.MX": 0.2, "AAPL": 0.3, "BRKB.MX": 0.5}}
    out = market.run("analyze", {**base, "combine_sic_listings": True,
                                 "sic_underlyings": {"BRKB.MX": "BRK-B"}}, {})
    assert out["status"] == "ready", out
    assert out["result"]["portfolio"]["weights"] == pytest.approx({"AAPL": 0.5, "BRK-B": 0.5})
    assert any("combine_sic_listings=true" in a and "AAPL.MX->AAPL" in a for a in out["assumptions"])


def test_combine_is_off_by_default_and_flags_pairs():
    prices = {"currency": "USD", "source": "fixture", "rows": rows(["AAPL.MX", "AAPL", "VTI"])}
    out = market.run("analyze", {"currency": "USD", "prices": prices, "risk_free": "none",
                                 "weights": {"AAPL.MX": 0.4, "AAPL": 0.6}}, {})
    assert set(out["result"]["portfolio"]["weights"]) == {"AAPL.MX", "AAPL"}
    assert any("AAPL.MX/AAPL" in w for w in out["warnings"])
    assert not any("combine_sic_listings=true" in a for a in out["assumptions"])


def test_combine_applies_to_household_compare_stress_and_construct():
    household = {"currency": "USD", "complete": True, "positions": [
        {"symbol": "MSFT.MX", "value": 100, "currency": "USD"},
        {"symbol": "MSFT", "value": 100, "currency": "USD"},
        {"symbol": "VTI", "value": 200, "currency": "USD"}]}
    stress = market.run("stress", {"household": household, "combine_sic_listings": True,
                                   "scenarios": [{"name": "crash", "shocks": {"MSFT.MX": -0.3, "VTI": -0.2}}]}, {})
    assert stress["result"]["weights"] == pytest.approx({"MSFT": 0.5, "VTI": 0.5})
    assert stress["result"]["scenarios"][0]["portfolio_return"] == pytest.approx(-0.25)

    prices = {"currency": "USD", "source": "fixture", "rows": rows(["MSFT", "VTI"])}
    compare = market.run("compare", {"currency": "USD", "prices": prices, "risk_free": "none",
                                     "combine_sic_listings": True,
                                     "current_weights": {"MSFT.MX": 0.5, "VTI": 0.5},
                                     "proposed_weights": {"MSFT.MX": 0.2, "MSFT": 0.2, "VTI": 0.6}}, {})
    assert compare["status"] == "ready", compare
    assert any("proposed_weights" in a for a in compare["assumptions"])

    construct = market.run("construct", {"currency": "USD", "prices": prices, "combine_sic_listings": True,
                                         "tickers": ["MSFT.MX", "MSFT", "VTI"], "method": "equal"}, {})
    assert construct["status"] == "ready", construct
    assert set(construct["result"]["weights"]) == {"MSFT", "VTI"}
