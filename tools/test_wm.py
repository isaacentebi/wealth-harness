# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "yfinance>=0.2.50",
#   "pandas",
#   "numpy",
#   "scipy",
#   "statsmodels",
#   "pytest",
# ]
# ///
"""Offline tests for wm.py on synthetic geometric-Brownian prices.

Run:  uv run --with pytest tools/test_wm.py
  or: uv run --with pytest -m pytest tools/
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_spec = importlib.util.spec_from_file_location("wm", Path(__file__).with_name("wm.py"))
wm = importlib.util.module_from_spec(_spec)
sys.modules["wm"] = wm
_spec.loader.exec_module(wm)

START = "2017-01-03"
NDAYS = 2300  # ~9 years, so the regime table has several live windows


def _dates(n=NDAYS):
    return pd.bdate_range(START, periods=n)


def gbm_returns(n=NDAYS, mu=0.08, sigma=0.20, seed=0):
    rng = np.random.default_rng(seed)
    dt = 1.0 / wm.TRADING_DAYS
    return rng.normal((mu - 0.5 * sigma**2) * dt, sigma * np.sqrt(dt), n)


def prices_from(rets, index=None):
    idx = _dates(len(rets)) if index is None else index
    return pd.Series(100.0 * np.exp(np.cumsum(rets)), index=idx)


def synthetic_prices():
    """SPY plus: DOUBLE (exactly 2x SPY daily), TWIN (corr ~0.9), INDIE, BND."""
    idx = _dates()
    spy_r = gbm_returns(seed=1)
    noise = gbm_returns(seed=2, mu=0.0, sigma=0.10)
    px = pd.DataFrame({
        "SPY": prices_from(spy_r, idx),
        "DOUBLE": prices_from(2.0 * spy_r, idx),
        "TWIN": prices_from(spy_r + 0.25 * noise, idx),
        "INDIE": prices_from(gbm_returns(seed=3, mu=0.05, sigma=0.25), idx),
        "BND": prices_from(gbm_returns(seed=4, mu=0.02, sigma=0.05), idx),
    })
    px.attrs.update(currency='USD',data_kind='synthetic',source='deterministic test fixture')
    return px


def synthetic_factors(index):
    rng = np.random.default_rng(7)
    n = len(index)
    return pd.DataFrame({
        "Mkt-RF": rng.normal(0.0003, 0.010, n),
        "SMB": rng.normal(0.0000, 0.005, n),
        "HML": rng.normal(0.0000, 0.005, n),
        "RF": np.full(n, 0.02 / wm.TRADING_DAYS),
    }, index=index)


@pytest.fixture
def px():
    return synthetic_prices()


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """No network: inject the price loader, the factor loader and fee lookup."""
    data = synthetic_prices()

    def fake_load_prices(tickers, years=5, use_cache=True, currency=None,
                         fx_loader=None):
        warnings, keep = [], []
        for t in [t.upper() for t in tickers]:
            if t in data.columns:
                keep.append(t)
            else:
                warnings.append(f"dropped {t}: not in synthetic universe")
        out = data[list(dict.fromkeys(keep))]
        cutoff = out.index.max() - pd.Timedelta(days=int(365.25 * years))
        out = out.loc[out.index >= cutoff]
        return wm.to_currency(out, currency, warnings, fx_loader=fx_loader), warnings

    monkeypatch.setattr(wm, "_load_prices", fake_load_prices)
    monkeypatch.setattr(wm, "_load_factors",
                        lambda model=3: synthetic_factors(data.index))
    monkeypatch.setattr(wm, "_ticker_meta", lambda tickers: {
        t: {"expense_ratio": 0.0003 if t in ("SPY", "BND") else None,
            "currency": "USD"} for t in tickers})
    monkeypatch.setattr(wm, "_fx_series",
                        lambda frm, to: pd.Series(20.0, index=data.index))
    return data


# --------------------------------------------------------------------------
# analyze
# --------------------------------------------------------------------------
def test_analyze_shape_and_window(px):
    res = wm.analyze_frame(px, "SPY")
    assert set(res) >= {"window", "window_start", "window_end", "n_days", "bench",
                        "rf_annual", "assets", "correlation", "clusters", "portfolio",
                        "warnings"}
    assert res["window_start"] < res["window_end"]
    assert res["window"] == {"start": res["window_start"], "end": res["window_end"],
                             "n_days": res["n_days"]}
    assert res["n_days"] == len(px) - 1
    for t, a in res["assets"].items():
        assert set(a) >= {"ann_return", "ann_vol", "max_drawdown", "sharpe", "beta",
                          "alpha", "r2", "first_date", "expense_ratio", "currency"}
        assert a["ann_vol"] > 0


def test_beta_of_double_bench_is_two(px):
    res = wm.analyze_frame(px, "SPY")
    assert res["assets"]["DOUBLE"]["beta"] == pytest.approx(2.0, abs=0.02)
    assert res["assets"]["DOUBLE"]["r2"] == pytest.approx(1.0, abs=0.01)
    assert res["assets"]["SPY"]["beta"] == pytest.approx(1.0, abs=1e-6)


def test_stats_are_sane(px):
    res = wm.analyze_frame(px, "SPY")
    a = res["assets"]
    assert a["DOUBLE"]["ann_vol"] > a["SPY"]["ann_vol"] > a["BND"]["ann_vol"]
    for t in a:
        assert -1.0 <= a[t]["max_drawdown"] <= 0.0
    assert res["rf_annual"] == pytest.approx(0.02, abs=1e-6)


def test_correlation_matrix_symmetric_unit_diagonal(px):
    corr = wm.analyze_frame(px, "SPY")["correlation"]
    names = list(corr)
    assert "SPY" not in names  # benchmark excluded from the holdings matrix
    for a in names:
        assert corr[a][a] == pytest.approx(1.0)
        for b in names:
            assert corr[a][b] == pytest.approx(corr[b][a])
            assert -1.0 <= corr[a][b] <= 1.0


def test_effective_bets_is_inverse_herfindahl():
    assert wm.effective_bets(np.array([0.25] * 4)) == pytest.approx(4.0)
    w = np.array([0.5, 0.3, 0.2])
    assert wm.effective_bets(w) == pytest.approx(1.0 / np.sum(w**2))
    res = wm.analyze_frame(synthetic_prices(), "SPY",
                           {"DOUBLE": 0.5, "INDIE": 0.3, "BND": 0.2})
    p = res["portfolio"]
    assert p["effective_bets"] == pytest.approx(1.0 / np.sum(w**2), abs=0.01)
    assert p["n_holdings"] == 3
    assert 0.0 < p["pca_first_pc_share"] <= 1.0


def test_portfolio_weights_normalised(px):
    res = wm.analyze_frame(px, "SPY", {"DOUBLE": 2.0, "BND": 2.0})
    assert sum(res["portfolio"]["weights"].values()) == pytest.approx(1.0)


def test_clusters_detect_high_correlation(px):
    res = wm.analyze_frame(px, "SPY")
    corr = res["correlation"]
    assert corr["DOUBLE"]["TWIN"] > 0.7
    clusters = res["clusters"]
    assert any(set(c["members"]) >= {"DOUBLE", "TWIN"} for c in clusters)
    assert all("INDIE" not in c["members"] for c in clusters)
    c = next(c for c in clusters if "TWIN" in c["members"])
    assert c["avg_corr"] > 0.7 and c["weight"] is None  # no weights given


def test_cluster_threshold_is_pure():
    corr = pd.DataFrame([[1.0, 0.8, 0.1], [0.8, 1.0, 0.2], [0.1, 0.2, 1.0]],
                        index=list("ABC"), columns=list("ABC"))
    assert wm.corr_clusters(corr) == [["A", "B"]]
    assert wm.corr_clusters(corr, threshold=0.9) == []


def test_missing_ticker_warns_not_crashes(capsys):
    wm.main(["analyze", "DOUBLE", "NOPE", "--bench", "SPY", "--years", "3"])
    res = json.loads(capsys.readouterr().out)
    assert "DOUBLE" in res["assets"] and "NOPE" not in res["assets"]
    assert any("NOPE" in w for w in res["warnings"])


def test_rf_unavailable_stays_unknown(monkeypatch, px):
    monkeypatch.setattr(wm, "_load_factors",lambda model=3: (_ for _ in ()).throw(OSError("offline")))
    warnings=[]
    res=wm.analyze_frame(px,"SPY",warnings=warnings)
    assert res['rf_annual'] is None
    assert res['assets']['DOUBLE']['sharpe'] is None
    assert res['assets']['DOUBLE']['alpha'] is None
    assert res['assets']['DOUBLE']['beta'] > 1
    assert any('risk-free series unavailable' in w for w in res['warnings'])


def test_expense_ratio_and_currency_surface(capsys):
    wm.main(["analyze", "SPY", "BND", "INDIE", "--bench", "SPY"])
    res = json.loads(capsys.readouterr().out)
    assert res["assets"]["BND"]["expense_ratio"] == pytest.approx(0.0003)
    assert res["assets"]["INDIE"]["expense_ratio"] is None
    assert res["assets"]["BND"]["currency"] == "USD"


def test_cluster_weight_reported_when_weights_given(px):
    res = wm.analyze_frame(px, "SPY", {"DOUBLE": 0.5, "TWIN": 0.2, "BND": 0.3})
    c = next(c for c in res["clusters"] if "TWIN" in c["members"])
    assert c["weight"] == pytest.approx(0.7, abs=1e-6)


def test_first_date_from_price_attrs(px):
    px.attrs["first_dates"] = {t: "2001-01-02" for t in px.columns}
    res = wm.analyze_frame(px, "SPY")
    assert res["assets"]["BND"]["first_date"] == "2001-01-02"


# --------------------------------------------------------------------------
# factors
# --------------------------------------------------------------------------
def test_factor_regression_shape(px):
    res = wm.factor_regression(px, 3, [])
    assert res["factors"] == ["Mkt-RF", "SMB", "HML"]
    assert set(res["window"]) == {"start", "end", "n_days"}
    for t, a in res["assets"].items():
        assert set(a) == {"alpha_annual", "alpha_t", "r2", "loadings", "tilts"}
        assert set(a["loadings"]) == set(res["factors"])
        assert set(a["tilts"]) <= set(res["factors"])


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------
@pytest.mark.parametrize("method", ["equal", "invvol", "minvar", "riskparity"])
def test_build_methods_long_only_sum_to_one(method, px):
    rets = wm.daily_returns(px)
    w = wm.solve_weights(rets, method, max_w=0.30)
    assert list(w.index) == list(rets.columns)
    assert w.sum() == pytest.approx(1.0, abs=1e-6)
    assert (w >= -1e-9).all()
    assert w.max() <= 0.30 + 1e-6


@pytest.mark.parametrize("method", ["equal", "invvol", "minvar", "riskparity"])
def test_build_respects_tight_cap(method, px):
    rets = wm.daily_returns(px)
    w = wm.solve_weights(rets, method, max_w=0.21)
    assert w.max() <= 0.21 + 1e-6
    assert w.sum() == pytest.approx(1.0, abs=1e-6)


def test_invvol_puts_more_in_the_quiet_asset(px):
    w = wm.solve_weights(wm.daily_returns(px), "invvol", max_w=1.0)
    assert w["BND"] > w["DOUBLE"]


def test_sleeve_mode_combines_at_given_weights(px):
    rets = wm.daily_returns(px[["DOUBLE", "TWIN", "INDIE", "BND"]])
    w = wm.build_weights(rets, "equal", 1.0,
                         sleeves={"theme": ["DOUBLE", "TWIN"],
                                  "ballast": ["INDIE", "BND"]},
                         sleeve_weights={"theme": 0.3, "ballast": 0.7})
    assert w.sum() == pytest.approx(1.0)
    assert w["DOUBLE"] + w["TWIN"] == pytest.approx(0.30, abs=1e-6)
    assert w["INDIE"] + w["BND"] == pytest.approx(0.70, abs=1e-6)
    assert w["DOUBLE"] == pytest.approx(0.15, abs=1e-6)


def test_build_command_writes_portfolio_json(tmp_path, capsys):
    out = tmp_path / "p.json"
    wm.main(["build", "DOUBLE", "TWIN", "INDIE", "BND", "--method", "riskparity",
             "--max-weight", "0.3", "--years", "3", "--save", str(out)])
    capsys.readouterr()
    saved = json.loads(out.read_text())
    assert set(saved) >= {"name", "created", "bench", "years", "weights", "sleeves",
                          "stats", "warnings"}
    assert sum(saved["weights"].values()) == pytest.approx(1.0, abs=1e-6)
    assert max(saved["weights"].values()) <= 0.3 + 1e-6
    assert saved["stats"]["portfolio"]["n_holdings"] == 4


# --------------------------------------------------------------------------
# regimes
# --------------------------------------------------------------------------
def test_regime_slicing_picks_the_right_days(px):
    rets = wm.daily_returns(px)
    seg = wm.slice_regime(rets, "2022-01-01", "2022-10-12")
    assert seg.index.min() >= pd.Timestamp("2022-01-01")
    assert seg.index.max() <= pd.Timestamp("2022-10-12")
    assert 180 < len(seg) < 210  # business days in that window
    open_ended = wm.slice_regime(rets, "2025-01-01", None)
    assert open_ended.index.max() == rets.index.max()


def test_regime_table_stats_match_a_direct_slice(px):
    rets = wm.daily_returns(px)
    table = wm.regime_table(rets, "SPY", None)
    labels = [r["regime"] for r in table]
    assert "2022 hiking cycle" in labels and "2020 COVID crash" in labels
    row = next(r for r in table if r["regime"] == "2022 hiking cycle")
    seg = wm.slice_regime(rets, "2022-01-01", "2022-10-12")
    assert row["assets"]["INDIE"]["total_return"] == pytest.approx(
        wm.total_return(seg["INDIE"]), abs=1e-4)
    assert row["assets"]["DOUBLE"]["beta"] == pytest.approx(2.0, abs=0.05)


def test_regime_portfolio_leg_present(px):
    table = wm.regime_table(wm.daily_returns(px), "SPY",
                            pd.Series({"DOUBLE": 0.5, "BND": 0.5}))
    assert all(r["portfolio"] is not None for r in table)
    assert all(-1.0 <= r["portfolio"]["max_drawdown"] <= 0.0 for r in table)


def test_correlation_break_is_found_at_the_flip_date():
    idx = _dates(1600)
    flip = 800
    base = gbm_returns(n=1600, seed=11)
    noise = gbm_returns(n=1600, seed=12, mu=0.0, sigma=0.05)
    sign = np.where(np.arange(1600) < flip, 1.0, -1.0)
    other = sign * base + 0.1 * noise
    data = pd.DataFrame({"A": prices_from(base, idx),
                         "B": prices_from(other, idx)})
    rets = wm.daily_returns(data)
    rc = wm.rolling_pair_corr(rets, window=126)
    breaks = wm.correlation_breaks(rc, lookback=63)
    assert len(breaks) == 1
    b = breaks[0]
    assert b["pair"] == ["A", "B"]
    assert b["max"] > 0.9 and b["min"] < -0.9
    assert b["broke_change"] < -1.0
    flip_date = idx[flip]
    broke = pd.Timestamp(b["broke_on"])
    assert 0 <= (broke - flip_date).days <= 200  # detected just after the flip


def test_holding_periods_and_regimes_frame(px):
    res = wm.regimes_frame(px, "SPY", {"DOUBLE": 0.4, "INDIE": 0.3, "BND": 0.3})
    assert set(res) >= {"window", "regimes", "correlation_breaks",
                        "avg_pairwise_corr", "holding_periods", "corr_window",
                        "warnings"}
    assert res["window"]["n_days"] == res["n_days"]
    periods = {h["period"] for h in res["holding_periods"]}
    assert {"1y", "2y", "3y", "5y"} <= periods
    for h in res["holding_periods"]:
        assert h["portfolio"]["total_return"] is not None
        assert h["bench"]["total_return"] is not None
    assert -1.0 <= res["avg_pairwise_corr"]["current"] <= 1.0
    assert all("|" not in p for b in res["correlation_breaks"] for p in b["pair"])


def test_regimes_command_json(capsys):
    wm.main(["regimes", "DOUBLE", "INDIE", "BND", "--weights", "0.4", "0.3", "0.3",
             "--years", "8"])
    res = json.loads(capsys.readouterr().out)
    assert res["bench"] == "SPY"
    assert len(res["regimes"]) >= 3
    assert res["corr_window"] == 126


def test_regimes_pretty_runs(capsys):
    wm.main(["regimes", "DOUBLE", "INDIE", "--years", "8", "--pretty"])
    out = capsys.readouterr().out
    assert "2022 hiking cycle" in out and "correlation changes" in out


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------
def _portfolio(px):
    stats = wm.analyze_frame(px, "SPY", {"DOUBLE": 0.4, "INDIE": 0.3, "BND": 0.3})
    return {"name": "Synthetic", "created": "2026-01-01", "bench": "SPY", "years": 5,
            "weights": {"DOUBLE": 0.4, "INDIE": 0.3, "BND": 0.3}, "stats": stats}


def test_report_writes_self_contained_html(tmp_path, px):
    out = wm.write_report(_portfolio(px), px, tmp_path / "r.html")
    html = out.read_text()
    assert out.exists() and "<!doctype html" in html and '<div class="wm"' in html
    # the standalone head is the only place a font may be fetched from
    assert html.count('rel="stylesheet"') == 1 and html.count("fonts.googleapis.com/css2") == 1
    assert html.index("fonts.googleapis.com") < html.index('<div class="wm"')
    assert not re.search(r'(src|href)=(?!"https://fonts\.)', html)
    assert html.count("<svg") >= 5
    assert "Synthetic" in html
    for title in ("What you own", "Growth of 100", "Falls from peak",
                  "Moves with the market", "Move together", "Correlation over time",
                  "Details"):
        assert title in html
    # v3: cards on a twelve-column grid, no section numbers, never a card in a card
    assert 'class="wm-no"' not in html and not re.search(r">0[1-9]<", html)
    assert html.count('data-align="card"') == 7      # six sections plus details
    assert '<div class="wm-grid"' in html and html.count('class="wm-grid"') == 1
    assert "wm-tile" not in html
    assert not re.search(r'wm-card[^"]*"[^>]*>\s*<[^>]*wm-card', html)
    # the raw regime name never leaks into a band strip: only its caps form does
    assert "2022 hiking cycle" not in html
    assert "rotate(" not in html  # never rotated: it is dropped instead
    assert "Plotly" not in html and "plotly" not in html
    # the correlation grid blanks its diagonal: no asset is scored against itself
    assert ">1.00</text>" not in html
    assert html.count('fill="none" stroke="var(--wm-edge)"') == 3  # one per holding
    assert "Historical. Not a forecast." not in html and "tap or hover" not in html


def test_report_fragment_has_no_page_wrapper(tmp_path, px):
    out = wm.write_report(_portfolio(px), px, tmp_path / "f.html", fragment=True)
    html = out.read_text()
    assert html.startswith('<div class="wm">') and html.endswith("</div>")
    assert "<html" not in html and "<body" not in html and "<!doctype" not in html.lower()
    assert "<style>" in html and "<script>" in html


def test_report_command_end_to_end(tmp_path, capsys):
    pj = tmp_path / "p.json"
    wm.main(["build", "DOUBLE", "INDIE", "BND", "--method", "invvol",
             "--max-weight", "0.5", "--years", "5", "--save", str(pj)])
    capsys.readouterr()
    html = tmp_path / "p.html"
    wm.main(["report", str(pj), "--out", str(html)])
    assert json.loads(capsys.readouterr().out)["out"] == str(html)
    assert html.exists() and "<svg" in html.read_text()


# --------------------------------------------------------------------------
# cards
# --------------------------------------------------------------------------
CARD_CMDS = {
    "analyze": ["analyze", "DOUBLE", "INDIE", "BND", "--weights", "0.4", "0.3", "0.3"],
    "build": ["build", "DOUBLE", "INDIE", "BND", "--method", "invvol",
              "--max-weight", "0.5"],
    "regimes": ["regimes", "DOUBLE", "INDIE", "BND", "--weights", "0.4", "0.3", "0.3",
                "--years", "8"],
    "factors": ["factors", "DOUBLE", "INDIE", "BND"],
}


@pytest.mark.parametrize("cmd", sorted(CARD_CMDS))
def test_card_fragment_is_a_small_self_contained_block(cmd, tmp_path, capsys):
    card = tmp_path / f"{cmd}.html"
    wm.main(CARD_CMDS[cmd] + ["--card", str(card), "--fragment"])
    capsys.readouterr()
    html = card.read_text(encoding="utf-8")
    assert "<html" not in html and "<body" not in html
    assert "<!doctype" not in html.lower() and "<head" not in html
    assert 'class="wm"' in html and "<svg" in html
    assert 'id="' not in html  # nothing that could collide with a host page
    assert not re.search(r'(src|href)=', html)
    assert len(html.encode("utf-8")) <= 60_000, f"{cmd} card is {len(html)} bytes"


@pytest.mark.parametrize("cmd", sorted(CARD_CMDS))
def test_card_without_fragment_is_a_standalone_page(cmd, tmp_path, capsys):
    card = tmp_path / f"{cmd}.html"
    wm.main(CARD_CMDS[cmd] + ["--card", str(card)])
    capsys.readouterr()
    html = card.read_text(encoding="utf-8")
    assert html.startswith("<!doctype html") and "</body></html>" in html
    assert 'class="wm"' in html and "<svg" in html


def test_analyze_card_keeps_the_plain_english_wording(tmp_path,capsys):
    card=tmp_path/'a.html'
    wm.main(CARD_CMDS['analyze']+['--card',str(card),'--fragment']);capsys.readouterr()
    html=card.read_text()
    for phrase in ('How holdings move together','Worst fall','Annual cost','Sources and assumptions'):
        assert phrase in html
    # descriptive only: no forecast, no reading a correlation as a percentage
    for phrase in ('0.8 means 80%','Count them as one bet','tap or hover',
                   'square root of 252','expected','will '):
        assert phrase not in html
    assert html.index('<details>') < html.index('Worst fall')
    # a fragment never reaches the network
    assert 'fonts.googleapis' not in html and not re.search(r'(src|href)=', html)


def test_build_card_adds_growth_and_regimes_card_adds_diversification(tmp_path, capsys):
    b, r = tmp_path / "b.html", tmp_path / "r.html"
    wm.main(CARD_CMDS["build"] + ["--card", str(b), "--fragment"])
    wm.main(CARD_CMDS["regimes"] + ["--card", str(r), "--fragment"])
    capsys.readouterr()
    assert "Growth of 100" in b.read_text()
    rt = r.read_text()
    assert "Return by period" in rt and "Correlation over time" in rt


LUCIDE = {"trending-down", "git-fork", "activity", "receipt", "layers",
          "line-chart", "arrow-down-to-line", "waves", "grid-2x2", "split", "list"}


def test_report_draws_exactly_the_named_lucide_icons(tmp_path, px):
    html = wm.write_report(_portfolio(px), px, tmp_path / "r.html").read_text()
    assert set(re.findall(r'data-icon="([a-z0-9-]+)"', html)) == LUCIDE
    # 16px, 1.5 stroke, round joins, currentColor - and never an emoji
    assert 'width="16" height="16"' in html and 'stroke-width="1.5"' in html
    assert 'stroke-linecap="round" stroke-linejoin="round"' in html
    assert 'stroke="currentColor"' in html and 'fill="none"' in html
    assert not re.search(r'[\U0001F300-\U0001FAFF\u2600-\u27BF]', html)


def test_every_icon_sits_beside_a_label():
    for name in LUCIDE:
        assert wm.ICONS[name].startswith("<") and "d=" in wm.ICONS[name] or \
            "<circle" in wm.ICONS[name]
    assert set(wm.ICONS) == LUCIDE          # no icon we never use


# --------------------------------------------------------------------------
# the lede
# --------------------------------------------------------------------------
def _grammatical(text):
    """One or two whole sentences: no stray connective, no double space, no None."""
    assert text and text[0].isupper() and text.endswith(".")
    assert "  " not in text and "None" not in text and "—" not in text
    assert not re.search(r"\b(and|against|like|at)\.", text)
    assert ".." not in text
    return text


def test_lede_describes_observed_decline_not_beta_forecast():
    text=_grammatical(wm.lede({'n_holdings':6,'effective_bets_corr':2.55,'beta':.714,'max_drawdown':-.2296},{'max_drawdown':-.2892},'the S&P 500'))
    assert text=="Six holdings that behave like three. It moves at 0.71\u00d7 the market and its worst fall was \u221223% against the S&P 500's \u221229%."
    # every clause is past tense or present state; nothing promises a future move
    for word in ('will', 'expect', 'should', 'forecast', 'predict'):
        assert word not in text.lower()


def test_lede_without_a_bench_drops_the_comparison_and_stays_grammatical():
    text=_grammatical(wm.lede({'n_holdings':6,'effective_bets_corr':2.55,'max_drawdown':-.2296},{},'the S&P 500'))
    assert text=='Six holdings that behave like three. Its worst fall was \u221223%.'


@pytest.mark.parametrize("p", [
    {"n_holdings": 1},
    {"n_holdings": 4, "effective_bets_corr": 2.0},
    {"n_holdings": 4, "beta": 1.31},
    {"n_holdings": 20, "effective_bets_corr": 11.5, "max_drawdown": -0.5},
    {"beta": 0.9, "max_drawdown": -0.1},
])
def test_lede_degrades_one_clause_at_a_time(p):
    _grammatical(wm.lede(p, {}, "the S&P\u00a0500"))


def test_lede_is_empty_only_when_there_is_nothing_to_say():
    assert wm.lede({}, {}, "the S&P 500") == ""
    assert wm.lede({"n_holdings": 1}, {}, "x") == "One holding."


def test_report_has_lede_default_card_is_focused(tmp_path,px,capsys):
    html=wm.write_report(_portfolio(px),px,tmp_path/'r.html').read_text()
    _grammatical(re.search(r'<p class="wm-lede">(.*?)</p>',html).group(1))
    card=tmp_path/'b.html';wm.main(CARD_CMDS['build']+['--card',str(card),'--fragment']);capsys.readouterr()
    assert 'Growth of 100' in card.read_text()
    assert 'wm-lede">' not in card.read_text()


# --------------------------------------------------------------------------
# size budgets
# --------------------------------------------------------------------------
def test_report_fits_its_size_budget(tmp_path, px):
    out = wm.write_report(_portfolio(px), px, tmp_path / "r.html")
    assert len(out.read_bytes()) <= 120_000
    assert 'Lucide Icons and Contributors' in out.read_text()
    assert 'Cole Bemis' in out.read_text()


def test_hover_readout_data_is_attached_to_every_line_chart(tmp_path, px):
    html = wm.write_report(_portfolio(px), px, tmp_path / "r.html",
                           fragment=True).read_text()
    assert html.count("data-wm-rows=") >= 3  # growth, drawdown, beta, diversification
    assert "data-wm-r=" in html              # per-mark readouts on donut/heatmap
    assert html.count("wm-ro") >= 4


def test_band_label_shrinks_to_fit_then_drops_rather_than_overlapping():
    wide = wm._band_strip([("2022 hiking cycle", 0, 100)], lambda i: i * 4.0, 100, 20, 80)
    assert "2022 hikes" in wide                     # 400 units: the full short form
    mid = wm._band_strip([("2022 hiking cycle", 0, 100)], lambda i: i * 1.1, 100, 20, 80)
    assert "Hikes" in mid and "2022 hikes" not in mid  # 110 units: the shorter form
    assert "HIKES" not in wide + mid                   # sentence case, never caps
    tight = wm._band_strip([("2022 hiking cycle", 0, 100)], lambda i: i * 0.4, 100, 20, 80)
    assert "<text" not in tight and "<rect" in tight   # 40 units: tint, no caption
    assert "rotate(" not in wide + mid + tight


def test_correlation_grid_is_never_wrapped_in_a_sideways_scroller(tmp_path, px):
    html = wm.write_report(_portfolio(px), px, tmp_path / "r.html").read_text()
    assert "wm-plot" not in html and "overflow-x" not in html.split("wm-scroll")[0]
    # the only horizontal scroller on the page is the details spec table
    assert html.count("overflow-x:auto") == 1
    assert 'class="wm-scroll"' in html
    assert "min-width:600px" not in html


def test_grid_labels_drop_the_exchange_suffix_and_cap_at_seven():
    assert wm._short("NAFTRAC.MX") == "NAFTRAC"
    assert wm._short("BTC-USD") == "BTC"
    assert wm._short("SMH") == "SMH"
    assert wm._short("ABCDEFGHIJ") == "ABCDEF\u2026"
    svg = wm.svg_heatmap(["NAFTRAC.MX", "SMH"], [[1.0, 0.4], [0.4, 1.0]])
    assert ">NAFTRAC</text>" in svg and "NAFTRAC.MX" in svg  # full identity in hover


@pytest.mark.parametrize("n", [2, 4, 6, 8])
def test_grid_numerals_clear_11px_on_a_375px_screen(n):
    """335px of column over a 640 viewBox: a label must not outgrow its cell."""
    labels = [f"TIC{i}" for i in range(n)]
    m = [[1.0 if i == j else 0.42 for j in range(n)] for i in range(n)]
    svg = wm.svg_heatmap(labels, m)
    xs = sorted({float(x) for x in re.findall(r'<rect x="([\d.]+)"', svg)})
    cell = xs[1] - xs[0] if len(xs) > 1 else 640.0
    assert cell >= 4 * wm.LB_ADV + 2, f"{n} holdings: cell {cell:.1f} too tight"
    assert wm.LB_MAX * 335 / 640 >= 11.0          # the label lands at >= 11px there


def test_more_than_eight_holdings_becomes_a_ranked_pair_list():
    labels = [f"T{i}" for i in range(10)]
    m = [[1.0 if i == j else round(0.9 - 0.01 * (i * 10 + j), 3)
          for j in range(10)] for i in range(10)]
    body, total = wm.html_pairs(labels, m)
    assert total == 45 and body.count('class="wm-pair"') == 24
    vals = [float(v) for v in re.findall(r"<b>([\u2212\d.]+)</b>",
                                         body.replace("\u2212", "-"))]
    assert vals == sorted(vals, reverse=True)      # strongest pair first
    assert "var(--wm-h" in body                    # tinted on the same ramp
    block = wm._block_heatmap.__wrapped__ if hasattr(wm._block_heatmap, "__wrapped__") \
        else wm._block_heatmap
    assert "<svg" not in body and "wm-plot" not in body


def test_bar_charts_carry_short_labels_and_no_scroller():
    groups = [{"label": wm.REGIME_SHORT[r], "bars": [{"label": "Yours", "value": 0.1,
                                                      "cls": "me"}]}
              for r in wm.REGIME_SHORT]
    svg = wm.svg_bars(groups)
    assert "wm-plot" not in svg and "\u2026" not in svg   # nothing truncated
    for short in wm.REGIME_SHORT.values():
        assert short in svg


def test_bar_group_head_puts_the_ticker_on_the_left_edge():
    groups = [{"head": "NVDA", "label": "The market",
               "bars": [{"label": "The market", "value": 1.9}]},
              {"head": "NVDA", "label": "Cheap over pricey",
               "bars": [{"label": "Cheap over pricey", "value": -0.9}]},
              {"head": "VOO", "label": "The market",
               "bars": [{"label": "The market", "value": 1.0}]}]
    svg = wm.svg_bars(groups)
    assert svg.count('class="wm-hd"') == 2          # one per ticker, not per bar
    assert '<text x="0"' in svg                     # flush to the column edge


# --------------------------------------------------------------------------
# typography and alignment (design system v2.1)
# --------------------------------------------------------------------------
def test_nothing_on_the_page_is_uppercased_or_tracked(tmp_path, px):
    html = wm.write_report(_portfolio(px), px, tmp_path / "r.html").read_text()
    assert "text-transform" not in html
    # tracking is negative (optical tightening) or exactly zero - never positive
    for v in re.findall(r"letter-spacing:(-?[\d.]+)em", html):
        assert float(v) <= 0, f"positive tracking {v}em"
    assert "letter-spacing:0" in html          # the reset is explicit on .wm and .wm *
    # no shouted words survive anywhere in the rendered copy
    for word in ("WORST FALL", "DETAILS", "HOLDING", "THEME", "CORE", "CASH"):
        assert word not in html


def test_fonts_are_geist_with_a_local_fallback_stack(tmp_path,px):
    html=wm.write_report(_portfolio(px),px,tmp_path/'r.html').read_text()
    assert '"Geist"' in html and '"Geist Mono"' in html
    # offline still reads correctly: every face has a local fallback behind it
    assert 'ui-monospace' in html and '-apple-system' in html
    assert 'display=swap' in html
    # only the <link> in the standalone head may reach out
    assert len(re.findall(r'(?:src|href)=', html)) == len(re.findall(r'(?:src|href)="https://fonts\.', html))


def test_the_grid_is_twelve_columns_of_cards(tmp_path, px):
    """The v3 ground: one twelve-column grid, 16px gutters, cards on its tracks."""
    html = wm.write_report(_portfolio(px), px, tmp_path / "r.html").read_text()
    assert "grid-template-columns:repeat(12,1fr)" in wm.WM_CSS
    assert "gap:16px" in wm.WM_CSS and "max-width:960px" in wm.WM_CSS
    for n in (12, 7, 6, 5, 3):
        assert f".wm .wm-s{n}{{grid-column:span {n}" in wm.WM_CSS, n
    # every card declares the track it sits on, and the spec's spans are the ones used
    spans = re.findall(r'data-span="(\d+)"', html)
    assert spans == ["3", "3", "3", "3", "12", "7", "5", "6", "6", "12", "12"]
    # the correlation grid takes the full twelve columns but caps its own width,
    # so its cells stay square instead of growing into buttons
    assert 'class="wm-card wm-s12 wm-hm"' in html and '<div class="wm-hmw">' in html
    assert ".wm .wm-hmw{flex:0 1 560px" in wm.WM_CSS
    assert ".wm .wm-hm{--wm-fa:14px" in wm.WM_CSS
    # below 900px the twelve columns collapse; key figures stay two across
    assert "@media (max-width:899px){.wm .wm-grid>*{grid-column:span 12}" in wm.WM_CSS
    assert ".wm .wm-kf{grid-column:span 6}" in wm.WM_CSS
    # and a chat fragment keys off its own width, not the viewport's
    assert wm.WM_CSS.count("@container (max-width:") == wm.WM_CSS.count("@media (max-width:")


def test_a_card_is_a_flat_panel_with_a_twelve_radius(tmp_path, px):
    """Panel, hairline, 12px radius, 20px padding - and never a shadow."""
    html = wm.write_report(_portfolio(px), px, tmp_path / "r.html").read_text()
    rule = next(l for l in wm.WM_CSS.splitlines() if l.startswith(".wm .wm-card{"))
    assert "border-radius:12px" in rule and "padding:20px" in rule
    assert "border:1px solid var(--wm-edge)" in rule
    assert "background:var(--wm-panel)" in rule
    assert "box-shadow" not in wm.WM_CSS and "box-shadow" not in html
    assert "gradient" not in wm.WM_CSS and "gradient" not in html
    assert "--wm-panel:#ffffff" in wm.WM_CSS and "--wm-panel:#1a1a1a" in wm.WM_CSS


def test_four_key_figure_cards_with_a_28px_chip(tmp_path, px):
    """The key-figure row: four cards of three columns, each led by a 28px chip."""
    html = wm.write_report(_portfolio(px), px, tmp_path / "r.html").read_text()
    assert html.count('data-align="key-card"') == 4
    assert html.count('class="wm-card wm-kf wm-s3"') == 4
    for slot in ("key-chip", "key-label", "key-value", "key-cmp"):
        assert html.count(f'data-align="{slot}"') == 4, slot
    chip = next(l for l in wm.WM_CSS.splitlines() if l.startswith(".wm .wm-chip{"))
    assert "width:28px" in chip and "height:28px" in chip and "flex:0 0 28px" in chip
    assert "border-radius:8px" in chip and "background:var(--wm-chip)" in chip
    # the label always reserves its two lines, so the values share one baseline
    assert "min-height:32px" in wm.WM_CSS
    # the comparison is pushed to the bottom of the card, so the row bottom-aligns
    assert "margin:auto 0 0;padding-top:8px" in wm.WM_CSS


def test_six_section_cards_and_a_details_card(tmp_path, px):
    """The spec's six sections, each its own card, plus the spec table."""
    html = wm.write_report(_portfolio(px), px, tmp_path / "r.html").read_text()
    titles = re.findall(r'data-align="head-title">([^<]+)<', html)
    assert titles == ["What you own", "Growth of 100", "Falls from peak",
                      "Moves with the market", "Correlation over time",
                      "Move together", "Details"]
    assert html.count('data-align="head-icon"') == 7
    assert html.count('data-align="head-ro"') == 7
    # the details table is open, not a disclosure, and scrolls inside its own card
    assert '<details class="wm-evidence">' in html and "Sources and assumptions" in html
    assert '<div class="wm-scroll">' in html


