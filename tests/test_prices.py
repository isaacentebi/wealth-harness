"""Market-data layer (wealth/prices.py) and its wiring; a fake fetcher stands in for Yahoo (no network)."""
from __future__ import annotations

import threading
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from wealth import profile, situation
from wealth.prices import Fetched, PriceProvider, ledger_market, provider_symbol
from wealth.service import WealthService, dispatch
from wealth.store import WealthStore

OPEN = datetime(2026, 9, 21, 15, 0, tzinfo=timezone.utc)     # Monday, NYSE and BMV open
CLOSED = datetime(2026, 9, 21, 22, 0, tzinfo=timezone.utc)   # Monday evening
OWNER = [{"person_id": "ana", "share": 1}]


def weekdays(first: str, last: str, start: float, step: float) -> dict[str, float]:
    day, end, out, value = date.fromisoformat(first), date.fromisoformat(last), {}, start
    while day <= end:
        if day.weekday() < 5:
            out[day.isoformat()] = round(value, 4)
            value += step
        day += timedelta(days=1)
    return out


class Clock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class FakeYahoo:
    """Serves fixed daily series; records calls; can fail or be slow."""

    def __init__(self, data: dict[str, tuple[str, dict[str, float]]]):
        self.data = data
        self.calls: list[tuple] = []
        self.fail = False
        self.gate: threading.Event | None = None

    def __call__(self, symbol, start, end, adjusted):
        self.calls.append((symbol, start.isoformat(), end.isoformat(), adjusted))
        if self.gate is not None:
            self.gate.wait(5)
        if self.fail:
            raise ConnectionError("network is down")
        if symbol not in self.data:
            raise LookupError(f"{symbol}: no data returned")
        currency, points = self.data[symbol]
        factor = 1.01 if adjusted else 1.0  # adjusted closes differ from plain closes
        return Fetched({d: v * factor for d, v in points.items() if start.isoformat() <= d <= end.isoformat()},
                       currency, f"fake Yahoo {'adjusted close' if adjusted else 'daily close'} of {symbol}")


def market_data() -> dict[str, tuple[str, dict[str, float]]]:
    return {
        "VOO": ("USD", weekdays("2026-03-02", "2026-09-18", 480.0, 0.5)),
        "BND": ("USD", weekdays("2026-03-02", "2026-09-18", 72.0, 0.01)),
        "AAPL.MX": ("MXN", weekdays("2026-03-02", "2026-09-18", 4000.0, 2.0)),
        "USDMXN=X": ("MXN", weekdays("2026-03-02", "2026-09-18", 18.5, -0.002)),
        "ACWI": ("USD", weekdays("2026-03-02", "2026-09-18", 110.0, 0.1)),
        "AGG": ("USD", weekdays("2026-03-02", "2026-09-18", 98.0, 0.01)),
        "BNDW": ("USD", weekdays("2026-03-02", "2026-09-18", 68.0, 0.01)),
    }


@pytest.fixture
def fake():
    return FakeYahoo(market_data())


@pytest.fixture
def db(tmp_path):
    return tmp_path / "w.sqlite3"


def provider(db, fake, clock=None, **kwargs) -> PriceProvider:
    return PriceProvider(db, fetcher=fake, offline=False, clock=clock or Clock(CLOSED), **kwargs)


# ------------------------------------------------------------------ symbol mapping


@pytest.mark.parametrize("symbol, kwargs, expected", [
    ("AAPL *", {}, "AAPL.MX"),                                    # SIC, MXN quote
    ("WALMEX *", {}, "WALMEX.MX"),                                # BMV single series
    ("NAFTRAC ISHRS", {}, "NAFTRAC.MX"),                          # BMV iShares trust
    ("GFNORTE O", {}, "GFNORTEO.MX"),                             # BMV series appended
    ("VOO", {"venue": "sic", "currency": "MXN"}, "VOO.MX"),       # ledger SIC instrument
    ("VOO", {"venue": "us", "currency": "USD"}, "VOO"),           # the US listing
    ("CSPXN", {"listing": "SIC"}, "CSPXN.MX"),
    ("BRK.B", {"currency": "USD"}, "BRK-B"),                      # US share class
    ("AAPL.MX", {}, "AAPL.MX"),
    ("USDMXN=X", {}, "USDMXN=X"),
])
def test_symbol_mapping(symbol, kwargs, expected):
    assert provider_symbol(symbol, **kwargs) == (expected, None)


