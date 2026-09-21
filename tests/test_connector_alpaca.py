"""Alpaca Trading API connector: fetch, map, reconcile, read-only, confirm, re-sync.  No network.

Fixtures are fictional and follow the documented Alpaca v2 schemas (see
``wealth/connectors/alpaca.py`` for the sources).
"""
from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import pickle
import traceback
from types import SimpleNamespace
import urllib.parse

import pytest

from wealth import connectors
from wealth.connectors import _rest, alpaca
from wealth.connectors._rest import ConnectorError, ReadOnlyViolation, Secret
from wealth.connectors.alpaca import AlpacaConnector, AlpacaKeys, fetch_snapshot, load_keys, proposal_from_alpaca
from wealth.service import WealthService, dispatch
from wealth.store import WealthStore


FIXTURES = Path(__file__).parent / "fixtures" / "alpaca"
KEY_ID = "AKFICTIONAL0KEY0ID42"
SECRET = "fictionalSecret0123456789abcdefGHIJKLmnop"
TODAY = date(2026, 9, 18)
SINCE = date(2026, 1, 1)


def _load(name):
    return json.loads((FIXTURES / name).read_text())


def _keys():
    return AlpacaKeys(Secret(KEY_ID, "env", "Alpaca key id"), Secret(SECRET, "env", "Alpaca secret"))


class FakeAlpaca:
    """Serves the fixtures like the Trading API, paging activities by page_token/page_size."""

    def __init__(self, **overrides):
        self.data = {"account": _load("account.json"), "positions": _load("positions.json"),
                     "activities": _load("activities.json"), "assets": _load("assets.json"), **_load("extras.json")}
        self.data.update(overrides)
        self.calls: list[tuple[str, str, dict]] = []
        self.headers: list[dict] = []
        self.script: dict[str, list] = {}
        self.sleeps: list[float] = []
        self.now = 0.0

    def transport(self, method, url, headers, timeout):
        parsed = urllib.parse.urlsplit(url)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        self.calls.append((method, parsed.path, query))
        self.headers.append(dict(headers))
        assert method == "GET", f"non-GET request {method} {parsed.path}"
        assert parsed.path in {"/v2/account", "/v2/positions", "/v2/orders", "/v2/account/activities",
                               "/v2/account/portfolio/history"} or parsed.path.startswith("/v2/assets/"), parsed.path
        queued = self.script.get(parsed.path)
        if queued:
            answer = queued.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        path = parsed.path
        if path == "/v2/account":
            body = self.data["account"]
        elif path == "/v2/positions":
            body = self.data["positions"]
        elif path == "/v2/orders":
            body = self.data["orders"]
        elif path == "/v2/account/portfolio/history":
            body = self.data["portfolio_history"]
        elif path.startswith("/v2/assets/"):
            symbol = path.rsplit("/", 1)[1]
            if symbol not in self.data["assets"]:
                return 404, {}, b'{"code": 40410000, "message": "asset not found"}'
            body = self.data["assets"][symbol]
        else:
            items = sorted(self.data["activities"], key=lambda a: a["id"])
            if query.get("page_token"):
                items = [a for a in items if a["id"] > query["page_token"]]
            body = items[:int(query.get("page_size", 100))]
        return 200, {"content-type": "application/json"}, json.dumps(body).encode()

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def clock(self):
        return self.now

    def connector(self, **kwargs):
        return AlpacaConnector(keys=_keys(), transport=self.transport, sleep=self.sleep, clock=self.clock, today=TODAY,
                               since=kwargs.pop("since", SINCE.isoformat()), **kwargs)


def _snapshot(fake=None):
    fake = fake or FakeAlpaca()
    api = alpaca.client(_keys(), paper=False, transport=fake.transport, sleep=fake.sleep, clock=fake.clock)
    return fetch_snapshot(api, since=SINCE, today=TODAY)


def _proposal(fake=None, **kwargs):
    return proposal_from_alpaca(_snapshot(fake), retrieved_at="2026-09-18T21:00:00+00:00", **kwargs)


