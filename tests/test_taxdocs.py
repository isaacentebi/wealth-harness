"""Annual tax documents from uploads: detection, parsing, validation, confirmation and the tax pack."""
from __future__ import annotations

from copy import deepcopy

import pytest

from tests.fixtures.ingest import statements, taxdocs
from wealth import consent, ledger as ledger_module, server
from wealth.catalog import CATALOG
from wealth.ingest import detect_tax_document, ingest_bytes, validate_llm_extraction
from wealth.ingest.pdf import extract_pdf
from wealth.ingest.taxdoc import TAX_EXTRACTION_SCHEMA
from wealth.service import WealthService, upload_dir
from wealth.store import WealthStore

MX = CATALOG["tax_pack"]["example"]
US = CATALOG["tax_pack"]["variants"]["us_schwab_wash_sale"]


def pages(data: bytes) -> list[tuple[int, str]]:
    return extract_pdf(data)["pages"]


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.delenv("WEALTH_UPLOAD_DIR", raising=False)
    wealth = WealthService(tmp_path / "wealth.sqlite3")
    wealth.create("ana", "Ana")
    upload_dir("ana", wealth.db_path).mkdir(parents=True)
    return wealth


def upload(service, name, data, client="ana"):
    (upload_dir(client, service.db_path) / name).write_bytes(data)
    return service.ingest(client, "file", {"path": name})


def seed(service, client, example):
    """The catalog example's profile, stated tax facts and ledger, without its typed-in constancias."""
    said = {"kind": "user", "ref": "conversation", "observed_on": "2026-02-20"}
    service.remember(client, [dict(f, source=said) for f in deepcopy(example["facts"])
                              if not f["key"].startswith("constancia.")])
    ledger = example["ledger"]
    with WealthStore(service.db_path) as store:
        ledger_module.post(store, client, {
            "batch_id": "seed", "source": {"kind": "document", "ref": "estado de cuenta", "observed_on": "2026-01-10"},
            "accounts": ledger["accounts"], "instruments": ledger["instruments"],
            "transactions": [dict({k: v for k, v in e.items() if k not in {"id", "confidence", "source"}},
                                  external_id=e["id"]) for e in ledger["entries"]]})


# ------------------------------------------------------------------ detection


@pytest.mark.parametrize("fixture, kind, institution", [
    (taxdocs.gbm_constancia, "mx_constancia", "GBM"),
    (taxdocs.bbva_interest_constancia, "mx_constancia", "BBVA"),
    (taxdocs.schwab_1099, "us_1099", "Charles Schwab"),
    (taxdocs.schwab_5498, "us_5498", "Charles Schwab"),
    (taxdocs.actinver_prose_constancia, "mx_constancia", "Actinver"),
])
def test_the_document_type_institution_and_year_come_from_the_text(fixture, kind, institution):
    found = detect_tax_document(pages(fixture()))
    assert (found["document_type"], found["institution"], found["tax_year"]) == (kind, institution, 2025)


def test_statements_are_not_taken_for_tax_documents():
    for data in (statements.us_brokerage(), statements.gbm_multicurrency(), statements.bbva_checking()):
        assert detect_tax_document(pages(data)) is None
    mention = [(1, "Charles Schwab\nBrokerage Statement\nAugust 2026\n\nYour 2025 Form 1099-B and constancia fiscal "
                   "are available online.")]
    assert detect_tax_document(mention) is None


# -------------------------------------------------------------------- parsing


def test_a_gbm_constancia_is_read_deterministically_and_reconciles():
    report = ingest_bytes(taxdocs.gbm_constancia(), "constancia-gbm-2025.pdf")
    assert report["status"] == "ready_to_confirm"
    result = report["result"]
    assert result["provenance"]["parser"] == "taxdoc:mx_constancia"
    assert result["figures"]["enajenacion"] == {"gain": "5417.00", "loss": "2830.00", "net": "2587.00"}
    assert result["figures"]["intereses"] == {"nominal": "4500.00", "inflation_adjustment": "2100.00",
                                              "real": "2400.00", "real_loss": "0.00", "isr_withheld": "400.00"}
    assert result["figures"]["dividendos"]["domestic_gross"] == "1200.00"
    assert result["figures"]["dividendos"]["isr_withheld"] == "120.00"
    assert (result["tax_year"], result["issued_on"], result["account_last4"]) == (2025, "2026-02-13", "5678")
    assert result["reconciliation"]["status"] == "reconciled"
    assert {c["check"].split(":")[0] for c in result["reconciliation"]["checks"]} == {"Art. 129", "Intereses",
                                                                                   "Dividendos"}
    assert result["verification"]["unverified"] == []
    [preview] = result["facts_preview"]
    assert preview["key"] == "constancia.gbm_2025"
    assert preview["value"]["enajenacion"] == {"gain": 5417.0, "loss": 2830.0, "net": 2587.0}
    assert "inflation_adjustment" not in preview["value"]["intereses"]  # shown and checked, not a fact field
    assert "XAXX010101000" not in str(preview) and "ANA EJEMPLO" not in str(preview)