@pytest.mark.parametrize("symbol, kwargs", [
    ("CETES", {"asset_class": "fixed_income", "currency": "MXN"}),
    ("UDIBONO 351122", {}),
    ("BONDES D", {}),
    ("CASH:MXN", {}),
])
def test_mexican_fixed_income_and_cash_are_not_sent_to_yahoo(symbol, kwargs):
    yahoo, reason = provider_symbol(symbol, **kwargs)
    assert yahoo is None and reason


# ------------------------------------------------------------------ cache


def test_latest_is_dated_sourced_and_cached_with_market_hours_ttl(db, fake):
    clock = Clock(OPEN)
    prices = provider(db, fake, clock)
    first = prices.latest(["VOO"], budget=5)
    quote = first["prices"]["VOO"]
    assert quote["date"] == "2026-09-18" and quote["currency"] == "USD" and quote["origin"] == "live"
    assert quote["source"] == "fake Yahoo daily close of VOO" and quote["retrieved_at"].startswith("2026-09-21T15:00")
    assert Decimal(quote["price"]) > 0 and not quote["stale"] and len(fake.calls) == 1
    clock.now = OPEN + timedelta(minutes=10)                 # inside the 15-minute open-market TTL
    assert prices.latest(["VOO"], budget=5)["prices"]["VOO"]["origin"] == "cache" and len(fake.calls) == 1
    clock.now = OPEN + timedelta(minutes=20)                 # past it: refetched
    prices.latest(["VOO"], budget=5)
    assert len(fake.calls) == 2
    clock.now = CLOSED                                       # after the close the TTL is 12 hours
    prices.latest(["VOO"], budget=5)
    assert len(fake.calls) == 3
    clock.now = CLOSED + timedelta(hours=1)
    prices.latest(["VOO"], budget=5)
    assert len(fake.calls) == 3
    saturday = datetime(2026, 9, 26, 1, 0, tzinfo=timezone.utc)
    clock.now = saturday                                     # a new day: refetched
    prices.latest(["VOO"], budget=5)
    assert len(fake.calls) == 4
    clock.now = saturday + timedelta(hours=11)
    prices.latest(["VOO"], budget=5)
    assert len(fake.calls) == 4
    clock.now = saturday + timedelta(hours=13)
    prices.latest(["VOO"], budget=5)
    assert len(fake.calls) == 5