def _by_id(rows):
    return {row["id"]: row for row in rows}


# -- mapping ------------------------------------------------------------------

def test_positions_keep_average_cost_fractional_quantities_and_instrument_facts():
    proposal = _proposal()
    result = proposal["result"]
    [account] = result["household"]["accounts"]
    assert account == {"id": "alpaca-5432", "owner_id": "self", "type": "brokerage", "currency": "USD",
                       "name": "Alpaca brokerage account", "institution": "Alpaca", "number_masked": "****5432"}
    positions = _by_id(result["household"]["positions"])
    voo, aapl, asml = positions["alpaca-5432:VOO"], positions["alpaca-5432:AAPL"], positions["alpaca-5432:ASML"]
    assert (voo["quantity"], voo["value"], voo["cost_basis"], aapl["quantity"]) == ("10.5", "5775", "5040", "3.25")
    assert (voo["venue"], voo["listing_exchange"], voo["asset_class"], voo["cusip"]) == ("us", "ARCA", "fund", "922908363")
    assert (aapl["asset_class"], aapl["venue"]) == ("equity", "us")
    assert "issuer_domicile" not in voo  # a numeric CUSIP may be US or Canadian: not guessed
    assert asml["issuer_domicile"] == "other"  # CINS CUSIP (N...) = non-North-American issuer
    for position in (voo, aapl, asml):
        assert (position["cost_basis_method"], position["lots"], position["sic_listed"]) == \
            ("average_entry", "unavailable", "unknown")
    assert result["household"]["lots"] == []
    assert any("average" in a and "lots" in a for a in proposal["assumptions"])


def test_sic_listing_is_recorded_only_when_provided():
    positions = _by_id(_proposal(sic_listed={"VOO": True})["result"]["household"]["positions"])
    assert positions["alpaca-5432:VOO"]["sic_listed"] is True
    assert positions["alpaca-5432:AAPL"]["sic_listed"] == "unknown"


def test_positions_plus_cash_reconcile_to_equity_and_nav_is_asserted():
    proposal = _proposal()
    result = proposal["result"]
    assert proposal["status"] == "ready_to_confirm"
    [recon] = result["reconciliation"]["accounts"]
    assert (recon["positions_value"], recon["cash"], recon["computed_total"], recon["reported_total"], recon["status"]) == \
        ("8022.5", {"USD": "2234.56"}, "10257.06", "10257.06", "reconciled")
    [nav] = result["balance_assertions"]
    assert (nav["balance_kind"], nav["opening"], nav["closing"], nav["period_start"], nav["period_end"]) == \
        ("nav", "4100", "10257.06", "2026-01-01", "2026-09-18")
    assert any("open Alpaca order" in w for w in proposal["warnings"])


def test_equity_that_disagrees_needs_review_and_moving_prices_widen_tolerance():
    account = {**_load("account.json"), "equity": "10357.06"}
    wrong = _proposal(FakeAlpaca(account=account))
    assert wrong["status"] == "needs_review"
    assert wrong["result"]["reconciliation"]["accounts"][0]["difference"] == "-100"
    fake = FakeAlpaca()
    fake.script["/v2/account"] = [
        (200, {}, json.dumps(_load("account.json")).encode()),
        (200, {}, json.dumps({**_load("account.json"), "equity": "10258.06"}).encode())]
    moving = _proposal(fake)
    assert moving["status"] == "ready_to_confirm"
    assert any("moved by 1" in w for w in moving["warnings"])


