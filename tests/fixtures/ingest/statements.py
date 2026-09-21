"""Synthetic, fictional statement PDFs for ingestion tests (fpdf2 + pypdf)."""

from __future__ import annotations

from datetime import datetime, timezone
import io

from fpdf import FPDF
from pypdf import PdfReader, PdfWriter


def render(blocks, *, password: str | None = None) -> bytes:
    """Blocks: ("text", str) | ("row", [(text, width, align), ...]) | ("blank",) | ("page",)."""
    pdf = FPDF()
    pdf.set_creation_date(datetime(2026, 1, 1, tzinfo=timezone.utc))
    pdf.set_auto_page_break(True, 15)
    pdf.add_page()
    pdf.set_font("Helvetica", size=8)
    for block in blocks:
        if block[0] == "text":
            pdf.cell(0, 5, block[1], new_x="LMARGIN", new_y="NEXT")
        elif block[0] == "row":
            for text, width, align in block[1]:
                pdf.cell(width, 5, text, align=align)
            pdf.ln(5)
        elif block[0] == "blank":
            pdf.ln(5)
        elif block[0] == "page":
            pdf.add_page()
    if password:
        pdf.set_encryption(owner_password="owner-" + password, user_password=password)
    return bytes(pdf.output())


def table(columns, rows):
    """columns: [(header, width, align)]; rows: [[cell, ...]] -> row blocks."""
    blocks = [("row", [(h, w, a) for h, w, a in columns])]
    for row in rows:
        blocks.append(("row", [(cell, w, a) for cell, (_, w, a) in zip(row, columns)]))
    return blocks


def with_javascript(data: bytes) -> bytes:
    from pypdf.generic import DictionaryObject, NameObject, TextStringObject

    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(data)))
    writer._root_object[NameObject("/OpenAction")] = writer._add_object(DictionaryObject({
        NameObject("/Type"): NameObject("/Action"), NameObject("/S"): NameObject("/JavaScript"),
        NameObject("/JS"): TextStringObject("app.alert('hello');"),
    }))
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def with_attachment(data: bytes) -> bytes:
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(data)))
    writer.add_attachment("payload.txt", b"not a statement")
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


US_COLUMNS = [("Symbol", 22, "L"), ("Description", 62, "L"), ("Quantity", 22, "R"), ("Price", 24, "R"),
              ("Market Value", 30, "R"), ("Cost Basis", 30, "R")]


def us_brokerage(total: str = "38,601.05", *, second_account: bool = False) -> bytes:
    blocks = [
        ("text", "Charles Schwab & Co., Inc."), ("text", "Brokerage Statement"),
        ("text", "Statement Period: August 1, 2026 - August 31, 2026"),
        ("text", "Taxpayer ID: 123-45-6789"),
        ("blank",),
        ("text", "Individual Brokerage Account    Account Number: 1234-5678"),
        ("text", "Positions"),
        *table(US_COLUMNS, [
            ["VTI", "Vanguard Total Stock Market ETF", "100", "250.00", "25,000.00", "20,000.00"],
            ["AAPL", "Apple Inc", "10.5", "200.10", "2,101.05", "1,500.00"],
            ["BND", "Vanguard Total Bond Market ETF", "150", "66.667", "10,000.00", "10,500.00"],
        ]),
        ("row", [("Total Positions", 106, "L"), ("", 24, "R"), ("37,101.05", 30, "R")]),
        ("text", "Cash & Cash Investments                                  1,500.00"),
        ("text", f"Total Account Value                                      {total}"),
    ]
    if second_account:
        blocks += [
            ("page",),
            ("text", "Roth IRA    Account Number: 9999-4321"),
            *table(US_COLUMNS, [["VXUS", "Vanguard Total International Stock ETF", "200", "60.00", "12,000.00", "11,000.00"]]),
            ("text", "Cash & Cash Investments                                  250.00"),
            ("text", "Total Account Value                                      12,250.00"),
        ]
    return render(blocks)


MX_COLUMNS = [("Emisora", 22, "L"), ("Serie", 16, "L"), ("Titulos", 20, "R"), ("Costo promedio", 28, "R"),
              ("Precio de mercado", 30, "R"), ("Valor de mercado", 32, "R"), ("Plusvalia/Minusvalia", 34, "R")]


