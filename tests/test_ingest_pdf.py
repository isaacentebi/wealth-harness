import json

from tests.fixtures.ingest import statements as fixtures
from wealth.household import validate_household
from wealth.ingest import ingest_bytes
from wealth.ingest.pdf import ingest_pdf


def dump(proposal):
    return json.dumps(proposal, ensure_ascii=False)


def positions(proposal, account_id=None):
    rows = proposal["result"]["household"]["positions"]
    return {p["instrument_id"]: p for p in rows if account_id in (None, p["account_id"])}


def test_reconciled_us_statement_is_ready_and_page_referenced():
    proposal = ingest_pdf(fixtures.us_brokerage(), "schwab-aug.pdf")
    result = proposal["result"]
    assert proposal["status"] == "ready_to_confirm"
    assert (result["as_of"], result["currency"]) == ("2026-08-31", "USD")
    account = result["reconciliation"]["accounts"][0]
    assert account["status"] == "reconciled" and account["computed_total"] == account["reported_total"] == "38601.05"
    assert account["positions_subtotal"]["matches"] is True
    held = positions(proposal)
    assert held["VTI"]["value"] == "25000" and held["VTI"]["cost_basis"] == "20000" and held["VTI"]["page"] == 1
    assert held["CASH:USD"]["value"] == "1500"
    assert result["household"]["accounts"][0]["number_masked"] == "****5678"
    assert result["provenance"]["sha256"] and result["provenance"]["ref"].startswith("document:sha256:")
    assert validate_household({**result["household"], "as_of": "2026-08-31"})["household"]


def test_total_mismatch_needs_review_with_discrepancy_and_llm_fallback():
    proposal = ingest_pdf(fixtures.us_brokerage(total="40,000.00"), "schwab-aug.pdf")
    result = proposal["result"]
    assert proposal["status"] == "needs_review"
    difference = next(d for d in result["reconciliation"]["differences"] if d["check"] == "account_total")
    assert difference["expected"] == "40000" and difference["computed"] == "38601.05"
    assert difference["difference"] == "-1398.95"
    assert result["extraction_request"]["kind"] == "extraction_request"


def test_multi_account_statement_splits_accounts_and_types():
    proposal = ingest_pdf(fixtures.us_brokerage(second_account=True), "two.pdf")
    accounts = {a["id"]: a for a in proposal["result"]["household"]["accounts"]}
    assert proposal["status"] == "ready_to_confirm"
    assert set(accounts) == {"schwab-5678", "schwab-4321"}
    assert accounts["schwab-4321"]["type"] == "roth_ira"
    assert positions(proposal, "schwab-4321")["VXUS"]["value"] == "12000"


def test_mexican_statement_bmv_sic_ucits_and_government_paper():
    proposal = ingest_pdf(fixtures.gbm_multicurrency(), "gbm-agosto.pdf")
    result = proposal["result"]
    assert proposal["status"] == "ready_to_confirm"
    assert (result["as_of"], result["currency"]) == ("2026-08-31", "MXN")
    held = positions(proposal, "gbm-4567")
    assert held["BMV:AMX B"]["venue"] == "bmv" and held["BMV:WALMEX"]["listing_symbol"] == "WALMEX *"
    assert held["BMV:FUNO 11"]["instrument_type"] == "fibra" and held["BMV:NAFTRAC ISHRS"]["asset_class"] == "fund"
    aapl, cspx, ivv = held["SIC:AAPL"], held["SIC:CSPX"], held["SIC:IVV"]
    assert (aapl["venue"], aapl["currency"], aapl["listing_currency"], aapl["underlying_symbol"]) == ("sic", "MXN", "MXN", "AAPL")
    assert aapl["issuer_domicile"] == "US" and aapl["value"] == "21000" and aapl["cost_basis"] == "19500"
    assert cspx["issuer_domicile"] == "IE" and cspx["listing_symbol"] == "CSPX N" and cspx["asset_class"] == "fund"
    assert ivv["issuer_domicile"] == "US" and ivv["currency"] == "MXN"
    cetes = held["BI CETES 261015"]
    assert (cetes["instrument_type"], cetes["maturity"], cetes["issuer"]) == ("cetes", "2026-10-15", "Gobierno Federal (MX)")
    assert "venue" not in cetes
    assert held["S UDIBONO 351122"]["denomination"] == "UDI"
    fx = {(f["from"], f["to"]): f["rate"] for f in result["household"]["fx"]}
    assert fx == {("USD", "MXN"): "18.25", ("UDI", "MXN"): "8.4512"}
    usd = next(a for a in result["reconciliation"]["accounts"] if a["account_id"] == "gbm-4321")
    assert usd["currency"] == "USD" and usd["status"] == "reconciled"
    assert validate_household(result["household"])["household"]["accounts"]


