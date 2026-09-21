"""IBKR Flex Web Service connector: fetch, parse, reconcile, confirm, re-sync.  No network."""
from __future__ import annotations

from decimal import Decimal
import json
import pickle
import traceback
from pathlib import Path
from types import SimpleNamespace

import pytest

from wealth import connectors
from wealth.connectors import ibkr_flex
from wealth.connectors.ibkr_flex import (
    ERRORS, FlexError, FlexTimeout, FlexToken, FlexTransportError, IbkrFlexConnector, fetch_statement,
    load_token, parse_flex, proposal_from_flex, scrub,
)
from wealth.ingest import proposal_to_facts
from wealth.service import WealthService, dispatch
from wealth.store import WealthStore


FIXTURES = Path(__file__).parent / "fixtures" / "ibkr"
AUGUST = (FIXTURES / "activity_2026-08-31.xml").read_bytes()
SEPTEMBER = (FIXTURES / "activity_2026-09-18.xml").read_bytes()
TOKEN = "871236498712364987123649"
QUERY = "987654"
GET = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement"


def _ok(reference="1234567890", url=GET) -> bytes:
    return (f'<FlexStatementResponse timestamp="21 September, 2026 09:15 AM EDT"><Status>Success</Status>'
            f"<ReferenceCode>{reference}</ReferenceCode><Url>{url}</Url></FlexStatementResponse>").encode()


def _fail(code: int, status="Fail") -> bytes:
    return (f'<FlexStatementResponse timestamp="21 September, 2026 09:15 AM EDT"><Status>{status}</Status>'
            f"<ErrorCode>{code}</ErrorCode><ErrorMessage>{ERRORS[code]}</ErrorMessage></FlexStatementResponse>").encode()


class FakeIBKR:
    """Scripted SendRequest/GetStatement answers plus a fake clock that sleeps instantly."""

    def __init__(self, send, get):
        self.send, self.get = list(send), list(get)
        self.urls: list[str] = []
        self.sleeps: list[float] = []
        self.now = 0.0

    def transport(self, url: str, timeout: float) -> bytes:
        self.urls.append(url)
        queue = self.send if "SendRequest" in url else self.get
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def clock(self) -> float:
        return self.now

    def fetch(self, **kwargs):
        return fetch_statement(FlexToken(TOKEN, "env"), QUERY, transport=self.transport, sleep=self.sleep,
                               clock=self.clock, **kwargs)


def _proposal(body=AUGUST, **kwargs):
    return proposal_from_flex(body, query_id=QUERY, retrieved_at="2026-09-01T08:15:02+00:00", **kwargs)


def _by_id(rows):
    return {row["id"]: row for row in rows}


# -- parsing ------------------------------------------------------------------

def test_every_section_is_parsed_from_the_fixture():
    [statement] = parse_flex(AUGUST)
    assert (statement["account_id"], statement["from_date"], statement["to_date"]) == ("U7654321", "2026-01-01", "2026-08-31")
    counts = {name: len(statement[name]) for name in ("account_information", "equity_summary", "cash_report",
                                                      "open_positions", "trades", "closed_lots", "cash_transactions",
                                                      "corporate_actions", "conversion_rates")}
    assert counts == {"account_information": 1, "equity_summary": 2, "cash_report": 3, "open_positions": 9, "trades": 4,
                      "closed_lots": 1, "cash_transactions": 7, "corporate_actions": 3, "conversion_rates": 4}


def test_accounts_are_masked_and_the_holder_never_appears():
    proposal = _proposal()
    [account] = proposal["result"]["household"]["accounts"]
    assert account == {"id": "ibkr-4321", "owner_id": "self", "type": "brokerage", "currency": "USD",
                       "name": "Interactive Brokers Individual", "institution": "Interactive Brokers",
                       "number_masked": "****4321"}
    text = json.dumps(proposal)
    for private in ("U7654321", "Ana Example", "ana@example.com", "Calle Ficticia"):
        assert private not in text
    assert "DISBURSEMENT INITIATED BY [account holder]" in text