def test_fills_dividends_nra_withholding_cash_fees_and_interest_map_to_ledger_kinds():
    transactions = _by_id(_proposal()["result"]["transactions"])
    ids = {t["activity_type"] if "activity_type" in t else t["type"]: t for t in transactions.values()}
    buy = transactions["ALPACA-A20260106103000123_1a2b3c4d000240008000000000000002"]
    assert (buy["type"], buy["amount"], buy["quantity"], buy["price"], buy["symbol"], buy["date"]) == \
        ("buy", "-2695", "5.5", "490", "VOO", "2026-01-06")
    assert buy["instrument"]["venue"] == "us"
    partial = transactions["ALPACA-A20260203145501000_1a2b3c4d000340008000000000000003"]
    assert (partial["quantity"], partial["amount"]) == ("1.25", "-250") and "partial fill" in partial["description"]
    sell = transactions["ALPACA-A20260410133000000_1a2b3c4d000840008000000000000008"]
    assert (sell["type"], sell["amount"], sell["quantity"], sell["symbol"]) == ("sell", "450", "1.5", "TSLA")
    kinds = {t["activity_type"]: (t["type"], t["amount"], t.get("symbol")) for t in transactions.values()
             if t.get("activity_type")}
    assert kinds == {
        "CSD": ("deposit", "5000", None), "FEE": ("fee", "-0.02", None), "DIV": ("dividend", "15.75", "VOO"),
        "DIVNRA": ("tax_withheld", "-1.58", "VOO"), "INT": ("interest", "2.1", None),
        "JNLC": ("transfer", "-100", None), "CSW": ("withdrawal", "-500", None), "SC": ("corporate_action", None, "XYZ"),
    }
    assert "US withholding on dividend (NRA)" in ids["DIVNRA"]["description"]
    assert "not_posted_reason" in ids["SC"] and "not_posted_reason" not in ids["DIVNRA"]
    assert not any(t["id"].startswith("ALPACA-H") for t in transactions.values())


def test_negative_interest_is_a_fee_and_unmapped_or_wrong_sign_lines_are_explained():
    extra = [
        {"activity_type": "INT", "id": "20260901000000000::1a2b3c4d-0013-4000-8000-000000000013", "date": "2026-09-01",
         "net_amount": "-3.5", "description": "Margin interest"},
        {"activity_type": "DIVNRA", "id": "20260902000000000::1a2b3c4d-0014-4000-8000-000000000014", "date": "2026-09-02",
         "net_amount": "0.4", "symbol": "VOO"},
        {"activity_type": "WEIRD", "id": "20260903000000000::1a2b3c4d-0015-4000-8000-000000000015", "date": "2026-09-03",
         "net_amount": "1"},
    ]
    fake = FakeAlpaca(activities=_load("activities.json") + extra)
    rows = {t["date"]: t for t in _proposal(fake)["result"]["transactions"] if t["date"] >= "2026-09-01"}
    assert (rows["2026-09-01"]["type"], rows["2026-09-01"]["amount"]) == ("fee", "-3.5")
    assert "refund" in rows["2026-09-02"]["not_posted_reason"]
    assert "WEIRD" in rows["2026-09-03"]["not_posted_reason"]


# -- credentials and redaction ------------------------------------------------

def test_keys_never_appear_in_repr_pickle_connector_proposal_or_headers_outside_the_transport():
    keys = _keys()
    assert KEY_ID not in repr(keys) and SECRET not in repr(keys) and SECRET not in str(keys.secret)
    with pytest.raises(TypeError):
        pickle.dumps(keys)
    with pytest.raises(TypeError):
        pickle.dumps(keys.secret)
    fake = FakeAlpaca()
    connector = fake.connector()
    assert SECRET not in repr(connector) and SECRET not in repr(vars(connector))
    proposal = connector.proposal()
    text = json.dumps(proposal)
    assert KEY_ID not in text and SECRET not in text and "912765432" not in text
    # the keys travel only in the documented headers, never in a URL
    assert all(h["APCA-API-KEY-ID"] == KEY_ID and h["APCA-API-SECRET-KEY"] == SECRET for h in fake.headers)
    assert not any(SECRET in json.dumps(q) or KEY_ID in path for _, path, q in fake.calls)