def test_the_card_header_puts_icon_title_and_readout_on_one_baseline():
    """A 24px icon box whose own baseline is the title's: centres agree to the pixel."""
    css = wm.WM_CSS
    head = next(l for l in css.splitlines() if l.startswith(".wm .wm-ch{"))
    assert "grid-template-columns:24px 1fr auto" in head
    assert "align-items:baseline" in head and "min-height:24px" in head
    lead = next(l for l in css.splitlines() if l.startswith(".wm .wm-hi{"))
    assert "width:24px;height:24px" in lead and "align-items:center" in lead
    strut = next(l for l in css.splitlines() if l.startswith(".wm .wm-hi::before{"))
    # a real CSS zero-width-space escape, not an octal one Python already ate
    assert r'content:"\200b"' in strut and "font:600 15px/24px" in strut
    title = next(l for l in css.splitlines() if l.startswith(".wm .wm-ch h2{"))
    assert "font:600 15px/24px" in title      # the same metrics as the strut


def test_every_plot_opens_on_the_column_edge():
    """No chart may start inboard of the column: x origin is exactly 0."""
    frame = wm._frame(0, wm.VB_W, 20, 200, [(1.0, 60.0)], lambda v: f"{v:.1f}",
                      [(0.0, "2024"), (300.0, "2025")])
    assert '<line x1="0" x2="640"' in frame    # the gridline spans the whole column
    # the y label rides the gridline, right-aligned inside the plot, never left of it
    # the values themselves are drawn last, so no series is painted across them
    labels = wm._yticks(wm.VB_W, [(1.0, 60.0)], lambda v: f"{v:.1f}", 11)
    assert 'x="640" y="63.7" text-anchor="end" class="wm-ax"' in labels
    assert ">1.0</text>" in labels
    # nothing is drawn across them any more, so the knockout is gone
    assert "paint-order" not in labels and "stroke" not in labels
    assert "paint-order" not in frame          # y values are not part of the frame
    assert 'x="-' not in frame
    # x ticks hang below, left-aligned to their own tick; the baseline follows the
    # size the ticks are drawn at, which is set by the card's span
    assert 'x="0.0" y="217.0" class="wm-ax">2024<' in frame
    assert "text-anchor" not in frame.split("217.0")[1]
    big = wm._frame(0, wm.VB_W, 20, 200, [], lambda v: v, [(0.0, "2024")], 24)
    assert f'y="{200 + wm._base(24)}.0"' in big and wm._base(24) > wm._base(11)