def test_positions_carry_cost_basis_lots_venue_and_issuer_domicile():
    result = _proposal()["result"]
    positions = _by_id(result["household"]["positions"])
    voo, cspx, asml = positions["ibkr-4321:VOO"], positions["ibkr-4321:CSPX"], positions["ibkr-4321:ASML"]
    assert (voo["quantity"], voo["value"], voo["cost_basis"]) == ("20", "11000", "9800.2")
    assert (voo["venue"], voo["listing_exchange"], voo["underlying_symbol"], voo["issuer_domicile"], voo["isin"]) == \
        ("us", "ARCA", "VOO", "US", "US9229083632")
    assert (cspx["venue"], cspx["listing_exchange"], cspx["issuer_domicile"]) == ("other", "LSEETF", "IE")
    assert (asml["currency"], asml["issuer_domicile"], asml["asset_class"]) == ("EUR", "other", "equity")
    # SIC listing is never guessed; it is unknown until provided
    assert {p["sic_listed"] for p in positions.values() if "sic_listed" in p} == {"unknown"}
    assert any("SIC" in note and "unknown" in note for note in _proposal()["assumptions"])
    lots = [(l["instrument_id"], l["quantity"], l["acquired_on"], l["cost_basis"]) for l in result["household"]["lots"]]
    assert ("VOO", "12", "2025-03-10", "5700") in lots and ("VOO", "8", "2026-02-12", "4100.2") in lots
    assert ("ASML", "5", "2026-03-05", "3299") in lots


def test_sic_listing_is_recorded_only_when_provided():
    positions = _by_id(_proposal(sic_listed={"VOO": True, "CSPX": False})["result"]["household"]["positions"])
    assert positions["ibkr-4321:VOO"]["sic_listed"] is True
    assert positions["ibkr-4321:CSPX"]["sic_listed"] is False
    assert positions["ibkr-4321:AAPL"]["sic_listed"] == "unknown"


def test_trades_cash_lines_fx_and_corporate_actions_map_to_ledger_kinds():
    transactions = _by_id(_proposal()["result"]["transactions"])
    buy = transactions["IBKR-T812345671"]
    assert (buy["type"], buy["amount"], buy["quantity"], buy["price"], buy["fees"], buy["symbol"]) == \
        ("buy", "-4100.2", "8", "512.4", "1", "VOO")
    sell = transactions["IBKR-T812345674"]
    assert (sell["type"], sell["amount"], sell["quantity"], sell["fees"]) == ("sell", "1178.5", "2", "1.5")
    assert sell["closed_lots"] == [{"acquired_on": "2025-06-02", "quantity": "2", "cost_basis": "1000"}]
    eur_buy = transactions["IBKR-T812345673"]
    assert (eur_buy["currency"], eur_buy["amount"], eur_buy["fees"]) == ("EUR", "-3299", "4")
    fx = transactions["IBKR-T812345672"]
    assert (fx["type"], fx["amount"], fx["currency"], fx["to_amount"], fx["to_currency"]) == ("fx", "-4340", "USD", "4000", "EUR")
    assert (transactions["IBKR-T812345672-fee"]["type"], transactions["IBKR-T812345672-fee"]["amount"]) == ("fee", "-2")
    kinds = {tid: (t["type"], t["amount"]) for tid, t in transactions.items() if tid.startswith("IBKR-C")}
    assert kinds == {"IBKR-C29876543301": ("deposit", "10000"), "IBKR-C29876543302": ("dividend", "33"),
                     "IBKR-C29876543303": ("tax_withheld", "-3.3"), "IBKR-C29876543304": ("interest", "12.34"),
                     "IBKR-C29876543305": ("fee", "-10"), "IBKR-C29876543306": ("withdrawal", "-500")}
    split = transactions["IBKR-A29876543401"]
    assert (split["type"], split["symbol"], split["ratio"], split["quantity"]) == ("split", "AAPL", "4", "15")
    for other in ("IBKR-A29876543402", "IBKR-A29876543403"):
        assert transactions[other]["type"] == "corporate_action"
        assert "not posted automatically" in transactions[other]["not_posted_reason"]
    assert not any(t["id"].startswith("IBKR-H") for t in transactions.values())  # every line had an IBKR id


