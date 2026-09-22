"""Realistic statements and tax forms (tests/fixtures/realdocs) read end to end, offline.

The documents are fictional but laid out like what GBM, BBVA México, Banorte, Schwab, Fidelity and Interactive
Brokers send: two-column summary boxes, per-section subtotals, unsigned "Importe" columns, SPEI references beside
the amounts, balances printed only on a day's last line, 1099-B totals per Form 8949 box.  Every figure is checked
against ground_truth.json, which make_docs.py writes from the same numbers it prints.  Each test names the defect
an outside agent host hit with these documents before the fix.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
import json
from pathlib import Path

import pytest

from wealth.ingest import ingest_bytes
from wealth.ingest.redact import redact_text
from wealth.service import WealthService, upload_dir

DOCS = Path(__file__).parent / "fixtures" / "realdocs"
TRUTH = json.loads((DOCS / "ground_truth.json").read_text(encoding="utf-8"))
GBM, BBVA, CARD = "gbm_estado_de_cuenta_2026-08.pdf", "bbva_estado_de_cuenta_2026-08.pdf", "banorte_tdc_2026-08.pdf"
CONSTANCIA, B1099, F5498, IBKR = ("gbm_constancia_2025.pdf", "schwab_1099_2025.pdf", "fidelity_5498_2025.pdf",
                                  "ibkr_activity_2026-08.csv")
ALL = (GBM, BBVA, CARD, CONSTANCIA, B1099, F5498, IBKR)


def read(name: str) -> dict:
    return ingest_bytes((DOCS / name).read_bytes(), name)


def money(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


def positions(report: dict) -> dict[str, dict]:
    return {p["instrument_id"]: p for p in report["result"]["household"]["positions"]}


def transactions(report: dict) -> list[tuple[str, Decimal, str]]:
    return [(t["date"], money(t["amount"]), t["type"]) for t in report["result"]["transactions"]]


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.delenv("WEALTH_UPLOAD_DIR", raising=False)
    wealth = WealthService(tmp_path / "wealth.sqlite3")
    wealth.create("mariana", "Mariana")
    root = upload_dir("mariana", wealth.db_path)
    root.mkdir(parents=True)
    for name in ALL:
        (root / name).write_bytes((DOCS / name).read_bytes())
    return wealth


# ------------------------------------------------------------------ every document


@pytest.mark.parametrize("name", ALL)
def test_every_realistic_document_is_read_deterministically_and_reconciles(name):
    """Before: GBM, BBVA and the constancia fell to model extraction; the 1099, 5498 and IBKR needed review."""
    report = read(name)
    assert report["status"] == "ready_to_confirm", report["result"].get("review_reasons")
    assert "extraction_request" not in report["result"]


def test_the_fixture_script_reproduces_the_committed_documents(tmp_path):
    pytest.importorskip("reportlab")
    from tests.fixtures.realdocs import make_docs

    truth = make_docs.build(tmp_path)
    assert truth == TRUTH
    for name in ALL:
        assert (tmp_path / name).read_bytes() == (DOCS / name).read_bytes(), name


# ------------------------------------------------------------------ GBM monthly statement


def test_gbm_section_subtotals_opening_value_and_fx_with_a_date():
    report = read(GBM)
    truth = TRUTH[GBM]
    recon = report["result"]["reconciliation"]["accounts"][0]
    # Before: the BMV "Subtotal 70,338.00" was checked against every holding (a 309,595.67 "mismatch"), and the
    # opening "Valor del portafolio al 31/07/2026" was taken for a second total.
    assert recon["status"] == "reconciled" and money(recon["reported_total"]) == money(truth["total"])
    assert [s["matches"] for s in recon["positions_subtotals"]] == [True, True]
    assert not report["assumptions"] or not any("different totals" in a for a in report["assumptions"])
    # Before: "Tipo de cambio FIX al 31/08/2026: 18.6235" read no rate (the date stopped the pattern).
    assert report["result"]["household"]["fx"][0]["rate"] == truth["fx_usd_mxn"]
    held = positions(report)
    for instrument in ("SIC:AAPL", "SIC:VOO", "BMV:FUNO 11", "BMV:WALMEX"):
        assert money(held[instrument]["value"]) == money(truth["positions"][instrument]["value"])
        assert int(Decimal(held[instrument]["quantity"])) == truth["positions"][instrument]["quantity"]
    assert money(held["CASH:MXN"]["value"]) == money(truth["cash"])
    # Before: WALMEX and the GBMF2 fund had no asset class.
    assert held["BMV:WALMEX"]["asset_class"] == "equity" and held["GBMF2 BO"]["asset_class"] == "fund"


def test_gbm_account_is_not_named_after_the_holder():
    """Before: the holder's name, printed on the same text line as "Contrato:", became the account's name."""
    report = read(GBM)
    account = report["result"]["household"]["accounts"][0]
    assert account["id"] == "gbm-7832" and "MARIANA" not in (account.get("name") or "").upper()


def test_gbm_unsigned_importe_is_signed_from_the_running_balance():
    """Before: the purchase posted as a +20,210.60 deposit and both ISR retentions as money in."""
    report = read(GBM)
    rows = transactions(report)
    assert rows == [("2026-08-05", money("15000"), "transfer"), ("2026-08-07", money("-20269.21"), "buy"),
                    ("2026-08-14", money("145.24"), "dividend"), ("2026-08-14", money("-14.52"), "tax_withheld"),
                    ("2026-08-20", money("811.80"), "dividend"), ("2026-08-20", money("-243.54"), "tax_withheld")]
    buy = report["result"]["transactions"][1]
    assert money(buy["fees"]) == money("58.61")  # commission 50.53 + IVA 8.08
    assert buy["settlement_date"] == "2026-08-09"
    assertion = report["result"]["balance_assertions"][0]
    assert money(assertion["opening"]) == money(TRUTH[GBM]["opening_cash"]) and assertion["transactions_reconcile"]


# ------------------------------------------------------------------ BBVA México checking


def test_bbva_account_number_in_its_own_column_and_two_column_summary():
    """Before: "No. de Cuenta   0482917365" (no colon) was missed, and the two-column summary box
    ("Saldo Promedio ... Saldo Anterior ...") gave no opening or closing balance."""
    report = read(BBVA)
    account = report["result"]["household"]["accounts"][0]
    assert (account["id"], account["type"]) == ("bbva-7365", "checking")
    assertion = report["result"]["balance_assertions"][0]
    truth = TRUTH[BBVA]
    assert (money(assertion["opening"]), money(assertion["closing"])) == (money(truth["opening"]), money(truth["closing"]))
    assert assertion["transactions_reconcile"] is True
    assert len(report["result"]["transactions"]) == truth["transactions"]


def test_bbva_spei_references_are_not_amounts():
    """Before: the reference "0150826" beside a 42,500.00 payroll made it a -108,326 withdrawal."""
    rows = transactions(read(BBVA))
    payroll = [r for r in rows if r[2] == "income"]
    assert payroll == [("2026-08-15", money("42500"), "income"), ("2026-08-30", money("42500"), "income")]
    assert max(abs(r[1]) for r in rows) == money("42500")
    assert sum(r[1] for r in rows) == money(TRUTH[BBVA]["deposits"]) - money(TRUTH[BBVA]["withdrawals"])


def test_bbva_purchases_rent_and_cash_are_spending_but_own_transfers_are_not():
    """Before: OXXO, Amazon, Uber and the rent sent by SPEI were withdrawals or transfers, outside spending."""
    by_text = {r["description"]: r["type"] for r in read(BBVA)["result"]["transactions"]}
    rent = next(v for k, v in by_text.items() if "RENTA AGOSTO" in k)
    gbm = next(v for k, v in by_text.items() if "SPEI ENVIADO GBM" in k)
    card = next(v for k, v in by_text.items() if "PAGO TARJETA" in k)
    oxxo = next(v for k, v in by_text.items() if k.startswith("OXXO"))
    atm = next(v for k, v in by_text.items() if k.startswith("RETIRO CAJERO"))
    assert (rent, gbm, card, oxxo, atm) == ("expense", "transfer", "loan_payment", "expense", "expense")


def test_bbva_statement_with_a_service_number_can_be_saved(service):
    """Before: "SERV 542 110 870 012" in a description made the store refuse the whole statement."""
    proposal = service.ingest("mariana", "file", {"path": BBVA})
    assert "542 110 870 012" not in json.dumps(proposal)
    saved = service.ingest("mariana", "confirm", {"proposal_id": proposal["result"]["proposal_id"]})
    assert saved["status"] == "saved"


def test_grouped_digits_are_masked_but_broker_ids_are_not():
    assert redact_text("DOMICILIACION CFE SERV 542 110 870 012") == "DOMICILIACION CFE SERV ****0012"
    kept = "ALPACA-A20260106103000123_1a2b3c4d000240008000000000000002"
    assert redact_text(kept) == kept


# ------------------------------------------------------------------ Banorte credit card


def test_card_rate_minimum_no_interest_payment_and_installments():
    """Before: the 0.00% moratorium rate replaced the 52.80% ordinary rate, "Pago mínimo + MSI" (3,350) was taken
    for the minimum, the payment to avoid interest was not read and the 13,500 at meses sin intereses was lost."""
    report = read(CARD)
    truth = TRUTH[CARD]
    card, plans = report["result"]["household"]["liabilities"]
    assert money(card["value"]) == money(truth["saldo_al_corte"])
    assert Decimal(card["interest_rate"]) == Decimal(truth["annual_rate"])
    assert money(card["monthly_payment"]) == money(truth["pago_minimo"])
    assert money(card["no_interest_payment"]) == money(truth["pago_no_intereses"])
    assert Decimal(card["cat"]) == Decimal(truth["cat"]) and card["due_date"] == truth["due_date"]
    assert money(plans["value"]) == money(truth["msi_pending_incl_current"]) and Decimal(plans["interest_rate"]) == 0
    assert money(card["value"]) + money(plans["value"]) == money(truth["saldo_deudor_total"])
    assert "MARIANA" not in card["name"].upper() and "mariana" not in card["id"]
    kinds = {t["description"]: t["type"] for t in report["result"]["transactions"]}
    assert kinds["PAGO SPEI RECIBIDO - GRACIAS"] == "loan_payment"


# ------------------------------------------------------------------ tax documents


def test_constancia_cufin_dividends_are_domestic_not_creditable_isr():
    """Before: "Dividendos ... (CUFIN) 1,245.60" was saved as isr_creditable and the real 533.83 conflicted."""
    report = read(CONSTANCIA)
    saved = report["result"]["facts_preview"][0]["value"]
    truth = TRUTH[CONSTANCIA]
    assert saved["dividendos"] == {"domestic_gross": 1245.60, "foreign_gross": 1580.34, "isr_withheld": 282.59,
                                   "isr_creditable": 533.83}
    assert saved["enajenacion"]["net"] == float(truth["enajenacion"]["net"])
    assert saved["intereses"]["real"] == float(truth["intereses"]["real"])


def test_1099b_totals_with_a_market_discount_column_and_one_total_per_box():
    """Before: totals printing all five columns were skipped and two "Total Long-Term" lines (Box D and E) could
    not both stand, so no gains were saved at all."""
    report = read(B1099)
    truth = TRUTH[B1099]
    figures = report["result"]["figures"]["form_1099_b"]
    assert (money(figures["short_term_gain"]), money(figures["long_term_gain"])) == (
        money(truth["b_short_term_gain"]), money(truth["b_long_term_gain"]))
    assert (money(figures["proceeds"]), money(figures["cost_basis"]), money(figures["wash_sale_disallowed"])) == (
        money(truth["b_proceeds"]), money(truth["b_cost_basis"]), money(truth["b_wash"]))
    assert [lot["box"] for lot in report["result"]["lots"]] == ["A", "A", "A", "D", "D", "E"]
    assert report["result"]["reconciliation"]["status"] == "reconciled"
    div = report["result"]["figures"]["form_1099_div"]
    assert (div["ordinary"], div["qualified"], div["foreign_tax_paid"]) == ("1284.56", "1102.33", "12.45")


def test_a_5498_note_does_not_contest_the_box_beside_the_payer_column():
    """Before: the payer's address column shared the box lines, and the note "... include $3,500.00 made from
    01/01/2026 through 04/15/2026" was a second Roth figure that forced a review."""
    report = read(F5498)
    figures = report["result"]["figures"]["form_5498"]
    assert (figures["roth_contributions"], figures["fair_market_value"]) == ("7000.00", "48215.62")
    assert report["result"]["account_last4"] == "1926" and not report["result"]["review_reasons"]