def test_the_y_values_sit_in_a_gutter_outside_the_plot():
    """The plot stops short of the content edge; the values live past it."""
    dates = list(pd.bdate_range("2021-01-01", periods=400))
    svg = wm.svg_line(dates, [{"label": "beta", "values": [1.0 + 0.001 * i
                                                           for i in range(400)],
                               "cls": "me"}], yfmt=lambda v: f"{v:.1f}×")
    gut = wm._ygut([1.0, 1.4], lambda v: f"{v:.1f}×")
    assert gut > 0
    right = wm.VB_W - gut
    # every gridline stops at the plot edge, and every y value is drawn past it
    grid = re.findall(r'<line x1="0" x2="([\d.]+)" y1="[\d.]+" y2="[\d.]+" '
                      r'stroke="var\(--wm-grid\)"', svg)
    assert grid and all(abs(float(x) - right) < 0.01 for x in grid), grid
    assert f'<text x="{wm.VB_W}"' in svg
    assert 'paint-order="stroke" >' not in svg
    # the gutter is sized for the longest value at the phone label size
    assert gut == len("1.0×") * wm.LB_ADV + 8
    # an area chart does the same
    area = wm.svg_area(dates, [-0.01 * (i % 30) for i in range(400)])
    assert f'<text x="{wm.VB_W}"' in area and 'x1="0"' in area