def test_keys_come_from_env_then_keychain_without_a_shell():
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout={"key_id": KEY_ID, "secret": SECRET}[command[5]] + "\n")

    from_env = load_keys({"WEALTH_ALPACA_KEY_ID": "envkey", "WEALTH_ALPACA_SECRET": "envsecret"}, runner, "darwin")
    assert from_env.source == "env" and calls == []
    keys = load_keys({}, runner, "darwin")
    assert keys.source == "keychain" and keys.values() == (KEY_ID, SECRET)
    assert [c[0] for c in calls] == [["security", "find-generic-password", "-s", "wealth-alpaca", "-a", "key_id", "-w"],
                                      ["security", "find-generic-password", "-s", "wealth-alpaca", "-a", "secret", "-w"]]
    assert all("shell" not in kwargs for _, kwargs in calls)
    assert load_keys({"WEALTH_ALPACA_KEY_ID": "only-one"}, lambda *a, **k: SimpleNamespace(returncode=44, stdout=""),
                     "darwin") is None
    assert load_keys({}, runner, "win32") is None


def test_errors_and_tracebacks_never_hold_the_keys():
    def leaky(method, url, headers, timeout):
        raise RuntimeError(f"boom calling {url} with {headers}")

    api = alpaca.client(_keys(), paper=True, transport=leaky, sleep=lambda s: None, clock=lambda: 0.0)
    with pytest.raises(ConnectorError) as caught:
        api.get_json("/v2/account")
    rendered = "".join(traceback.format_exception(caught.value))
    assert KEY_ID not in rendered and SECRET not in rendered
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert _rest.scrub(f"https://x/?api_key={SECRET}&q=1") == "https://x/?api_key=****&q=1"
    assert _rest.scrub(f"APCA-API-SECRET-KEY: {SECRET}") == "APCA-API-SECRET-KEY: ****"

    fake = FakeAlpaca()
    fake.script["/v2/account"] = [(401, {}, json.dumps({"message": f"bad key {KEY_ID}"}).encode())]
    proposal = fake.connector().proposal()
    assert proposal["status"] == "rejected" and proposal["result"]["error"]["code"] == 401
    assert "paper and live keys differ" in proposal["warnings"][0]
    assert KEY_ID not in json.dumps(proposal)


def test_connector_without_keys_asks_for_setup_never_for_the_keys_in_chat():
    proposal = AlpacaConnector(key_loader=lambda: None, today=TODAY).proposal()
    assert proposal["status"] == "needs_input" and proposal["missing"][0]["key"] == "alpaca.keys"
    assert any("security add-generic-password -U -s wealth-alpaca -a key_id -w" in step
               for step in proposal["result"]["setup"])


# -- read-only enforcement ------------------------------------------------------

def test_a_full_sync_uses_only_get_on_the_allowlisted_read_paths():
    fake = FakeAlpaca()
    fake.connector().proposal()
    assert {method for method, _, _ in fake.calls} == {"GET"}
    assert {path for _, path, _ in fake.calls} == {
        "/v2/account", "/v2/positions", "/v2/orders", "/v2/account/activities", "/v2/account/portfolio/history",
        "/v2/assets/VOO", "/v2/assets/AAPL", "/v2/assets/ASML", "/v2/assets/TSLA"}
    [orders] = [q for _, path, q in fake.calls if path == "/v2/orders"]
    assert orders["status"] == "open"


@pytest.mark.parametrize("method,path", [
    ("POST", "/v2/orders"), ("DELETE", "/v2/orders"), ("PATCH", "/v2/orders/abc"), ("DELETE", "/v2/positions"),
    ("DELETE", "/v2/positions/AAPL"), ("POST", "/v2/account/configurations"), ("GET", "/v2/orders/abc"),
    ("GET", "/v2/watchlists"), ("GET", "/v2/account/../orders"), ("PUT", "/v2/account"),
    ("GET", "https://evil.example.com/v2/account"),
])
def test_the_guard_refuses_every_mutating_or_unlisted_request_before_it_is_sent(method, path):
    fake = FakeAlpaca()
    api = alpaca.client(_keys(), paper=False, transport=fake.transport, sleep=fake.sleep, clock=fake.clock)
    with pytest.raises(ReadOnlyViolation):
        api.guard(method, path)
    assert fake.calls == []


