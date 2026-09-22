"""The annual tax pack: Mexico and US working papers from the ledger and saved facts, offline."""
from __future__ import annotations

import csv
import io
import json
from copy import deepcopy
from decimal import Decimal
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from wealth import cli, ledger as ledger_module, taxpack, web
from wealth.catalog import CATALOG
from wealth.service import WealthService
from wealth.situation.schema import SchemaError, validate
from wealth.store import WealthStore

from test_web_streaming import serving

MX = CATALOG["tax_pack"]["example"]
US = CATALOG["tax_pack"]["variants"]["us_schwab_wash_sale"]
XB = CATALOG["tax_pack"]["variants"]["us_person_in_mexico"]


def run(inputs, tmp_path, client_id=None):
    return WealthService(tmp_path / "w.sqlite3").run("tax_pack", inputs=deepcopy(inputs), client_id=client_id)


def section(report, sid):
    return report["result"]["sections"][sid]


def fact(inputs, key):
    return next(f for f in inputs["facts"] if f["key"] == key)


# ------------------------------------------------------------------ Mexico


def test_mexico_art129_per_broker_against_the_constancia(tmp_path):
    report = run(MX, tmp_path)
    assert report["status"] == "partial"  # only BBVA's interest constancia is missing
    art129 = section(report, "mx_enajenacion")
    rows = {r["symbol"]: r for r in art129["table"]["rows"]}
    # Average cost, each purchase updated from its month to the month before the sale (CFF Art. 17-A).
    amx = rows["AMXB"]
    expected = (Decimal(15000) * Decimal("140.726") / Decimal("128.363")
                + Decimal(8000) * Decimal("140.726") / Decimal("138.343"))
    assert amx["cost_nominal_mxn"] == "23000.00"
    assert amx["cost_updated_mxn"] == format(expected.quantize(Decimal("0.01")), "f")
    assert rows["NAFTRAC"]["gain_mxn"] == "-2828.89"
    assert "CETES" not in rows  # a debt security's gain is interest, not Art. 129
    broker = art129["summary"]["brokers"][0]
    assert (broker["net_mxn"], broker["constancia_net_mxn"], broker["declared_net_mxn"]) == ("2588.62", "2587.00", "2587.00")
    recon = art129["reconciliation"][0]
    assert recon["difference"] == "1.62" and recon["source_of_truth"] == "constancia"
    # The declared figure is the constancia's; the eligible 2023 loss is used before the 10%.
    assert art129["summary"]["carry_used_mxn"] == "1500.00"
    assert art129["summary"]["tax_10pct_mxn"] == "108.70"


def test_mexico_interest_dividends_deductions_and_pendientes(tmp_path):
    report = run(MX, tmp_path)
    interest = section(report, "mx_intereses")
    by_bank = {r["institution"]: r for r in interest["table"]["rows"]}
    assert by_bank["GBM"]["of_which_debt_sale_gains_mxn"] == "4500.00"
    assert by_bank["GBM"]["declared_real_mxn"] == "2400.00"  # the constancia wins
    assert by_bank["BBVA"]["basis"] == "computed" and by_bank["BBVA"]["real_mxn"] == "0.00"
    assert by_bank["BBVA"]["real_loss_mxn"] == "923.47"  # inflation above the interest: a real loss, not negative interest
    assert all(r["difference"] == "0.00" for r in interest["reconciliation"] if r["ours"] is not None)
    dividends = section(report, "mx_dividendos")
    assert dividends["table"]["rows"][0]["origin"] == "domestic"
    assert dividends["status"] == "ready"
    deductions = section(report, "mx_deducciones")
    assert deductions["summary"]["allowed"]["total_personal_deductions_mxn"] == "60000.00"
    assert {r["uso_cfdi"] for r in deductions["table"]["rows"]} >= {"D01", "D05", "D06", "D07", "D10"}
    assert [p["key"] for p in report["result"]["pendientes"]] == ["constancia.bbva.2025"]
    deadlines = {d["date"]: d for d in report["result"]["deadlines"]}
    assert deadlines["2026-04-30"]["jurisdiction"] == "MX" and "2026-02-15" in deadlines
    assert "mx_aguinaldo_ptu" not in report["result"]["sections"]  # only when stated


