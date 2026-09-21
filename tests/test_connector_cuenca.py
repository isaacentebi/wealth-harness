"""Cuenca connector: fetch, map, reconcile, read-only, categorise, confirm, re-sync.  No network.

Fixtures are fictional and follow the shapes of the official ``cuenca`` SDK
resources and its recorded sandbox responses (see ``wealth/connectors/cuenca.py``).
"""
from __future__ import annotations

import base64
from datetime import date
import json
from pathlib import Path
import pickle
import traceback
from types import SimpleNamespace
import urllib.parse

import pytest

from wealth import cashflow, connectors
from wealth.connectors import _rest, cuenca
from wealth.connectors._rest import ConnectorError, ReadOnlyViolation, Secret
from wealth.connectors.cuenca import CuencaConnector, CuencaKeys, fetch_snapshot, load_keys, proposal_from_cuenca
from wealth.service import WealthService, dispatch
from wealth.store import WealthStore


FIXTURE = Path(__file__).parent / "fixtures" / "cuenca" / "snapshot.json"
API_KEY = "PKficticio0000key42"
API_SECRET = "ficticioSecretoCuenca0123456789abcdef"
TODAY = date(2026, 9, 18)
SINCE = date(2026, 8, 1)
CLABE = "072180001234567897"


def _keys():
    return CuencaKeys(Secret(API_KEY, "env", "Cuenca API key"), Secret(API_SECRET, "env", "Cuenca API secret"))


class FakeCuenca:
    """Serves the fixture like api.cuenca.com: newest first, ``{"items", "next_page_uri"}`` pages."""

    def __init__(self, page_size=100):
        self.data = json.loads(FIXTURE.read_text())
        self.page_size = page_size
        self.calls: list[tuple[str, str, dict]] = []
        self.headers: list[dict] = []
        self.script: dict[str, list] = {}
        self.sleeps: list[float] = []
        self.now = 0.0

    def _page(self, path, items, query):
        items = sorted(items, key=lambda i: (i["created_at"], i["id"]), reverse=True)
        if query.get("created_after"):
            items = [i for i in items if i["created_at"] >= query["created_after"]]
        if query.get("limit"):
            items = items[:int(query["limit"])]
        start = int(query.get("cursor", 0))
        size = min(int(query.get("page_size", 100)), self.page_size)
        chunk = items[start:start + size]
        following = None
        if start + size < len(items):
            rest = {k: v for k, v in query.items() if k != "cursor"}
            following = path + "?" + urllib.parse.urlencode({**rest, "cursor": start + size})
        return {"items": chunk, "next_page_uri": following}

    def transport(self, method, url, headers, timeout):
        parsed = urllib.parse.urlsplit(url)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        self.calls.append((method, parsed.path, query))
        self.headers.append(dict(headers))
        assert method == "GET", f"non-GET request {method} {parsed.path}"
        queued = self.script.get(parsed.path)
        if queued:
            answer = queued.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        path = parsed.path
        if path == "/balance_entries":
            body = self._page(path, self.data["balance_entries"].get(query.get("wallet_id", "default"), []), query)
        elif path in {"/deposits", "/transfers", "/card_transactions", "/commissions", "/bill_payments", "/savings"}:
            body = self._page(path, self.data[path.strip("/")], query)
        elif path == "/statements":
            body = {"items": [s for s in self.data["statements"] if (str(s["year"]), str(s["month"])) ==
                              (query.get("year"), query.get("month"))], "next_page_uri": None}
        elif path.startswith("/card_transactions/"):
            body = self.data["card_transaction_lookup"][path.rsplit("/", 1)[1]]
        else:
            raise AssertionError(f"unexpected path {path}")
        return 200, {"content-type": "application/json"}, json.dumps(body).encode()

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def clock(self):
        return self.now

    def connector(self, **kwargs):
        return CuencaConnector(keys=_keys(), transport=self.transport, sleep=self.sleep, clock=self.clock,
                               today=TODAY, since=kwargs.pop("since", SINCE.isoformat()), **kwargs)


def _snapshot(fake=None):
    fake = fake or FakeCuenca()
    api = cuenca.client(_keys(), transport=fake.transport, sleep=fake.sleep, clock=fake.clock)
    return fetch_snapshot(api, since=SINCE, today=TODAY)


def _proposal(fake=None):
    return proposal_from_cuenca(_snapshot(fake), retrieved_at="2026-09-18T20:00:00+00:00")


def _by_id(rows):
    return {row["id"]: row for row in rows}