def test_history_is_immutable_once_past(db, fake):
    prices = provider(db, fake)
    got = prices.history(["VOO"], "2026-06-01", "2026-06-30", budget=5)
    points = got["series"]["VOO"]["points"]
    assert points[0]["date"] == "2026-06-01" and points[-1]["date"] == "2026-06-30" and len(fake.calls) == 1
    again = prices.history(["VOO"], "2026-06-01", "2026-06-30", budget=5)
    assert again["series"]["VOO"]["origin"] == "cache" and len(fake.calls) == 1   # a closed window is not refetched
    # A later fetch that disagrees about a past day does not rewrite it.
    fake.data["VOO"] = ("USD", {d: v + 100 for d, v in fake.data["VOO"][1].items()})
    prices.refresh(["VOO"], days=120)
    kept = prices.history(["VOO"], "2026-06-01", "2026-06-30", budget=5)["series"]["VOO"]["points"]
    assert kept == points
    # An intraday value (fetched on its own day) is replaced by the next fetch.
    with WealthStore(db) as store:
        store.put_market([{"symbol": "ZZZ", "kind": "close", "date": "2026-09-21", "value": "10", "currency": "USD",
                           "source": "s", "retrieved_at": "2026-09-21T15:00:00+00:00"}],
                         {"symbol": "ZZZ", "kind": "close", "start": "2026-09-21", "end": "2026-09-21",
                          "retrieved_at": "2026-09-21T15:00:00+00:00"})
        store.put_market([{"symbol": "ZZZ", "kind": "close", "date": "2026-09-21", "value": "11", "currency": "USD",
                           "source": "s", "retrieved_at": "2026-09-22T01:00:00+00:00"}],
                         {"symbol": "ZZZ", "kind": "close", "start": "2026-09-21", "end": "2026-09-21",
                          "retrieved_at": "2026-09-22T01:00:00+00:00"})
        store.put_market([{"symbol": "ZZZ", "kind": "close", "date": "2026-09-21", "value": "12", "currency": "USD",
                           "source": "s", "retrieved_at": "2026-09-23T01:00:00+00:00"}],
                         {"symbol": "ZZZ", "kind": "close", "start": "2026-09-21", "end": "2026-09-21",
                          "retrieved_at": "2026-09-23T01:00:00+00:00"})
        assert [r["value"] for r in store.market_rows("ZZZ", "close")] == ["11"]


def test_adjusted_and_plain_closes_are_cached_apart(db, fake):
    prices = provider(db, fake)
    plain = prices.history(["ACWI"], "2026-06-01", "2026-06-05", budget=5)["series"]["ACWI"]["points"]
    adjusted = prices.history(["ACWI"], "2026-06-01", "2026-06-05", adjusted=True, budget=5)["series"]["ACWI"]["points"]
    assert Decimal(adjusted[0]["price"]) > Decimal(plain[0]["price"])


# ------------------------------------------------------------------ offline and failures


def test_offline_serves_the_cache_labelled_or_missing_never_zero(db, fake, monkeypatch):
    prices = provider(db, fake)
    prices.latest(["VOO"], budget=5)
    calls = len(fake.calls)
    offline = PriceProvider(db, fetcher=fake, clock=Clock(CLOSED + timedelta(days=1)))   # env decides
    monkeypatch.setenv("WEALTH_OFFLINE", "1")
    got = offline.latest(["VOO", "AGG"])
    assert len(fake.calls) == calls                                   # no network at all
    assert got["offline"] and got["prices"]["VOO"]["origin"] == "cache_fallback"
    assert got["missing"] == [{"symbol": "AGG", "provider_symbol": "AGG", "reason": "offline and nothing cached"}]
    assert "AGG" not in got["prices"]
    status = offline.status()
    assert status["offline"] and [r["symbol"] for r in status["symbols"]] == ["VOO"]
    assert offline.refresh(["VOO"])["offline"] is True


def test_a_failed_fetch_falls_back_to_the_cache_and_never_raises(db, fake):
    clock = Clock(CLOSED)
    prices = provider(db, fake, clock)
    prices.latest(["VOO"], budget=5)
    fake.fail = True
    clock.now = CLOSED + timedelta(days=1)
    got = prices.latest(["VOO", "BND"], budget=5)
    assert got["prices"]["VOO"]["origin"] == "cache_fallback"
    assert got["missing"][0]["symbol"] == "BND" and "network is down" in got["missing"][0]["reason"]
    assert prices.history(["BND"], "2026-06-01", "2026-06-30", budget=5)["missing"][0]["symbol"] == "BND"
    assert prices.fx("EUR", "MXN", "2026-06-01", "2026-06-30", budget=5)["rates"] == []


def test_a_slow_fetch_does_not_block_past_the_budget(db, fake):
    fake.gate = threading.Event()
    prices = provider(db, fake)
    got = prices.latest(["VOO"], budget=0.05)
    assert got["pending"] == ["VOO"] and got["missing"][0]["symbol"] == "VOO"   # cold cache: missing for now
    fake.gate.set()
    prices.wait(5)
    assert prices.latest(["VOO"], budget=0.05)["prices"]["VOO"]["origin"] == "cache"


