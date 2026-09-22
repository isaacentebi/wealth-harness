"""Reference rates (wealth/rates.py): parsing real provider shapes, the cache, fallbacks and every consumer.

No test reaches the network: providers are fakes serving trimmed real responses from tests/fixtures/rates
(Treasury Fiscal Data, TreasuryDirect and Banxico's CETES 28 indicator, fetched 2026-09-22; the SIE reply is
the documented SIE shape carrying the values of Banxico table CF107 that day, as no SIE token was available).
"""
from __future__ import annotations

import copy
import json
import threading
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from wealth import proactive, rates, situation
from wealth.catalog import CATALOG
from wealth.prices import PriceProvider
from wealth.service import WealthService, dispatch
from wealth.store import WealthStore

FIX = Path(__file__).parent / "fixtures" / "rates"
TODAY = date(2026, 9, 22)
NOW = datetime(2026, 9, 22, 18, 0, tzinfo=timezone.utc)


def fixture(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


class Fake:
    """A transport answering by URL prefix; records every request."""

    def __init__(self, routes=None, gate: threading.Event | None = None):
        self.routes = routes if routes is not None else {
            rates.FISCAL_DATA_URL: fixture("fiscaldata_bills.json"),
            rates.TREASURYDIRECT_URL: fixture("treasurydirect_bills.json"),
            rates.BANXICO_INDICATOR_URL: fixture("banxico_single_cetes28.json"),
            "https://www.banxico.org.mx/SieAPIRest/": fixture("banxico_sie_oportuno.json"),
        }
        self.calls: list[tuple[str, str, dict, float]] = []
        self.gate = gate

    def __call__(self, method, url, headers, timeout):
        self.calls.append((method, url, dict(headers), timeout))
        if self.gate is not None:
            self.gate.wait(5)
        for prefix, reply in self.routes.items():
            if url.startswith(prefix):
                if isinstance(reply, Exception):
                    raise reply
                if isinstance(reply, tuple):
                    return reply
                return 200, {"content-type": "application/json"}, json.dumps(reply).encode()
        raise AssertionError(f"unexpected request {url}")


class Token:
    def __init__(self, value):
        self.value = value

    def reveal(self):
        return self.value


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "w.sqlite3"
    with WealthStore(path):
        pass
    return path


def seed(db, group="mx", now=NOW, fake=None, token=None):
    return rates.refresh_group(db, group, transport=fake or Fake(), now=now, token_loader=lambda: token)


# ------------------------------------------------------------------ parsing real shapes


def test_fiscal_data_takes_the_latest_auction_with_a_published_rate_per_term():
    payload = fixture("fiscaldata_bills.json")
    got = rates.parse_fiscal_data(payload, TODAY)
    assert {k: (float(v["rate"]), v["as_of"]) for k, v in got.items()} == {
        "us_tbill_13w": (0.04113, "2026-09-21"), "us_tbill_26w": (0.04303, "2026-09-21"),
        "us_tbill_52w": (0.04161, "2026-09-01")}
    assert "13-Week bill auction of 2026-09-21, high investment rate 4.113%" in got["us_tbill_13w"]["source"]
    assert rates.FISCAL_DATA_PAGE in got["us_tbill_13w"]["source"]
    # An announced auction not yet held has "null" rates (as Fiscal Data sends them); a CMB is not a bill series.
    upcoming = {**payload["data"][0], "auction_date": "2026-09-28", "high_investment_rate": "null",
                "high_discnt_rate": "null", "cusip": "912797XX0"}
    cmb = {**payload["data"][0], "auction_date": "2026-09-25", "high_investment_rate": "5.000000",
           "cash_management_bill_cmb": "Yes"}
    again = rates.parse_fiscal_data({**payload, "data": [upcoming, cmb, *payload["data"]]}, TODAY)
    assert again["us_tbill_13w"]["as_of"] == "2026-09-21" and again["us_tbill_13w"]["rate"] == Decimal("0.04113")
    with pytest.raises(rates.RateError):
        rates.parse_fiscal_data({"error": "bad filter"}, TODAY)


def test_treasurydirect_backup_reads_the_same_auction():
    got = rates.parse_treasurydirect(fixture("treasurydirect_bills.json"), TODAY)
    assert (got["us_tbill_13w"]["rate"], got["us_tbill_13w"]["as_of"]) == (Decimal("0.04113"), "2026-09-21")
    assert got["us_tbill_26w"]["rate"] == Decimal("0.04303") and "us_tbill_52w" not in got  # none in 35 days
    assert "TreasuryDirect" in got["us_tbill_13w"]["source"]


def test_banxico_indicator_is_dated_by_the_auction():
    got = rates.parse_banxico_indicator(fixture("banxico_single_cetes28.json"), TODAY)["mx_cetes_28d"]
    assert (got["rate"], got["as_of"]) == (Decimal("0.0615"), "2026-09-22")
    assert "Banco de México" in got["source"] and "subasta primaria del 2026-09-22 (6.15%)" in got["source"]
    for bad in ({"valor": "N/E", "fecha": "22 - SEP - 2026"}, {"valor": "6.15", "fecha": "22 - XYZ - 2026"},
                {"valor": "6.15", "fecha": "01 - ENE - 2031"}, ["6.15"]):
        with pytest.raises(rates.RateError):
            rates.parse_banxico_indicator(bad, TODAY)


def test_sie_series_skip_unpublished_values():
    got = rates.parse_sie(fixture("banxico_sie_oportuno.json"), TODAY)
    assert {k: (float(v["rate"]), v["as_of"]) for k, v in got.items()} == {
        "mx_cetes_28d": (0.0615, "2026-09-24"), "mx_cetes_91d": (0.0659, "2026-09-24"),
        "mx_cetes_182d": (0.0691, "2026-09-24"), "mx_cetes_364d": (0.0724, "2026-09-17")}
    assert "serie SF43945" in got["mx_cetes_364d"]["source"] and "liquidación" in got["mx_cetes_364d"]["source"]


# ------------------------------------------------------------------ fetching


def test_us_falls_back_to_treasurydirect_when_fiscal_data_fails():
    fake = Fake({rates.FISCAL_DATA_URL: (503, {}, b"busy"),
                 rates.TREASURYDIRECT_URL: fixture("treasurydirect_bills.json")})
    found, errors = rates.fetch_us(fake, TODAY)
    assert found["us_tbill_13w"]["as_of"] == "2026-09-21" and "TreasuryDirect" in found["us_tbill_13w"]["source"]
    assert errors == ["api.fiscaldata.treasury.gov answered HTTP 503"]
    fiscal = fake.calls[0]
    assert fiscal[0] == "GET" and fiscal[3] == rates.FISCAL_DATA_TIMEOUT
    assert "security_term:in:(13-Week,26-Week,52-Week)" in fiscal[1] and "page%5Bsize%5D=100" in fiscal[1]
    assert fake.calls[1][3] == rates.TIMEOUT_SECONDS
    # All three terms from Fiscal Data: TreasuryDirect is not asked.
    fake = Fake()
    found, errors = rates.fetch_us(fake, TODAY)
    assert set(found) == {"us_tbill_13w", "us_tbill_26w", "us_tbill_52w"} and not errors and len(fake.calls) == 1


def test_mexico_works_without_a_token_and_sends_one_only_as_a_header():
    fake = Fake()
    found, errors = rates.fetch_mx(fake, TODAY, None)
    assert set(found) == {"mx_cetes_28d"} and not errors
    assert [c[1] for c in fake.calls] == [rates.BANXICO_INDICATOR_URL]
    fake = Fake()
    found, _ = rates.fetch_mx(fake, TODAY, Token("tok-123"))
    assert found["mx_cetes_28d"]["as_of"] == "2026-09-22"  # the auction-dated indicator wins over SIE's settlement date
    assert found["mx_cetes_91d"]["rate"] == Decimal("0.0659")
    sie = fake.calls[1]
    assert sie[2]["Bmx-Token"] == "tok-123" and "tok-123" not in sie[1]
    assert "SF43936,SF43939,SF43942,SF43945" in sie[1]


def test_a_failure_message_never_carries_the_token():
    fake = Fake({rates.BANXICO_INDICATOR_URL: fixture("banxico_single_cetes28.json"),
                 "https://www.banxico.org.mx/SieAPIRest/": OSError("refused Bmx-Token: tok-123 for this client")})
    found, errors = rates.fetch_mx(fake, TODAY, Token("tok-123"))
    assert set(found) == {"mx_cetes_28d"} and errors and not any("tok-123" in e for e in errors)


def test_the_token_comes_from_the_environment_or_a_read_only_keychain_lookup(monkeypatch):
    monkeypatch.undo()  # the real loader, with a fake runner
    assert rates.banxico_token(environ={"BANXICO_TOKEN": " abc "}).reveal() == "abc"
    seen = []

    class Done:
        returncode, stdout = 0, "from-keychain\n"

    def runner(command, **kw):
        seen.append(command)
        return Done()
    token = rates.banxico_token(environ={}, runner=runner, platform="darwin")
    assert token.reveal() == "from-keychain" and token.source == "keychain" and "from-keychain" not in repr(token)
    assert seen == [["security", "find-generic-password", "-s", "wealth-banxico", "-w"]]

    class Missing:
        returncode, stdout = 44, ""
    assert rates.banxico_token(environ={}, runner=lambda *a, **k: Missing(), platform="darwin") is None


# ------------------------------------------------------------------ cache, freshness, fallbacks


def test_refresh_writes_dated_sourced_rows_and_reference_reads_them(db):
    report = seed(db)
    assert report["fetched"] == {"mx_cetes_28d": {"rate": "0.0615", "as_of": "2026-09-22"}} and not report["failed"]
    ref = rates.reference("MXN", db_path=db, on=TODAY, refresh=False)
    assert (ref["rate"], ref["percent"], ref["as_of"], ref["origin"]) == ("0.0615", "6.15", "2026-09-22", "fetched")
    assert ref["fetched_at"] == NOW.isoformat(timespec="seconds") and "Banco de México" in ref["source"]
    assert ref["stale"] is False and ref["note"] is None
    with WealthStore(db) as store:
        assert [r["value"] for r in store.market_rows("rate:mx_cetes_28d", "close")] == ["0.0615"]
    seed(db, "us")
    usd = rates.reference("USD", db_path=db, on=TODAY, refresh=False)
    assert (usd["rate"], usd["as_of"], usd["series"]) == ("0.04113", "2026-09-21", "us_tbill_13w")
    assert rates.reference("USD", db_path=db, on=TODAY, series="us_tbill_52w", refresh=False)["as_of"] == "2026-09-01"


def test_a_failed_fetch_is_logged_and_the_builtin_value_is_served(db):
    report = seed(db, fake=Fake({rates.BANXICO_INDICATOR_URL: (500, {}, b"")}))
    assert report["failed"] == ["mx_cetes_28d"] and "HTTP 500" in report["errors"][0]
    with WealthStore(db) as store:
        log = store.market_fetches("rate:mx_cetes_28d", "close")
    assert log[0]["status"] == "failed" and "HTTP 500" in log[0]["detail"]
    ref = rates.reference("MXN", db_path=db, on=TODAY, refresh=False)
    assert ref["origin"] == "builtin" and ref["as_of"] == rates.CETES_28D_REFERENCE["as_of"]
    assert "Built-in dated value" in ref["note"] and ref["checked_on"] == "2026-09-21"


def _scheduled(db, fake, on=TODAY):
    ref = rates.reference("MXN", db_path=db, on=on, offline=False, transport=fake)
    rates.wait(5)
    return ref, len(fake.calls)


def test_a_series_is_fetched_at_most_once_a_day(db):
    seed(db, now=datetime.now(timezone.utc) - timedelta(hours=2))
    assert _scheduled(db, Fake())[1] == 0  # fetched two hours ago: fresh
    older = db.parent / "older.sqlite3"
    seed(older, now=datetime.now(timezone.utc) - timedelta(hours=25))
    assert _scheduled(older, Fake())[1] == 1  # a day old: refreshed once, in the background
    assert _scheduled(older, Fake())[1] == 0  # and not again


def test_a_failed_attempt_waits_three_hours_before_retrying(db):
    down = Fake({rates.BANXICO_INDICATOR_URL: (500, {}, b"")})
    seed(db, fake=down, now=datetime.now(timezone.utc) - timedelta(hours=1))
    assert _scheduled(db, Fake())[1] == 0
    later = db.parent / "later.sqlite3"
    seed(later, fake=down, now=datetime.now(timezone.utc) - timedelta(hours=4))
    ref, calls = _scheduled(later, Fake())
    assert calls == 1 and rates.reference("MXN", db_path=later, on=TODAY, refresh=False)["origin"] == "fetched"


def test_offline_never_touches_the_network(db, monkeypatch):
    fake = Fake()
    ref = rates.reference("MXN", db_path=db, on=TODAY, transport=fake)  # conftest sets WEALTH_OFFLINE=1
    rates.wait(5)
    assert not fake.calls and ref["origin"] == "builtin" and "WEALTH_OFFLINE is set" in ref["note"]
    assert rates.refresh(db, transport=fake)["offline"] is True and not fake.calls
    assert dispatch("rates", {"action": "refresh"}, db)["offline"] is True


def test_background_refresh_never_blocks_the_caller(db):
    gate = threading.Event()
    fake = Fake(gate=gate)
    started = time.monotonic()
    ref = rates.reference("MXN", db_path=db, on=TODAY, offline=False, transport=fake)
    assert time.monotonic() - started < 1.0
    assert ref["origin"] == "builtin" and ref["refreshing"] is True and "a refresh is running" in ref["note"]
    gate.set()
    rates.wait(5)
    after = rates.reference("MXN", db_path=db, on=TODAY, offline=False, transport=fake)
    assert after["origin"] == "fetched" and after["as_of"] == "2026-09-22" and after["refreshing"] is False
    assert len(fake.calls) == 1


def test_stale_by_auction_date(db):
    seed(db)
    later = rates.reference("MXN", db_path=db, on=date(2026, 11, 1), refresh=False)
    assert later["origin"] == "fetched" and later["stale"] is True and later["age_days"] == 40
    assert "(40 days old)" in later["note"]
    builtin = rates.reference("USD", on=date(2026, 12, 1))
    assert builtin["origin"] == "builtin" and builtin["stale"] is True and "days old" in builtin["note"]


def test_fallback_order_saved_fact_then_fetched_then_builtin(db):
    fact = {"id": "f9", "key": "cash_reference_rate", "source": {"kind": "user", "observed_on": "2026-09-20"},
            "value": {"low": "7.0", "high": "7.5", "unit": "percent", "source": "CETES 28 dias, mi banco",
                      "currency": "MXN"}}
    seed(db)
    saved = rates.reference("MXN", fact=fact, db_path=db, on=TODAY, refresh=False)
    assert (saved["origin"], saved["rate"], saved["low"], saved["high"]) == ("saved_fact", "0.07", "0.07", "0.075")
    assert saved["as_of"] == "2026-09-20" and saved["fact_id"] == "f9"
    # A fact in another currency does not apply; an incomplete one is set aside, and says so.
    assert rates.reference("USD", fact=fact, db_path=db, on=TODAY, refresh=False)["origin"] == "builtin"
    broken = copy.deepcopy(fact)
    del broken["value"]["source"]
    fetched = rates.reference("MXN", fact=broken, db_path=db, on=TODAY, refresh=False)
    assert fetched["origin"] == "fetched" and "not used" in fetched["ignored"]
    # A cached value older than the built-in one loses to it; nothing for a currency without a series.
    old = db.parent / "old.sqlite3"
    seed(old, now=datetime(2026, 8, 1, tzinfo=timezone.utc),
         fake=Fake({rates.BANXICO_INDICATOR_URL: {"valor": "7.01", "fecha": "28 - JUL - 2026"}}))
    assert rates.reference("MXN", db_path=old, on=TODAY, refresh=False)["origin"] == "builtin"
    assert rates.reference("EUR", db_path=db) is None


def test_the_price_cache_leaves_rate_rows_alone(db):
    seed(db)
    provider = PriceProvider(db, offline=True)
    assert provider.status()["symbols"] == []
    calls = []
    provider = PriceProvider(db, offline=False, fetcher=lambda *a: calls.append(a))
    assert provider.refresh()["refreshed"] == [] and not calls


# ------------------------------------------------------------------ consumers


MX = {"name": "Lucía", "residence": {"country": "MX"}, "tax_residence": ["MX"], "language": "es"}


def _idle(facts, db, as_of="2026-09-22"):
    rows = [{"id": f"f{i}", "key": k, "value": v, "confidence": "reported", "status": "active", "revision": i,
             "source": {"kind": "user", "ref": "test", "observed_on": "2026-09-01"}} for i, (k, v) in enumerate(facts, 1)]
    snap = {"client": {"id": "c", "revision": len(rows)}, "facts": rows, "decisions": []}
    sit = situation.build(snap, None, as_of)
    found = proactive.evaluate(sit, None, snap, as_of, rates_db=db)
    return next(i for i in found["candidates"] if i["kind"] == "idle_yield")


def test_idle_yield_uses_the_fetched_cetes_rate_with_its_date_and_source(db):
    seed(db)
    facts = [("client.profile", MX), ("spending.monthly", {"essential": 20000, "currency": "MXN"}),
             ("cash.nu", {"amount": 400000, "currency": "MXN", "purpose": "reserve"}), ("reserve", {"target_months": 6})]
    item = _idle(facts, db)
    data = item["data"]
    assert (data["reference_rate"], data["reference_as_of"], data["reference_stale"]) == (0.0615, "2026-09-22", False)
    assert data["reference_origin"] == f"fetched {NOW.date()}" and "Banco de México" in data["reference_source"]
    assert data["lost_per_year"] == 17220  # 280,000 x 6.15%
    assert "(al 2026-09-22)" in item["why"]["es"] and "(as of 2026-09-22)" in item["why"]["en"]
    assert data["offer"]["rungs"][0]["cashflows"][0]["amount"] == pytest.approx(70000 * (1 + 0.0615 * 28 / 360), abs=0.01)
    # A small weekly move keeps the fingerprint, so a dismissed nudge stays dismissed.
    with WealthStore(db) as store:
        store.put_market([{"symbol": "rate:mx_cetes_28d", "kind": "close", "date": "2026-09-29", "value": "0.0612",
                           "currency": "MXN", "source": "Banxico test", "retrieved_at": "2026-09-29T18:00:00+00:00"}],
                         {"symbol": "rate:mx_cetes_28d", "kind": "close", "start": "2026-09-29", "end": "2026-09-29",
                          "retrieved_at": "2026-09-29T18:00:00+00:00", "status": "ok"})
    moved = _idle(facts, db, "2026-09-29")
    assert moved["data"]["reference_rate"] == 0.0612 and moved["fingerprint"] == item["fingerprint"]


def test_debt_prepay_vs_invest_uses_the_fetched_t_bill(db):
    seed(db, "us")
    inputs = {k: v for k, v in CATALOG["debt"]["variants"]["us_mortgage_prepay_vs_vti"].items() if k != "risk_free"}
    report = WealthService(db).run("debt", {**inputs, "as_of": "2026-09-22"})
    risk_free = report["result"]["investing"]["risk_free"]
    assert risk_free["rate"] == pytest.approx(0.0411, abs=1e-4) and risk_free["as_of"] == "2026-09-21"
    assert risk_free["origin"] == "fetched" and risk_free["stale"] is False and "Fiscal Data" in risk_free["source"]
    assert not any("built-in" in a for a in report["assumptions"])
    builtin = WealthService(db.parent / "empty.sqlite3").run("debt", {**inputs, "as_of": "2026-09-22"})
    assert builtin["result"]["investing"]["risk_free"]["origin"] == "builtin"
    assert any("built-in dated value" in a for a in builtin["assumptions"])


def test_service_risk_free_prefers_the_saved_rate_and_cites_it(db):
    seed(db)
    service = WealthService(db)
    service.create("ana", "Ana")
    service.remember("ana", [
        {"key": "client.profile", "value": MX, "source": {"kind": "user", "ref": "chat", "observed_on": "2026-09-20"}},
        {"key": "liability.tarjeta", "value": {"kind": "card", "balance": 25000, "currency": "MXN", "annual_rate": 0.45,
                                               "payment": 3000, "payment_frequency": "monthly"},
         "source": {"kind": "user", "ref": "chat", "observed_on": "2026-09-20"}}])
    live = service.run("debt", {"mode": "prepay_vs_invest", "lump_sum": 25000, "as_of": "2026-09-22"}, client_id="ana")
    assert live["result"]["investing"]["risk_free"]["rate"] == pytest.approx(0.0615, abs=1e-4)
    assert live["result"]["investing"]["risk_free"]["as_of"] == "2026-09-22"
    service.remember("ana", [{"key": "cash_reference_rate",
                              "value": {"low": 7, "high": 7, "unit": "percent", "source": "CETES en mi banco",
                                        "currency": "MXN"},
                              "source": {"kind": "user", "ref": "chat", "observed_on": "2026-09-21"}}])
    saved = service.run("debt", {"mode": "prepay_vs_invest", "lump_sum": 25000, "as_of": "2026-09-22"}, client_id="ana")
    risk_free = saved["result"]["investing"]["risk_free"]
    assert (risk_free["rate"], risk_free["origin"], risk_free["source"]) == (0.07, "saved_fact", "CETES en mi banco")
    with WealthStore(db) as store:
        fact_id = next(f["id"] for f in store.snapshot("ana")["facts"] if f["key"] == "cash_reference_rate")
    assert fact_id in saved["evidence_ids"]


def test_fee_audit_prices_idle_cash_at_the_reference_rate(db):
    seed(db, "us")
    variant = {k: v for k, v in CATALOG["fee_audit"]["variants"]["holdings_only"].items() if k != "cash_reference_rate"}
    report = WealthService(db).run("fee_audit", {**variant, "as_of": "2026-09-22"})
    drag = next(c for c in report["result"]["components"] if c["id"] == "cash_drag")
    assert drag["complete"] and float(drag["annual_high"]) == pytest.approx(5000 * 0.04113, abs=0.01)
    source = next(s for s in report["sources"] if s["title"] == "Cash reference rate")
    assert source["date"] == "2026-09-21" and "4.113% as of 2026-09-21 (fetched" in source["ref"]


def test_reference_rates_task_and_rates_command(db):
    seed(db)
    seed(db, "us")
    report = WealthService(db).run("reference_rates", {"as_of": "2026-09-22"})
    assert report["status"] == "ready"
    by = {r["currency"]: r for r in report["result"]["rates"]}
    assert (by["MXN"]["percent"], by["MXN"]["as_of"]) == ("6.15", "2026-09-22")
    assert (by["USD"]["percent"], by["USD"]["as_of"]) == ("4.113", "2026-09-21")
    assert {t["series"] for t in report["result"]["other_tenors"]} == {"us_tbill_26w", "us_tbill_52w"}
    assert {s["date"] for s in report["sources"]} == {"2026-09-21", "2026-09-22", "2026-09-01"}
    only = WealthService(db).run("reference_rates", {"currency": "usd", "as_of": "2026-09-22"})
    assert [r["currency"] for r in only["result"]["rates"]] == ["USD"]
    with pytest.raises(ValueError, match="MXN or USD"):
        WealthService(db).run("reference_rates", {"currency": "EUR"})
    status = dispatch("rates", {"action": "status"}, db)
    rows = {r["series"]: r for r in status["rates"]}
    assert rows["mx_cetes_28d"]["origin"] == "fetched" and rows["mx_cetes_91d"]["rate"] is None
    assert "SIE token" in rows["mx_cetes_91d"]["note"]