# -- mapping ------------------------------------------------------------------

def test_balance_and_savings_pocket_become_mxn_accounts_with_balance_assertions():
    proposal = _proposal()
    result = proposal["result"]
    assert proposal["status"] == "ready_to_confirm"
    accounts = {a["id"]: a for a in result["household"]["accounts"]}
    assert {k: (a["type"], a["currency"], a["name"]) for k, a in accounts.items()} == {
        "cuenca-cuenta-cuenca": ("checking", "MXN", "Cuenta Cuenca"),
        "cuenca-apartado-sv01": ("savings", "MXN", "Apartado Viaje a Japón")}
    cash = {p["account_id"]: p["value"] for p in result["household"]["positions"]}
    assert cash == {"cuenca-cuenta-cuenca": "10764.7", "cuenca-apartado-sv01": "2000"}
    assertions = {a["account_id"]: a for a in result["balance_assertions"]}
    main = assertions["cuenca-cuenta-cuenca"]
    assert (main["balance_kind"], main["opening"], main["closing"], main["period_start"], main["period_end"]) == \
        ("cash", "1000", "10764.7", "2026-08-01", "2026-09-18")
    recon = {r["account_id"]: r for r in result["reconciliation"]["accounts"]}
    assert recon["cuenca-cuenta-cuenca"]["status"] == "reconciled"
    assert recon["cuenca-cuenta-cuenca"]["cash_flow"] == {"opening": "1000", "deposits": "25550",
                                                          "withdrawals": "15785.3", "closing": "10764.7", "matches": True}
    assert result["connector"]["statements_available"] == ["2026-08"]


def test_movements_map_to_income_expense_transfer_fee_with_spanish_text_and_spei_ids():
    transactions = _by_id(_proposal()["result"]["transactions"])
    payroll = transactions["CUENCA-SPEI-2026080140014TRAC0000123"]
    assert (payroll["type"], payroll["amount"], payroll["date"], payroll["currency"]) == \
        ("income", "25000", "2026-08-01", "MXN")
    assert payroll["description"] == "SPEI recibido · EMPRESA FICTICIA SA DE CV · PAGO DE NOMINA QUINCENA 15"
    assert payroll["tracking_key"] == "2026080140014TRAC0000123"
    rent = transactions["CUENCA-SPEI-CUENCA1234567890"]
    assert (rent["type"], rent["amount"]) == ("transfer", "-12000")
    assert rent["description"] == "SPEI enviado · Arrendador Ficticio · Renta agosto · a ****7897"
    friend = transactions["CUENCA-SPEI-MBAN01002608150000456"]
    assert (friend["type"], friend["amount"]) == ("transfer", "500")
    oxxo = transactions["CUENCA-CT-CT01"]
    assert (oxxo["type"], oxxo["amount"], oxxo["description"], oxxo["merchant"], oxxo["card"]) == \
        ("expense", "-450.5", "OXXO INSURGENTES CDMX MX", "OXXO INSURGENTES CDMX MX", "****1470")
    refund = transactions["CUENCA-CT-CT03"]
    assert (refund["type"], refund["amount"], refund["description"]) == \
        ("expense", "50", "OXXO INSURGENTES CDMX MX · Reembolso")
    assert (transactions["CUENCA-CT-CT02"]["type"], transactions["CUENCA-CT-CT02"]["description"]) == \
        ("expense", "NETFLIX.COM")  # related card transaction fetched by id when outside the list window
    atm = transactions["CUENCA-CT-CT04"]
    assert (atm["type"], atm["amount"], atm["description"]) == ("withdrawal", "-1000", "Retiro en cajero · CAJERO BANORTE REFORMA")
    fee = transactions["CUENCA-CO-CO01"]
    assert (fee["type"], fee["amount"], fee["description"], fee["commission_type"]) == \
        ("fee", "-5.8", "Comisión por SPEI enviado · Comisión (outgoing_spei)", "outgoing_spei")
    wallet_moves = sorted((t["account_id"], t["amount"]) for t in _proposal()["result"]["transactions"]
                          if t["id"] == "CUENCA-WT-WT01")
    assert wallet_moves == [("cuenca-apartado-sv01", "2000"), ("cuenca-cuenta-cuenca", "-2000")]
    assert not any("not_posted_reason" in t for t in transactions.values())


