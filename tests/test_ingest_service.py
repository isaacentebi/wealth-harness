"""Service-level ingestion: stored proposals, explicit confirmation, ledger posting."""
from __future__ import annotations

from decimal import Decimal

import pytest

from tests.fixtures.ingest import statements as fixtures
from wealth import server
from wealth.service import WealthService, dispatch, upload_dir
from wealth.store import WealthStore
from wealth.web import Uploads


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.delenv("WEALTH_UPLOAD_DIR", raising=False)
    wealth = WealthService(tmp_path / "wealth.sqlite3")
    wealth.create("ana", "Ana")
    upload_dir("ana", wealth.db_path).mkdir(parents=True)
    return wealth


def _upload(service, name, data):
    path = upload_dir("ana", service.db_path) / name
    path.write_bytes(data)
    return path


def test_upload_dir_matches_the_browser_chat_and_honours_the_override(tmp_path, monkeypatch):
    monkeypatch.delenv("WEALTH_UPLOAD_DIR", raising=False)
    db = tmp_path / "data" / "wealth.sqlite3"
    for client in ("ana", "a/b c", "..x"):
        assert upload_dir(client, db) == Uploads(db.resolve().parent, client).dir
    monkeypatch.setenv("WEALTH_UPLOAD_DIR", str(tmp_path / "elsewhere"))
    assert upload_dir("ana", db) == tmp_path / "elsewhere" / "ana"


def test_statement_flows_from_upload_to_ledger_household_and_exposure(service):
    _upload(service, "schwab-aug.pdf", fixtures.us_brokerage())
    proposal = service.ingest("ana", "file", {"path": "schwab-aug.pdf"})
    assert proposal["status"] == "ready_to_confirm"
    proposal_id = proposal["result"]["proposal_id"]
    assert "says yes" in proposal["result"]["confirmation"]["next_step"]
    assert service.inspect("ana", keys=["account.schwab-5678"])["absent_keys"] == ["account.schwab-5678"]

    saved = service.ingest("ana", "confirm", {"proposal_id": proposal_id})
    assert saved["status"] == "saved"
    assert saved["result"]["saved"]["keys"] == ["account.schwab-5678"]
    ledger = saved["result"]["ledger"]
    assert ledger["posted"] == 4 and ledger["held"] == [] and ledger["not_posted"] == []
    assert ledger["reconciliation"]["status"] == "ready"
    assert "Balances agree" in saved["result"]["summary"]
    assert service.ingest("ana", "confirm", {"proposal_id": proposal_id})["replayed"] is True

    holdings = service.run("ledger", {"view": "holdings", "as_of": "2026-08-31"}, client_id="ana")
    quantities = {p["instrument_id"]: p["quantity"] for p in holdings["result"]["positions"]}
    assert quantities == {"AAPL": "10.5", "BND": "150", "VTI": "100"}
    assert holdings["result"]["cash"][0]["balance"] == "1500.00"

    household = service.run("ledger", {"view": "household", "as_of": "2026-08-31", "currency": "USD",
                                       "prices": saved["result"]["statement_prices"]}, client_id="ana")
    values = {p["instrument_id"]: Decimal(p["value"]) for p in household["result"]["household"]["positions"]}
    assert values == {"AAPL": Decimal("2101.05"), "BND": Decimal("10000"), "VTI": Decimal("25000"),
                      "cash:USD": Decimal("1500")}

    exposure = service.run("exposure", {"household": household["result"]["household"], "evaluation_date": "2026-08-31"})
    assert exposure["status"] in {"ready", "partial"}
    assert Decimal(exposure["result"]["known_nav"]) == Decimal("38601.05")


def test_confirm_uses_only_the_stored_proposal_and_requires_acknowledged_discrepancies(service):
    with pytest.raises(ValueError, match="proposal_id is unknown"):
        service.ingest("ana", "confirm", {"proposal_id": "0" * 64})
    with pytest.raises(ValueError, match="unknown"):
        service.ingest("ana", "confirm", {"proposal_id": "x", "proposal": {"status": "ready_to_confirm"}})
    _upload(service, "bad-total.pdf", fixtures.us_brokerage(total="40,000.00"))
    proposal = service.ingest("ana", "file", {"path": "bad-total.pdf"})
    assert proposal["status"] == "needs_review"
    assert "acknowledge_discrepancies=true" in proposal["result"]["confirmation"]["next_step"]
    held = service.ingest("ana", "confirm", {"proposal_id": proposal["result"]["proposal_id"]})
    assert held["status"] == "needs_review"
    assert service.inspect("ana", keys=["account.schwab-5678"])["absent_keys"] == ["account.schwab-5678"]
    saved = service.ingest("ana", "confirm", {"proposal_id": proposal["result"]["proposal_id"],
                                              "acknowledge_discrepancies": True})
    assert saved["status"] == "saved"