def test_a_bbva_interest_constancia_carries_a_real_loss():
    result = ingest_bytes(taxdocs.bbva_interest_constancia(), "bbva.pdf")["result"]
    assert result["figures"] == {"intereses": {"nominal": "1800.00", "inflation_adjustment": "2718.20",
                                               "real": "0.00", "real_loss": "918.20", "isr_withheld": "300.00"}}
    assert result["facts_preview"][0]["key"] == "constancia.bbva_2025"
    assert result["reconciliation"]["checks"][0]["ok"] is True  # 1,800 - 2,718.20 = 0 - 918.20


def test_a_schwab_1099_composite_reads_every_lot_and_the_wash_sale():
    report = ingest_bytes(taxdocs.schwab_1099(), "schwab-1099-2025.pdf")
    assert report["status"] == "ready_to_confirm"
    result = report["result"]
    lots = result["lots"]
    assert [(lot["symbol"], lot["term"], lot["gain"]) for lot in lots] == [
        ("VTI", "short", "0.00"), ("VTI", "short", "400.00"), ("SCHD", "long", "700.00")]
    assert lots[0]["wash_sale_disallowed"] == "400.00" and lots[1]["wash_sale_disallowed"] is None
    assert {t["term"] for t in result["term_totals"]} == {"short", "long", "all"}
    assert result["figures"]["form_1099_b"] == {"short_term_gain": "400.00", "long_term_gain": "700.00",
                                                "proceeds": "16300.00", "cost_basis": "15600.00",
                                                "wash_sale_disallowed": "400.00"}
    assert result["figures"]["form_1099_div"]["qualified"] == "90.00"
    assert result["figures"]["form_1099_int"]["interest"] == "40.00"
    checks = result["reconciliation"]["checks"]
    assert all(c["ok"] for c in checks) and any("lots sum" in c["check"] for c in checks)
    value = result["facts_preview"][0]["value"]
    assert result["facts_preview"][0]["key"] == "constancia.schwab_2025_1099"
    assert value["form_1099_div"] == {"ordinary": 105.0, "qualified": 90.0, "capital_gain_distributions": 0.0,
                                      "foreign_tax_paid": 4.0}
    assert "6789" not in str(result["facts_preview"])


def test_a_form_5498_is_read_by_box():
    result = ingest_bytes(taxdocs.schwab_5498(), "5498.pdf")["result"]
    assert result["facts_preview"][0]["key"] == "constancia.schwab_2025_5498"
    assert result["figures"]["form_5498"]["roth_contributions"] == "6000.00"
    assert result["figures"]["form_5498"]["fair_market_value"] == "6420.00"


# ----------------------------------------------------------------- validation


def test_a_tampered_1099_total_needs_review_and_offers_a_re_extraction():
    report = ingest_bytes(taxdocs.schwab_1099(short_proceeds="12,500.00"), "schwab-1099-2025.pdf")
    assert report["status"] == "needs_review"
    result = report["result"]
    assert result["reconciliation"]["status"] == "unreconciled"
    assert any("short-term lots sum to the printed proceeds" in r and "12500.00" in r for r in result["review_reasons"])
    assert result["extraction_request"]["schema"] is TAX_EXTRACTION_SCHEMA


def test_a_tampered_constancia_net_needs_review():
    report = ingest_bytes(taxdocs.gbm_constancia(net="2,600.00"), "gbm.pdf")
    assert report["status"] == "needs_review"
    assert "the parts give 2587.00 but the document prints 2600.00" in report["result"]["review_reasons"][0]