def gbm_multicurrency(total: str = "217,837.35") -> bytes:
    """MXN contract mixing BMV, SIC (US and Irish UCITS) and government paper, plus a USD contract."""
    return render([
        ("text", "GBM Grupo Bursátil Mexicano"), ("text", "Estado de cuenta"),
        ("text", "Periodo del 01/08/2026 al 31/08/2026"),
        ("text", "RFC: GODE561231GR8    CURP: GODE561231HDFRRN09"),
        ("text", "Tipo de cambio: 18.2500"),
        ("text", "Valor de la UDI: 8.451200"),
        ("blank",),
        ("text", "Contrato: 1234567    Moneda: MXN"),
        ("text", "Mercado de Capitales"),
        *table(MX_COLUMNS, [
            ["AMX", "B", "1,000", "14.00", "15.50", "15,500.00", "1,500.00"],
            ["WALMEX", "*", "100", "60.00", "62.00", "6,200.00", "200.00"],
            ["FUNO", "11", "500", "22.00", "24.00", "12,000.00", "1,000.00"],
            ["NAFTRAC", "ISHRS", "100", "55.00", "58.25", "5,825.00", "325.00"],
        ]),
        ("text", "Mercado Global (SIC)"),
        *table(MX_COLUMNS, [
            ["AAPL", "*", "5", "3,900.00", "4,200.00", "21,000.00", "1,500.00"],
            ["CSPX", "N", "2", "11,000.00", "11,500.00", "23,000.00", "1,000.00"],
            ["IVV", "*", "3", "11,500.00", "12,000.00", "36,000.00", "1,500.00"],
        ]),
        ("text", "Mercado de Dinero"),
        *table(MX_COLUMNS, [
            ["BI CETES", "261015", "1,000", "9.60", "9.812345", "9,812.35", "212.35"],
            ["S UDIBONO", "351122", "100", "850.00", "860.00", "86,000.00", "1,000.00"],
        ]),
        ("text", "Efectivo                                                                     2,500.00"),
        ("text", f"Valor total de la cartera                                                  {total}"),
        ("page",),
        ("text", "Contrato: 7654321    Moneda: Dls"),
        ("text", "Efectivo                                                                     1,000.00"),
        ("text", "Valor total de la cartera                                                    1,000.00"),
    ])


def bbva_checking(closing: str = "26,500.00") -> bytes:
    columns = [("Fecha Oper", 20, "L"), ("Fecha Liq", 20, "L"), ("Descripción", 70, "L"),
               ("Cargos", 26, "R"), ("Abonos", 26, "R"), ("Saldo", 28, "R")]
    return render([
        ("text", "BBVA México"), ("text", "Estado de cuenta - Cuenta de cheques"),
        ("text", "Periodo: 01/08/2026 al 31/08/2026"),
        ("text", "No. de Cuenta: 0123456789"),
        ("text", "CLABE: 012180001234567891"),
        ("text", "Saldo anterior                                      10,000.00"),
        ("text", "Depósitos / Abonos (+)                              25,000.00"),
        ("text", "Retiros / Cargos (-)                                 8,500.00"),
        ("text", f"Saldo final                                         {closing}"),
        ("blank",),
        ("text", "Detalle de movimientos"),
        *table(columns, [
            ["01/AGO", "01/AGO", "SPEI RECIBIDO BANORTE REF 7781", "", "20,000.00", "30,000.00"],
            ["05/AGO", "05/AGO", "PAGO TARJETA DE CREDITO", "5,000.00", "", "25,000.00"],
            ["15/AGO", "15/AGO", "PAGO DE NOMINA EMPRESA SA", "", "5,000.00", "30,000.00"],
            ["20/AGO", "21/AGO", "RETIRO CAJERO AUTOMATICO", "3,000.00", "", "27,000.00"],
            ["31/AGO", "31/AGO", "COMISION MANEJO DE CUENTA", "500.00", "", "26,500.00"],
        ]),
    ])


def mx_credit_card() -> bytes:
    columns = [("Fecha", 22, "L"), ("Descripción", 90, "L"), ("Importe", 30, "R")]
    msi = [("Fecha", 18, "L"), ("Descripción", 44, "L"), ("Monto original", 28, "R"), ("Saldo pendiente", 28, "R"),
           ("Pago requerido", 28, "R"), ("Núm. de pago", 24, "R")]
    return render([
        ("text", "Banorte"), ("text", "Estado de cuenta Tarjeta de Crédito"),
        ("text", "Periodo: 01/08/2026 al 31/08/2026"),
        ("text", "Número de tarjeta: 5555 4444 3333 1234"),
        ("text", "Saldo anterior                                       3,000.00"),
        ("text", "Saldo al corte                                       2,200.00"),
        ("text", "Pago mínimo                                            220.00"),
        ("text", "Tasa de interés anual: 45.5%"),
        ("blank",),
        ("text", "Movimientos"),
        *table(columns, [
            ["03/AGO", "AMAZON MX MARKETPLACE", "1,500.00"],
            ["10/AGO", "SU PAGO GRACIAS", "-3,000.00"],
            ["20/AGO", "RESTAURANTE EL FAROL", "700.00"],
        ]),
        ("blank",),
        ("text", "Compras a meses sin intereses"),
        *table(msi, [["15/JUN", "LIVERPOOL INSURGENTES", "12,000.00", "8,000.00", "1,000.00", "3 de 12"]]),
    ])


def prose_statement() -> bytes:
    return render([
        ("text", "Northwind Advisors"),
        ("text", "Quarterly letter prepared as of August 31, 2026."),
        ("text", "Over the quarter the account held 100 shares of VTI valued at $25,000.00 and"),
        ("text", "uninvested cash of $1,500.00, bringing the account value to $26,500.00."),
        ("text", "Your account ending in 4321 remains invested according to plan."),
    ])