def test_files_outside_the_upload_dir_are_refused(service, tmp_path):
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(fixtures.us_brokerage())
    refused = service.ingest("ana", "file", {"path": str(outside)})
    assert refused["status"] == "rejected"
    assert service.ingest("ana", "file", {"path": "../outside.pdf"})["status"] == "rejected"


def test_chat_items_need_the_persons_words_and_confirm_posts_cash(service):
    item = {"kind": "cash", "label": "HYSA", "account_type": "savings", "amount": "40000", "currency": "USD"}
    with pytest.raises(ValueError, match=r"items\[0\]\.quote"):
        service.ingest("ana", "chat", {"items": [item]})
    proposal = service.ingest("ana", "chat", {"items": [dict(item, quote="I have $40k in a HYSA")], "as_of": "2026-09-01"})
    assert proposal["status"] == "ready_to_confirm"
    saved = service.ingest("ana", "confirm", {"proposal_id": proposal["result"]["proposal_id"]})
    assert saved["result"]["ledger"]["posted"] == 1
    cash = service.run("ledger", {"view": "holdings", "as_of": "2026-09-01"}, client_id="ana")["result"]["cash"]
    assert [c["balance"] for c in cash] == ["40000.00"]


def test_extraction_validates_against_the_stored_request(service):
    _upload(service, "letter.pdf", fixtures.prose_statement())
    first = service.ingest("ana", "file", {"path": "letter.pdf"})
    assert first["status"] == "needs_extraction"
    extraction_id = first["result"]["extraction_id"]
    with pytest.raises(ValueError, match="extraction_id is unknown"):
        service.ingest("ana", "extraction", {"extraction_id": "nope", "payload": {}})
    rejected = service.ingest("ana", "extraction", {"extraction_id": extraction_id, "payload": {"accounts": "x"}})
    assert rejected["status"] == "rejected"


def test_held_duplicates_post_only_after_confirm_duplicates(service):
    with WealthStore(service.db_path) as store:
        store.post_ledger("ana", {
            "batch_id": "chat-1", "source": {"kind": "user", "ref": "conversation"},
            "accounts": [{"id": "bbva-6789", "institution": "BBVA", "type": "checking", "currency": "MXN"}],
            "transactions": [{"kind": "transfer", "account_id": "bbva-6789", "date": "2026-08-02", "amount": "20000",
                              "currency": "MXN", "description": "SPEI RECIBIDO BANORTE"}],
        })
    _upload(service, "bbva.pdf", fixtures.bbva_checking())
    proposal = service.ingest("ana", "file", {"path": "bbva.pdf"})
    proposal_id = proposal["result"]["proposal_id"]
    saved = service.ingest("ana", "confirm", {"proposal_id": proposal_id})
    held = saved["result"]["ledger"]["held"]
    assert len(held) == 1 and held[0]["amount"] == "20000"
    with pytest.raises(ValueError, match="were not held"):
        service.ingest("ana", "confirm_duplicates", {"proposal_id": proposal_id, "entry_ids": ["tx_other"]})
    posted = service.ingest("ana", "confirm_duplicates", {"proposal_id": proposal_id, "entry_ids": [held[0]["entry_id"]]})
    assert posted["result"]["posted"] == [held[0]["entry_id"]] and posted["result"]["still_held"] == []


def test_diff_reports_changes_since_the_last_confirmed_statement(service):
    _upload(service, "one.pdf", fixtures.us_brokerage())
    first = service.ingest("ana", "file", {"path": "one.pdf"})
    service.ingest("ana", "confirm", {"proposal_id": first["result"]["proposal_id"]})
    _upload(service, "two.pdf", fixtures.us_brokerage(second_account=True))
    second = service.ingest("ana", "file", {"path": "two.pdf"})
    diff = service.ingest("ana", "diff", {"proposal_id": second["result"]["proposal_id"]})
    assert diff["result"]["previous_proposal_id"] == first["result"]["proposal_id"]
    added = {(c["entity"], c["id"]) for c in diff["result"]["changes"] if c["change"] == "added"}
    assert ("accounts", "schwab-4321") in added


def test_ingest_is_on_the_cli_and_mcp_surface(service):
    with pytest.raises(ValueError, match="action must be one of"):
        dispatch("ingest", {"client_id": "ana", "action": "upload", "inputs": {}}, service.db_path)
    with pytest.raises(ValueError, match="unknown operation 'prepare'"):
        dispatch("prepare", {}, service.db_path)
    assert "wealth_ingest" in {tool.name for tool in server.build_server(str(service.db_path))._tool_manager.list_tools()}