def test_an_unknown_layout_goes_to_the_host_model_with_the_tax_schema(service):
    held = upload(service, "actinver-2025.pdf", taxdocs.actinver_prose_constancia())
    assert held["status"] == "needs_extraction"
    request = held["result"]["extraction_request"]
    assert request["document_kind"] == "tax_document" and request["schema"]["title"].startswith("Wealth annual tax")
    blank = {key: None for key in TAX_EXTRACTION_SCHEMA["properties"]} | {"lots": [], "term_totals": []}
    invented = dict(blank, document_type="mx_constancia", institution="Actinver", tax_year="2025",
                    enajenacion={"gain": "3,210.50", "loss": None, "net": None, "isr_withheld": None})
    flagged = service.ingest("ana", "extraction", {"extraction_id": held["result"]["extraction_id"], "payload": invented})
    assert flagged["status"] == "needs_review"
    assert flagged["result"]["verification"]["unverified"][0]["path"] == "enajenacion.gain"
    # Not saved unless the person accepts the unverified figure.
    assert service.ingest("ana", "confirm", {"proposal_id": flagged["result"]["proposal_id"]})["status"] == "needs_review"
    printed = dict(invented, enajenacion={"gain": "3,210", "loss": None, "net": None, "isr_withheld": None})
    good = service.ingest("ana", "extraction", {"extraction_id": held["result"]["extraction_id"], "payload": printed})
    assert good["status"] == "ready_to_confirm"
    assert good["result"]["provenance"]["parser"] == "host-llm-validated"
    assert good["result"]["facts_preview"][0] == {"key": "constancia.actinver_2025", "value": {
        "tax_year": 2025, "institution": "Actinver", "currency": "MXN", "enajenacion": {"gain": 3210.0}}}
    broken = dict(blank, enajenacion={"gain": 3210})
    assert validate_llm_extraction(broken, request)["status"] == "rejected"


# ------------------------------------------------------------------- confirm


def test_confirming_saves_grounded_document_facts(service):
    proposal = upload(service, "constancia-gbm-2025.pdf", taxdocs.gbm_constancia())
    result = proposal["result"]
    assert "says yes" in result["confirmation"]["next_step"]
    assert service.inspect("ana", keys=["constancia.gbm_2025"])["absent_keys"] == ["constancia.gbm_2025"]
    summary = server._proposal_summary(result, {})
    assert "Constancia fiscal anual 2025 from GBM, as constancia.gbm_2025" in summary
    assert "enajenacion: gain 5,417.00, loss 2,830.00, net 2,587.00" in summary

    saved = service.ingest("ana", "confirm", {"proposal_id": result["proposal_id"]})
    assert saved["status"] == "saved" and saved["result"]["saved"]["keys"] == ["constancia.gbm_2025"]
    [fact] = service.inspect("ana", keys=["constancia.gbm_2025"])["facts"]
    assert fact["source"]["kind"] == "document" and fact["confidence"] == "reported"
    assert fact["value"] == result["facts_preview"][0]["value"]
    # Grounding as the MCP boundary checks it: the ref names an ingested file and every figure is the proposal's.
    sources = service.ingested_sources("ana")
    assert server._cites_ingested(fact["source"]["ref"], sources)
    assert consent.ungrounded(fact["value"], consent.document_figures(result)) == []
    assert service.ingest("ana", "confirm", {"proposal_id": result["proposal_id"]})["replayed"] is True
    with pytest.raises(ValueError, match="statements"):
        service.ingest("ana", "diff", {"proposal_id": result["proposal_id"]})


def test_a_reviewed_document_needs_the_acknowledgement_and_replaces_a_typed_one(service):
    typed = {"tax_year": 2025, "institution": "GBM", "enajenacion": {"net": 2500.0}}
    service.remember("ana", [{"key": "constancia.gbm_2025", "value": typed,
                              "source": {"kind": "user", "ref": "conversation", "observed_on": "2026-02-20"}}])
    proposal = upload(service, "gbm.pdf", taxdocs.gbm_constancia(net="2,600.00"))
    pid = proposal["result"]["proposal_id"]
    refused = service.ingest("ana", "confirm", {"proposal_id": pid})
    assert refused["status"] == "needs_review"
    saved = service.ingest("ana", "confirm", {"proposal_id": pid, "acknowledge_discrepancies": True})
    assert saved["status"] == "saved" and saved["result"]["needs_user"] == []
    assert any("acknowledged" in w for w in saved["warnings"])
    [fact] = service.inspect("ana", keys=["constancia.gbm_2025"])["facts"]
    assert fact["source"]["kind"] == "document" and fact["value"]["enajenacion"]["net"] == 2600.0