def test_the_default_transport_refuses_non_get_and_foreign_hosts():
    transport = alpaca.default_transport
    with pytest.raises(ReadOnlyViolation):
        transport("POST", "https://api.alpaca.markets/v2/orders", {}, 1)
    with pytest.raises(ConnectorError, match="outside"):
        transport("GET", "https://api.alpaca.markets.evil.example/v2/account", {}, 1)
    with pytest.raises(ConnectorError, match="outside"):
        transport("GET", "http://api.alpaca.markets/v2/account", {}, 1)


def test_the_connector_modules_contain_no_mutating_http_verbs():
    import re
    from wealth.connectors import cuenca
    for module in (alpaca, cuenca, _rest):
        source = Path(module.__file__).read_text()
        code = source.split('"""', 2)[2]  # skip the docstring, which names the verbs it never uses
        assert not re.search(r"""["'](POST|PUT|PATCH|DELETE)["']""", code), module.__name__
        assert "get_json(" in code and ".post(" not in code and ".delete(" not in code


# -- pagination and backoff ----------------------------------------------------

def test_activities_are_paged_by_page_token(monkeypatch):
    monkeypatch.setattr(alpaca, "PAGE_SIZE", 5)
    fake = FakeAlpaca()
    snapshot = _snapshot(fake)
    pages = [q for _, path, q in fake.calls if path == "/v2/account/activities"]
    assert len(pages) == 3 and [q["page_size"] for q in pages] == ["5"] * 3
    assert "page_token" not in pages[0] and pages[1]["page_token"].endswith("000000000005")
    assert all(q["direction"] == "asc" and q["after"] == "2025-12-31" for q in pages)
    assert len(snapshot["activities"]) == 12
    assert len({a["id"] for a in snapshot["activities"]}) == 12


def test_429_and_5xx_back_off_honouring_retry_after_and_the_rate_limit_spacing():
    fake = FakeAlpaca()
    fake.script["/v2/positions"] = [(429, {"Retry-After": "7"}, b'{"message": "too many requests"}'),
                                    (503, {}, b"{}"),
                                    ConnectorError("Could not reach the provider (URLError).", retryable=True)]
    snapshot = _snapshot(fake)
    assert len(snapshot["positions"]) == 3
    assert 7.0 in fake.sleeps  # Retry-After honoured
    assert all(s >= alpaca.MIN_INTERVAL - 1e-9 for s in fake.sleeps)  # never faster than 200/minute
    assert [p for _, p, _ in fake.calls].count("/v2/positions") == 4


def test_persistent_rate_limiting_gives_up_with_a_retryable_error():
    fake = FakeAlpaca()
    fake.script["/v2/account"] = [(429, {}, b"{}")] * 10
    proposal = fake.connector().proposal()
    assert proposal["status"] == "rejected"
    assert proposal["result"]["error"] == {"code": 429, "retryable": True,
                                           "message": "Alpaca answered HTTP 429 after 6 attempts; try again later."}


def test_the_overall_deadline_stops_retries():
    fake = FakeAlpaca()
    fake.script["/v2/account"] = [(503, {"Retry-After": "50"}, b"{}")] * 10
    api = alpaca.client(_keys(), paper=False, transport=fake.transport, sleep=fake.sleep, clock=fake.clock, timeout=100)
    with pytest.raises(_rest.ConnectorTimeout):
        api.get_json("/v2/account")
    assert fake.now <= 100


def test_paper_uses_the_paper_host():
    fake = FakeAlpaca()
    urls = []
    real = fake.transport

    def spy(method, url, headers, timeout):
        urls.append(url)
        return real(method, url, headers, timeout)

    proposal = AlpacaConnector(paper=True, keys=_keys(), transport=spy, sleep=fake.sleep, clock=fake.clock,
                               today=TODAY, since="2026-01-01").proposal()
    assert all(u.startswith("https://paper-api.alpaca.markets/v2/") for u in urls)
    assert proposal["result"]["provenance"]["ref"] == "alpaca:paper"
    assert any("paper (simulated)" in a for a in proposal["assumptions"])


# -- service: confirm, ledger, situation, re-sync -------------------------------

