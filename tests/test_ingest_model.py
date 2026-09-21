import os
from datetime import date, timedelta
from decimal import Decimal

import pytest

from tests.fixtures.ingest import statements as fixtures
from wealth.ingest import (
    diff_proposals, ingest_bytes, ingest_file, merge_household, proposal_from_chat, proposal_to_facts, redact_text,
)
from wealth.ingest.common import find_dates, number_tokens, parse_amount
from wealth.ingest.pdf import ingest_pdf
from wealth.ingest.safety import sniff
from wealth.store import WealthStore


@pytest.fixture(scope="module")
def ready():
    return ingest_pdf(fixtures.gbm_multicurrency(), "gbm.pdf")


def test_facts_require_explicit_confirmation_and_matching_id(ready):
    pid = ready["result"]["proposal_id"]
    assert proposal_to_facts(ready, confirmed=False, proposal_id=pid)["status"] == "needs_input"
    with pytest.raises(ValueError, match="does not match"):
        proposal_to_facts(ready, confirmed=True, proposal_id="0" * 64)
    tampered = {**ready, "result": {**ready["result"], "household": {**ready["result"]["household"], "fx": []}}}
    with pytest.raises(ValueError, match="does not match"):
        proposal_to_facts(tampered, confirmed=True, proposal_id=pid)


def test_confirmed_facts_are_valid_store_payloads_and_idempotent(ready):
    saved = proposal_to_facts(ready, confirmed=True, proposal_id=ready["result"]["proposal_id"], today=date(2026, 9, 1))
    facts = saved["result"]["facts"]
    assert saved["status"] == "ready"
    assert [f["key"] for f in facts] == ["account.gbm-4567", "account.gbm-4321"]
    assert all(f["source"]["kind"] == "document" and f["confidence"] == "reported" for f in facts)
    assert facts[0]["source"]["observed_on"] == "2026-08-31" and facts[0]["expires_on"] == "2026-10-15"
    assert facts[0]["value"]["reconciliation"]["status"] == "reconciled"
    with WealthStore(":memory:") as store:
        store.create_client("c1", "Test")
        request_id = saved["result"]["request_id"]
        first = store.remember("c1", facts, 0, request_id)
        again = store.remember("c1", facts, 0, request_id)
        assert first["write_result"]["resulting_revision"] == 1 and again["write_result"]["replayed"] is True


def test_needs_review_requires_acknowledged_discrepancies():
    proposal = ingest_pdf(fixtures.us_brokerage(total="40,000.00"), "s.pdf")
    pid = proposal["result"]["proposal_id"]
    assert proposal_to_facts(proposal, confirmed=True, proposal_id=pid)["status"] == "needs_review"
    saved = proposal_to_facts(proposal, confirmed=True, proposal_id=pid, acknowledge_discrepancies=True)
    assert saved["result"]["facts"][0]["value"]["acknowledged_discrepancies"]


def test_transactions_are_saved_with_balance_assertions():
    proposal = ingest_pdf(fixtures.bbva_checking(), "bbva.pdf")
    facts = proposal_to_facts(proposal, confirmed=True, proposal_id=proposal["result"]["proposal_id"])["result"]["facts"]
    by_key = {f["key"]: f["value"] for f in facts}
    assert len(by_key["account.bbva-6789.activity"]["transactions"]) == 5
    assert by_key["account.bbva-6789"]["balance_assertions"][0]["closing"] == "26500"


def test_diff_is_empty_for_identical_pulls_and_lists_changes():
    first = ingest_pdf(fixtures.us_brokerage(), "a.pdf")
    same = ingest_pdf(fixtures.us_brokerage(), "a.pdf")
    assert first["result"]["proposal_id"] == same["result"]["proposal_id"]
    assert diff_proposals(first, same) == []
    wider = ingest_pdf(fixtures.us_brokerage(second_account=True), "b.pdf")
    changes = diff_proposals(first, wider)
    assert {"entity": "accounts", "id": "schwab-4321", "change": "added"} in changes
    assert all(c["change"] == "added" for c in changes)