def test_a_date_tick_that_would_overrun_the_right_edge_is_dropped():
    frame = wm._frame(0, wm.VB_W, 20, 200, [], lambda v: v,
                      [(10.0, "2024"), (wm.VB_W - 4.0, "2025")])
    assert "2024" in frame and "2025" not in frame


@pytest.mark.parametrize("fn,args", [
    ("svg_line", None), ("svg_area", None), ("svg_heatmap", None),
])
def test_charts_share_the_one_left_edge(fn, args, px):
    dates = list(px.index)
    if fn == "svg_line":
        svg = wm.svg_line(dates, [{"label": "Yours",
                                   "values": list(range(len(dates))), "cls": "me"}])
    elif fn == "svg_area":
        svg = wm.svg_area(dates, [-0.01 * (i % 30) for i in range(len(dates))])
    else:
        svg = wm.svg_heatmap(["AAA", "BBB"], [[1.0, 0.4], [0.4, 1.0]])
    assert 'x1="0"' in svg or 'x="0"' in svg
    assert 'x="-' not in svg and 'x1="-' not in svg   # nothing hangs off the left


def test_a_chart_label_size_is_one_token_per_span_not_a_repeated_number():
    """Every card scales its 640-unit viewBox differently, so the label size is a
    token the span sets and the breakpoints restate - never a literal in a rule."""
    css = wm.WM_CSS
    lines = {l.split("{")[0]: l for l in css.splitlines() if "{" in l}
    assert "font-size:var(--wm-fa)" in lines[".wm .wm-ax,.wm .wm-hl,.wm .wm-hd"]
    assert "font-size:var(--wm-fb)" in lines[".wm .wm-bd,.wm .wm-bl"]
    for n in (7, 6, 5):
        assert f"--wm-fa:{wm.AX_VB[n]}px" in css and f"--wm-fb:{wm.BD_VB[n]}px" in css
    # the token is the inverse of the card's scale: a narrower card needs it larger
    assert wm.AX_VB[5] > wm.AX_VB[6] > wm.AX_VB[7] > wm.AX_VB[12]