@pytest.fixture
def service(tmp_path, monkeypatch):
    fake = FakeAlpaca()
    monkeypatch.setenv("WEALTH_ALPACA_KEY_ID", KEY_ID)
    monkeypatch.setenv("WEALTH_ALPACA_SECRET", SECRET)
    monkeypatch.delenv("WEALTH_ALPACA_PAPER", raising=False)
    monkeypatch.setattr(alpaca, "default_transport", fake.transport)
    monkeypatch.setattr(alpaca, "default_sleep", lambda seconds: None)
    wealth = WealthService(tmp_path / "wealth.sqlite3")
    wealth.create("ana", "Ana")
    wealth.fake = fake
    return wealth


def _sync(service, **inputs):
    return service.ingest("ana", "connector", {"name": "alpaca", "since": "2026-01-01", **inputs})


def test_connector_flows_to_confirmation_ledger_holdings_and_situation(service, monkeypatch):
    monkeypatch.setattr(AlpacaConnector, "today", property(lambda self: TODAY))
    proposal = _sync(service)
    assert proposal["status"] == "ready_to_confirm"
    assert "says yes" in proposal["result"]["confirmation"]["next_step"]
    assert service.inspect("ana", keys=["account.alpaca-5432"])["absent_keys"] == ["account.alpaca-5432"]

    saved = service.ingest("ana", "confirm", {"proposal_id": proposal["result"]["proposal_id"]})
    assert saved["status"] == "saved" and "account.alpaca-5432" in saved["result"]["saved"]["keys"]
    ledger = saved["result"]["ledger"]
    assert ledger["held"] == [] and ledger["reconciliation"]["status"] == "ready"
    assert [item["reason"] for item in ledger["not_posted"]] == [
        "symbol change: check the symbol of the holding"]

    holdings = service.run("ledger", {"view": "holdings", "as_of": "2026-09-18"}, client_id="ana")["result"]
    assert {p["instrument_id"]: p["quantity"] for p in holdings["positions"]} == \
        {"AAPL": "3.25", "ASML": "2", "VOO": "10.5"}
    assert {c["currency"]: c["balance"] for c in holdings["cash"]} == {"USD": "2234.56"}
    before = service.run("ledger", {"view": "holdings", "as_of": "2026-01-02"}, client_id="ana")["result"]
    assert {p["instrument_id"]: p["quantity"] for p in before["positions"]} == {"ASML": "2", "TSLA": "1.5", "VOO": "5"}
    basis = {p["instrument_id"]: p.get("cost_basis") for p in holdings["positions"]}
    assert basis["AAPL"] == "650.00" and basis["ASML"] == "1400.00"
    with WealthStore(service.db_path) as store:
        stored = store.ledger("ana")
    instruments = {i["id"]: i for i in stored["instruments"]}
    assert {k: instruments["VOO"].get(k) for k in ("venue", "listing", "cusip")} == \
        {"venue": "us", "listing": "ARCA", "cusip": "922908363"}
    assert instruments["ASML"]["issuer_domicile"] == "other"
    withheld = [e for e in stored["entries"] if e["kind"] == "tax_withheld"]
    assert [(e["amount"], e.get("instrument_id")) for e in withheld] == [("-1.58", "VOO")]

    situation = service.situation("ana")
    assert "alpaca-5432" in json.dumps(situation)