def test_unknown_is_never_zero(tmp_path):
    inputs = deepcopy(MX)
    mx = fact(inputs, "tax.2025")["value"]["mx"]
    del mx["inpc"], mx["article_129_loss_carryforwards"]
    inputs["facts"] = [f for f in inputs["facts"] if not f["key"].startswith("constancia.")]
    report = run(inputs, tmp_path)
    art129 = section(report, "mx_enajenacion")
    assert all(r["cost_updated_mxn"] is None and r["gain_mxn"] is None for r in art129["table"]["rows"])
    assert all(r["gain_nominal_mxn"] is not None for r in art129["table"]["rows"])
    summary = art129["summary"]
    assert summary["net_result_mxn"] is None and summary["tax_10pct_mxn"] is None
    keys = {p["key"] for p in report["result"]["pendientes"]}
    assert {"inpc", "tax.2025.mx.article_129_loss_carryforwards", "constancia.gbm.2025"} <= keys
    inpc = next(p for p in report["result"]["pendientes"] if p["key"] == "inpc")
    assert "2023-04" in inpc["detail"] and "2025-08" in inpc["detail"]
    # Without INPC, real interest is unknown rather than equal to the nominal amount.
    bbva = next(r for r in section(report, "mx_intereses")["table"]["rows"] if r["institution"] == "BBVA")
    assert bbva["real_mxn"] is None and bbva["declared_real_mxn"] is None
    assert section(report, "mx_intereses")["summary"]["real_interest_mxn"] is None


def test_aguinaldo_and_ptu_only_when_stated(tmp_path):
    inputs = deepcopy(MX)
    fact(inputs, "tax.2025")["value"]["mx"].update(aguinaldo_mxn=20000, ptu_mxn=1000)
    report = run(inputs, tmp_path)
    rows = {r["concept"]: r for r in section(report, "mx_aguinaldo_ptu")["table"]["rows"]}
    assert rows["aguinaldo"]["exempt_mxn"] is None  # the 2025 UMA is not verified in the table
    assert any(p["key"].startswith("parameters.uma_daily_mxn") for p in report["result"]["pendientes"])
    inputs["parameters"]["uma_daily_mxn"] = {"value": "113.14", "source": "INEGI UMA 2025 (DOF 10-01-2025)"}
    rows = {r["concept"]: r for r in section(run(inputs, tmp_path), "mx_aguinaldo_ptu")["table"]["rows"]}
    assert rows["aguinaldo"]["exempt_mxn"] == "3394.20" and rows["aguinaldo"]["taxable_mxn"] == "16605.80"
    assert rows["ptu"]["exempt_mxn"] == "1000.00"


def test_foreign_broker_needs_the_sic_answer_then_nets_under_art129(tmp_path):
    inputs = deepcopy(MX)
    ledger = inputs["ledger"]
    ledger["accounts"].append({"id": "ibkr", "institution": "Interactive Brokers", "type": "brokerage",
                               "currency": "USD", "country": "US", "owners": [{"person_id": "ana", "share": "1"}]})
    ledger["instruments"].append({"id": "VOO", "symbol": "VOO", "currency": "USD", "asset_class": "fund",
                                  "venue": "us", "issuer_domicile": "US"})
    ledger["entries"] += [
        {"id": "ib1", "account_id": "ibkr", "kind": "opening_balance", "date": "2024-12-31", "currency": "USD",
         "instrument_id": "VOO", "quantity": "10", "cost_basis": "4000", "acquired_on": "2024-06-03"},
        {"id": "ib2", "account_id": "ibkr", "kind": "sell", "date": "2025-09-15", "currency": "USD",
         "amount": "5000", "instrument_id": "VOO", "quantity": "10"}]
    ledger["fx"] = [{"date": "2024-06-03", "base": "USD", "quote": "MXN", "rate": "17.00", "source": "Banxico FIX"},
                    {"date": "2025-09-15", "base": "USD", "quote": "MXN", "rate": "18.50", "source": "Banxico FIX"}]
    report = run(inputs, tmp_path)
    assert "tax.2025.mx.sic_listed.VOO" in {p["key"] for p in report["result"]["pendientes"]}
    assert section(report, "mx_enajenacion")["summary"]["tax_10pct_mxn"] is None  # unknown until answered
    fact(inputs, "tax.2025")["value"]["mx"]["sic_listed"] = {"VOO": True}
    report = run(inputs, tmp_path)
    foreign = section(report, "mx_extranjero")
    row = foreign["table"]["rows"][0]
    assert row["regime"] == "article_129" and row["proceeds_mxn"] == "92500.00"
    art129 = section(report, "mx_enajenacion")
    lines = {b["broker"]: b for b in art129["summary"]["brokers"]}
    sic = next(v for k, v in lines.items() if "SIC" in k)
    assert sic["declared_net_mxn"] == row["gain_mxn"]
    total = Decimal("2587.00") + Decimal(row["gain_mxn"])
    assert art129["summary"]["net_result_mxn"] == format(total, "f")