# ------------------------------------------------------------------ staleness and fixed income


def test_old_prices_are_labelled_stale(db, fake):
    prices = provider(db, fake)
    fresh = prices.latest(["VOO"], "2026-09-18", budget=5)
    assert fresh["stale_prices"] == [] and not fresh["prices"]["VOO"]["stale"]
    fake.data["BND"] = ("USD", weekdays("2026-03-02", "2026-09-08", 72.0, 0.01))   # stopped updating
    old = prices.latest(["BND"], "2026-09-18", budget=5)
    assert old["prices"]["BND"]["stale"] and old["prices"]["BND"]["age_days"] == 10
    assert old["stale_prices"] == [{"symbol": "BND", "date": "2026-09-08", "age_days": 10,
                                    "source": "fake Yahoo daily close of BND"}]


def test_cetes_use_a_statement_price_accrued_at_a_stated_rate_or_stay_missing(db, fake):
    prices = provider(db, fake)
    cetes = {"id": "CETES", "symbol": "CETES", "asset_class": "fixed_income", "currency": "MXN",
             "statement_price": {"price": "9.90", "date": "2026-09-01", "source": "GBM statement"}}
    as_stated = prices.latest([cetes], "2026-09-21")["prices"]["CETES"]
    assert as_stated["price"] == "9.900000" and as_stated["origin"] == "statement" and as_stated["stale"]
    accrued = prices.latest([{**cetes, "annual_rate": "0.072", "rate_source": "Banxico CETES 28"}],
                            "2026-09-21")["prices"]["CETES"]
    assert Decimal(accrued["price"]) == (Decimal("9.90") * (1 + Decimal("0.072") * 20 / 360)).quantize(Decimal("0.000001"))
    assert accrued["origin"] == "accrual" and "Banxico CETES 28" in accrued["source"]
    unknown = prices.latest([{"id": "UDI", "symbol": "UDIBONO 351122"}], "2026-09-21")
    assert unknown["prices"] == {} and "not quoted on Yahoo" in unknown["missing"][0]["reason"]
    assert fake.calls == []


# ------------------------------------------------------------------ wiring: situation, review, CLI op


def _post(db, client_id, accounts, instruments, transactions):
    with WealthStore(db) as store:
        store.post_ledger(client_id, {
            "batch_id": f"{client_id}-opening",
            "source": {"kind": "document", "ref": "broker statements", "observed_on": "2026-04-01"},
            "accounts": accounts, "instruments": instruments, "transactions": transactions})


IB = {"id": "ib", "institution": "Interactive Brokers", "type": "brokerage", "currency": "USD", "owners": OWNER}
GBM = {"id": "gbm", "institution": "GBM", "type": "brokerage", "currency": "MXN", "owners": OWNER}
VOO = {"id": "VOO", "symbol": "VOO", "currency": "USD", "asset_class": "equity", "venue": "us", "issuer_domicile": "US"}
BND = {"id": "BND", "symbol": "BND", "currency": "USD", "asset_class": "bond_fund", "venue": "us"}
AAPL_SIC = {"id": "AAPL-SIC", "symbol": "AAPL *", "currency": "MXN", "asset_class": "equity", "venue": "sic",
            "underlying_symbol": "AAPL", "issuer_domicile": "US"}


def _opening(account, day="2026-03-31", **line):
    return {"account_id": account, "kind": "opening_balance", "date": day, **line}


def _remember(service, client_id, facts):
    service.remember(client_id, [{**f, "source": {"kind": "user", "ref": "chat", "observed_on": "2026-04-01"}}
                                 for f in facts])


