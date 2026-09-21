import io
from pathlib import Path

from openpyxl import Workbook

from wealth.ingest import ingest_bytes, ingest_file


FIXTURES = Path(__file__).parent / "fixtures" / "ingest"
MX_EXPORT = (
    "Posición de valores al 31/08/2026;;;;;;\r\nContrato: 1234567;;;;;;\r\n"
    "Emisora;Serie;Títulos;Costo promedio;Precio;Valor de mercado;Plusvalía/Minusvalía\r\n"
    "AMX;B;1,000;14.00;15.50;15,500.00;1,500.00\r\nAAPL;*;5;3,900.00;4,200.00;21,000.00;1,500.00\r\n"
    "CSPX;N;2;11,000.00;11,500.00;23,000.00;1,000.00\r\nBI CETES;261015;1,000;9.60;9.812345;9,812.35;212.35\r\n"
    "Efectivo;;;;;2,500.00;\r\nTotal cartera;;;;;71,812.35;\r\n"
)


def load(name, **options):
    return ingest_file(FIXTURES / name, allowed_roots=[FIXTURES], **options)


def by_instrument(proposal):
    return {(p["account_id"], p["instrument_id"]): p for p in proposal["result"]["household"]["positions"]}


def test_schwab_multi_account_export_reconciles_each_account_total():
    proposal = load("schwab_positions.csv")
    result = proposal["result"]
    assert proposal["status"] == "ready_to_confirm"
    assert (result["as_of"], result["currency"]) == ("2026-08-31", "USD")
    assert {a["id"]: a["type"] for a in result["household"]["accounts"]} == {"schwab-5678": "brokerage", "schwab-4321": "roth_ira"}
    held = by_instrument(proposal)
    assert held[("schwab-5678", "CASH:USD")]["value"] == "1500"
    assert held[("schwab-5678", "AAPL")]["asset_class"] == "equity"
    assert [a["reported_total"] for a in result["reconciliation"]["accounts"]] == ["28601.05", "12250"]


def test_fidelity_export_without_totals_needs_review_and_excludes_pending():
    proposal = load("fidelity_positions.csv")
    result = proposal["result"]
    assert proposal["status"] == "needs_review"
    assert result["as_of"] == "2026-08-31"
    held = by_instrument(proposal)
    assert held[("fidelity-5678", "SPAXX")]["asset_class"] == "cash"
    assert "asset_class" not in held[("fidelity-6789", "QQQ")]
    assert any("Pending activity" in w for w in proposal["warnings"])
    assert {a["status"] for a in result["reconciliation"]["accounts"]} == {"unverifiable", "single_value"}


def test_vanguard_export_positions_and_transactions():
    proposal = load("vanguard.csv", as_of="2026-08-31")
    result = proposal["result"]
    assert result["as_of"] == "2026-08-31"
    kinds = {t["type"]: t for t in result["transactions"]}
    assert kinds["buy"]["amount"] == "-1190" and kinds["buy"]["symbol"] == "VTSAX" and kinds["buy"]["quantity"] == "10"
    assert kinds["dividend"]["amount"] == "2.1"
    assert by_instrument(proposal)[("vanguard-5678", "VMFXX")]["asset_class"] == "cash"


def test_ibkr_activity_statement_reconciles_multi_currency_with_statement_fx():
    proposal = load("ibkr_activity.csv")
    result = proposal["result"]
    assert proposal["status"] == "ready_to_confirm"
    assert result["reconciliation"]["accounts"][0]["computed_total"] == "8300"
    assert by_instrument(proposal)[("ibkr-4567", "WALMEX")]["currency"] == "MXN"
    assert {(t["type"], t["amount"]) for t in result["transactions"]} == {
        ("buy", "-951"), ("dividend", "5"), ("tax_withheld", "-0.75"), ("deposit", "1000")}
    assert "Jane Example" not in str(proposal)


def test_mexican_cp1252_semicolon_export_marks_sic_and_bmv(tmp_path):
    path = tmp_path / "posicion.csv"
    path.write_bytes(MX_EXPORT.encode("cp1252"))
    proposal = ingest_file(path, allowed_roots=[tmp_path])
    result = proposal["result"]
    assert proposal["status"] == "ready_to_confirm"
    assert (result["as_of"], result["currency"]) == ("2026-08-31", "MXN")
    held = {p["instrument_id"]: p for p in result["household"]["positions"]}
    assert held["SIC:AAPL"]["currency"] == "MXN" and held["SIC:CSPX"]["issuer_domicile"] == "IE"
    assert held["BMV:AMX B"]["venue"] == "bmv" and held["CASH:MXN"]["value"] == "2500"
    assert result["household"]["accounts"][0]["number_masked"] == "****4567"


def test_mexican_bank_csv_running_balance_reconciles_newest_first_export():
    proposal = load("bbva_movimientos.csv")
    result = proposal["result"]
    assert proposal["status"] == "ready_to_confirm"
    assert [t["date"] for t in result["transactions"]][0] == "2026-08-01"
    assert result["balance_assertions"][0]["running_balance_verified"] is True
    assert by_instrument(proposal)[(result["household"]["accounts"][0]["id"], "CASH:MXN")]["value"] == "26500"


def test_running_balance_break_needs_review(tmp_path):
    text = (FIXTURES / "bbva_movimientos.csv").read_text().replace("27000.00", "27100.00")
    path = tmp_path / "movs.csv"
    path.write_text(text)
    proposal = ingest_file(path, allowed_roots=[tmp_path])
    assert proposal["status"] == "needs_review"
    assert [d["check"] for d in proposal["result"]["reconciliation"]["differences"]] == ["running_balance"]


def test_xlsx_export_with_custom_header_aliases():
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Holdings report as of 2026-08-31"])
    sheet.append(["Ticker Code", "Units Held", "Unit Px", "Position Worth"])
    sheet.append(["MSFT", 10, 400.5, 4005])
    sheet.append(["Total", None, None, 4005])
    buffer = io.BytesIO()
    workbook.save(buffer)
    data = buffer.getvalue()
    unmapped = ingest_bytes(data, "report.xlsx", currency="USD")
    assert unmapped["status"] == "needs_extraction"
    aliases = {"symbol": ["Ticker Code"], "quantity": ["Units Held"], "price": ["Unit Px"], "value": ["Position Worth"]}
    proposal = ingest_bytes(data, "report.csv", aliases=aliases, currency="USD")
    assert proposal["status"] == "ready_to_confirm"
    assert proposal["result"]["household"]["positions"][0]["value"] == "4005"