def test_a_chart_label_clears_11px_in_every_card_at_every_width():
    """rendered px = viewBox px x (card content width / 640). Never under eleven."""
    gap, pad, bord = 16, 40, 2
    def content(vw, span, margin=24):
        col = (min(960, vw) - 2 * margin - 11 * gap) / 12
        return span * col + (span - 1) * gap - pad - bord
    for vw in (1100, 1008, 960, 900):               # the twelve-column range
        for span in (12, 7, 6, 5):
            px_ = wm.AX_VB[span] * content(vw, span) / wm.VB_W
            assert px_ >= 11.0, (vw, span, round(px_, 2))
    for vw, ax in ((899, 12), (760, 12), (759, 14), (600, 14),
                   (599, 18), (480, 18), (479, 24), (375, 24)):
        margin = 24 if vw >= 600 else 16
        w = min(960, vw) - 2 * margin - pad - bord   # below 900 every card is full width
        assert ax * w / wm.VB_W >= 11.0, (vw, ax, round(ax * w / wm.VB_W, 2))


def test_every_icon_is_a_16px_box_with_a_stroke_that_does_not_shrink(tmp_path, px):
    """16x16 on a 24-unit viewBox, and 1.5 stays 1.5 through the downscale."""
    html = wm.write_report(_portfolio(px), px, tmp_path / "r.html").read_text()
    svgs = re.findall(r'<svg class="wm-ic"[^>]*>', html)
    assert len(svgs) == len(LUCIDE)          # one per key figure, head and details
    for svg in svgs:
        assert 'width="16" height="16"' in svg, svg
        assert 'viewBox="0 0 24 24"' in svg, svg
        assert 'stroke-width="1.5"' in svg, svg
        assert 'vector-effect="non-scaling-stroke"' in svg, svg
    # the attribute is not inherited, so a rule carries it to every shape inside
    assert ".wm .wm-ic,.wm .wm-ic *{vector-effect:non-scaling-stroke}" in wm.WM_CSS