# ------------------------------------------------------------------ Interactive Brokers


def test_ibkr_cash_by_currency_accruals_fund_types_and_dividend_symbols():
    """Before: NAV counted 5.02 of interest accruals (a mismatch), 25,000 MXN was folded into dollars, VT and
    SGOV were "equity", and the SGOV dividend had no symbol so the ledger could not post it."""
    report = read(IBKR)
    held = positions(report)
    assert (money(held["CASH:MXN"]["value"]), money(held["CASH:USD"]["value"])) == (money("25000"), money("2318.44"))
    assert report["result"]["household"]["fx"][0] == {**report["result"]["household"]["fx"][0], "from": "MXN",
                                                        "to": "USD", "rate": "0.053694"}
    assert held["VT"]["asset_class"] == held["SGOV"]["asset_class"] == "fund" and held["AMZN"]["asset_class"] == "equity"
    dividend = next(t for t in report["result"]["transactions"] if t["type"] == "dividend")
    assert dividend["symbol"] == "SGOV"


# ------------------------------------------------------------------ the whole person, through the service


def _save_all(service: WealthService) -> None:
    for name in ALL:
        proposal = service.ingest("mariana", "file", {"path": name})
        assert proposal["status"] == "ready_to_confirm", (name, proposal["result"].get("review_reasons"))
        saved = service.ingest("mariana", "confirm", {"proposal_id": proposal["result"]["proposal_id"]})
        assert saved["status"] == "saved", name