# ------------------------------------------------------------------ United States


def test_us_8949_wash_sale_code_w_basis_and_holding_period(tmp_path):
    report = run(US, tmp_path)
    assert report["status"] == "ready"
    rows = section(report, "us_8949")["table"]["rows"]
    loss, schd, replacement = rows
    assert (loss["code"], loss["adjustment_usd"], loss["gain_usd"], loss["box"]) == ("W", "400.00", "0.00", "A")
    # The disallowed loss moves to the replacement's basis, with the sold shares' holding period tacked on.
    assert replacement["cost_usd"] == "6100.00" and replacement["date_acquired"] == "2025-01-30"
    assert (schd["term"], schd["box"], schd["gain_usd"]) == ("long", "D", "700.00")
    assert all(r["difference"] == "0.00" for r in section(report, "us_8949")["reconciliation"])
    sched = section(report, "us_schedule_d")["summary"]
    assert sched["net_capital_gain_or_loss_usd"] == "-100.00" and sched["deductible_loss_usd"] == "100.00"
    div = section(report, "us_1099")
    assert div["table"]["rows"][0]["qualified_dividends_usd"] == "90.00"
    assert div["table"]["rows"][0]["ordinary_non_qualified_usd"] == "15.00"
    retire = section(report, "us_retirement")["summary"]["contributions"]
    assert (retire["roth_usd"], retire["limit_usd"], retire["excess_usd"]) == ("6000.00", "7000.00", "0.00")
    assert "us_fbar_8938" not in report["result"]["sections"]  # no foreign accounts


def test_ira_replacement_disallows_the_loss_permanently(tmp_path):
    inputs = deepcopy(US)
    for entry in inputs["ledger"]["entries"]:
        if entry["id"] == "us005":
            entry["account_id"] = "schwab_roth"
    inputs["ledger"]["entries"] = [e for e in inputs["ledger"]["entries"] if e["id"] != "us009"]
    rows = section(run(inputs, tmp_path), "us_8949")["table"]["rows"]
    loss = rows[0]
    assert loss["code"] == "W" and loss["permanently_disallowed_usd"] == "400.00"


def test_schedule_d_needs_the_carryover(tmp_path):
    inputs = deepcopy(US)
    del fact(inputs, "tax.2025")["value"]["us"]["capital_loss_carryover"]
    report = run(inputs, tmp_path)
    sched = section(report, "us_schedule_d")["summary"]
    assert sched["short_term_before_carryover_usd"] == "400.00"
    assert sched["net_capital_gain_or_loss_usd"] is None and sched["carryover_to_next_year"] is None
    assert "tax.2025.us.capital_loss_carryover" in {p["key"] for p in report["result"]["pendientes"]}


def test_december_loss_waits_for_january_purchases(tmp_path):
    inputs = deepcopy(US)
    entries = inputs["ledger"]["entries"]
    entries[:] = [e for e in entries if e["id"] not in {"us009", "us012"}]
    entries.append({"id": "us020", "account_id": "schwab", "kind": "sell", "date": "2025-12-20", "currency": "USD",
                    "amount": "5000", "instrument_id": "VTI", "quantity": "20"})
    report = run(inputs, tmp_path)
    assert "ledger.2026-01" in {p["key"] for p in report["result"]["pendientes"]}


# ------------------------------------------------------------------ US person living in Mexico


def test_us_person_in_mexico_gets_fbar_with_thresholds_and_sources(tmp_path):
    report = run(XB, tmp_path)
    assert report["result"]["jurisdictions"] == ["MX", "US"]
    fbar = section(report, "us_fbar_8938")
    summary = fbar["summary"]
    assert summary["fbar"]["required"] is True
    assert Decimal(summary["fbar"]["aggregate_max_value_usd_at_least"]) > Decimal(10000)
    assert summary["fbar"]["threshold_usd"] == "10000.00"
    # Form 8938 abroad (single): $200,000 at year end or $300,000 at any time; a CSPXN price is missing -> unknown.
    assert summary["form_8938"]["threshold_last_day_usd"] == "200000.00"
    assert summary["form_8938"]["required"] is None
    urls = {s.get("url") for s in fbar["sources"]}
    assert taxpack.FINCEN_FBAR_URL in urls and taxpack.IRS_8938_URL in urls
    assert any("PFIC" in w for w in fbar["warnings"])
    assert section(report, "us_foreign_tax")["table"]["rows"][0]["country"] == "MX"
    assert any(d["date"] == "2026-06-15" for d in report["result"]["deadlines"])