def test_an_off_centre_glyph_is_nudged_onto_the_centre_of_its_viewbox():
    """Only glyphs that miss (12,12) carry a translate, and it is a small one."""
    assert set(wm.ICON_NUDGE) <= set(wm.ICONS)
    for name, (dx, dy) in wm.ICON_NUDGE.items():
        assert abs(dx) <= wm.ICON_TOL and abs(dy) <= wm.ICON_TOL, name
        assert f'<g transform="translate({dx:g} {dy:g})">' in wm.icon(name)
    for name in set(wm.ICONS) - set(wm.ICON_NUDGE):
        assert "<g transform" not in wm.icon(name), name


def test_the_details_card_rides_the_same_header_as_any_other(tmp_path, px):
    html = wm.write_report(_portfolio(px), px, tmp_path / "r.html").read_text()
    card = next(c for c in re.findall(r'<section class="wm-card[^>]*>.*?</section>',
                                      html, re.S) if ">Details<" in c)
    assert 'class="wm-ch"' in card and 'data-align="head-icon"' in card
    assert 'data-align="head-title">Details<' in card
    assert 'data-icon="list"' in card and 'data-align="head-ro"' in card
    assert "<table>" in card and 'class="wm-scroll"' in card


def test_mono_is_reserved_for_numbers_tickers_and_section_numbers(tmp_path, px):
    css = wm.WM_CSS
    mono = {line.split("{")[0].strip() for line in css.splitlines()
            if "var(--wm-mono)" in line}
    words = {".wm .wm-lede", ".wm h1", ".wm .wm-kk", ".wm th",
             ".wm .wm-ch h2", ".wm .wm-foot"}
    assert not (mono & words), f"mono leaked onto words: {mono & words}"
    for sel in (".wm .wm-kv", ".wm .wm-ro", ".wm .wm-meta",
                ".wm .wm-tag", ".wm td"):
        assert any(m.startswith(sel) for m in mono), f"{sel} should be mono"


def test_the_v3_scale_is_the_one_that_ships():
    """v2.1 type, v3 sizes: masthead 40/44, lede 18/28, key value 32/36,
    card title 15/24, labels 12/16, readouts and meta 11/16."""
    css = wm.WM_CSS
    for rule in ("font:600 40px/44px", "font:400 18px/28px",
                 "font-size:32px;line-height:36px", "font:600 15px/24px",
                 "font:500 12px/16px", "font-size:11px"):
        assert rule in css, rule
    assert "font-size:28px" not in css          # the v2.1 key value is gone


# --------------------------------------------------------------------------
# svg primitives
# --------------------------------------------------------------------------
def test_svg_line_is_responsive_and_downsampled():
    dates = list(pd.bdate_range("2020-01-01", periods=2000))
    vals = list(np.linspace(100.0, 180.0, 2000))
    svg = wm.svg_line(dates, [{"label": "Yours", "values": vals, "cls": "me"}])
    assert svg.startswith('<svg viewBox="0 0 640 230" width="100%"')
    assert svg.count("data-wm-rows=") == 1
    rows = re.search(r'data-wm-rows="([^"]*)"', svg).group(1).split(wm.SEP)
    assert len(rows) <= wm.HOVER_PTS
    assert len(svg) < 40_000


def test_x_ticks_land_on_calendar_boundaries_not_data_indices():
    """Every date label is a real 1st-of-period, whatever the data window."""
    def labels(start, periods):
        dates = list(pd.bdate_range(start, periods=periods))
        sx = lambda i: 48.0 + (640 - 12 - 48) * i / (len(dates) - 1)
        return [t for _, t in wm._date_ticks(dates, sx)]

    # over three years: 1 January of each year
    assert labels("2021-03-04", 1200) == ["2022", "2023", "2024", "2025"]
    # one to three years: every six months
    assert labels("2022-11-08", 420) == ["Jan 23", "Jul 23", "Jan 24"]
    # under a year: monthly
    assert labels("2024-02-07", 150) == ["Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep"]

    # and the label is never the first or last day of the data
    dates = list(pd.bdate_range("2021-03-04", periods=1200))
    svg = wm.svg_line(dates, [{"label": "Yours",
                               "values": list(np.linspace(100.0, 180.0, 1200)),
                               "cls": "me"}])
    assert "04 Mar 21" not in svg.split("data-wm-rows")[0]
    assert '>2022</text>' in svg and '>2023</text>' in svg


def test_downsampling_keeps_the_true_trough():
    """A one-day spike must survive: the drawdown's real bottom is the point."""
    n = 2000
    vals = [-0.05] * n
    vals[977] = -0.42                       # the only day that matters
    dates = list(pd.bdate_range("2019-01-01", periods=n))
    svg = wm.svg_area(dates, vals)
    assert "−42%" in svg or "-42%" in svg or "42%" in svg
    keep = wm._bucket(n, [vals])
    assert 977 in keep and len(keep) <= wm.MAX_PTS


def test_allocation_strip_has_one_segment_per_holding():
    w = {"A": 0.5, "B": 0.3, "C": 0.2}
    order, color = wm._order_colors(w, None)
    html = wm._block_alloc(w, order, color)[3]
    assert html.count('class="wm-seg"') == len(w)
    assert html.count('class="wm-tag"') == len(w)
    # segments grow in proportion to the weight, so the bar always fills the column
    assert "flex:0.500000 1 0" in html and "flex:0.200000 1 0" in html
    assert "<circle" not in html and "<path" not in html  # no donut anywhere