def test_every_document_saves_and_the_picture_adds_up(service):
    _save_all(service)
    sit = service.situation("mariana")
    owed = sum(Decimal(str(r["balance"])) for r in sit["liabilities"])
    assert money(owed) == money(TRUTH[CARD]["saldo_deudor_total"])
    from wealth.situation import brief
    text = brief(sit, "en")
    # Before: the card showed as "Investments: Banorte 0" and both card debts read "Debt card (Banorte)".
    assert "Banorte 0" not in text and "installments (Banorte)" in text
    assert "pay 20,611 by 2026-09-14 to avoid interest" in text


def test_the_tax_pack_uses_constancia_dividends_and_reports_1099_figures(service):
    """Before: with no 2025 ledger the constancia's dividends were "no activity" and the 1099 summary null."""
    _save_all(service)
    service.remember("mariana", [{"key": "client.profile", "value": {"residence": {"country": "MX"},
                                                                     "citizenship": ["MX", "US"]},
                                  "source": {"kind": "user", "ref": "chat", "observed_on": "2026-09-01"}}])
    report = service.run("tax_pack", inputs={"tax_year": 2025}, client_id="mariana")
    sections = report["result"]["sections"]
    broker = sections["mx_dividendos"]["summary"]["brokers"][0]
    # The constancia is the whole year's dividends: nothing is missing (before: "partial", asking for the very
    # constancia it was reading).
    assert sections["mx_dividendos"]["status"] == "ready" and not sections["mx_dividendos"]["missing"]
    assert not any(p["key"].endswith(".dividendos") for p in report["result"]["pendientes"])
    assert (broker["domestic_gross_mxn"], broker["foreign_gross_mxn"], broker["basis"]) == ("1245.60", "1580.34",
                                                                                            "constancia")
    assert broker["art140"]["corporate_isr_credit_mxn"] == "533.83"
    assert sections["mx_intereses"]["summary"]["nominal_interest_mxn"] == "3842.17"
    us = sections["us_1099"]["summary"]
    assert (us["ordinary_dividends_to_report_usd"], us["interest_to_report_usd"]) == ("1284.56", "412.87")
    assert sections["us_8949"]["summary"]["long"]["gain"] == TRUTH[B1099]["b_long_term_gain"]