def test_multi_currency_cash_fx_and_nav_reconcile():
    proposal = _proposal()
    result = proposal["result"]
    assert proposal["status"] == "ready_to_confirm"
    assert {(f["from"], f["to"], f["rate"]) for f in result["household"]["fx"]} == {("EUR", "USD", "1.1"), ("MXN", "USD", "0.054")}
    [recon] = result["reconciliation"]["accounts"]
    assert recon["cash"] == {"EUR": "701", "USD": "2345.67"}
    # 22050 stock (ASML 3500 EUR x 1.1) + 2345.67 USD + 701 EUR x 1.1 = NAV 25171.27 minus 4.50 accrued interest
    assert (recon["positions_value"], recon["computed_total"], recon["reported_total"], recon["status"]) == \
        ("22050", "25166.77", "25166.77", "reconciled")
    assert any("accrued" in note for note in proposal["assumptions"])
    [nav] = result["balance_assertions"]
    assert (nav["balance_kind"], nav["opening"], nav["closing"], nav["period_start"], nav["period_end"]) == \
        ("nav", "13947.33", "25166.77", "2026-01-01", "2026-08-31")
    assert proposal_to_facts(proposal, confirmed=True, proposal_id=result["proposal_id"],
                             today=__import__("datetime").date(2026, 9, 21))["status"] == "ready"


def test_nav_that_disagrees_with_positions_plus_cash_needs_review():
    body = AUGUST.replace(b'interestAccruals="4.5" dividendAccruals="0" total="25171.27"',
                          b'interestAccruals="4.5" dividendAccruals="0" total="25271.27"')
    proposal = _proposal(body)
    assert proposal["status"] == "needs_review"
    assert proposal["result"]["reconciliation"]["accounts"][0]["difference"] == "-100"


# -- credentials --------------------------------------------------------------

def test_token_never_appears_in_repr_str_pickle_or_connector():
    token = FlexToken(TOKEN, "keychain")
    assert TOKEN not in repr(token) and TOKEN not in str(token) and TOKEN not in f"{token}"
    with pytest.raises(TypeError):
        pickle.dumps(token)
    connector = IbkrFlexConnector(QUERY, token=token)
    assert TOKEN not in repr(connector) and TOKEN not in repr(vars(connector))


def test_token_comes_from_env_then_keychain_without_a_shell():
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=TOKEN + "\n")

    assert load_token({"WEALTH_IBKR_FLEX_TOKEN": "fromenv"}, runner, "darwin").source == "env"
    assert calls == []
    token = load_token({}, runner, "darwin")
    assert token.source == "keychain" and token.reveal() == TOKEN
    command, kwargs = calls[0]
    assert command == ["security", "find-generic-password", "-s", "wealth-ibkr-flex", "-w"]
    assert "shell" not in kwargs and kwargs["timeout"] == 10
    assert load_token({}, lambda *a, **k: SimpleNamespace(returncode=44, stdout=""), "darwin") is None
    assert load_token({}, lambda *a, **k: (_ for _ in ()).throw(OSError("no security")), "darwin") is None
    assert load_token({}, runner, "win32") is None


def test_token_is_scrubbed_from_errors_urls_and_tracebacks():
    def leaky(url, timeout):
        raise RuntimeError(f"connection reset while fetching {url}")

    fake = FakeIBKR([_ok()], [_ok()])
    with pytest.raises(FlexTransportError) as caught:
        fetch_statement(FlexToken(TOKEN, "env"), QUERY, transport=leaky, sleep=fake.sleep, clock=fake.clock, timeout=5)
    error = caught.value
    rendered = "".join(traceback.format_exception(error))
    assert TOKEN not in str(error) and TOKEN not in repr(error) and TOKEN not in rendered
    assert error.__cause__ is None and error.__context__ is None
    assert "t=****" in str(error)
    assert scrub(f"https://x/?q=1&t={TOKEN}&v=3", TOKEN) == "https://x/?q=1&t=****&v=3"