def test_allocation_strip_segment_count_matches_the_report(tmp_path, px):
    html = wm.write_report(_portfolio(px), px, tmp_path / "r.html").read_text()
    assert html.count('class="wm-seg"') == 3   # DOUBLE, INDIE, BND
    assert "wm-donut" not in html


def test_sleeve_tones_come_from_the_sleeve_family():
    w = {"NVDA": 0.1, "SMH": 0.1, "VOO": 0.5, "SGOV": 0.3}
    sleeves = {"theme": ["NVDA", "SMH"], "ballast": ["VOO"], "cash": ["SGOV"]}
    order, color = wm._order_colors(w, sleeves)
    assert order == ["NVDA", "SMH", "VOO", "SGOV"]
    assert color["NVDA"] == "th1" and color["SMH"] == "th2"
    assert color["VOO"] == "co1" and color["SGOV"] == "ca1"


def test_the_correlation_card_pairs_the_grid_with_a_ranked_pane():
    """Two panes: the capped grid, and the same `_pairs` data ranked beside it."""
    labels = ["SMH", "NVDA", "VOO", "SGOV"]
    m = [[1.0, .84, .81, .27], [.84, 1.0, .69, .18],
         [.81, .69, 1.0, .51], [.27, .18, .51, 1.0]]
    pane = wm.html_pane(labels, m, [{"members": ["SMH", "NVDA"], "avg_corr": .84}])
    assert 'class="wm-hmp"' in pane and ">Closest pairs<" in pane
    rows = re.findall(r'<li class="wm-plr".*?</li>', pane, re.S)
    assert len(rows) == len(wm._pairs(labels, m)) <= wm.PANE_ROWS
    # strongest first, and the value is the grid's own four-character form
    vals = [float(v.replace("\u2212", "-"))
            for v in re.findall(r'class="wm-plv">([^<]+)<', pane)]
    assert vals == sorted(vals, reverse=True) and vals[0] == .84
    # the bar is |r| of the track, and the first cluster's pair carries the accent
    assert 'width:84.0%' in rows[0] and "var(--wm-accent)" in rows[0]
    assert "var(--wm-ink)" in rows[-1] and "var(--wm-accent)" not in rows[-1]
    # names share one mono track so every bar starts on the same x
    assert re.search(r"flex:0 0 \d+ch", pane)
    # a row reads out like any other mark, and nothing here explains anything
    assert 'data-wm-r="NVDA &amp; SMH' in pane
    assert wm.PANE_ROWS == 8


def test_the_pane_sits_beside_the_grid_and_stacks_under_it_on_a_phone():
    css = wm.WM_CSS
    assert ".wm .wm-panes{display:flex;gap:20px;align-items:flex-start}" in css
    pane = next(l for l in css.splitlines() if l.startswith(".wm .wm-hmp{"))
    assert "border-left:1px solid var(--wm-edge)" in pane and "padding-left:20px" in pane
    # the grid is nudged down by the pane's header so the two first rows agree
    assert ".wm .wm-hmw{flex:0 1 560px;min-width:0;padding-top:32px}" in css
    assert ".wm .wm-plb{flex:1 1 auto;min-width:20px;height:4px;" \
           "background:var(--wm-chip)}" in css
    # below the twelve-column breakpoint it stacks, and the grid uncaps
    flat = css.split("@media (max-width:899px){")[1].split("}@media")[0]
    assert ".wm .wm-panes{display:block}" in flat
    assert ".wm .wm-hmw{max-width:none;padding-top:0}" in flat
    assert "border-top:1px solid var(--wm-edge)" in flat


def test_svg_heatmap_annotates_every_cell_but_the_diagonal():
    svg = wm.svg_heatmap(["A", "B"], [[1.0, 0.84], [0.84, 1.0]])
    # a correlation cell drops its leading zero: .84 in the cell, 0.84 in the readout
    assert svg.count("<rect") == 4 and ">.84</text>" in svg
    assert ">0.84</text>" not in svg and "B \u00b7 0.84" in svg
    assert "var(--wm-h4)" in svg  # 0.84 lands high on the ramp
    # the diagonal is an asset against itself: a faint empty square, no 1.00
    assert svg.count('fill="none" stroke="var(--wm-edge)"') == 2
    assert "1.00" not in svg
    assert svg.count("data-wm-r=") == 2  # only the two off-diagonal cells read out


def test_svg_bars_handles_negative_values():
    svg = wm.svg_bars([{"label": "2022 hiking cycle",
                        "bars": [{"label": "Yours", "value": -0.21, "cls": "me"},
                                 {"label": "S&P 500", "value": 0.05, "cls": "bench"}]}])
    assert svg.count("<rect") == 2 and "\u221221%" in svg and "+5%" in svg


def test_ticks_are_round_numbers():
    assert wm._ticks(0.0, 1.0, 4) == [0.0, 0.25, 0.5, 0.75, 1.0]
    assert wm._ticks(0.0, 100.0, 3) == [0.0, 50.0, 100.0]
    t = wm._ticks(97.0, 183.0, 4)
    assert 3 <= len(t) <= 5 and all(v % 25 == 0 for v in t)


# --------------------------------------------------------------------------
# correlation-adjusted bets
# --------------------------------------------------------------------------
def test_effective_bets_corr_counts_one_bet_when_perfectly_correlated():
    w = np.array([0.5, 0.5])
    cov = np.array([[0.04, 0.04], [0.04, 0.04]])  # rho = 1, same vol
    assert wm.effective_bets_corr(w, cov) == pytest.approx(1.0)
    assert wm.effective_bets(w) == pytest.approx(2.0)  # the weight-only view


def test_effective_bets_corr_counts_two_when_uncorrelated():
    w = np.array([0.5, 0.5])
    cov = np.array([[0.04, 0.0], [0.0, 0.04]])
    assert wm.effective_bets_corr(w, cov) == pytest.approx(2.0)


def test_portfolio_reports_both_bet_numbers(px):
    res = wm.analyze_frame(px, "SPY", {"DOUBLE": 0.5, "TWIN": 0.5})
    p = res["portfolio"]
    assert p["effective_bets"] == pytest.approx(2.0, abs=0.01)
    assert 1.0 <= p["effective_bets_corr"] < 1.4  # DOUBLE and TWIN are one bet


def test_keys_render_the_four_v3_figures_without_overclaiming():
    keys=wm._keys({'effective_bets':4.,'effective_bets_corr':1.8,'n_holdings':4},{},'the S&P 500',[{'members':['MU','NVDA'],'avg_corr':.8}])
    for label in ('Worst fall','Separate bets','Moves with the market','Annual cost'):
        assert label in keys
    # a whole number out of the holding count, and the cluster named plainly
    assert '2 of 4' in keys and 'MU and NVDA move as one' in keys
    # no unknowable number is invented: an unpublished fee stays an em dash
    assert '\u2014' in keys
    # never claims statistical independence or a forecast
    for phrase in ('independent','uncorrelated','will ','expected'):
        assert phrase not in keys.lower()


# --------------------------------------------------------------------------
# home currency
# --------------------------------------------------------------------------
def _flat_fx(rate):
    return lambda frm, to: pd.Series(rate, index=_dates())


def test_to_currency_multiplies_prices_and_records_what_it_converted(px):
    warnings = []
    meta = {t: {"currency": "USD"} for t in px.columns}
    out = wm.to_currency(px, "MXN", warnings, meta=meta, fx_loader=_flat_fx(20.0))
    assert out.attrs["currency"] == "MXN"
    assert set(out.attrs["fx_converted"]) == set(px.columns)
    assert out["SPY"].iloc[-1] == pytest.approx(20.0 * px["SPY"].iloc[-1])
    assert not warnings


def test_to_currency_skips_assets_already_in_the_home_currency(px):
    warnings = []
    meta = {t: {"currency": "MXN" if t == "BND" else "USD"} for t in px.columns}
    out = wm.to_currency(px, "MXN", warnings, meta=meta, fx_loader=_flat_fx(20.0))
    assert "BND" not in out.attrs["fx_converted"]
    assert out["BND"].iloc[-1] == pytest.approx(px["BND"].iloc[-1])


def test_unknown_native_currency_blocks_relabeling(px):
    meta={t:{'currency':None if t=='INDIE' else 'USD'} for t in px.columns}
    with pytest.raises(ValueError,match='native currency unavailable: INDIE'):
        wm.to_currency(px,'MXN',[],meta=meta,fx_loader=_flat_fx(20.))