def test_card_versus_invest_compares_the_only_debt_that_charges_interest(service):
    """Before: a card with its meses-sin-intereses plans made prepay_vs_invest refuse with no debt ids."""
    for name in (CARD,):
        proposal = service.ingest("mariana", "file", {"path": name})
        service.ingest("mariana", "confirm", {"proposal_id": proposal["result"]["proposal_id"]})
    report = service.run("debt", inputs={"mode": "prepay_vs_invest"}, client_id="mariana")
    assert report["result"]["verdict"] == "prepay"
    assert any("only debt that charges interest" in a for a in report["assumptions"])
    assert "an equity index fund" not in report["result"]["verdict_text"]["es"]


def test_a_designation_said_in_chat_reaches_the_statement_account(service):
    """Before: "investment.gbm" stayed an orphan without an amount while account.gbm-7832 had no beneficiary,
    and IBKR (a US custodian) carried no word about US estate papers."""
    for name in (GBM, IBKR):
        proposal = service.ingest("mariana", "file", {"path": name})
        service.ingest("mariana", "confirm", {"proposal_id": proposal["result"]["proposal_id"]})
    said = {"kind": "user", "ref": "chat", "observed_on": "2026-09-01"}
    service.remember("mariana", [
        {"key": "client.profile", "value": {"residence": {"country": "MX"}}, "source": said},
        {"key": "estate.family", "value": {"marital_status": "married", "spouse": "Diego",
                                           "children": [{"name": "Sofía", "minor": True}]}, "source": said},
        {"key": "estate.designation.investment-gbm", "value": {"account": "investment.gbm", "beneficiaries": [
            {"name": "Diego", "relationship": "spouse", "share": 1}]}, "source": said}])
    rows = {r["key"]: r for r in service.run("estate_register", client_id="mariana")["result"]["rows"]}
    assert "investment.gbm" not in rows
    gbm = rows["account.gbm-7832"]
    assert gbm["mechanism"] == "beneficiary" and gbm["heirs"][0]["name"] == "Diego" and gbm["value"] is not None
    ibkr = rows["account.ibkr-2345"]
    assert ibkr["custody_country"] == "US" and any("US custodian" in n["en"] for n in ibkr["notes"])