def test_ibkr_token_errors_are_not_retried_and_do_not_leak():
    fake = FakeIBKR([_fail(1015)], [_ok()])
    with pytest.raises(FlexError) as caught:
        fake.fetch()
    assert caught.value.code == 1015 and not caught.value.retryable
    assert "Token is invalid" in str(caught.value) and TOKEN not in str(caught.value)
    assert len(fake.urls) == 1 and fake.sleeps == []


def test_connector_without_a_token_asks_for_setup_and_never_for_the_token_in_chat():
    proposal = IbkrFlexConnector(QUERY, token_loader=lambda: None).proposal()
    assert proposal["status"] == "needs_input"
    assert proposal["missing"][0]["key"] == "ibkr_flex.token"
    assert any("security add-generic-password" in step for step in proposal["result"]["setup"])


def test_connector_failures_come_back_rejected_with_a_clean_message():
    fake = FakeIBKR([_fail(1012)], [_ok()])
    connector = IbkrFlexConnector(QUERY, token=FlexToken(TOKEN, "env"), transport=fake.transport,
                                  sleep=fake.sleep, clock=fake.clock)
    proposal = connector.proposal()
    assert proposal["status"] == "rejected"
    assert proposal["result"]["error"]["code"] == 1012
    assert "Token has expired" in proposal["warnings"][0]
    assert TOKEN not in json.dumps(proposal)


# -- fetch: retry and poll ----------------------------------------------------

def test_documented_codes_1018_throttle_and_1019_in_progress():
    assert "Too many requests" in ERRORS[1018]
    assert ERRORS[1019] == "Statement generation in progress. Please try again shortly."
    assert {1018, 1019} <= ibkr_flex.RETRYABLE
    assert not {1003, 1012, 1013, 1014, 1015, 1020} & ibkr_flex.RETRYABLE


def test_send_is_retried_after_1018_and_statement_is_polled_through_1019():
    fake = FakeIBKR([_fail(1018), _ok()], [_fail(1019, "Warn"), _fail(1019, "Warn"), AUGUST])
    assert fake.fetch() == AUGUST
    send = [u for u in fake.urls if "SendRequest" in u]
    get = [u for u in fake.urls if "GetStatement" in u]
    assert len(send) == 2 and len(get) == 3
    assert send[0].startswith("https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest?")
    assert f"q={QUERY}" in send[0] and "v=3" in send[0] and f"t={TOKEN}" in send[0]
    assert all("q=1234567890" in u and u.startswith(GET) for u in get)
    assert fake.sleeps[0] >= 10  # 1018: fall back under 10 requests per minute
    assert all(s >= 1 for s in fake.sleeps)  # never faster than one request per second
    assert fake.sleeps[1:] == sorted(fake.sleeps[1:])  # backoff grows while generation is in progress


def test_polling_gives_up_after_the_overall_timeout():
    fake = FakeIBKR([_ok()], [_fail(1019, "Warn")])
    with pytest.raises(FlexTimeout) as caught:
        fake.fetch()
    assert fake.now <= 120 and "120 seconds" in str(caught.value)
    assert len(fake.urls) > 3


def test_transient_transport_errors_are_retried_and_other_statement_errors_stop():
    fake = FakeIBKR([FlexTransportError("Could not reach IBKR (URLError).", retryable=True), _ok()],
                    [_fail(1009), AUGUST])
    assert fake.fetch() == AUGUST
    fatal = FakeIBKR([_ok()], [_fail(1017)])
    with pytest.raises(FlexError, match="1017"):
        fatal.fetch()


def test_statement_url_outside_ibkr_is_ignored_and_oversized_or_unsafe_xml_is_refused(monkeypatch):
    fake = FakeIBKR([_ok(url="https://evil.example.com/GetStatement")], [AUGUST])
    fake.fetch()
    assert fake.urls[-1].startswith(GET)
    monkeypatch.setattr(ibkr_flex, "MAX_RESPONSE_BYTES", 1000)
    with pytest.raises(FlexTransportError, match="more than"):
        FakeIBKR([_ok()], [AUGUST]).fetch()
    monkeypatch.undo()
    bomb = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><FlexQueryResponse>&a;</FlexQueryResponse>'
    with pytest.raises(FlexError, match="DTD"):
        FakeIBKR([_ok()], [bomb]).fetch()
    with pytest.raises(FlexTransportError, match="non-IBKR"):
        ibkr_flex.urllib_transport("http://ndcdyn.interactivebrokers.com/x", 1)
    with pytest.raises(FlexTransportError, match="non-IBKR"):
        ibkr_flex.urllib_transport("https://interactivebrokers.com.evil.example/x", 1)