def test_situation_values_a_ledger_only_account_with_provider_prices(db, fake):
    prices = provider(db, fake)
    service = WealthService(db, prices=prices)
    service.create("ana", "Ana")
    _remember(service, "ana", [{"key": "client.profile", "value": {"reporting_currency": "MXN",
                                                                   "residence": {"country": "MX"}}}])
    _post(db, "ana", [IB, GBM], [VOO, AAPL_SIC], [
        _opening("ib", instrument_id="VOO", quantity="10", cost_basis="4000", acquired_on="2025-01-02", currency="USD"),
        _opening("ib", amount="1000", currency="USD"),
        _opening("gbm", instrument_id="AAPL-SIC", quantity="5", cost_basis="15000", acquired_on="2025-01-02",
                 currency="MXN"),
        _opening("gbm", amount="2000", currency="MXN")])
    with WealthStore(db) as store:
        snapshot, ledger = store.snapshot("ana"), store.ledger("ana")

    # Without prices the pure function leaves both accounts unvalued, as before.
    bare = situation.build(snapshot, ledger, date(2026, 9, 21))
    assert sorted(bare["net_worth"]["unvalued_accounts"]) == ["GBM", "Interactive Brokers"]

    sit = service.situation("ana", today=date(2026, 9, 21))
    nw = sit["net_worth"]
    assert nw["unvalued_accounts"] == [] and nw["complete"]
    voo = Decimal(str(market_data()["VOO"][1]["2026-09-18"]))
    aapl = Decimal(str(market_data()["AAPL.MX"][1]["2026-09-18"]))
    usdmxn = Decimal(str(market_data()["USDMXN=X"][1]["2026-09-18"]))
    ib = next(a for a in sit["accounts"] if a["id"] == "ib")
    gbm = next(a for a in sit["accounts"] if a["id"] == "gbm")
    assert Decimal(str(gbm["value"])) == 5 * aapl + 2000
    assert abs(Decimal(str(ib["value"])) - (10 * voo + 1000) * usdmxn) < Decimal("0.01")
    assert ib["valued_by"] == "prices" and ib["as_of"] == "2026-09-18"
    assert {"source": "fake Yahoo daily close of VOO", "date": "2026-09-18"} in ib["price_sources"]
    assert {"source": "fake Yahoo daily close of AAPL.MX", "date": "2026-09-18"} in nw["price_sources"]
    assert any(f["pair"] == "USD/MXN" and "USDMXN=X" in f["source"] for f in sit["fx"])
    assert sit["stale_prices"] == [] and sit["market"]["missing"] == []
    assert sit["holdings"]["positions"] == 4 and sit["holdings"]["unvalued"] == 0

    # The same quotes as the pure function's input: nothing is fetched inside build.
    calls = len(fake.calls)
    market = ledger_market(prices, ledger, "2026-09-21", "MXN")
    again = situation.build(snapshot, ledger, date(2026, 9, 21), market=market)
    assert again["net_worth"]["total"] == nw["total"] and len(fake.calls) == calls

    # The You page overview reads the same valued picture and names the price sources.
    view = profile.profile_view(service, "ana", today=date(2026, 9, 21))
    assert view["overview"]["net_worth"] == nw["total"]
    assert view["overview"]["price_sources"] and view["overview"]["stale_prices"] == []

    # Proactive rules (harvest, drift, concentration) see the valued ledger positions and cite the prices.
    today = service.run("today", {"as_of": "2026-09-21"}, "ana")
    assert today["status"] == "ready" and today["result"]["stale_prices"] == []
    assert any(s.get("kind") == "market_price" for s in today["sources"])

    # Ten days on with no new closes: still valued, and the prices are listed as stale.
    later = service.situation("ana", today=date(2026, 10, 1))
    assert later["net_worth"]["unvalued_accounts"] == []
    assert {s["symbol"] for s in later["stale_prices"]} >= {"VOO", "AAPL *"}


def test_a_ledger_account_stays_unvalued_without_a_price(db, fake):
    service = WealthService(db, prices=provider(db, fake))
    service.create("ana", "Ana")
    other = {"id": "XYZ", "symbol": "XYZQ", "currency": "USD", "asset_class": "equity", "venue": "us"}
    _post(db, "ana", [IB], [other], [
        _opening("ib", instrument_id="XYZ", quantity="3", cost_basis="30", acquired_on="2025-01-02", currency="USD")])
    sit = service.situation("ana", today=date(2026, 9, 21))
    account = sit["accounts"][0]
    assert sit["net_worth"]["unvalued_accounts"] == ["Interactive Brokers"] and account["value"] is None
    assert account["unpriced"] == ["XYZQ"] and sit["market"]["missing"][0]["symbol"] == "XYZ"