# ------------------------------------------------------------------ what a foreign host sees


def _server(service):
    pytest.importorskip("mcp")
    from wealth import server

    return server.build_server(str(service.db_path), environ={})


def _call(srv, name, arguments):
    return asyncio.run(srv.call_tool(name, arguments)).structured_content


def test_a_proposal_carries_its_code_so_one_yes_saves_it(service):
    """Before: the code came only from a first confirm call, so a person who had said yes to the figures was
    asked again, and a host that moved on left every statement unsaved."""
    srv = _server(service)
    proposal = _call(srv, "wealth_ingest", {"client_id": "mariana", "action": "file", "inputs": {"path": CARD}})
    confirmation = proposal["result"]["confirmation"]
    assert confirmation["confirmation_code"] and "owes 19,111.43" in confirmation["summary"]
    saved = _call(srv, "wealth_ingest", {"client_id": "mariana", "action": "confirm",
                                         "inputs": {"proposal_id": proposal["result"]["proposal_id"]},
                                         "confirm": True, "confirmation_code": confirmation["confirmation_code"]})
    assert saved["status"] == "saved"


def test_chat_items_in_a_models_own_words_and_monthly_income(service):
    """Before: kind "investment" was refused, and an income said per month was saved as a year's."""
    srv = _server(service)
    report = _call(srv, "wealth_ingest", {"client_id": "mariana", "action": "chat", "inputs": {"currency": "MXN", "items": [
        {"kind": "investment", "label": "GBM", "amount": 400000, "quote": "Tengo 400k en GBM"},
        {"kind": "income", "label": "Sueldo", "amount": 85000, "quote": "gano 85 mil al mes neto"}]}})
    household = report["result"]["household"]
    assert household["accounts"][0]["type"] == "brokerage"
    assert household["income_exposures"][0]["annual_amount"] == "1020000"


def test_a_thread_summary_is_saved_as_its_text(service):
    receipt = service.remember("mariana", [{"key": "thread.idle_cash", "value": {
        "kind": "advice", "status": "open", "summary": "Move surplus pesos to CETES once the reserve is full."},
        "source": {"kind": "user", "ref": "chat", "observed_on": "2026-09-01"}}])
    assert receipt["written"][0]["key"] == "thread.idle_cash"
    stored = service.inspect("mariana", key="thread.idle_cash")["facts"][0]["value"]
    assert stored["text"].startswith("Move surplus pesos")