def test_a_moving_rate_changes_the_returns(px):
    drift = pd.Series(np.linspace(18.0, 22.0, len(px)), index=px.index)
    warnings = []
    meta = {t: {"currency": "USD"} for t in px.columns}
    out = wm.to_currency(px, "MXN", warnings, meta=meta,
                         fx_loader=lambda f, t: drift)
    native = wm.ann_return(wm.daily_returns(px)["SPY"])
    home = wm.ann_return(wm.daily_returns(out)["SPY"])
    assert home > native  # the peso weakened over the window


def test_currency_flag_surfaces_in_the_analyze_output(capsys):
    wm.main(["analyze", "SPY", "BND", "--bench", "SPY", "--currency", "MXN"])
    res = json.loads(capsys.readouterr().out)
    assert res["currency"] == "MXN"
    assert set(res["fx_converted"]) == {"SPY", "BND"}


# --------------------------------------------------------------------------
# rebalancing policy
# --------------------------------------------------------------------------
def _doubler_and_flat(years=2):
    idx = pd.bdate_range("2021-01-04", periods=int(252 * years))
    n = len(idx)
    up = pd.Series(2.0 ** (years * np.arange(n) / (n - 1)), index=idx)
    flat = pd.Series(1.0, index=idx)
    return pd.DataFrame({"UP": up, "FLAT": flat})


def test_buy_and_hold_ends_at_the_blend_of_the_two_legs():
    px = _doubler_and_flat(years=1)
    rets = wm.daily_returns(px)
    pr = wm.portfolio_returns(rets, {"UP": 0.5, "FLAT": 0.5}, "none")
    # UP doubles over the window, FLAT does nothing: 0.5*2 + 0.5*1 = 1.5x
    assert float((1.0 + pr).prod()) == pytest.approx(1.5, rel=1e-6)


def test_daily_constant_mix_differs_from_buy_and_hold():
    px = _doubler_and_flat(years=1)
    rets = wm.daily_returns(px)
    w = {"UP": 0.5, "FLAT": 0.5}
    hold = float((1.0 + wm.portfolio_returns(rets, w, "none")).prod())
    mix = float((1.0 + wm.portfolio_returns(rets, w, "daily")).prod())
    assert mix != pytest.approx(hold, rel=1e-6)
    assert mix == pytest.approx(
        float((1.0 + (rets * pd.Series(w)).sum(axis=1)).prod()), rel=1e-12)


def test_annual_resets_at_the_year_boundary():
    px = _doubler_and_flat(years=2)
    rets = wm.daily_returns(px)
    w = {"UP": 0.5, "FLAT": 0.5}
    annual = wm.portfolio_returns(rets, w, "annual")
    hold = wm.portfolio_returns(rets, w, "none")
    assert annual.index.equals(rets.index)
    # each calendar year is its own buy-and-hold leg, compounded together
    legs = [float((1.0 + wm.portfolio_returns(g, w, "none")).prod())
            for _, g in rets.groupby(rets.index.year)]
    assert float((1.0 + annual).prod()) == pytest.approx(np.prod(legs), rel=1e-9)
    assert float((1.0 + annual).prod()) != pytest.approx(
        float((1.0 + hold).prod()), rel=1e-6)


def test_rebalance_flag_echoes_and_moves_the_portfolio_numbers(capsys):
    wm.main(["analyze", "DOUBLE", "BND", "--weights", "0.5", "0.5",
             "--bench", "SPY", "--rebalance", "none"])
    held = json.loads(capsys.readouterr().out)
    wm.main(["analyze", "DOUBLE", "BND", "--weights", "0.5", "0.5",
             "--bench", "SPY", "--rebalance", "daily"])
    mixed = json.loads(capsys.readouterr().out)
    assert held["rebalance"] == "none" and mixed["rebalance"] == "daily"
    assert held["portfolio"]["ann_return"] != mixed["portfolio"]["ann_return"]


def test_build_records_the_rebalancing_policy(tmp_path, capsys):
    out = tmp_path / "p.json"
    wm.main(["build", "DOUBLE", "BND", "--years", "3", "--rebalance", "daily",
             "--save", str(out)])
    capsys.readouterr()
    saved = json.loads(out.read_text())
    assert saved["rebalance"] == "daily"
    assert saved["stats"]["rebalance"] == "daily"


def test_fragment_css_is_chat_safe(tmp_path, capsys):
    card = tmp_path / "c.html"
    wm.main(["analyze", "DOUBLE", "BND", "--weights", "0.5", "0.5",
             "--bench", "SPY", "--card", str(card), "--fragment"])
    capsys.readouterr()
    frag = card.read_text()
    assert "--wm-paper:transparent" in frag
    assert "padding:2px 0 8px" in frag
    # the ground goes transparent, but a card is still a card
    assert "--wm-panel:#ffffff" in frag and "border-radius:12px" in frag
    assert "font-size:9" not in frag and "font-size:10" not in frag
    assert not re.search(r'(src|href)=', frag)     # a fragment fetches nothing
    standalone = wm._css(False)
    assert "--wm-paper:#f7f6f2" in standalone  # the page keeps its paper


# --------------------------------------------------------------------------
# 13F holdings
# --------------------------------------------------------------------------
INFO_TABLE = """<?xml version="1.0" encoding="UTF-8"?>
<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><titleOfClass>COM</titleOfClass>
    <cusip>037833100</cusip><value>600</value>
    <shrsOrPrnAmt><sshPrnamt>6</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
    <investmentDiscretion>SOLE</investmentDiscretion></infoTable>
  <infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><titleOfClass>COM</titleOfClass>
    <cusip>037833100</cusip><value>200</value>
    <shrsOrPrnAmt><sshPrnamt>2</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
    <investmentDiscretion>SOLE</investmentDiscretion></infoTable>
  <infoTable><nameOfIssuer>NVIDIA CORPORATION</nameOfIssuer><titleOfClass>COM</titleOfClass>
    <cusip>67066G104</cusip><value>150</value>
    <shrsOrPrnAmt><sshPrnamt>3</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
    <investmentDiscretion>SOLE</investmentDiscretion></infoTable>
  <infoTable><nameOfIssuer>PRIVATE THING LLC</nameOfIssuer><titleOfClass>COM</titleOfClass>
    <cusip>999999999</cusip><value>50</value>
    <shrsOrPrnAmt><sshPrnamt>1</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
    <investmentDiscretion>SOLE</investmentDiscretion></infoTable>
</informationTable>"""


def test_13f_parser_merges_duplicate_positions_and_sorts_by_value():
    rows = wm.parse_13f_table(INFO_TABLE)
    assert [r["cusip"] for r in rows] == ["037833100", "67066G104", "999999999"]
    apple = rows[0]
    assert apple["value"] == pytest.approx(800.0) and apple["shares"] == pytest.approx(8)


def test_13f_preserves_report_weights_and_unverified_identity():
    holdings,weights,warnings=wm.holdings_from_table(wm.parse_13f_table(INFO_TABLE),{'APPLE':'AAPL','NVIDIA':'NVDA'})
    assert [h['weight'] for h in holdings]==pytest.approx([.8,.15,.05])
    assert weights=={}
    assert all(h['ticker'] is None for h in holdings)
    assert holdings[0]['ticker_suggestion']=='AAPL' and warnings


def test_13f_top_n_does_not_renormalize_into_manager_portfolio():
    holdings,weights,_=wm.holdings_from_table(wm.parse_13f_table(INFO_TABLE),{'APPLE':'AAPL','NVIDIA':'NVDA'},top=1)
    assert len(holdings)==1 and weights=={} and holdings[0]['weight']==pytest.approx(.8)


def test_13f_suggestions_do_not_feed_straight_into_analyze(px):
    rows=[{'name':'DOUBLE','cusip':'1','value':60.,'shares':1.},{'name':'BND','cusip':'2','value':40.,'shares':1.}]
    _,weights,_=wm.holdings_from_table(rows,{'DOUBLE':'DOUBLE','BND':'BND'})
    with pytest.raises(ValueError,match='at least one weight'):
        wm.analyze_frame(px,'SPY',weights)


if __name__ == "__main__":
    # `uv run --with pytest tools/test_wm.py tools/test_safety.py` hands the
    # extra paths through as argv; run every file named, not just this one.
    _extra = [a for a in sys.argv[1:] if not a.startswith("-")]
    _flags = [a for a in sys.argv[1:] if a.startswith("-")]
    sys.exit(pytest.main([__file__, *_extra, *(_flags or ["-q"])]))