def test_query_id_must_be_numeric():
    with pytest.raises(ValueError, match="query_id"):
        IbkrFlexConnector("12; rm -rf /")


# -- service: confirm, ledger, re-sync -----------------------------------------

@pytest.fixture
def service(tmp_path, monkeypatch):
    statements = {"body": AUGUST}
    monkeypatch.setenv("WEALTH_IBKR_FLEX_TOKEN", TOKEN)
    monkeypatch.setattr(ibkr_flex, "default_transport",
                        lambda url, timeout: _ok() if "SendRequest" in url else statements["body"])
    monkeypatch.setattr(ibkr_flex, "default_sleep", lambda seconds: None)
    wealth = WealthService(tmp_path / "wealth.sqlite3")
    wealth.create("ana", "Ana")
    wealth.statements = statements
    return wealth


def _sync(service):
    return service.ingest("ana", "connector", {"name": "ibkr_flex", "query_id": QUERY})


def test_connector_flows_to_confirmation_ledger_and_holdings(service):
    proposal = _sync(service)
    assert proposal["status"] == "ready_to_confirm"
    assert "says yes" in proposal["result"]["confirmation"]["next_step"]
    assert service.inspect("ana", keys=["account.ibkr-4321"])["absent_keys"] == ["account.ibkr-4321"]
    assert service.run("ledger", {"view": "holdings", "as_of": "2026-08-31"}, client_id="ana")["result"]["positions"] == []

    saved = service.ingest("ana", "confirm", {"proposal_id": proposal["result"]["proposal_id"]})
    assert saved["status"] == "saved" and "account.ibkr-4321" in saved["result"]["saved"]["keys"]
    ledger = saved["result"]["ledger"]
    assert ledger["held"] == [] and ledger["reconciliation"]["status"] == "ready"
    assert [item["reason"] for item in ledger["not_posted"]] == [
        "IBKR dividend rights issue (DI) is not posted automatically; check the holding after it",
        "IBKR expired dividend right (ED) is not posted automatically; check the holding after it"]

    holdings = service.run("ledger", {"view": "holdings", "as_of": "2026-08-31"}, client_id="ana")["result"]
    assert {p["instrument_id"]: p["quantity"] for p in holdings["positions"]} == {"AAPL": "20", "ASML": "5", "CSPX": "10", "VOO": "20"}
    assert {c["currency"]: c["balance"] for c in holdings["cash"]} == {"EUR": "701.00", "USD": "2345.67"}
    basis = {p["instrument_id"]: p["cost_basis"] for p in holdings["positions"]}
    # AAPL's pre-split lot, CSPX's lots rebuilt from IBKR's closed lot, VOO's pre-period lot plus the buy
    assert basis == {"AAPL": "1100.00", "ASML": "3299.00", "CSPX": "5000.00", "VOO": "9800.20"}
    before_split = service.run("ledger", {"view": "holdings", "as_of": "2026-06-14"}, client_id="ana")["result"]
    assert {p["instrument_id"]: p["quantity"] for p in before_split["positions"]}["AAPL"] == "5"
    with WealthStore(service.db_path) as store:
        instruments = {i["id"]: i for i in store.ledger("ana")["instruments"]}
    assert {k: instruments["CSPX"].get(k) for k in ("venue", "listing", "isin", "issuer_domicile")} == \
        {"venue": "other", "listing": "LSE", "isin": "IE00B5BMR087", "issuer_domicile": "IE"}
    assert (instruments["VOO"]["venue"], instruments["VOO"]["issuer_domicile"]) == ("us", "US")