def test_mexican_statement_mismatch_names_the_account():
    proposal = ingest_pdf(fixtures.gbm_multicurrency(total="220,000.00"), "gbm.pdf")
    assert proposal["status"] == "needs_review"
    assert [d["account_id"] for d in proposal["result"]["reconciliation"]["differences"]] == ["gbm-4567"]


def test_spanish_bank_statement_transactions_and_balance_assertions():
    proposal = ingest_pdf(fixtures.bbva_checking(), "bbva.pdf")
    result = proposal["result"]
    assert proposal["status"] == "ready_to_confirm"
    assert result["household"]["accounts"][0]["type"] == "checking"
    rows = result["transactions"]
    assert [(t["date"], t["amount"], t["type"]) for t in rows] == [
        ("2026-08-01", "20000", "transfer"), ("2026-08-05", "-5000", "loan_payment"),
        ("2026-08-15", "5000", "income"), ("2026-08-20", "-3000", "withdrawal"), ("2026-08-31", "-500", "fee"),
    ]
    assert rows[3]["settlement_date"] == "2026-08-21" and rows[0]["balance"] == "30000"
    assert len({t["dedupe_hash"] for t in rows}) == 5
    assert result["balance_assertions"] == [{
        "account_id": "bbva-6789", "period_start": "2026-08-01", "period_end": "2026-08-31", "opening": "10000",
        "closing": "26500", "currency": "MXN", "balance_kind": "cash", "page": 1, "transactions_reconcile": True,
    }]
    assert positions(proposal)["CASH:MXN"]["value"] == "26500"
    again = ingest_pdf(fixtures.bbva_checking(), "renamed.pdf")
    assert [t["dedupe_hash"] for t in again["result"]["transactions"]] == [t["dedupe_hash"] for t in rows]


def test_bank_statement_with_wrong_closing_balance_needs_review():
    proposal = ingest_pdf(fixtures.bbva_checking(closing="27,500.00"), "bbva.pdf")
    checks = {d["check"] for d in proposal["result"]["reconciliation"]["differences"]}
    assert proposal["status"] == "needs_review"
    assert {"cash_flow", "transaction_sum"} <= checks


def test_mexican_credit_card_with_meses_sin_intereses():
    proposal = ingest_pdf(fixtures.mx_credit_card(), "banorte-tdc.pdf")
    result = proposal["result"]
    assert proposal["status"] == "ready_to_confirm"
    liability = result["household"]["liabilities"][0]
    assert (liability["value"], liability["monthly_payment"], liability["interest_rate"]) == ("2200", "220", "0.455")
    rows = {t["description"]: t for t in result["transactions"]}
    assert rows["AMAZON MX MARKETPLACE"]["amount"] == "-1500" and rows["AMAZON MX MARKETPLACE"]["type"] == "expense"
    assert rows["SU PAGO GRACIAS"]["amount"] == "3000" and rows["SU PAGO GRACIAS"]["type"] == "loan_payment"
    msi = rows["LIVERPOOL INSURGENTES"]
    assert msi["installment"] == {"number": 3, "count": 12, "original_amount": "12000", "remaining_balance": "8000"}
    assert msi["amount"] == "-1000" and msi["date"] == "2026-06-15"
    assertion = result["balance_assertions"][0]
    assert assertion["balance_kind"] == "debt" and assertion["transactions_reconcile"] is True


def test_identifiers_are_redacted_everywhere():
    for data in (fixtures.us_brokerage(), fixtures.gbm_multicurrency(), fixtures.bbva_checking(),
                 fixtures.us_brokerage(total="1.00")):
        text = dump(ingest_pdf(data, "statement 0123456789.pdf"))
        for secret in ("123-45-6789", "GODE561231GR8", "GODE561231HDFRRN09", "0123456789", "012180001234567891",
                       "1234-5678", "1234567"):
            assert secret not in text


def test_encrypted_pdf_needs_an_unlocked_copy_and_never_a_password():
    proposal = ingest_pdf(fixtures.render([("text", "Statement")], password="secret"), "locked.pdf")
    assert proposal["status"] == "needs_input"
    assert proposal["missing"][0]["reason"] == "encrypted"
    assert "password" in proposal["missing"][0]["detail"] and "secret" not in dump(proposal)


def test_active_content_is_flagged_and_blocks_silent_confirmation():
    for build, flag in ((fixtures.with_javascript, "javascript"), (fixtures.with_attachment, "embedded_file")):
        proposal = ingest_bytes(build(fixtures.us_brokerage()), "statement.pdf")
        assert flag in proposal["result"]["provenance"]["risk_flags"]
        assert proposal["status"] == "needs_review"
        assert any("active or embedded" in w for w in proposal["warnings"])


def test_unknown_layout_returns_redacted_extraction_request():
    proposal = ingest_pdf(fixtures.prose_statement(), "letter.pdf")
    request = proposal["result"]["extraction_request"]
    assert proposal["status"] == "needs_extraction"
    assert request["schema"]["additionalProperties"] is False
    assert request["pages"][0]["page"] == 1 and "26,500.00" in request["pages"][0]["text"]