def test_fbar_is_unknown_without_a_year_end_rate(tmp_path):
    inputs = deepcopy(XB)
    inputs["ledger"]["fx"] = []
    report = run(inputs, tmp_path)
    fbar = section(report, "us_fbar_8938")["summary"]["fbar"]
    assert fbar["required"] is None
    assert "tax.2025.us.treasury_rate_per_usd.MXN" in {p["key"] for p in report["result"]["pendientes"]}


# ------------------------------------------------------------------ outputs


def test_csv_per_section_and_bilingual_printable_page(tmp_path):
    inputs = {**deepcopy(MX), "exports": ["csv", "html"]}
    del fact(inputs, "tax.2025")["value"]["mx"]["inpc"]
    report = run(inputs, tmp_path)
    files = report["result"]["csv"]
    assert {"tax-pack-2025-mx_enajenacion.csv", "tax-pack-2025-pendientes.csv", "tax-pack-2025-deadlines.csv",
            "tax-pack-2025-reconciliation.csv"} <= set(files)
    rows = list(csv.reader(io.StringIO(files["tax-pack-2025-mx_enajenacion.csv"])))
    assert rows[0][0] == "Casa de bolsa" and rows[1][7] == ""  # an unknown gain is an empty cell, never 0
    page = report["result"]["html"]
    assert 'lang="es"' in page and "Paquete fiscal 2025" in page and "Tax pack 2025" in page
    assert "@media print" in page and "window.print()" in page and "None" not in page
    assert "desconocido" in page and "Pendientes" in page
    english = taxpack.csv_files(report, "en")["tax-pack-2025-mx_enajenacion.csv"]
    assert english.startswith("Broker,")


def test_a_client_pack_reads_the_ledger_and_saved_documents(tmp_path):
    service = WealthService(tmp_path / "w.sqlite3")
    service.create("ana", "Ana")
    said = {"kind": "user", "ref": "conversation", "observed_on": "2026-02-20"}
    document = {"kind": "document", "ref": "constancia GBM 2025", "observed_on": "2026-02-20"}
    service.remember("ana", [dict(f, source=document if f["key"].startswith("constancia.") else said)
                             for f in deepcopy(MX["facts"])])
    ledger = MX["ledger"]
    with WealthStore(tmp_path / "w.sqlite3") as store:
        ledger_module.post(store, "ana", {
            "batch_id": "seed", "source": {"kind": "document", "ref": "estado de cuenta", "observed_on": "2026-01-10"},
            "accounts": ledger["accounts"], "instruments": ledger["instruments"],
            "transactions": [dict({k: v for k, v in e.items() if k not in {"id", "confidence", "source"}},
                                  external_id=e["id"]) for e in ledger["entries"]]})
    report = service.run("tax_pack", inputs={"tax_year": 2025, "parameters": MX["parameters"], "as_of": "2026-03-01"},
                         client_id="ana")
    assert report["result"]["jurisdictions"] == ["MX"]  # from the profile's tax residence
    assert section(report, "mx_enajenacion")["summary"]["net_result_mxn"] == "2587.00"
    saved = {f["key"]: f["id"] for f in service.inspect("ana")["facts"]}
    assert saved["constancia.gbm_2025"] in report["evidence_ids"]