def test_resync_dedupes_by_ibkr_ids_and_diffs_against_the_last_sync(service):
    first = _sync(service)
    service.ingest("ana", "confirm", {"proposal_id": first["result"]["proposal_id"]})
    again = _sync(service)
    assert again["result"]["proposal_id"] == first["result"]["proposal_id"]
    assert again["result"]["changes"] == []

    service.statements["body"] = SEPTEMBER
    second = _sync(service)
    assert second["result"]["previous_proposal_id"] == first["result"]["proposal_id"]
    changes = {(c["entity"], c["id"]): c for c in second["result"]["changes"]}
    assert changes[("lots", "ibkr-4321:VOO:lot-3")]["change"] == "added"
    assert changes[("positions", "ibkr-4321:VOO")]["fields"]["quantity"] == {"before": "20", "after": "22"}
    assert changes[("positions", "ibkr-4321:CASH:USD")]["fields"]["quantity"] == {"before": "2345.67", "after": "1234.67"}

    saved = service.ingest("ana", "confirm", {"proposal_id": second["result"]["proposal_id"]})
    ledger = saved["result"]["ledger"]
    assert ledger["posted"] == 1 and ledger["already_recorded"] == 12 and ledger["held"] == []
    assert ledger["reconciliation"]["status"] == "ready"
    holdings = service.run("ledger", {"view": "holdings", "as_of": "2026-09-18"}, client_id="ana")["result"]
    assert {p["instrument_id"]: p["quantity"] for p in holdings["positions"]}["VOO"] == "22"
    assert {c["currency"]: c["balance"] for c in holdings["cash"]}["USD"] == "1234.67"
    diff = service.ingest("ana", "diff", {"proposal_id": second["result"]["proposal_id"]})
    assert diff["result"]["previous_proposal_id"] == first["result"]["proposal_id"]


def test_connector_status_reports_the_token_source_never_the_token(service):
    before = service.ingest("ana", "connector_status", {})
    [row] = before["result"]["connectors"]
    assert (row["name"], row["token"], row["ready"], row["last_sync"]) == ("ibkr_flex", "env", True, None)
    _sync(service)
    after = dispatch("ingest", {"client_id": "ana", "action": "connector_status", "inputs": {"name": "ibkr_flex"}},
                     service.db_path)
    last = after["result"]["connectors"][0]["last_sync"]
    assert (last["as_of"], last["confirmed"], last["query_id"]) == ("2026-08-31", False, QUERY)
    assert TOKEN not in json.dumps(before) + json.dumps(after)


def test_the_token_is_never_stored(service):
    proposal = _sync(service)
    service.ingest("ana", "confirm", {"proposal_id": proposal["result"]["proposal_id"]})
    raw = Path(service.db_path).read_bytes()
    for path in Path(service.db_path).parent.glob("wealth.sqlite3*"):
        raw += path.read_bytes()
    assert TOKEN.encode() not in raw
    assert b"U7654321" not in raw


def test_connector_inputs_are_validated(service):
    with pytest.raises(ValueError, match="name must be one of"):
        service.ingest("ana", "connector", {"name": "plaid", "query_id": QUERY})
    with pytest.raises(ValueError, match="query_id"):
        service.ingest("ana", "connector", {"name": "ibkr_flex"})
    with pytest.raises(ValueError, match="unknown"):
        service.ingest("ana", "connector", {"name": "ibkr_flex", "query_id": QUERY, "token": TOKEN})


def test_a_malformed_fx_row_is_listed_as_not_posted_instead_of_failing_the_batch():
    body = AUGUST.replace(b'symbol="EUR.USD" description="EUR.USD"', b'symbol="USD.USD" description="USD.USD"')
    mapping = ibkr_flex.ledger_batch(_proposal(body), batch_id="ingest:test", ledger={"entries": []})
    [bad] = [item for item in mapping["not_posted"] if "currencies must differ" in (item["reason"] or "")]
    assert bad["date"] == "2026-03-04"
    assert all(line["kind"] != "fx_conversion" for line in mapping["batch"]["transactions"])


def test_generic_proposals_keep_the_statement_mapper():
    assert connectors.batch_mapper({"result": {"provenance": {"kind": "document", "ref": "x.pdf"}}}) is None
    assert connectors.batch_mapper(_proposal()) is ibkr_flex.ledger_batch
    assert Decimal(_proposal()["result"]["reconciliation"]["accounts"][0]["difference"]) == 0