def test_balances_said_in_chat_are_refined_by_statements_not_counted_beside_them(service):
    """Before: "400k en GBM, 60 mil en BBVA, debo 20 mil en Banorte" saved as statement records (GBM worth 0,
    its total an unresolved row); with the statements saved, BBVA and the card debt were counted twice."""
    chat = service.ingest("mariana", "chat", {"currency": "MXN", "items": [
        {"kind": "investment", "label": "GBM", "amount": 400000, "quote": "Tengo 400k en GBM"},
        {"kind": "cash", "label": "BBVA", "amount": 60000, "quote": "como 60 mil en mi cuenta de BBVA"},
        {"kind": "liability", "label": "Tarjeta Banorte", "amount": 20000,
         "quote": "debo unos 20 mil en la tarjeta Banorte"},
        {"kind": "income", "label": "Sueldo", "amount": 85000, "quote": "gano 85 mil al mes neto"}]})
    saved = service.ingest("mariana", "confirm", {"proposal_id": chat["result"]["proposal_id"],
                                                  "acknowledge_discrepancies": True})
    assert sorted(saved["result"]["saved"]["keys"]) == ["cash.bbva", "income.sueldo", "investment.gbm",
                                                        "liability.tarjeta-banorte"]
    facts = {f["key"]: f["value"] for f in service.inspect("mariana")["facts"]}
    assert facts["investment.gbm"]["amount"] == 400000 and facts["liability.tarjeta-banorte"]["lender"] == "Banorte"
    assert (facts["income.sueldo"]["amount"], facts["income.sueldo"]["frequency"]) == (85000, "monthly")
    before = service.situation("mariana")["net_worth"]
    assert (before["liquid"], before["liabilities"]) == (460000, 20000)
    for name in (GBM, BBVA, CARD):
        proposal = service.ingest("mariana", "file", {"path": name})
        service.ingest("mariana", "confirm", {"proposal_id": proposal["result"]["proposal_id"]})
    worth = service.situation("mariana")["net_worth"]
    assert money(worth["liquid"]) == money(TRUTH[GBM]["total"]) + money(TRUTH[BBVA]["closing"])
    assert money(worth["liabilities"]) == money(TRUTH[CARD]["saldo_deudor_total"])


def test_a_large_task_example_is_shown_by_its_shape(tmp_path):
    """Before: wealth_context(intent=tax_pack) returned ~28k characters (a whole example ledger) every turn."""
    pytest.importorskip("mcp")
    from wealth import server

    srv = server.build_server(str(tmp_path / "w.sqlite3"))
    text = "".join(c.text for c in asyncio.run(srv.call_tool("wealth_context", {"intent": "tax_pack"})).content)
    assert len(text) < 6000 and "detail=full shows it" in text
    full = asyncio.run(srv.call_tool("wealth_context", {"intent": "tax_pack", "detail": "full"})).structured_content
    assert isinstance(full["tasks"]["tax_pack"]["example"]["ledger"], dict)


def test_a_designation_on_the_statement_key_reaches_the_stated_account_it_covers(service):
    """Before: with "400k en GBM" saved and GBM's statement covering it, a designation saved on
    account.gbm-7832 was "not in the picture" and investment.gbm showed no beneficiary; the host moved it
    by hand, tripping over the forget rule twice."""
    said = {"kind": "user", "ref": "chat", "observed_on": "2026-09-01"}
    service.remember("mariana", [{"key": "investment.gbm", "value": {"amount": 400000, "currency": "MXN",
                                                                     "institution": "GBM"}, "source": said}])
    proposal = service.ingest("mariana", "file", {"path": GBM})
    service.ingest("mariana", "confirm", {"proposal_id": proposal["result"]["proposal_id"]})
    service.remember("mariana", [{"key": "estate.designation.account-gbm-7832", "value": {
        "account": "account.gbm-7832", "beneficiaries": [{"name": "Diego", "relationship": "spouse", "share": 1}]},
        "source": said}])
    report = service.run("estate_register", client_id="mariana")
    rows = {r["key"]: r for r in report["result"]["rows"]}
    assert "account.gbm-7832" not in rows and not any("not in the picture" in w for w in report["warnings"])
    gbm = rows["investment.gbm"]
    assert gbm["mechanism"] == "beneficiary" and [h["name"] for h in gbm["heirs"]] == ["Diego"]
    assert money(gbm["value"]) == money(TRUTH[GBM]["total"])


def _gbm_stated_and_statement(service):
    said = {"kind": "user", "ref": "chat", "observed_on": "2026-09-01"}
    service.remember("mariana", [{"key": "investment.gbm", "value": {"amount": 400000, "currency": "MXN",
                                                                     "institution": "GBM"}, "source": said}])
    proposal = service.ingest("mariana", "file", {"path": GBM})
    service.ingest("mariana", "confirm", {"proposal_id": proposal["result"]["proposal_id"]})