def test_schema_checks_constancias_and_tax_years():
    validate("constancia.gbm_2025", fact(MX, "constancia.gbm_2025")["value"])
    validate("tax.2025", fact(MX, "tax.2025")["value"])
    with pytest.raises(SchemaError):
        validate("constancia.x", {"tax_year": 2025, "institution": "GBM"})  # no block
    with pytest.raises(SchemaError):
        validate("constancia.x", {"tax_year": 2025, "institution": "GBM", "intereses": {"nominal": "mucho"}})
    with pytest.raises(SchemaError):
        validate("tax.2025", {"mx": {"article_129_loss_carryforwards": {"2023": 100}}})
    assert validate("tax.profile", {"anything": True}) == []  # the legacy key keeps its shape
    lot = {"description": "VANGUARD TOTAL STOCK", "symbol": "VTI", "quantity": 20.0, "acquired": "VARIOUS",
           "sold": "2025-03-10", "proceeds": 5600.0, "basis": 6000.0, "wash_sale_disallowed": 400.0, "gain": 0.0,
           "term": "short", "box": "A"}
    good = {"tax_year": 2025, "institution": "Charles Schwab", "account_last4": "5678",
            "form_1099_b": {"short_term_gain": 400.0, "lots": [lot]}}
    validate("constancia.schwab_2025_1099_5678", good)
    for broken in ({**good, "account_last4": "0012345678"},
                   {**good, "form_1099_b": {"lots": [dict(lot, box="Z")]}},
                   {**good, "form_1099_b": {"lots": [dict(lot, sold="03/10/2025")]}},
                   {**good, "form_1099_b": {"lots": [dict(lot, ssn="x")]}},
                   {**good, "form_1099_div": {"ordinary": 1.0, "lots": []}},
                   {**good, "form_1099_b": {"lots": [lot] * 5001}}):
        with pytest.raises(SchemaError):
            validate("constancia.x", broken)
    from wealth.ingest import taxdoc
    from wealth.situation import schema
    assert taxdoc.MAX_1099B_LOTS == schema.MAX_1099B_LOTS
    assert taxdoc.TAX_EXTRACTION_SCHEMA["properties"]["lots"]["maxItems"] == schema.MAX_1099B_LOTS


def test_two_constancias_of_one_broker_are_summed_and_each_reconciled(tmp_path):
    """Two GBM contracts, each with its own constancia: the pack declares their sum, never just one."""
    inputs = deepcopy(MX)
    whole = fact(inputs, "constancia.gbm_2025")["value"]
    inputs["facts"] = [f for f in inputs["facts"] if f["key"] != "constancia.gbm_2025"] + [
        {"key": "constancia.gbm_2025_1111", "value": {
            "tax_year": 2025, "institution": "GBM", "currency": "MXN", "account_last4": "1111",
            "enajenacion": {"gain": 5000.0, "loss": 2000.0, "net": 3000.0},
            "intereses": {"nominal": 4000.0, "real": 2000.0, "real_loss": 0, "isr_withheld": 300.0},
            "dividendos": {"domestic_gross": 1000.0, "isr_withheld": 100.0}}},
        {"key": "constancia.gbm_2025_2222", "value": {
            "tax_year": 2025, "institution": "GBM", "currency": "MXN", "account_last4": "2222",
            "enajenacion": {"gain": 417.0, "loss": 830.0},  # no net printed: gain less loss
            "intereses": {"nominal": 500.0, "real": 400.0, "real_loss": 0, "isr_withheld": 100.0},
            "dividendos": {"domestic_gross": 200.0, "isr_withheld": 20.0}}}]
    report = run(inputs, tmp_path)
    art129 = section(report, "mx_enajenacion")
    [broker] = art129["summary"]["brokers"]
    assert (broker["constancia_net_mxn"], broker["declared_net_mxn"]) == ("2587.00", "2587.00")
    net = art129["reconciliation"][0]
    assert net["document_id"] == "gbm_2025_1111+gbm_2025_2222"
    assert net["documents"] == [{"document_id": "gbm_2025_1111", "document": "3000.00"},
                                {"document_id": "gbm_2025_2222", "document": "-413.00"}]
    banks = {r["institution"]: r for r in section(report, "mx_intereses")["table"]["rows"]}
    assert banks["GBM"]["constancia_real_mxn"] == format(Decimal(str(whole["intereses"]["real"])), ".2f")
    assert banks["GBM"]["constancia_retention_mxn"] == "400.00"
    dividends = section(report, "mx_dividendos")["summary"]["brokers"][0]
    assert dividends["constancia"]["domestic_gross"] == "1200.00"
    # The same document saved twice (an older key and a re-upload) is read once.
    twice = deepcopy(MX)
    twice["facts"].append({"key": "constancia.gbm_2025_5678", "value": dict(whole)})
    again = run(twice, tmp_path)
    assert section(again, "mx_enajenacion")["summary"]["brokers"][0]["declared_net_mxn"] == "2587.00"
    assert any("counted once" in w for w in again["warnings"])