def test_a_broken_rolling_balance_needs_review():
    fake = FakeCuenca()
    fake.data["balance_entries"]["default"][4]["rolling_balance"] = 1321000
    proposal = _proposal(fake)
    assert proposal["status"] == "needs_review"
    assert any("do not follow the rolling balance" in r for r in proposal["result"]["review_reasons"])


# -- credentials and redaction ------------------------------------------------

def test_clabe_card_numbers_and_keys_never_appear_in_the_proposal():
    fake = FakeCuenca()
    proposal = fake.connector().proposal()
    text = json.dumps(proposal, ensure_ascii=False)
    for private in (CLABE, API_KEY, API_SECRET):
        assert private not in text
    assert "****7897" in text
    basic = "Basic " + base64.b64encode(f"{API_KEY}:{API_SECRET}".encode()).decode()
    assert all(h["Authorization"] == basic and h["X-Cuenca-Api-Version"] == "2020-03-19" for h in fake.headers)
    assert not any(API_SECRET in json.dumps(q) for _, _, q in fake.calls)


def test_keys_never_print_or_pickle_and_errors_are_scrubbed():
    keys = _keys()
    assert API_KEY not in repr(keys) and API_SECRET not in str(keys)
    with pytest.raises(TypeError):
        pickle.dumps(keys)

    def leaky(method, url, headers, timeout):
        raise RuntimeError(f"failed {url} with {headers}")

    api = cuenca.client(keys, transport=leaky, sleep=lambda s: None, clock=lambda: 0.0)
    with pytest.raises(ConnectorError) as caught:
        api.get_json("/balance_entries")
    rendered = "".join(traceback.format_exception(caught.value))
    token = base64.b64encode(f"{API_KEY}:{API_SECRET}".encode()).decode()
    assert API_KEY not in rendered and API_SECRET not in rendered and token not in rendered

    fake = FakeCuenca()
    fake.script["/balance_entries"] = [(401, {}, json.dumps({"error": "bad credentials", "code": 101}).encode())]
    proposal = fake.connector().proposal()
    assert proposal["status"] == "rejected" and proposal["result"]["error"]["code"] == 401
    assert "Check the Cuenca API key" in proposal["warnings"][0]


def test_keys_come_from_env_then_keychain_without_a_shell():
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout={"api_key": API_KEY, "api_secret": API_SECRET}[command[5]])

    assert load_keys({"WEALTH_CUENCA_API_KEY": "k", "WEALTH_CUENCA_API_SECRET": "s"}, runner, "darwin").source == "env"
    keys = load_keys({}, runner, "darwin")
    assert keys.source == "keychain" and keys.values() == (API_KEY, API_SECRET)
    assert calls == [["security", "find-generic-password", "-s", "wealth-cuenca", "-a", "api_key", "-w"],
                     ["security", "find-generic-password", "-s", "wealth-cuenca", "-a", "api_secret", "-w"]]


def test_connector_without_keys_points_to_setup_and_the_statement_upload():
    proposal = CuencaConnector(key_loader=lambda: None, today=TODAY).proposal()
    assert proposal["status"] == "needs_input" and proposal["missing"][0]["key"] == "cuenca.keys"
    assert "upload the monthly statement" in proposal["missing"][0]["detail"]


# -- read-only enforcement ------------------------------------------------------

def test_a_full_sync_uses_only_get_on_read_collections():
    fake = FakeCuenca()
    fake.connector().proposal()
    assert {m for m, _, _ in fake.calls} == {"GET"}
    assert {p for _, p, _ in fake.calls} == {
        "/balance_entries", "/savings", "/deposits", "/transfers", "/card_transactions", "/commissions",
        "/bill_payments", "/card_transactions/CT02", "/statements"}
    wallets = sorted(q["wallet_id"] for _, p, q in fake.calls if p == "/balance_entries")
    assert wallets == ["SV01", "default", "default"]


@pytest.mark.parametrize("method,path", [
    ("POST", "/transfers"), ("PATCH", "/transfers/TR01"), ("POST", "/card_transactions"), ("DELETE", "/api_keys/PK1"),
    ("POST", "/api_keys"), ("POST", "/token"), ("POST", "/user_logins"), ("POST", "/wallet_transactions"),
    ("POST", "/savings"), ("DELETE", "/savings/SV01"), ("GET", "/api_keys"), ("GET", "/users"), ("GET", "/cards"),
    ("GET", "/statements/ST01"), ("GET", "https://evil.example.com/balance_entries"), ("GET", "/transfers/../api_keys"),
])
def test_the_guard_refuses_every_mutating_or_unlisted_request(method, path):
    fake = FakeCuenca()
    api = cuenca.client(_keys(), transport=fake.transport, sleep=fake.sleep, clock=fake.clock)
    with pytest.raises(ReadOnlyViolation):
        api.guard(method, path)
    assert fake.calls == []