# ------------------------------------------------------------------ tax pack


def test_the_mexican_pack_uses_the_uploaded_constancias_as_the_source_of_truth(service):
    seed(service, "ana", MX)
    before = service.run("tax_pack", inputs={"tax_year": 2025, "parameters": MX["parameters"], "as_of": "2026-03-01"},
                         client_id="ana")
    assert {"constancia.gbm.2025", "constancia.bbva.2025"} <= {p["key"] for p in before["result"]["pendientes"]}
    for name, data in (("gbm-2025.pdf", taxdocs.gbm_constancia()), ("bbva-2025.pdf", taxdocs.bbva_interest_constancia())):
        proposal = upload(service, name, data)
        assert service.ingest("ana", "confirm", {"proposal_id": proposal["result"]["proposal_id"]})["status"] == "saved"
    report = service.run("tax_pack", inputs={"tax_year": 2025, "parameters": MX["parameters"], "as_of": "2026-03-01"},
                         client_id="ana")
    sections = report["result"]["sections"]
    broker = sections["mx_enajenacion"]["summary"]["brokers"][0]
    assert (broker["net_mxn"], broker["constancia_net_mxn"], broker["declared_net_mxn"], broker["basis"]) == (
        "2588.62", "2587.00", "2587.00", "constancia")
    assert sections["mx_enajenacion"]["reconciliation"][0]["difference"] == "1.62"
    banks = {r["institution"]: r for r in sections["mx_intereses"]["table"]["rows"]}
    assert banks["BBVA"]["basis"] == "constancia"
    assert (banks["BBVA"]["real_loss_mxn"], banks["BBVA"]["declared_real_loss_mxn"]) == ("923.47", "918.20")
    assert banks["GBM"]["declared_real_mxn"] == "2400.00"
    assert sections["mx_dividendos"]["status"] == "ready"
    assert not any(p["key"].startswith("constancia.") for p in report["result"]["pendientes"])
    documents = {d["id"]: d for d in report["result"]["documents"]}
    assert documents["gbm_2025"]["source"] == "uploaded document"
    assert documents["gbm_2025"]["ref"].startswith("document:sha256:")
    saved = {f["key"]: f["id"] for f in service.inspect("ana")["facts"]}
    assert {saved["constancia.gbm_2025"], saved["constancia.bbva_2025"]} <= set(report["evidence_ids"])


def test_the_us_pack_reconciles_the_1099_lots_and_the_5498(tmp_path, monkeypatch):
    monkeypatch.delenv("WEALTH_UPLOAD_DIR", raising=False)
    service = WealthService(tmp_path / "w.sqlite3")
    service.create("sam", "Sam")
    upload_dir("sam", service.db_path).mkdir(parents=True)
    seed(service, "sam", US)
    for name, data in (("1099.pdf", taxdocs.schwab_1099()), ("5498.pdf", taxdocs.schwab_5498())):
        proposal = upload(service, name, data, client="sam")
        assert service.ingest("sam", "confirm", {"proposal_id": proposal["result"]["proposal_id"]})["status"] == "saved"
    report = service.run("tax_pack", inputs={"tax_year": 2025, "parameters": US["parameters"], "as_of": "2026-06-01"},
                         client_id="sam")
    sections = report["result"]["sections"]
    recon = sections["us_8949"]["reconciliation"]
    assert {r["item"] for r in recon} == {"Charles Schwab: wash sale loss disallowed (1099-B box 1g)",
                                          "Charles Schwab: short-term gain (1099-B)",
                                          "Charles Schwab: long-term gain (1099-B)"}
    assert all(r["difference"] == "0.00" and r["document_id"] == "schwab_2025_1099" for r in recon)
    assert not any("1099" in p["key"] for p in report["result"]["pendientes"])
    rows = sections["us_1099"]["table"]["rows"]
    assert rows[0]["basis"] == "1099" and rows[0]["qualified_dividends_usd"] == "90.00"
    retirement = sections["us_retirement"]
    assert retirement["summary"]["contributions"]["roth_usd"] == "6000.00"
    rows = {r["item"]: r for r in retirement["reconciliation"]}
    assert rows["traditional IRA contributions (5498 box 1)"]["document"] == "0.00"
    roth = rows["Roth IRA contributions (5498 box 10)"]
    assert (roth["ours"], roth["document"], roth["difference"], roth["source_of_truth"]) == (
        "6000.00", "6000.00", "0.00", "5498")