def test_a_stale_profile_is_left_out_and_the_jurisdiction_is_asked_for():
    def snap(facts):
        return {"client": {"id": "ana", "revision": 1}, "decisions": [], "facts": [
            {"id": f"f:{f['key']}", "confidence": "reported", "status": "active",
             "source": {"kind": "user", "ref": "conversation", "observed_on": "2024-01-10"}, **f} for f in facts]}

    profile = {"key": "client.profile", "expires_on": "2025-06-30",
               "value": {"residence": {"country": "US"}, "us_person": True, "birth_year": 1960}}
    report = taxpack.run_task({"tax_year": 2025}, snap([profile]), None, "2026-03-01")
    assert report["status"] == "needs_input"
    assert {m["key"] for m in report["missing"]} == {"jurisdiction", "client.profile"}
    assert any("client.profile is past its review date" in w for w in report["warnings"])
    assert "f:client.profile" not in report["_evidence"]
    # Fresh year facts choose the jurisdiction; the stale profile still is not read (no US person, no FBAR).
    year = {"key": "tax.2025", "value": {"jurisdiction": ["MX"]}}
    report = taxpack.run_task({"tax_year": 2025}, snap([profile, year]), None, "2026-03-01")
    assert report["result"]["jurisdictions"] == ["MX"] and report["result"]["us_person"] is False
    assert "us_fbar" not in report["result"]["sections"]
    assert any(p["key"] == "client.profile" and p["reason"] == "stale" for p in report["result"]["pendientes"])
    assert "f:client.profile" not in report["_evidence"]
    # A fresh profile is read as before.
    fresh = dict(profile, expires_on="2026-12-31")
    report = taxpack.run_task({"tax_year": 2025}, snap([fresh]), None, "2026-03-01")
    assert report["result"]["jurisdictions"] == ["US"] and "f:client.profile" in report["_evidence"]


def test_cli_writes_json_csv_and_html(tmp_path, capsys):
    payload = tmp_path / "inputs.json"
    payload.write_text(json.dumps(MX), encoding="utf-8")
    out = tmp_path / "pack"
    code = cli.main(["tax-pack", "--db", str(tmp_path / "w.sqlite3"), "--input", str(payload), "--out", str(out),
                     "--lang", "en"])
    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["tax_year"] == 2025 and printed["status"] == "partial"
    names = {p.rsplit("/", 1)[-1] for p in printed["files"]}
    assert {"tax-pack-2025.json", "tax-pack-2025.html", "tax-pack-2025-mx_enajenacion.csv"} <= names
    assert not any("us_8949" in name for name in names)  # a Mexico-only pack
    assert (out / "tax-pack-2025-mx_intereses.csv").read_text(encoding="utf-8").startswith("Institution,")
    assert cli.main(["tax-pack", "--db", str(tmp_path / "w.sqlite3")]) == 2  # neither a client nor inputs


def test_web_endpoint_needs_the_token_and_serves_json_html_and_csv(tmp_path):
    db = tmp_path / "w.sqlite3"
    service = WealthService(db)
    service.create("ana", "Ana")
    service.remember("ana", [dict(f, source={"kind": "user", "ref": "chat", "observed_on": "2026-02-20"})
                             for f in deepcopy(MX["facts"])])
    chat = web.Chat(db, "ana")
    with serving(chat) as (base, _):
        with pytest.raises(HTTPError) as denied:
            urlopen(base + "/api/tax-pack?year=2025", timeout=20)
        assert denied.value.code == 403
        headers = {"X-Wealth-Token": chat.token}
        with urlopen(Request(base + "/api/tax-pack?year=2025", headers=headers), timeout=20) as response:
            body = json.loads(response.read())
        assert body["task"] == "tax_pack" and body["result"]["tax_year"] == 2025
        with urlopen(Request(base + "/api/tax-pack?year=2025&format=html&lang=en", headers=headers), timeout=20) as r:
            assert r.headers.get_content_type() == "text/html"
            assert "tax-pack-2025.html" in r.headers["Content-Disposition"]
            page = r.read()
            assert b"Tax pack 2025" in page
            # A self-contained file with its toggle inline: only this download keeps inline script allowed.
            assert b"<script>" in page and b'onclick="window.print()"' in page
            assert "script-src 'self' 'unsafe-inline';" in r.headers["Content-Security-Policy"]
        with urlopen(Request(base + "/api/tax-pack?year=2025&format=csv&section=pendientes", headers=headers),
                     timeout=20) as r:
            assert r.headers.get_content_type() == "text/csv"
            assert "script-src 'self';" in r.headers["Content-Security-Policy"]
        with pytest.raises(HTTPError) as bad:
            urlopen(Request(base + "/api/tax-pack?year=25", headers=headers), timeout=20)
        assert bad.value.code == 400