IPS = {"version": 1, "decision_id": "ips-1", "accepted_on": "2026-03-01", "as_of": "2026-03-01", "currency": "USD",
       "constraints": {"leverage": {"allowed": False}}, "rebalancing": {"absolute_band": 0.05, "relative_band": 0.25},
       "review": {"cadence": "annual"}, "allocation": {"model": "balanced", "sleeves": [
    {"id": "global_equity", "name": "Global equity", "asset": "equity", "target": 0.6, "min": 0.5, "max": 0.7},
    {"id": "us_fixed_income", "name": "US fixed income", "asset": "fixed_income", "target": 0.4, "min": 0.3,
     "max": 0.5}]}}


def test_review_uses_provider_prices_so_net_worth_performance_and_allocation_are_ready(db, fake):
    prices = provider(db, fake)
    service = WealthService(db, prices=prices)
    service.create("bob", "Bob")
    _remember(service, "bob", [{"key": "client.profile", "value": {"reporting_currency": "USD"}},
                               {"key": "policy.ips", "value": IPS, "confidence": "confirmed"}])
    _post(db, "bob", [IB], [VOO, BND], [
        _opening("ib", instrument_id="VOO", quantity="6", cost_basis="2400", acquired_on="2025-01-02", currency="USD"),
        _opening("ib", instrument_id="BND", quantity="55", cost_basis="3900", acquired_on="2025-01-02", currency="USD"),
        _opening("ib", amount="100", currency="USD"),
        {"account_id": "ib", "kind": "deposit", "date": "2026-05-04", "amount": "500", "currency": "USD"}])

    report = service.run("quarterly_review", {"period_start": "2026-04-01", "period_end": "2026-06-30"}, "bob")
    sections = report["result"]["sections"]
    for name in ("net_worth", "performance", "allocation"):
        assert sections[name]["status"] == "ready", (name, sections[name]["missing"])
    nw = sections["net_worth"]["data"]
    voo, bnd = market_data()["VOO"][1], market_data()["BND"][1]
    opening = Decimal(str(voo["2026-03-31"])) * 6 + Decimal(str(bnd["2026-03-31"])) * 55 + 100
    assert Decimal(nw["start"]) == opening.quantize(Decimal("0.01"))
    perf = sections["performance"]["data"]
    assert perf["total"]["twr_period"] is not None
    ips_bench = perf["benchmarks"]["ips_benchmark"]
    assert ips_bench["period_return"] is not None and "proxy: ACWI" in ips_bench["name"] and "proxy: AGG" in ips_bench["name"]
    assert perf["benchmarks"]["global_60_40"]["period_return"] is not None
    assert sections["allocation"]["data"]["portfolio_value"] is not None
    market = report["result"]["market_data"]
    assert {p["symbol"] for p in market["proxies"]} == {"ACWI", "AGG", "BNDW"}
    assert any(s.get("kind") == "market_price" and s["title"] == "fake Yahoo daily close of VOO" for s in report["sources"])
    assert market["stale_prices"] == []

    view = profile.review_view(service, "bob", "2026-Q2", today=date(2026, 9, 21))
    assert view["sections"]["net_worth"]["note"] is None and view["sections"]["performance"]["portfolio"]["v"] is not None
    assert view["sections"]["performance"]["benchmark"]["parts"]
    assert view["market_data"]["proxies"] and view["stale_prices"] == []