def _designate(service, key, account, beneficiaries, observed_on):
    service.remember("mariana", [{"key": key, "value": {"account": account, "beneficiaries": beneficiaries},
                                  "source": {"kind": "user", "ref": "chat", "observed_on": observed_on}}])


def test_designations_on_both_keys_of_one_account_yield_one_row_and_a_conflict_gap(service):
    """Before: with designations on investment.gbm and on account.gbm-7832, only one was consumed and the
    other came back as an orphan: GBM listed twice with conflicting beneficiaries."""
    _gbm_stated_and_statement(service)
    _designate(service, "estate.designation.investment-gbm", "investment.gbm",
               [{"name": "Diego", "relationship": "spouse", "share": 1}], "2026-08-01")
    _designate(service, "estate.designation.account-gbm-7832", "account.gbm-7832",
               [{"name": "Sofía", "relationship": "child", "share": 1}], "2026-09-01")
    report = service.run("estate_register", client_id="mariana")
    rows = [r for r in report["result"]["rows"] if "gbm" in r["key"]]
    assert [r["key"] for r in rows] == ["investment.gbm"] and not rows[0].get("orphan")
    # The newer designation stands; the conflict is asked about, never silently dropped.
    assert rows[0]["designation_key"] == "estate.designation.account-gbm-7832"
    assert [h["name"] for h in rows[0]["heirs"]] == ["Sofía"]
    conflict = [g for g in report["result"]["gaps"] if g["code"] == "designation_conflict"]
    assert conflict and conflict[0]["key"] == "investment.gbm"
    assert set(conflict[0]["designations"]) == {"estate.designation.account-gbm-7832",
                                                 "estate.designation.investment-gbm"}


def test_matching_designations_on_both_keys_are_not_a_conflict(service):
    _gbm_stated_and_statement(service)
    for key, account in (("estate.designation.investment-gbm", "investment.gbm"),
                         ("estate.designation.account-gbm-7832", "account.gbm-7832")):
        _designate(service, key, account, [{"name": "Diego", "relationship": "spouse", "share": 1}], "2026-09-01")
    report = service.run("estate_register", client_id="mariana")
    assert [r["key"] for r in report["result"]["rows"] if "gbm" in r["key"]] == ["investment.gbm"]
    assert not any(g["code"] == "designation_conflict" for g in report["result"]["gaps"])


def test_a_chat_wallet_in_two_currencies_is_two_balances_never_a_sum(service):
    """Before: USD 100 and MXN 1,000 in one "Wallet" were saved as USD 1,100."""
    chat = service.ingest("mariana", "chat", {"items": [
        {"kind": "cash", "label": "Wallet", "amount": 100, "currency": "USD", "quote": "tengo 100 dólares en Wallet"},
        {"kind": "cash", "label": "Wallet", "amount": 1000, "currency": "MXN", "quote": "y 1,000 pesos en Wallet"},
        {"kind": "account", "label": "Vest", "amount": 500, "currency": "USD", "account_type": "brokerage",
         "quote": "500 dólares en Vest"}]})
    service.ingest("mariana", "confirm", {"proposal_id": chat["result"]["proposal_id"],
                                          "acknowledge_discrepancies": True})
    facts = {f["key"]: f["value"] for f in service.inspect("mariana")["facts"]}
    assert (facts["cash.wallet-usd"]["amount"], facts["cash.wallet-usd"]["currency"]) == (100, "USD")
    assert (facts["cash.wallet-mxn"]["amount"], facts["cash.wallet-mxn"]["currency"]) == (1000, "MXN")
    assert (facts["investment.vest"]["amount"], facts["investment.vest"]["currency"]) == (500, "USD")
    assert not any(v.get("amount") == 1100 for v in facts.values() if isinstance(v, dict))


def test_estate_register_with_facts_in_inputs_keeps_ledger_accounts(service):
    """Before: facts passed in inputs left the statements' ledger accounts without a key and the task crashed
    with no message ("Error executing tool wealth_run")."""
    for name in (GBM, BBVA):
        proposal = service.ingest("mariana", "file", {"path": name})
        service.ingest("mariana", "confirm", {"proposal_id": proposal["result"]["proposal_id"]})
    report = service.run("estate_register", client_id="mariana", inputs={"facts": [
        {"key": "investment.fidelity", "value": {"institution": "Fidelity", "kind": "retirement", "plan_type": "roth_ira",
                                                 "balance_unknown": True}}]})
    keys = {row["key"] for row in report["result"]["rows"]}
    assert {"investment.fidelity", "ledger.gbm-7832", "ledger.bbva-7365"} <= keys