def test_a_next_page_uri_outside_the_allowlist_is_refused():
    fake = FakeCuenca()
    fake.script["/savings"] = [(200, {}, json.dumps({"items": [], "next_page_uri": "/api_keys?page_size=100"}).encode())]
    proposal = fake.connector().proposal()
    assert proposal["status"] == "rejected"
    assert "refusing GET /api_keys" in proposal["result"]["error"]["message"]
    assert "/api_keys" not in {p for _, p, _ in fake.calls}


def test_the_default_transport_refuses_non_get_and_other_hosts():
    with pytest.raises(ReadOnlyViolation):
        cuenca.default_transport("POST", "https://api.cuenca.com/transfers", {}, 1)
    with pytest.raises(ConnectorError, match="outside"):
        cuenca.default_transport("GET", "https://api.cuenca.com.evil.example/transfers", {}, 1)


# -- pagination and backoff ----------------------------------------------------

def test_every_page_is_followed_through_next_page_uri():
    fake = FakeCuenca(page_size=3)
    snapshot = _snapshot(fake)
    assert len(snapshot["entries"]["default"]) == 9
    pages = [q for _, p, q in fake.calls if p == "/balance_entries" and q.get("wallet_id") == "default" and "limit" not in q]
    assert len(pages) == 3 and [q.get("cursor") for q in pages] == [None, "3", "6"]
    assert _proposal(FakeCuenca(page_size=3))["result"]["proposal_id"] == _proposal()["result"]["proposal_id"]


def test_rate_limits_and_server_errors_back_off():
    fake = FakeCuenca()
    fake.script["/deposits"] = [(429, {"Retry-After": "5"}, b"{}"), (502, {}, b"{}")]
    snapshot = _snapshot(fake)
    assert "/deposits/SP01" in snapshot["related"]
    assert 5.0 in fake.sleeps and all(s >= cuenca.MIN_INTERVAL - 1e-9 for s in fake.sleeps)
    assert [p for _, p, _ in fake.calls].count("/deposits") == 3


# -- service: confirm, ledger, categories, situation, re-sync --------------------

@pytest.fixture
def service(tmp_path, monkeypatch):
    fake = FakeCuenca()
    monkeypatch.setenv("WEALTH_CUENCA_API_KEY", API_KEY)
    monkeypatch.setenv("WEALTH_CUENCA_API_SECRET", API_SECRET)
    monkeypatch.delenv("WEALTH_CUENCA_SANDBOX", raising=False)
    monkeypatch.setattr(cuenca, "default_transport", fake.transport)
    monkeypatch.setattr(cuenca, "default_sleep", lambda seconds: None)
    monkeypatch.setattr(CuencaConnector, "today", property(lambda self: TODAY))
    wealth = WealthService(tmp_path / "wealth.sqlite3")
    wealth.create("ana", "Ana")
    wealth.fake = fake
    return wealth


def _sync(service):
    return service.ingest("ana", "connector", {"name": "cuenca", "since": "2026-08-01"})


def test_connector_flows_to_confirmation_ledger_categories_and_situation(service):
    proposal = _sync(service)
    assert proposal["status"] == "ready_to_confirm"
    assert "says yes" in proposal["result"]["confirmation"]["next_step"]
    assert service.inspect("ana", keys=["account.cuenca-cuenta-cuenca"])["absent_keys"] == ["account.cuenca-cuenta-cuenca"]

    saved = service.ingest("ana", "confirm", {"proposal_id": proposal["result"]["proposal_id"]})
    assert saved["status"] == "saved"
    ledger = saved["result"]["ledger"]
    assert ledger["held"] == [] and ledger["not_posted"] == [] and ledger["reconciliation"]["status"] == "ready"
    holdings = service.run("ledger", {"view": "holdings", "as_of": "2026-09-18"}, client_id="ana")["result"]
    assert {(c["account_id"], c["currency"]): c["balance"] for c in holdings["cash"]} == {
        ("cuenca-cuenta-cuenca", "MXN"): "10764.70", ("cuenca-apartado-sv01", "MXN"): "2000.00"}
    opening = service.run("ledger", {"view": "holdings", "as_of": "2026-08-01"}, client_id="ana")["result"]
    assert {c["account_id"]: c["balance"] for c in opening["cash"]}["cuenca-cuenta-cuenca"] == "26000.00"

    with WealthStore(service.db_path) as store:
        stored = store.ledger("ana")
    labels = cashflow.categorize(stored)
    by_text = {e["description"]: labels[e["id"]]["category"] for e in stored["entries"] if e["id"] in labels}
    assert by_text["OXXO INSURGENTES CDMX MX"] == "convenience"
    assert by_text["NETFLIX.COM"] == "subscriptions"
    assert by_text["Comisión por SPEI enviado · Comisión (outgoing_spei)"] == "bank_fees"
    assert by_text["SPEI recibido · EMPRESA FICTICIA SA DE CV · PAGO DE NOMINA QUINCENA 15"] == "salary"
    spei = [e for e in stored["entries"] if e.get("external_id") == "CUENCA-SPEI-CUENCA1234567890"]
    assert len(spei) == 1 and spei[0]["kind"] == "transfer"

    situation = service.situation("ana")
    assert "cuenca-cuenta-cuenca" in json.dumps(situation)
    assert CLABE not in json.dumps(situation)