def test_review_without_a_published_cetes_rate_leaves_that_benchmark_missing(db, fake):
    prices = provider(db, fake)
    from wealth.prices import benchmarks
    mx_ips = {"allocation": {"sleeves": [
        {"id": "global_equity", "asset": "equity", "target": 0.6},
        {"id": "mx_fixed_income", "asset": "fixed_income", "target": 0.4}]}}
    got = benchmarks(prices, mx_ips, "MXN", "2026-03-31", "2026-06-30", budget=5)
    assert "global_equity" in got["benchmarks"]["ips"] and "mx_fixed_income" not in got["benchmarks"]["ips"]
    assert any(m["key"] == "benchmarks.ips.mx_fixed_income" and "CETES" in m["reason"] for m in got["missing"])
    equity = got["benchmarks"]["ips"]["global_equity"]
    assert equity["proxy"] and equity["name"].endswith("in MXN)")
    # ACWI in USD converted at the daily USDMXN rate
    first = equity["series"][0]
    acwi = Decimal(str(market_data()["ACWI"][1][first["date"]])) * Decimal("1.01")
    fx = Decimal(str(market_data()["USDMXN=X"][1][first["date"]])) * Decimal("1.01")
    assert abs(Decimal(first["value"]) - acwi * Decimal(str(float(fx) / 1.01))) < Decimal("0.001")
    with_rate = benchmarks(prices, mx_ips, "MXN", "2026-03-31", "2026-06-30",
                           cetes_rate={"rate": "0.072", "source": "Banxico, CETES 28 days"}, budget=5)
    assert with_rate["benchmarks"]["ips"]["mx_fixed_income"]["annual_rate"] == "0.072"


def test_rebalance_and_dca_backtest_get_provider_prices_by_default(db, fake):
    service = WealthService(db, prices=provider(db, fake))
    service.create("ana", "Ana")
    _post(db, "ana", [IB], [VOO], [
        _opening("ib", instrument_id="VOO", quantity="2", cost_basis="800", acquired_on="2025-01-02", currency="USD")])
    with WealthStore(db) as store:
        ledger = store.ledger("ana")
    context: dict = {"ledger": ledger}
    inputs, note = service._task_prices("rebalance", {"as_of": "2026-09-21", "currency": "MXN",
                                                      "jurisdiction_context": {"jurisdiction": "MX"}}, context, ledger)
    assert inputs["prices"]["VOO"][0]["date"] == "2026-09-18" and note["prices"][0]["source"].endswith("of VOO")
    assert any(r["source"].endswith("USDMXN=X") for r in context["ledger"]["fx"])      # FX for to_household
    explicit = {"as_of": "2026-09-21", "prices": {"VOO": [{"date": "2026-09-21", "price": 1}]}}
    assert service._task_prices("rebalance", explicit, {}, ledger) == (explicit, None)  # supplied prices win
    plan = {"id": "p", "cadence": "monthly", "start": "2026-04-01", "currency": "USD",
            "legs": [{"instrument_id": "VOO", "amount": 100}]}
    inputs, note = service._task_prices("dca", {"view": "backtest", "plan": plan, "start": "2026-04-01",
                                                "end": "2026-06-30"}, {}, ledger)
    assert inputs["prices"]["VOO"][0]["date"] <= "2026-04-01" and note["prices"][0]["last"] == "2026-06-30"


def test_prices_operation_reports_status_and_refreshes(db, fake):
    prices = provider(db, fake)
    service = WealthService(db, prices=prices)
    service.create("ana", "Ana")
    _post(db, "ana", [IB], [VOO], [
        _opening("ib", instrument_id="VOO", quantity="1", cost_basis="400", acquired_on="2025-01-02", currency="USD")])
    refreshed = service.prices("refresh", client_id="ana")
    assert [r["provider_symbol"] for r in refreshed["refreshed"]] == ["VOO"]
    assert refreshed["status"]["symbols"][0]["symbol"] == "VOO"
    status = dispatch("prices", {"action": "status"}, db)       # the CLI path: offline in tests, cache still read
    assert status["offline"] and status["symbols"][0]["last"] == "2026-09-18"
    with pytest.raises(ValueError, match="status or refresh"):
        service.prices("delete")
