import copy

from tests.fixtures.ingest import statements as fixtures
from wealth.ingest import ingest_bytes, validate_llm_extraction
from wealth.ingest.pdf import ingest_pdf

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def payload(**changes):
    value = {
        "institution": "Northwind Advisors", "as_of": "2026-08-31", "period_start": None, "currency": "USD",
        "accounts": [{
            "label": "Advisory account", "number_last4": "4321", "type": "brokerage", "currency": "USD", "page": 1,
            "positions": [{"symbol": "VTI", "description": None, "quantity": "100", "price": None,
                           "market_value": "$25,000.00", "cost_basis": None, "currency": "USD", "page": 1}],
            "cash": [{"label": "uninvested cash", "amount": "1,500.00", "currency": "USD", "page": 1}],
            "reported_total": {"label": "account value", "amount": "26,500.00", "currency": "USD", "page": 1},
            "flows": None, "liabilities": [], "transactions": [],
        }],
        "fx": [],
    }
    value.update(changes)
    return value


def request():
    return ingest_pdf(fixtures.prose_statement(), "letter.pdf")["result"]["extraction_request"]


def test_verified_llm_extraction_reconciles_and_is_ready():
    proposal = validate_llm_extraction(payload(), request())
    result = proposal["result"]
    assert proposal["status"] == "ready_to_confirm"
    assert result["verification"]["unverified"] == [] and result["verification"]["checked"] >= 6
    assert result["reconciliation"]["status"] == "reconciled"
    assert result["provenance"]["parser"] == "host-llm-validated" and result["provenance"]["sha256"]


def test_hallucinated_number_is_caught_and_blocks_confirmation():
    bad = payload()
    bad["accounts"][0]["positions"][0]["market_value"] = "25,400.00"
    bad["accounts"][0]["reported_total"]["amount"] = "26,900.00"
    proposal = validate_llm_extraction(bad, request())
    unverified = proposal["result"]["verification"]["unverified"]
    assert proposal["status"] == "needs_review"
    assert {u["path"] for u in unverified} == {
        "$.accounts[0].positions[0].market_value", "$.accounts[0].reported_total.amount"}
    assert any("do not appear in the source text" in r for r in proposal["result"]["review_reasons"])


def test_invented_symbol_date_and_account_suffix_are_caught():
    bad = payload(as_of="2026-08-30")
    bad["accounts"][0]["positions"][0]["symbol"] = "VOO"
    bad["accounts"][0]["number_last4"] = "9999"
    reasons = {u["path"] for u in validate_llm_extraction(bad, request())["result"]["verification"]["unverified"]}
    assert {"$.as_of", "$.accounts[0].positions[0].symbol", "$.accounts[0].number_last4"} <= reasons


def test_reconciliation_still_applies_to_verified_numbers():
    bad = payload()
    bad["accounts"][0]["cash"] = []
    proposal = validate_llm_extraction(bad, request())
    assert proposal["status"] == "needs_review"
    assert proposal["result"]["reconciliation"]["differences"][0]["difference"] == "-1500"


def test_schema_violations_are_rejected_without_a_proposal():
    bad = copy.deepcopy(payload())
    bad["accounts"][0]["ssn"] = "123-45-6789"
    bad["accounts"][0]["positions"][0]["quantity"] = 100
    proposal = validate_llm_extraction(bad, request())
    assert proposal["status"] == "rejected"
    assert "$.accounts[0].ssn is not allowed" in proposal["result"]["schema_errors"]
    assert "123-45-6789" not in str(proposal)


def test_image_routes_to_extraction_and_needs_text_to_verify():
    routed = ingest_bytes(PNG, "screenshot.jpg")
    assert routed["status"] == "needs_extraction" and routed["result"]["extraction_request"]["image"] is True
    blind = validate_llm_extraction(payload(), None)
    assert blind["status"] == "needs_review"
    assert blind["result"]["verification"]["source_text"] is False
    text = "Northwind Advisors as of August 31, 2026. VTI 100 shares $25,000.00, cash 1,500.00, total 26,500.00, account ending 4321"
    assert validate_llm_extraction(payload(), text)["status"] == "ready_to_confirm"


def test_image_with_host_text_uses_deterministic_parser():
    text = "\n".join([
        "Charles Schwab    Statement Period: August 1, 2026 - August 31, 2026",
        "Account Number: 1234-5678",
        "Symbol    Description         Quantity      Price      Market Value",
        "VTI       Total Market ETF         100     250.00         25,000.00",
        "Cash & Cash Investments                                    1,500.00",
        "Total Account Value                                       26,500.00",
    ])
    proposal = ingest_bytes(PNG, "phone.png", source_text=text)
    assert proposal["status"] == "ready_to_confirm"
    assert proposal["result"]["provenance"]["media_type"] == "image/png"