def test_merge_into_existing_household_replaces_same_account(ready):
    existing = {
        "currency": "MXN", "as_of": "2026-07-31", "complete": False, "people": [{"id": "self"}],
        "accounts": [{"id": "gbm-4567", "owner_id": "self", "type": "brokerage", "currency": "MXN"},
                     {"id": "bank-1", "owner_id": "self", "type": "checking", "currency": "MXN"}],
        "positions": [{"id": "old", "account_id": "gbm-4567", "instrument_id": "OLD", "symbol": "OLD",
                       "quantity": 1, "value": 1, "currency": "MXN"}],
        "lots": [], "liabilities": [], "external_assets": [], "income_exposures": [], "fx": [], "fund_holdings": [],
    }
    merged = merge_household(existing, ready)
    household = merged["household"]
    assert household["as_of"] == "2026-08-31"
    assert {a["id"] for a in household["accounts"]} == {"gbm-4567", "gbm-4321", "bank-1"}
    assert "old" not in {p["id"] for p in household["positions"]}
    assert any("dated 2026-08-31" in w for w in merged["warnings"])


def test_chat_facts_share_the_proposal_shape_and_verify_numbers():
    proposal = proposal_from_chat([
        {"kind": "cash", "label": "HYSA", "amount": "40000", "currency": "USD", "rate": "4.2%",
         "quote": "I have $40k in a HYSA at 4.2%"},
        {"kind": "liability", "label": "Car loan", "amount": "12,500", "currency": "USD", "monthly_payment": "450",
         "quote": "I still owe 12,500 on the car, paying $450 a month"},
        {"kind": "income", "label": "Salary", "amount": "120000", "currency": "USD", "quote": "I make about $120k a year"},
    ], as_of="2026-09-01", currency="USD")
    result = proposal["result"]
    assert proposal["status"] == "ready_to_confirm" and result["source_kind"] == "user"
    account = result["household"]["accounts"][0]
    assert (account["type"], account["interest_rate"]) == ("savings", "0.042")
    assert result["household"]["positions"][0]["value"] == "40000"
    facts = proposal_to_facts(proposal, confirmed=True, proposal_id=result["proposal_id"])["result"]["facts"]
    assert {f["source"]["kind"] for f in facts} == {"user"}
    assert {f["key"].split(".")[0] for f in facts} == {"account", "income", "liability"}
    liability = next(f["value"]["liability"] for f in facts if f["key"].startswith("liability."))
    assert (liability["value"], liability["monthly_payment"]) == ("12500", "450")


def test_chat_number_not_in_quote_needs_review_and_unknown_is_not_zero():
    proposal = proposal_from_chat([
        {"kind": "cash", "label": "HYSA", "amount": "45000", "currency": "USD", "quote": "I have $40k in a HYSA"},
        {"kind": "account", "label": "401k", "currency": "USD", "quote": "I also have a 401k"},
    ], as_of="2026-09-01")
    assert proposal["status"] == "needs_review"
    assert proposal["result"]["verification"]["unverified"][0]["path"] == "$.items[0].amount"
    assert any(m["key"] == "items[1].amount" for m in proposal["missing"])
    assert not any(p["value"] == "0" for p in proposal["result"]["household"]["positions"])


def test_amount_and_date_parsing_for_us_and_mexican_formats():
    assert parse_amount("$1,234.56 MN") == Decimal("1234.56")
    assert parse_amount("(1,234.56)") == Decimal("-1234.56")
    assert parse_amount("1.234,56") == Decimal("1234.56")
    assert parse_amount("1,234.56-") == Decimal("-1234.56")
    assert parse_amount("Dls. 950.00") == Decimal("950.00")
    assert parse_amount("--") is None and parse_amount("12%") is None
    assert Decimal(40000) in number_tokens("about $40k") and Decimal("0.042") in number_tokens("at 4.2%")
    assert find_dates("Periodo del 01/08/2026 al 31/08/2026", day_first=True)[-1][0] == date(2026, 8, 31)
    assert find_dates("Fecha de corte: 30 de septiembre de 2025")[0][0] == date(2025, 9, 30)
    assert find_dates("03/04/2026")[0][1] == "low"