def test_resync_posts_only_new_movements_and_diffs(service):
    first = _sync(service)
    service.ingest("ana", "confirm", {"proposal_id": first["result"]["proposal_id"]})
    again = _sync(service)
    assert again["result"]["proposal_id"] == first["result"]["proposal_id"] and again["result"]["changes"] == []

    fake = service.fake
    fake.data["balance_entries"]["default"].append(
        {"id": "LE11", "created_at": "2026-09-02T14:00:00.000000", "user_id": "USficticio01", "name": "CFE SUMINISTRADOR",
         "amount": 85000, "descriptor": "Pago de luz", "rolling_balance": 991470, "type": "debit",
         "related_transaction_uri": "/bill_payments/BP01", "funding_instrument_uri": "/service_providers/SP9",
         "wallet_id": "default"})
    fake.data["bill_payments"].append({"id": "BP01", "created_at": "2026-09-02T14:00:00.000000", "user_id": "USficticio01",
                                       "amount": 85000, "status": "succeeded", "descriptor": "Pago de luz",
                                       "account_number": "123456789012", "provider_uri": "/service_providers/SP9"})
    second = _sync(service)
    assert second["result"]["previous_proposal_id"] == first["result"]["proposal_id"]
    changes = {(c["entity"], c["id"]): c for c in second["result"]["changes"]}
    assert changes[("positions", "cuenca-cuenta-cuenca:CASH:MXN")]["fields"]["value"] == \
        {"before": "10764.7", "after": "9914.7"}
    saved = service.ingest("ana", "confirm", {"proposal_id": second["result"]["proposal_id"]})
    ledger = saved["result"]["ledger"]
    assert ledger["posted"] == 1 and ledger["held"] == [] and ledger["reconciliation"]["status"] == "ready"
    with WealthStore(service.db_path) as store:
        stored = store.ledger("ana")
    [bill] = [e for e in stored["entries"] if e.get("external_id") == "CUENCA-BP-BP01"]
    assert (bill["kind"], bill["amount"], bill["description"]) == ("expense", "-850", "Pago de servicio · CFE SUMINISTRADOR · Pago de luz")
    assert cashflow.categorize(stored)[bill["id"]]["category"] == "utilities"


def test_connector_status_and_no_stored_credentials(service):
    status = dispatch("ingest", {"client_id": "ana", "action": "connector_status", "inputs": {"name": "cuenca"}},
                      service.db_path)
    [row] = status["result"]["connectors"]
    assert (row["name"], row["token"], row["ready"], row["last_sync"]) == ("cuenca", "env", True, None)
    assert "estado de cuenta" in row["fallback"]
    proposal = _sync(service)
    service.ingest("ana", "confirm", {"proposal_id": proposal["result"]["proposal_id"]})
    after = service.ingest("ana", "connector_status", {"name": "cuenca"})
    assert after["result"]["connectors"][0]["last_sync"]["confirmed"] is True
    raw = b"".join(path.read_bytes() for path in Path(service.db_path).parent.glob("wealth.sqlite3*"))
    for private in (API_KEY, API_SECRET, CLABE):
        assert private.encode() not in raw


def test_registry_routes_cuenca_proposals_to_its_mapper():
    assert connectors.batch_mapper(_proposal()) is cuenca.ledger_batch
    with pytest.raises(ValueError, match="does not take paper"):
        connectors.connector("cuenca", paper=True)