def test_an_account_said_without_a_balance_is_saved_as_unknown(service):
    """Before: "a Roth IRA at Fidelity" with no amount was refused (amount, then currency, required): the host
    retried three times to record an account whose beneficiaries the person had just named."""
    said = {"kind": "user", "ref": "chat", "observed_on": "2026-09-01"}
    receipt = service.remember("mariana", [
        {"key": "investment.fidelity", "value": {"institution": "Fidelity", "kind": "retirement",
                                                 "plan_type": "roth_ira"}, "source": said},
        {"key": "estate.designation.investment-fidelity", "value": {"account": "investment.fidelity", "beneficiaries": [
            {"name": "Diego", "relationship": "spouse", "share": 0.5},
            {"name": "Sofía", "relationship": "child", "share": 0.5}]}, "source": said}])
    assert any("balance_unknown: true" in w for w in receipt["warnings"])
    fact = service.inspect("mariana", key="investment.fidelity")["facts"][0]["value"]
    assert fact["balance_unknown"] is True and "amount" not in fact
    assert service.situation("mariana")["net_worth"] is not None
    rows = {r["key"]: r for r in service.run("estate_register", client_id="mariana")["result"]["rows"]}
    assert [h["name"] for h in rows["investment.fidelity"]["heirs"]] == ["Diego", "Sofía"]


def test_a_us_citizen_is_a_us_person_without_saying_so(service):
    said = {"kind": "user", "ref": "chat", "observed_on": "2026-09-01"}
    service.remember("mariana", [{"key": "client.profile", "value": {"residence": {"country": "MX"},
                                                                     "citizenship": ["MX", "US"]}, "source": said}])
    assert service.situation("mariana")["profile"]["us_person"] is True
    report = service.run("tax_pack", inputs={"tax_year": 2025}, client_id="mariana")
    assert report["result"]["jurisdictions"] == ["MX", "US"] and report["result"]["us_person"] is True


@pytest.mark.parametrize("said, amount, frequency, annual, monthly", [
    ("gano 10 mil a la quincena", 10000, "semimonthly", "240000", 20000),   # quincenal: 24 a year
    ("me pagan 10 mil catorcenal", 10000, "biweekly", "260000", 21666.67),  # every 14 days: 26 a year
    ("gano 5 mil a la semana", 5000, "weekly", "260000", 21666.67),
    ("gano 85 mil al mes", 85000, "monthly", "1020000", 85000),
    ("cobro 30 mil bimestral de rentas", 30000, "annual", "180000", None),  # no bimonthly frequency: the year's
    ("gano 900 mil al año", 900000, "annual", "900000", None),
])
def test_chat_income_keeps_its_pay_period(service, said, amount, frequency, annual, monthly):
    """Before: "quincenal" was annualized at 24 but saved as biweekly (26 a year), so 10,000 a quincena read as
    21,666.67 a month instead of 20,000."""
    chat = service.ingest("mariana", "chat", {"currency": "MXN", "items": [
        {"kind": "income", "label": "Ingreso", "amount": amount, "quote": said}]})
    assert chat["result"]["household"]["income_exposures"][0]["annual_amount"] == annual
    service.ingest("mariana", "confirm", {"proposal_id": chat["result"]["proposal_id"],
                                          "acknowledge_discrepancies": True})
    fact = service.inspect("mariana", key="income.ingreso")["facts"][0]["value"]
    assert fact["frequency"] == frequency
    assert fact["amount"] == (int(annual) if frequency == "annual" else amount)
    got = service.situation("mariana")["income"]["monthly"]
    assert (round(got, 2) if got is not None else None) == monthly


def test_a_quincenal_payment_is_counted_twice_a_month(service):
    said = {"kind": "user", "ref": "chat", "observed_on": "2026-09-01"}
    service.remember("mariana", [{"key": "liability.auto", "value": {
        "kind": "auto", "balance": 100000, "currency": "MXN", "annual_rate": 0.12, "payment": 2500,
        "payment_frequency": "semimonthly"}, "source": said}])
    assert service.situation("mariana")["liabilities"][0]["monthly_payment"] == 5000