def test_resync_dedupes_by_alpaca_ids_and_diffs_against_the_last_sync(service, monkeypatch):
    monkeypatch.setattr(AlpacaConnector, "today", property(lambda self: TODAY))
    first = _sync(service)
    service.ingest("ana", "confirm", {"proposal_id": first["result"]["proposal_id"]})
    again = _sync(service)
    assert again["result"]["proposal_id"] == first["result"]["proposal_id"] and again["result"]["changes"] == []

    fake = service.fake
    fake.data["activities"] = fake.data["activities"] + [
        {"activity_type": "FILL", "id": "20260918140000000::1a2b3c4d-0020-4000-8000-000000000020", "cum_qty": "0.5",
         "leaves_qty": "0", "price": "550", "qty": "0.5", "side": "buy", "symbol": "VOO",
         "transaction_time": "2026-09-18T14:00:00Z", "order_id": "bb11cc22-dd33-4e44-8f55-667788990011", "type": "fill"}]
    fake.data["positions"][0] = {**fake.data["positions"][0], "qty": "11", "market_value": "6050", "cost_basis": "5315"}
    fake.data["account"] = {**fake.data["account"], "cash": "1959.56"}
    second = _sync(service)
    assert second["status"] == "ready_to_confirm"
    assert second["result"]["previous_proposal_id"] == first["result"]["proposal_id"]
    changes = {(c["entity"], c["id"]): c for c in second["result"]["changes"]}
    assert changes[("positions", "alpaca-5432:VOO")]["fields"]["quantity"] == {"before": "10.5", "after": "11"}

    saved = service.ingest("ana", "confirm", {"proposal_id": second["result"]["proposal_id"]})
    ledger = saved["result"]["ledger"]
    assert ledger["posted"] == 1 and ledger["held"] == [] and ledger["reconciliation"]["status"] == "ready"
    holdings = service.run("ledger", {"view": "holdings", "as_of": "2026-09-18"}, client_id="ana")["result"]
    assert {p["instrument_id"]: p["quantity"] for p in holdings["positions"]}["VOO"] == "11"


def test_connector_status_reports_the_key_source_never_the_keys(service, monkeypatch):
    monkeypatch.setattr(AlpacaConnector, "today", property(lambda self: TODAY))
    before = service.ingest("ana", "connector_status", {"name": "alpaca"})
    [row] = before["result"]["connectors"]
    assert (row["name"], row["token"], row["ready"], row["environment"], row["last_sync"]) == \
        ("alpaca", "env", True, "live", None)
    _sync(service)
    after = dispatch("ingest", {"client_id": "ana", "action": "connector_status", "inputs": {}}, service.db_path)
    rows = {r["name"]: r for r in after["result"]["connectors"]}
    assert set(rows) == {"alpaca", "cuenca", "ibkr_flex"}
    assert (rows["alpaca"]["last_sync"]["as_of"], rows["alpaca"]["last_sync"]["ref"]) == ("2026-09-18", "alpaca:live")
    assert KEY_ID not in json.dumps(after) and SECRET not in json.dumps(after)


def test_the_keys_are_never_stored(service, monkeypatch):
    monkeypatch.setattr(AlpacaConnector, "today", property(lambda self: TODAY))
    proposal = _sync(service)
    service.ingest("ana", "confirm", {"proposal_id": proposal["result"]["proposal_id"]})
    raw = b"".join(path.read_bytes() for path in Path(service.db_path).parent.glob("wealth.sqlite3*"))
    assert KEY_ID.encode() not in raw and SECRET.encode() not in raw and b"912765432" not in raw


def test_connector_inputs_are_validated(service):
    with pytest.raises(ValueError, match="does not take query_id"):
        service.ingest("ana", "connector", {"name": "alpaca", "query_id": "1"})
    with pytest.raises(ValueError, match="paper"):
        service.ingest("ana", "connector", {"name": "alpaca", "paper": "yes"})
    with pytest.raises(ValueError, match="since"):
        service.ingest("ana", "connector", {"name": "alpaca", "since": "last year"})
    with pytest.raises(ValueError, match="unknown"):
        service.ingest("ana", "connector", {"name": "alpaca", "key_id": KEY_ID})


def test_registry_routes_alpaca_proposals_to_its_mapper():
    assert connectors.batch_mapper(_proposal()) is alpaca.ledger_batch
    assert "alpaca" in connectors.names()


def test_an_unknown_asset_does_not_stop_the_sync():
    fake = FakeAlpaca()
    del fake.data["assets"]["TSLA"]
    proposal = fake.connector().proposal()
    assert proposal["status"] == "ready_to_confirm"
    [sell] = [t for t in proposal["result"]["transactions"] if t.get("symbol") == "TSLA"]
    assert (sell["type"], sell["amount"]) == ("sell", "450")