def test_redaction_masks_accounts_and_removes_tax_ids():
    text = redact_text("Account Number: 1234-5678 SSN 123-45-6789 RFC GODE561231GR8 CLABE 012180001234567891 "
                       "Contrato: 7654321 value 25,000.00 ISIN US9229083632 date 2026-08-31")
    assert "****5678" in text and "****4321" in text and "****7891" in text
    for secret in ("123-45-6789", "GODE561231GR8", "012180001234567891", "7654321"):
        assert secret not in text
    assert "25,000.00" in text and "US9229083632" in text and "2026-08-31" in text


def test_sniffing_uses_content_not_extension_and_paths_are_confined(tmp_path):
    pdf = fixtures.us_brokerage()
    assert sniff(pdf) == "pdf" and sniff(b"MZ\x90\x00" + b"\x00" * 100) == "binary"
    disguised = tmp_path / "statement.pdf"
    disguised.write_bytes(b"\x7fELF" + b"\x00" * 200)
    assert ingest_file(disguised, allowed_roots=[tmp_path])["status"] == "rejected"
    renamed = tmp_path / "positions.txt"
    renamed.write_bytes(pdf)
    assert ingest_file(renamed, allowed_roots=[tmp_path])["status"] == "ready_to_confirm"
    outside = tmp_path.parent / "outside.pdf"
    outside.write_bytes(pdf)
    assert ingest_file(outside, allowed_roots=[tmp_path])["status"] == "rejected"
    assert ingest_file(f"{tmp_path}/../outside.pdf", allowed_roots=[tmp_path])["status"] == "rejected"
    link = tmp_path / "link.pdf"
    os.symlink(outside, link)
    assert ingest_file(link, allowed_roots=[tmp_path])["status"] == "rejected"
    outside.unlink()


def test_size_limits_and_unsupported_types():
    assert ingest_bytes(b"%PDF-1.7\n" + b"0" * (26 * 1024 * 1024), "big.pdf")["result"]["reason"] == "too_large"
    assert ingest_bytes(b"PK\x03\x04" + b"\x00" * 100, "x.zip")["status"] == "rejected"


def test_proposal_to_facts_rejects_future_or_stale_dates():
    proposal = ingest_pdf(fixtures.us_brokerage(), "s.pdf")
    pid = proposal["result"]["proposal_id"]
    stale = proposal_to_facts(proposal, confirmed=True, proposal_id=pid, today=date(2026, 8, 31) + timedelta(days=120))
    assert stale["warnings"] and "already expired" in stale["warnings"][0]
    with pytest.raises(ValueError, match="future"):
        proposal_to_facts(proposal, confirmed=True, proposal_id=pid, today=date(2026, 8, 1))


def test_mexican_instrument_and_listing_recognition():
    from wealth.ingest.classify import classify_instrument, listing

    assert classify_instrument("PRLV BANORTE 261120", None) == {
        "asset_class": "fixed_income", "instrument_type": "pagare", "liquid": False, "maturity": "2026-11-20"}
    assert classify_instrument("LF BONDESF 290104", None)["instrument_type"] == "bondes"
    assert classify_instrument("S UDIBONO 351122", None)["denomination"] == "UDI"
    assert classify_instrument("Reporto", "Gubernamental")["instrument_type"] == "reporto"
    assert classify_instrument("FMTY 14", None)["instrument_type"] == "fibra"
    assert listing("FEMSA UBD", None, market="mx")["venue"] == "bmv"
    assert listing("VUAA N", "VANGUARD S&P 500 UCITS IE00BFMXXD54", market="mx")["issuer_domicile"] == "IE"
    assert listing("SHOP *", "SHOPIFY CA82509L1076", "Mercado Global (SIC)")["issuer_domicile"] == "CA"
    assert listing("XYZ", None, market="mx") == {}
