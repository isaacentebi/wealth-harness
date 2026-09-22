"""Realistic, fictional statements and tax forms for end-to-end ingestion tests.

Run ``uv run --extra dev python tests/fixtures/realdocs/make_docs.py`` to rebuild
every file in this directory.  The output is byte-for-byte reproducible
(reportlab ``invariant`` mode) and ``ground_truth.json`` records every figure
the documents print, so a test can check an extraction against what a person
would read off the page.

The people, addresses, RFCs, CURPs, account and card numbers are invented.  The
layouts, vocabulary and number formats follow what the institutions actually
send: GBM and BBVA México monthly statements, a Banorte credit card statement,
a GBM annual constancia, a Schwab 1099 composite, a Fidelity Form 5498 and an
Interactive Brokers activity statement CSV.  They are deliberately not clean
test data: page headers and footers repeat, summary boxes print the same
totals as the detail tables, descriptions wrap onto a second line, balances
are printed only on a day's last movement, and the legal fine print is there.
"""

from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path

from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas


HERE = Path(__file__).resolve().parent
W, H = LETTER
PERSON = "MARIANA ROBLES TREVIÑO"
PERSON_US = "MARIANA ROBLES TREVINO"
ADDRESS = ("AV. ALVARO OBREGON 185 INT 4", "COL. ROMA NORTE, CUAUHTEMOC", "CIUDAD DE MEXICO, C.P. 06700")
RFC = "ROTM940312QX1"


def money(value: Decimal | str | float, *, sign: str = "", parens: bool = False, dollar: bool = False) -> str:
    """Mexican and US statements both print 1,234.56; losses in parentheses or with a leading minus."""
    number = Decimal(str(value)).quantize(Decimal("0.01"))
    text = f"{abs(number):,.2f}"
    if dollar:
        text = "$" + text
    if number < 0:
        return f"({text})" if parens else f"-{text}"
    return sign + text


def D(value: str) -> Decimal:
    return Decimal(value)


class Doc:
    """A multi-page document drawn at fixed positions (so text extraction keeps the columns)."""

    def __init__(self, path: Path, *, title: str, author: str):
        self.canvas = canvas.Canvas(str(path), pagesize=LETTER, invariant=1)
        self.canvas.setTitle(title)
        self.canvas.setAuthor(author)
        self.canvas.setCreator("Statement composition engine 7.4")

    def font(self, size: float = 8, bold: bool = False) -> None:
        self.canvas.setFont("Helvetica-Bold" if bold else "Helvetica", size)

    def text(self, x: float, y: float, value: str, size: float = 8, bold: bool = False) -> None:
        self.font(size, bold)
        self.canvas.drawString(x, y, value)

    def right(self, x: float, y: float, value: str, size: float = 8, bold: bool = False) -> None:
        self.font(size, bold)
        self.canvas.drawRightString(x, y, value)

    def center(self, x: float, y: float, value: str, size: float = 8, bold: bool = False) -> None:
        self.font(size, bold)
        self.canvas.drawCentredString(x, y, value)

    def rule(self, y: float, x0: float = 36, x1: float = W - 36, width: float = 0.5) -> None:
        self.canvas.setLineWidth(width)
        self.canvas.line(x0, y, x1, y)

    def box(self, x: float, y: float, w: float, h: float) -> None:
        self.canvas.setLineWidth(0.6)
        self.canvas.rect(x, y, w, h)

    def shade(self, x: float, y: float, w: float, h: float, gray: float = 0.9) -> None:
        self.canvas.setFillGray(gray)
        self.canvas.rect(x, y, w, h, stroke=0, fill=1)
        self.canvas.setFillGray(0)

    def fine(self, x: float, y: float, lines: list[str], size: float = 5.5, leading: float = 7) -> float:
        for line in lines:
            self.text(x, y, line, size)
            y -= leading
        return y

    def row(self, y: float, cells: list[tuple[float, str, str]], size: float = 7.5, bold: bool = False) -> None:
        """cells: (x, align L|R|C, text); R and C use x as the right edge or the centre."""
        for x, align, value in cells:
            if not value:
                continue
            if align == "R":
                self.right(x, y, value, size, bold)
            elif align == "C":
                self.center(x, y, value, size, bold)
            else:
                self.text(x, y, value, size, bold)

    def page(self) -> None:
        self.canvas.showPage()

    def save(self) -> None:
        self.canvas.save()


# ============================================================================ GBM monthly statement

GBM_HOLDINGS = {
    "sic": [  # emisora, serie, titulos, costo promedio, precio, valor, plusvalia, % cartera
        ("AAPL", "*", 30, D("3650.00"), D("4212.50")),
        ("VOO", "*", 12, D("9480.00"), D("10265.80")),
    ],
    "bmv": [
        ("FUNO", "11", 1500, D("21.85"), D("23.40")),
        ("WALMEX", "*", 600, D("64.20"), D("58.73")),
    ],
    "fund": [("GBMF2", "BO", 3000, D("3.4380"), D("3.456789"))],
}
GBM_CETES = ("BI CETES", "261015", 5000, 45, "7.12%", D("9.932140"))
GBM_CASH = D("18432.15")
GBM_OPENING_VALUE = D("391120.45")
GBM_MOVES = [  # fecha, liquidacion, concepto, emisora, titulos, precio, importe, comision, iva, neto (signed)
    ("01/08/2026", "01/08/2026", "SALDO INICIAL", "", "", "", "", "", "", None),
    ("05/08/2026", "05/08/2026", "DEPOSITO DE EFECTIVO SPEI", "", "", "", "15,000.00", "", "", D("15000.00")),
    ("07/08/2026", "09/08/2026", "COMPRA MERCADO GLOBAL", "VOO *", "2", "10,105.30", "20,210.60", "50.53", "8.08",
     D("-20269.21")),
    ("14/08/2026", "14/08/2026", "DIVIDENDO EN EFECTIVO", "AAPL *", "30", "", "145.24", "", "", D("145.24")),
    ("14/08/2026", "14/08/2026", "RETENCION ISR DIVIDENDOS 10%", "AAPL *", "", "", "14.52", "", "", D("-14.52")),
    ("20/08/2026", "20/08/2026", "DISTRIBUCION FIBRA", "FUNO 11", "1,500", "", "811.80", "", "", D("811.80")),
    ("20/08/2026", "20/08/2026", "RETENCION ISR FIBRA 30%", "FUNO 11", "", "", "243.54", "", "", D("-243.54")),
]


def _gbm_rows():
    rows = {}
    for group, items in GBM_HOLDINGS.items():
        rows[group] = []
        for symbol, serie, qty, cost, price in items:
            value = (price * qty).quantize(Decimal("0.01"))
            gain = value - (cost * qty).quantize(Decimal("0.01"))
            rows[group].append((symbol, serie, qty, cost, price, value, gain))
    name, serie, qty, days, rate, price = GBM_CETES
    cetes_value = (price * qty).quantize(Decimal("0.01"))
    return rows, cetes_value


def gbm_statement(path: Path, *, split_sic: bool = False) -> dict:
    """``split_sic`` breaks the SIC table across a page: the next page repeats only the column header."""
    rows, cetes_value = _gbm_rows()
    sic = sum(r[5] for r in rows["sic"])
    bmv = sum(r[5] for r in rows["bmv"])
    fund = sum(r[5] for r in rows["fund"])
    total = sic + bmv + fund + cetes_value + GBM_CASH
    dividends = D("145.24") + D("811.80")
    withheld = D("14.52") + D("243.54")
    flows_in = D("15000.00")
    valuation = total - GBM_OPENING_VALUE - flows_in - dividends + withheld
    opening_cash = GBM_CASH - sum(m[9] for m in GBM_MOVES if m[9] is not None)
    pages = 4 if split_sic else 3
    doc = Doc(path, title="Estado de Cuenta GBM Agosto 2026", author="GBM Grupo Bursatil Mexicano")

    def header(number: int) -> None:
        doc.text(36, 750, "GBM", 22, True)
        doc.text(36, 738, "Grupo Bursátil Mexicano, S.A. de C.V., Casa de Bolsa", 7)
        doc.right(W - 36, 752, "ESTADO DE CUENTA", 11, True)
        doc.right(W - 36, 740, "Periodo: del 01/08/2026 al 31/08/2026", 8)
        if number > 1:
            doc.right(W - 36, 729, "Contrato: 10457832      " + PERSON, 7)
        if split_sic:  # chrome that changes on every page: pagination and a printed-at stamp
            doc.text(36, 714, f"Hoja {number}/{pages}", 6)
            doc.right(W - 36, 714, f"Page {number} of {pages}", 6)
            doc.text(36, 60, f"Impreso el 01/09/2026 08:1{number} hrs", 5.5)
        doc.rule(722, width=1.2)

    def footer(number: int) -> None:
        doc.rule(52)
        doc.text(36, 42, "GBM Grupo Bursátil Mexicano, S.A. de C.V., Casa de Bolsa. Av. Insurgentes Sur 1605 piso 31, "
                         "Col. San José Insurgentes, C.P. 03900, Ciudad de México.", 5.5)
        doc.text(36, 34, "Atención a clientes: 55 5480 5800   www.gbm.com   UNE: une@gbm.com", 5.5)
        doc.right(W - 36, 34, f"Página {number} de {pages}", 7)

    # ---- page 1: cliente, resumen, composicion
    header(1)
    y = 700
    doc.text(36, y, PERSON, 9, True)
    for line in ADDRESS:
        y -= 11
        doc.text(36, y, line, 8)
    doc.text(36, y - 11, f"R.F.C.: {RFC}", 8)
    doc.box(340, 632, 236, 76)
    doc.text(348, 696, "Contrato: 10457832", 8, True)
    doc.text(348, 684, "Tipo de contrato: Persona Física - Inversión", 7.5)
    doc.text(348, 673, "Perfil de inversión: Crecimiento", 7.5)
    doc.text(348, 662, "Servicio: Ejecución de operaciones (plataforma digital)", 7.5)
    doc.text(348, 651, "Moneda: Pesos mexicanos (M.N.)", 7.5)
    doc.text(348, 640, "Fecha de corte: 31/08/2026", 7.5)

    doc.shade(36, 600, W - 72, 14)
    doc.text(40, 604, "RESUMEN DEL PORTAFOLIO", 9, True)
    summary = [
        ("Valor del portafolio al 31/07/2026", money(GBM_OPENING_VALUE)),
        ("Entradas de efectivo", money(flows_in)),
        ("Salidas de efectivo", "0.00"),
        ("Dividendos, distribuciones e intereses", money(dividends)),
        ("Impuestos retenidos", money(-withheld)),
        ("Variación por valuación de mercado", money(valuation)),
    ]
    y = 586
    for label, amount in summary:
        doc.text(44, y, label, 8)
        doc.right(330, y, amount, 8)
        y -= 12
    doc.rule(y + 8, 44, 330)
    doc.text(44, y - 2, "Valor del portafolio al 31/08/2026", 8.5, True)
    doc.right(330, y - 2, money(total, dollar=True), 8.5, True)
    doc.text(360, 586, "Rendimiento del periodo", 8, True)
    doc.text(360, 574, "Rendimiento nominal del mes:", 7.5)
    doc.right(W - 44, 574, "-2.09%", 7.5)
    doc.text(360, 562, "Rendimiento acumulado 2026:", 7.5)
    doc.right(W - 44, 562, "6.84%", 7.5)
    doc.text(360, 550, "Rendimiento de los últimos 12 meses:", 7.5)
    doc.right(W - 44, 550, "11.37%", 7.5)

    y = 488
    doc.shade(36, y - 3, W - 72, 14)
    doc.text(40, y + 1, "COMPOSICIÓN DEL PORTAFOLIO", 9, True)
    y -= 16
    doc.row(y, [(44, "L", "Tipo de valor"), (380, "R", "Valor de mercado"), (470, "R", "% del portafolio")], 7.5, True)
    composition = [("Renta variable nacional", bmv), ("Renta variable internacional (SIC)", sic),
                   ("Mercado de dinero", cetes_value), ("Sociedades de inversión", fund), ("Efectivo", GBM_CASH)]
    for label, amount in composition:
        y -= 11
        doc.row(y, [(44, "L", label), (380, "R", money(amount)), (470, "R", f"{amount / total * 100:.2f}%")])
    y -= 13
    doc.rule(y + 9, 44, 470)
    doc.row(y, [(44, "L", "Total"), (380, "R", money(total)), (470, "R", "100.00%")], 7.5, True)

    y -= 34
    doc.text(36, y, "Estimado cliente:", 7.5, True)
    y = doc.fine(36, y - 10, [
        "Le recordamos que las operaciones en el Sistema Internacional de Cotizaciones (SIC) se liquidan en pesos y se valúan",
        "con el precio de cierre publicado por la bolsa en la que operan, convertido al tipo de cambio FIX publicado por Banxico.",
        "Tipo de cambio FIX al 31/08/2026: 18.6235 pesos por dólar.",
    ], 7, 9)
    footer(1)
    doc.page()

    # ---- page 2: desglose del portafolio
    header(2)
    y = 706
    doc.shade(36, y - 3, W - 72, 14)
    doc.text(40, y + 1, "DESGLOSE DEL PORTAFOLIO", 9, True)
    cols = [(40, "L"), (110, "L"), (185, "R"), (250, "R"), (318, "R"), (398, "R"), (482, "R"), (W - 40, "R")]

    def holdings_header(y: float) -> float:
        top = ["Emisora", "Serie", "Títulos", "Costo", "Precio de", "Valor de", "Plusvalía /", "% del"]
        bottom = ["", "", "", "promedio", "mercado", "mercado", "Minusvalía", "portafolio"]
        doc.row(y, [(x, a, t) for (x, a), t in zip(cols, top)], 7, True)
        doc.row(y - 9, [(x, a, t) for (x, a), t in zip(cols, bottom)], 7, True)
        doc.rule(y - 12)
        return y - 23

    for title, group in (("RENTA VARIABLE - MERCADO GLOBAL (SIC)", "sic"), ("RENTA VARIABLE - MERCADO NACIONAL", "bmv")):
        y -= 22
        doc.text(40, y, title, 8, True)
        y = holdings_header(y - 12)
        for index, (symbol, serie, qty, cost, price, value, gain) in enumerate(rows[group]):
            if split_sic and group == "sic" and index == 1:
                footer(2)
                doc.page()
                header(3)
                y = holdings_header(706)
            doc.row(y, [(x, a, t) for (x, a), t in zip(cols, [
                symbol, serie, f"{qty:,}", money(cost), money(price), money(value), money(gain, parens=True),
                f"{value / total * 100:.2f}%"])])
            y -= 11
        subtotal = sum(r[5] for r in rows[group])
        gains = sum(r[6] for r in rows[group])
        doc.rule(y + 7, 300)
        doc.row(y - 2, [(40, "L", "Subtotal"), (398, "R", money(subtotal)), (482, "R", money(gains, parens=True)),
                        (W - 40, "R", f"{subtotal / total * 100:.2f}%")], 7.5, True)
        y -= 10

    y -= 22
    doc.text(40, y, "MERCADO DE DINERO", 8, True)
    y -= 12
    money_cols = [(40, "L"), (110, "L"), (185, "R"), (250, "R"), (318, "R"), (398, "R"), (482, "R")]
    doc.row(y, [(x, a, t) for (x, a), t in zip(money_cols, ["Emisora", "Serie", "Títulos", "Días por", "Tasa",
                                                              "Precio", "Valor de"])], 7, True)
    doc.row(y - 9, [(x, a, t) for (x, a), t in zip(money_cols, ["", "", "", "vencer", "", "", "mercado"])], 7, True)
    doc.rule(y - 12)
    y -= 23
    name, serie, qty, days, rate, price = GBM_CETES
    doc.row(y, [(x, a, t) for (x, a), t in zip(money_cols, [
        name, serie, f"{qty:,}", str(days), rate, f"{price:.6f}", money(cetes_value)])])
    y -= 22
    doc.text(40, y, "SOCIEDADES DE INVERSIÓN", 8, True)
    y = holdings_header(y - 12)
    for symbol, serie, qty, cost, price, value, gain in rows["fund"]:
        doc.row(y, [(x, a, t) for (x, a), t in zip(cols, [
            symbol, serie, f"{qty:,}", f"{cost:.4f}", f"{price:.6f}", money(value), money(gain, parens=True),
            f"{value / total * 100:.2f}%"])])
        y -= 11
    y -= 18
    doc.text(40, y, "EFECTIVO", 8, True)
    y -= 13
    doc.row(y, [(40, "L", "Efectivo disponible"), (398, "R", money(GBM_CASH)),
                (W - 40, "R", f"{GBM_CASH / total * 100:.2f}%")])
    y -= 11
    doc.row(y, [(40, "L", "Efectivo por liquidar"), (398, "R", "0.00")])
    y -= 22
    doc.rule(y + 10, width=1)
    doc.row(y, [(40, "L", "Valor total del portafolio"), (398, "R", money(total, dollar=True))], 8.5, True)
    y -= 30
    doc.fine(40, y, [
        "Los valores de renta variable se presentan a precio de cierre. Las emisoras marcadas con (*) en serie cotizan en el SIC.",
        "El costo promedio incluye comisiones e IVA de las compras. La plusvalía o minusvalía no realizada no constituye ganancia",
        "o pérdida fiscal; ésta se determina al enajenar los títulos conforme al artículo 129 de la Ley del ISR.",
    ], 6, 8)
    footer(pages - 1)
    doc.page()

    # ---- last page: movimientos
    header(pages)
    y = 706
    doc.shade(36, y - 3, W - 72, 14)
    doc.text(40, y + 1, "MOVIMIENTOS DEL PERIODO", 9, True)
    y -= 20
    mcols = [(40, "L"), (86, "L"), (132, "L"), (262, "L"), (330, "R"), (380, "R"), (432, "R"), (474, "R"),
             (506, "R"), (W - 40, "R")]
    doc.row(y, [(x, a, t) for (x, a), t in zip(mcols, ["Fecha", "Fecha", "Concepto", "Emisora", "Títulos", "Precio",
                                                         "Importe", "Comisión", "IVA", "Saldo"])], 7, True)
    doc.row(y - 9, [(x, a, t) for (x, a), t in zip(mcols, ["operación", "liquidación"])], 7, True)
    doc.rule(y - 12)
    y -= 23
    balance = opening_cash
    for fecha, liq, concepto, emisora, titulos, precio, importe, comision, iva, net in GBM_MOVES:
        if net is not None:
            balance += net
        doc.row(y, [(x, a, t) for (x, a), t in zip(mcols, [
            fecha, liq, concepto, emisora, titulos, precio, importe, comision, iva, money(balance)])], 7)
        y -= 11
    doc.rule(y + 7)
    doc.row(y - 2, [(132, "L", "SALDO FINAL EN EFECTIVO"), (W - 40, "R", money(balance))], 7.5, True)
    y -= 34
    doc.text(40, y, "DIVIDENDOS Y RETENCIONES DEL PERIODO", 8, True)
    y -= 12
    doc.row(y, [(40, "L", "Dividendos y distribuciones cobrados"), (300, "R", money(dividends))])
    y -= 11
    doc.row(y, [(40, "L", "ISR retenido"), (300, "R", money(withheld))])
    y -= 30
    doc.fine(40, y, [
        "Aviso: Usted cuenta con un plazo de 90 días naturales a partir de la fecha de corte para objetar este estado de cuenta.",
        "Las comisiones por intermediación son del 0.25% sobre el monto operado más el Impuesto al Valor Agregado (IVA) del 16%.",
        "Las retenciones de ISR sobre dividendos de emisoras del SIC y distribuciones de FIBRAS se informarán en su constancia anual.",
        "Las inversiones en valores no están garantizadas por el Instituto para la Protección al Ahorro Bancario (IPAB).",
        "Este documento es un estado de cuenta; no es un comprobante fiscal. Para consultas acuda a la Unidad Especializada de",
        "Atención a Usuarios (UNE) o a la CONDUSEF: 55 5340 0999, www.condusef.gob.mx.",
    ], 6, 8)
    footer(pages)
    doc.save()
    return {
        "institution": "GBM", "contract_last4": "7832", "period": ["2026-08-01", "2026-08-31"], "currency": "MXN",
        "total": str(total), "cash": str(GBM_CASH), "opening_cash": str(opening_cash),
        "positions": {
            **{f"SIC:{r[0]}": {"quantity": r[2], "price": str(r[4]), "value": str(r[5]),
                               "cost_basis": str(r[3] * r[2])} for r in rows["sic"]},
            **{f"BMV:{r[0]} {r[1]}" if r[1] != "*" else f"BMV:{r[0]}": {"quantity": r[2], "price": str(r[4]),
                                                                        "value": str(r[5]),
                                                                        "cost_basis": str(r[3] * r[2])}
               for r in rows["bmv"]},
            "CETES 261015": {"quantity": GBM_CETES[2], "price": str(GBM_CETES[5]), "value": str(cetes_value)},
            "GBMF2 BO": {"quantity": 3000, "price": "3.456789", "value": str(rows["fund"][0][5])},
        },
        "dividends": str(dividends), "isr_withheld": str(withheld), "fx_usd_mxn": "18.6235",
        "commission": "50.53", "commission_iva": "8.08",
    }


# ============================================================================ BBVA Mexico checking

BBVA_OPENING = D("45245.67")
BBVA_MOVES = [  # dia, cod, descripcion, segunda linea, referencia, cargo, abono
    ("01/AGO", "T17", "SPEI ENVIADO SANTANDER", "RENTA AGOSTO DEPTO 302", "0010826401", "18,500.00", ""),
    ("02/AGO", "C07", "OXXO INSURGENTES SUR", "TARJETA 4152 **** 7730", "", "187.50", ""),
    ("03/AGO", "C07", "UBER *TRIP", "HELP.UBER.COM", "", "142.37", ""),
    ("05/AGO", "T17", "SPEI ENVIADO GBM", "0001218001045783 INVERSION", "0050826144", "15,000.00", ""),
    ("06/AGO", "G30", "DOMICILIACION CFE SUMINISTRADOR", "SERV 542 110 870 012", "", "684.00", ""),
    ("08/AGO", "C07", "AMAZON MX MARKETPLACE", "", "", "1,249.00", ""),
    ("10/AGO", "Y45", "RETIRO CAJERO AUTOMATICO", "BBVA SUC 1187 ROMA", "", "2,000.00", ""),
    ("12/AGO", "G30", "DOMICILIACION TELMEX INFINITUM", "", "", "589.00", ""),
    ("14/AGO", "C07", "OXXO ROMA NORTE", "", "", "96.50", ""),
    ("15/AGO", "N06", "PAGO DE NOMINA", "ACME SOLUCIONES DIGITALES SA DE CV", "0150826", "", "42,500.00"),
    ("15/AGO", "T17", "SPEI ENVIADO BANORTE", "PAGO TARJETA DE CREDITO", "0150826377", "10,000.00", ""),
    ("17/AGO", "C07", "LIVERPOOL POLANCO", "", "", "2,350.00", ""),
    ("18/AGO", "G30", "DOMICILIACION GNP SEGURO GMM", "POLIZA 88-2031947", "", "1,875.40", ""),
    ("20/AGO", "C07", "WALMART EXPRESS COYOACAN", "", "", "1,642.83", ""),
    ("22/AGO", "C07", "UBER EATS", "HELP.UBER.COM", "", "385.20", ""),
    ("24/AGO", "T20", "SPEI RECIBIDO BANAMEX", "DIEGO HERNANDEZ GASTOS CASA", "0240826910", "", "5,000.00"),
    ("25/AGO", "C07", "NETFLIX.COM", "", "", "299.00", ""),
    ("27/AGO", "C07", "OXXO DEL VALLE", "", "", "64.00", ""),
    ("28/AGO", "G30", "DOMICILIACION SMART FIT", "", "", "599.00", ""),
    ("29/AGO", "T17", "SPEI ENVIADO BANAMEX", "MARIA TREVIÑO APOYO MAMA", "0290826552", "3,000.00", ""),
    ("30/AGO", "N06", "PAGO DE NOMINA", "ACME SOLUCIONES DIGITALES SA DE CV", "0300826", "", "42,500.00"),
]


def bbva_statement(path: Path) -> dict:
    debits = sum(D(m[5].replace(",", "")) for m in BBVA_MOVES if m[5])
    credits = sum(D(m[6].replace(",", "")) for m in BBVA_MOVES if m[6])
    closing = BBVA_OPENING + credits - debits
    pages = 2
    doc = Doc(path, title="Estado de Cuenta BBVA", author="BBVA Mexico")

    def header(number: int) -> None:
        doc.text(36, 748, "BBVA", 24, True)
        doc.right(W - 36, 756, "Estado de Cuenta", 10, True)
        doc.right(W - 36, 744, "Libretón Básico Cuenta Digital", 8)
        doc.right(W - 36, 733, f"PAGINA {number} / {pages}", 7)
        doc.rule(724, width=1.4)

    def footer() -> None:
        doc.rule(50)
        doc.fine(36, 42, [
            "BBVA México, S.A., Institución de Banca Múltiple, Grupo Financiero BBVA México. Av. Paseo de la Reforma 510, "
            "Col. Juárez, C.P. 06600, Ciudad de México. R.F.C. BBA830831LJ2",
            "Línea BBVA 55 5226 2663. Consultas, reclamaciones o aclaraciones: Unidad Especializada de Atención a Usuarios.",
        ], 5.5, 7)

    header(1)
    doc.text(36, 700, PERSON, 9, True)
    y = 700
    for line in ADDRESS:
        y -= 11
        doc.text(36, y, line, 8)
    info = [("Periodo", "DEL 01/AGO/2026 AL 31/AGO/2026"), ("Fecha de Corte", "31/AGO/2026"),
            ("No. de Cuenta", "0482917365"), ("No. de Cliente", "D4829173"), ("R.F.C", RFC),
            ("No. Cuenta CLABE", "012180004829173651"), ("Sucursal", "1187 ROMA NORTE")]
    y = 704
    for label, value in info:
        doc.text(340, y, label, 7.5, True)
        doc.text(440, y, value, 7.5)
        y -= 11
    y = 610
    doc.shade(36, y - 3, W - 72, 14, 0.85)
    doc.text(40, y + 1, "Información Financiera", 9, True)
    doc.right(W - 40, y + 1, "MONEDA NACIONAL", 8, True)
    y -= 18
    doc.text(40, y, "Rendimiento", 8, True)
    doc.text(320, y, "Comportamiento", 8, True)
    left = [("Saldo Promedio", "52,214.77"), ("Días del Periodo", "31"), ("Tasa Bruta Anual %", "0.000"),
            ("Saldo Promedio Gravable", "0.00"), ("Intereses a Favor (+)", "0.00"), ("ISR Retenido (-)", "0.00"),
            ("Comisiones (-)", "0.00")]
    right = [("Saldo Anterior", money(BBVA_OPENING)), ("Depósitos / Abonos (+)", money(credits)),
             ("Retiros / Cargos (-)", money(debits)), ("Saldo Final (+)", money(closing)),
             ("Saldo Promedio Mínimo Mensual", "0.00")]
    yl = yr = y - 13
    for label, value in left:
        doc.text(40, yl, label, 7.5)
        doc.right(280, yl, value, 7.5)
        yl -= 11
    for label, value in right:
        doc.text(320, yr, label, 7.5)
        doc.right(W - 40, yr, value, 7.5)
        yr -= 11
    y = min(yl, yr) - 14
    doc.text(40, y, "Total de Movimientos", 8, True)
    count_debits = sum(1 for m in BBVA_MOVES if m[5])
    count_credits = sum(1 for m in BBVA_MOVES if m[6])
    doc.text(40, y - 12, f"Total Importe Cargos      {money(debits)}      Total Movimientos Cargos      {count_debits}", 7.5)
    doc.text(40, y - 23, f"Total Importe Abonos      {money(credits)}      Total Movimientos Abonos      {count_credits}", 7.5)
    y -= 50
    doc.fine(40, y, [
        "GAT Nominal 0.00% y GAT Real -3.87% antes de impuestos. Para fines informativos y de comparación.",
        "La GAT Real es el rendimiento que obtendría después de descontar la inflación estimada. Fecha de cálculo: 31/07/2026.",
        "Los recursos de los Usuarios en las operaciones realizadas con BBVA México no se encuentran respaldados por ninguna",
        "dependencia gubernamental, salvo los depósitos garantizados por el IPAB hasta por 400,000 UDIS por persona.",
    ], 6, 8)

    # detalle de movimientos: continues on pages 1-3
    mcols = [(40, "L"), (72, "L"), (104, "L"), (126, "L"), (330, "L"), (410, "R"), (470, "R"), (522, "R"), (W - 36, "R")]

    def table_header(y: float) -> float:
        doc.shade(36, y - 3, W - 72, 14, 0.85)
        doc.text(40, y + 1, "Detalle de Movimientos Realizados", 9, True)
        y -= 18
        doc.row(y, [(x, a, t) for (x, a), t in zip(mcols, ["FECHA", "", "COD.", "DESCRIPCIÓN", "REFERENCIA", "CARGOS",
                                                             "ABONOS", "SALDO", ""])], 7, True)
        doc.row(y - 9, [(x, a, t) for (x, a), t in zip(mcols, ["OPER", "LIQ", "", "", "", "", "", "OPERACIÓN",
                                                                 "LIQUIDACIÓN"])], 7, True)
        doc.rule(y - 12)
        return y - 24

    y = table_header(y - 44)
    balance = BBVA_OPENING
    last_of_day = {m[0]: i for i, m in enumerate(BBVA_MOVES)}
    for index, (day, code, desc, second, ref, cargo, abono) in enumerate(BBVA_MOVES):
        if y < 110:
            footer()
            doc.page()
            header(2)
            y = table_header(706)
        balance += (D(abono.replace(",", "")) if abono else 0) - (D(cargo.replace(",", "")) if cargo else 0)
        shown = money(balance) if last_of_day[day] == index else ""  # BBVA prints the balance on a day's last line
        doc.row(y, [(x, a, t) for (x, a), t in zip(mcols, [day, day, code, desc, ref, cargo, abono, shown, shown])], 7)
        if second:
            y -= 9
            doc.text(126, y, second, 6.5)
        y -= 12
    doc.rule(y + 6)
    doc.row(y - 4, [(126, "L", "TOTAL IMPORTE CARGOS / ABONOS"), (410, "R", money(debits)),
                    (470, "R", money(credits))], 7, True)
    doc.fine(40, y - 30, [
        "Estimado Cliente: Su Estado de Cuenta ha sido modificado y ahora tiene más detalle de los movimientos.",
        "También le informamos que su Contrato ha sido modificado; puede consultarlo en bbva.mx.",
        "Si desea recibir pagos a través de transferencias electrónicas de fondos interbancarias, facilite a sus pagadores su CLABE.",
    ], 6, 8)
    footer()
    doc.save()
    return {"institution": "BBVA México", "account_last4": "7365", "period": ["2026-08-01", "2026-08-31"],
            "currency": "MXN", "opening": str(BBVA_OPENING), "deposits": str(credits), "withdrawals": str(debits),
            "closing": str(closing), "payroll_monthly": "85000.00", "transactions": len(BBVA_MOVES),
            "spending_outflows_excl_transfers": str(debits - D("15000.00") - D("10000.00") - D("3000.00"))}


# ============================================================================ Banorte credit card

CARD = {"previous": D("22310.55"), "payment": D("10000.00"), "interest": D("845.20"), "iva": D("135.23"),
        "fees": D("0.00"), "msi_installment": D("1500.00"), "msi_pending": D("12000.00"), "limit": D("80000.00"),
        "minimum": D("1850.00"), "rate": "52.80%", "cat": "68.4%"}
CARD_MOVES = [  # fecha operacion, fecha cargo, descripcion, monto (payment negative)
    ("27/07/2026", "28/07/2026", "AMAZON MEXICO  MEXICO DF", "1,099.00"),
    ("03/08/2026", "04/08/2026", "SPOTIFY  STOCKHOLM", "129.00"),
    ("07/08/2026", "08/08/2026", "COSTCO SATELITE  NAUCALPAN", "2,184.56"),
    ("12/08/2026", "12/08/2026", "GAS BP INSURGENTES  CDMX", "950.00"),
    ("15/08/2026", "15/08/2026", "PAGO SPEI RECIBIDO - GRACIAS", "-10,000.00"),
    ("19/08/2026", "20/08/2026", "RESTAURANTE CONTRAMAR  CDMX", "1,457.89"),
    ("25/08/2026", "25/08/2026", "INTERESES ORDINARIOS", "845.20"),
    ("25/08/2026", "25/08/2026", "IVA DE INTERESES", "135.23"),
]


def banorte_card(path: Path) -> dict:
    purchases = sum(D(m[3].replace(",", "")) for m in CARD_MOVES[:-2] if not m[3].startswith("-"))
    cut = CARD["previous"] - CARD["payment"] + purchases + CARD["interest"] + CARD["iva"] + CARD["fees"]
    no_interest = cut + CARD["msi_installment"]
    total_debt = cut + CARD["msi_installment"] + CARD["msi_pending"]
    available = CARD["limit"] - total_debt
    pages = 2
    doc = Doc(path, title="Estado de Cuenta Tarjeta de Credito Banorte", author="Banorte")

    def header(number: int) -> None:
        doc.text(36, 748, "BANORTE", 20, True)
        doc.text(36, 737, "Banco Mercantil del Norte, S.A., Institución de Banca Múltiple, Grupo Financiero Banorte", 6)
        doc.right(W - 36, 752, "ESTADO DE CUENTA", 10, True)
        doc.right(W - 36, 741, "TARJETA DE CRÉDITO BANORTE CLÁSICA", 8)
        doc.right(W - 36, 730, f"Hoja {number} de {pages}", 7)
        doc.rule(722, width=1.2)

    def footer() -> None:
        doc.rule(50)
        doc.fine(36, 42, [
            "Banco Mercantil del Norte, S.A. Av. Revolución 3000, Col. Primavera, C.P. 64830, Monterrey, N.L. "
            "R.F.C. BMN930209927. Banortel 55 5140 5600.",
            "UNE: Av. Prolongación Reforma 1230, Santa Fe, CDMX. une@banorte.com. CONDUSEF 55 5340 0999.",
        ], 5.5, 7)

    header(1)
    doc.text(36, 700, PERSON, 9, True)
    y = 700
    for line in ADDRESS:
        y -= 11
        doc.text(36, y, line, 8)
    doc.box(330, 614, W - 366, 96)
    doc.text(338, 698, "Número de tarjeta: 4915 66XX XXXX 3847", 8, True)
    rows = [("Periodo:", "26/07/2026 al 25/08/2026"), ("Fecha de corte:", "25/08/2026"),
            ("Fecha límite de pago:", "14/09/2026"), ("Días del periodo:", "31"),
            ("Límite de crédito:", money(CARD["limit"], dollar=True)), ("Crédito disponible:", money(available, dollar=True))]
    y = 686
    for label, value in rows:
        doc.text(338, y, label, 7.5)
        doc.right(W - 44, y, value, 7.5)
        y -= 11
    y = 596
    doc.shade(36, y - 30, W - 72, 44, 0.88)
    doc.text(44, y + 2, "Pago para no generar intereses:", 9, True)
    doc.right(300, y + 2, money(no_interest, dollar=True), 11, True)
    doc.text(320, y + 2, "Pago mínimo:", 9, True)
    doc.right(W - 44, y + 2, money(CARD["minimum"], dollar=True), 11, True)
    doc.text(44, y - 14, "Pago mínimo + compras y cargos diferidos a meses:", 7.5)
    doc.right(300, y - 14, money(CARD["minimum"] + CARD["msi_installment"], dollar=True), 8)
    doc.text(320, y - 14, "Fecha límite de pago: 14/09/2026", 7.5)
    y = 540
    doc.text(40, y, "RESUMEN DE SALDOS", 9, True)
    summary = [("Saldo anterior", money(CARD["previous"])), ("Pagos y abonos", money(-CARD["payment"])),
               ("Compras y cargos del periodo", money(purchases)), ("Intereses", money(CARD["interest"])),
               ("IVA de intereses", money(CARD["iva"])), ("Comisiones", money(CARD["fees"])),
               ("Saldo al corte", money(cut)),
               ("Compras y cargos diferidos a meses (saldo pendiente)", money(CARD["msi_installment"] + CARD["msi_pending"])),
               ("Saldo deudor total", money(total_debt))]
    y -= 14
    for label, value in summary:
        bold = label in ("Saldo al corte", "Saldo deudor total")
        doc.text(44, y, label, 8, bold)
        doc.right(300, y, value, 8, bold)
        y -= 12
    doc.text(330, 526, "Tasa de interés anual ordinaria:", 7.5)
    doc.right(W - 44, 526, CARD["rate"], 7.5)
    doc.text(330, 515, "Tasa de interés mensual ordinaria:", 7.5)
    doc.right(W - 44, 515, "4.40%", 7.5)
    doc.text(330, 504, "Tasa de interés moratoria anual:", 7.5)
    doc.right(W - 44, 504, "0.00%", 7.5)
    doc.text(330, 490, f"CAT PROMEDIO: {CARD['cat']} Sin IVA", 8, True)
    doc.text(330, 480, "Para fines informativos y de comparación.", 6.5)
    doc.text(330, 471, "Fecha de cálculo: 30/06/2026", 6.5)

    y -= 16
    doc.box(36, y - 58, W - 72, 66)
    doc.text(44, y - 4, "Si sólo realizas el pago mínimo:", 8, True)
    doc.row(y - 18, [(44, "L", "Pago mensual"), (200, "L", "Tiempo en que terminarás de pagar"),
                     (430, "L", "Monto aproximado a pagar")], 7.5, True)
    doc.row(y - 30, [(44, "L", money(CARD["minimum"], dollar=True)), (200, "L", "4 años 2 meses"),
                     (430, "L", "$41,860.00")], 7.5)
    doc.row(y - 44, [(44, "L", "Si pagas $3,000.00"), (200, "L", "8 meses"), (430, "L", "$22,954.00")], 7.5)
    footer()
    doc.page()

    header(2)
    y = 700
    doc.shade(36, y - 3, W - 72, 14, 0.88)
    doc.text(40, y + 1, "DESGLOSE DE MOVIMIENTOS", 9, True)
    y -= 20
    cols = [(40, "L"), (110, "L"), (180, "L"), (W - 40, "R")]
    doc.row(y, [(x, a, t) for (x, a), t in zip(cols, ["Fecha de", "Fecha de", "Descripción del movimiento", "Monto"])],
            7, True)
    doc.row(y - 9, [(x, a, t) for (x, a), t in zip(cols, ["operación", "cargo"])], 7, True)
    doc.rule(y - 12)
    y -= 24
    for operation, charge, desc, amount in CARD_MOVES:
        doc.row(y, [(x, a, t) for (x, a), t in zip(cols, [operation, charge, desc, amount])])
        y -= 11
    y -= 18
    doc.text(40, y, "COMPRAS Y CARGOS DIFERIDOS A MESES SIN INTERESES", 8.5, True)
    y -= 14
    mcols = [(40, "L"), (110, "L"), (300, "R"), (370, "R"), (440, "R"), (500, "R"), (W - 40, "R")]
    doc.row(y, [(x, a, t) for (x, a), t in zip(mcols, ["Fecha", "Descripción", "Monto original", "Saldo pendiente",
                                                         "Pago requerido", "Núm. de pago", "Tasa"])], 7, True)
    doc.rule(y - 3)
    y -= 13
    doc.row(y, [(x, a, t) for (x, a), t in zip(mcols, ["15/04/2026", "LIVERPOOL INSURGENTES 12 MSI", "18,000.00",
                                                         money(CARD["msi_pending"]), money(CARD["msi_installment"]),
                                                         "4 de 12", "0.00%"])])
    y -= 30
    doc.fine(40, y, [
        "Los intereses se calculan sobre el saldo promedio diario del periodo y causan IVA a la tasa del 16%.",
        "El pago mínimo no cubre los intereses del periodo en su totalidad; pagar sólo el mínimo prolonga el plazo y aumenta el costo.",
        "Si su pago es menor al pago para no generar intereses, se cobrarán intereses sobre el saldo promedio diario.",
        "Para dudas o aclaraciones tiene 90 días a partir de la fecha de corte.",
    ], 6, 8)
    footer()
    doc.save()
    return {"institution": "Banorte", "card_last4": "3847", "cut_date": "2026-08-25", "due_date": "2026-09-14",
            "previous": str(CARD["previous"]), "payments": str(CARD["payment"]), "purchases": str(purchases),
            "interest": str(CARD["interest"]), "iva_on_interest": str(CARD["iva"]), "saldo_al_corte": str(cut),
            "pago_no_intereses": str(no_interest), "pago_minimo": str(CARD["minimum"]),
            "msi_pending_incl_current": str(CARD["msi_installment"] + CARD["msi_pending"]),
            "saldo_deudor_total": str(total_debt), "credit_limit": str(CARD["limit"]), "annual_rate": "0.528",
            "cat": "0.684"}


# ============================================================================ GBM constancia 2025

CONSTANCIA = {
    "intereses": {"nominal": D("3842.17"), "inflation_adjustment": D("2015.40"), "real": D("1826.77"),
                  "real_loss": D("0.00"), "isr_withheld": D("612.40")},
    "dividendos": {"domestic_gross": D("1245.60"), "foreign_gross": D("1580.34"), "total": D("2825.94"),
                   "isr_withheld": D("282.59"), "isr_creditable": D("533.83")},
    "sales": [("AMX B", "2,000", "12/05/2022", "15/03/2025", D("38400.00"), D("33210.40")),
              ("AAPL *", "10", "03/02/2023", "22/07/2025", D("43560.30"), D("33929.35")),
              ("WALMEX *", "300", "18/09/2023", "11/11/2025", D("17715.00"), D("20827.40"))],
}


def gbm_constancia(path: Path) -> dict:
    sales = [(e, t, a, s, p, c, max(p - c, D(0)), max(c - p, D(0))) for e, t, a, s, p, c in CONSTANCIA["sales"]]
    gain = sum(s[6] for s in sales)
    loss = sum(s[7] for s in sales)
    net = gain - loss
    doc = Doc(path, title="Constancia Anual 2025", author="GBM Grupo Bursatil Mexicano")
    doc.text(36, 750, "GBM", 22, True)
    doc.text(36, 738, "Grupo Bursátil Mexicano, S.A. de C.V., Casa de Bolsa", 7)
    doc.right(W - 36, 752, "CONSTANCIA ANUAL DE INTERESES, DIVIDENDOS", 9.5, True)
    doc.right(W - 36, 741, "Y ENAJENACIÓN DE ACCIONES", 9.5, True)
    doc.right(W - 36, 729, "Ejercicio fiscal: 2025", 8)
    doc.rule(720, width=1.2)
    info = [("Nombre del contribuyente:", PERSON), ("R.F.C.:", RFC), ("C.U.R.P.:", "ROTM940312MDFBRR08"),
            ("Contrato:", "10457832"), ("Periodo:", "01/01/2025 al 31/12/2025"), ("Fecha de emisión:", "15/02/2026")]
    y = 704
    for label, value in info:
        doc.text(40, y, label, 7.5, True)
        doc.text(170, y, value, 7.5)
        y -= 11
    doc.text(360, 704, "Datos del retenedor", 7.5, True)
    doc.text(360, 693, "R.F.C.: GBM860415HD1", 7.5)
    doc.text(360, 682, "Grupo Bursátil Mexicano, S.A. de C.V.", 7.5)
    doc.text(360, 671, "Casa de Bolsa", 7.5)

    def section(y: float, title: str) -> float:
        doc.shade(36, y - 3, W - 72, 14, 0.88)
        doc.text(40, y + 1, title, 8.5, True)
        return y - 18

    def line(y: float, label: str, amount: Decimal) -> float:
        doc.text(48, y, label, 8)
        doc.right(W - 48, y, money(amount, dollar=True), 8)
        return y - 12

    y = section(624, "I. INTERESES (Artículos 133 a 136 LISR)")
    y = line(y, "Saldo promedio diario de la inversión", D("245380.12"))
    y = line(y, "Intereses nominales", CONSTANCIA["intereses"]["nominal"])
    y = line(y, "Ajuste anual por inflación", CONSTANCIA["intereses"]["inflation_adjustment"])
    y = line(y, "Intereses reales", CONSTANCIA["intereses"]["real"])
    y = line(y, "Pérdida real", CONSTANCIA["intereses"]["real_loss"])
    y = line(y, "ISR retenido", CONSTANCIA["intereses"]["isr_withheld"])
    y = section(y - 10, "II. DIVIDENDOS O UTILIDADES DISTRIBUIDAS (Artículo 140 LISR)")
    y = line(y, "Dividendos de personas morales residentes en México (CUFIN)", CONSTANCIA["dividendos"]["domestic_gross"])
    y = line(y, "Dividendos de fuente extranjera (SIC)", CONSTANCIA["dividendos"]["foreign_gross"])
    y = line(y, "Total de dividendos", CONSTANCIA["dividendos"]["total"])
    y = line(y, "ISR retenido sobre dividendos (10%)", CONSTANCIA["dividendos"]["isr_withheld"])
    y = line(y, "ISR acreditable pagado por la persona moral", CONSTANCIA["dividendos"]["isr_creditable"])
    y = section(y - 10, "III. ENAJENACIÓN DE ACCIONES EN BOLSA (Artículo 129 LISR)")
    cols = [(48, "L"), (130, "R"), (185, "R"), (240, "R"), (318, "R"), (398, "R"), (476, "R"), (W - 48, "R")]
    doc.row(y, [(x, a, t) for (x, a), t in zip(cols, ["Emisora", "Títulos", "Fecha de", "Fecha de", "Precio de",
                                                        "Costo fiscal", "Ganancia", "Pérdida"])], 7, True)
    doc.row(y - 9, [(x, a, t) for (x, a), t in zip(cols, ["", "", "adquisición", "enajenación", "venta",
                                                            "actualizado", "", ""])], 7, True)
    doc.rule(y - 12)
    y -= 24
    for emisora, titulos, acquired, sold, price, cost, g, l in sales:
        doc.row(y, [(x, a, t) for (x, a), t in zip(cols, [emisora, titulos, acquired, sold, money(price), money(cost),
                                                            money(g), money(l)])])
        y -= 11
    doc.rule(y + 7, 300)
    doc.row(y - 2, [(48, "L", "Totales"), (318, "R", money(sum(s[4] for s in sales))),
                    (398, "R", money(sum(s[5] for s in sales))), (476, "R", money(gain)), (W - 48, "R", money(loss))],
            7.5, True)
    y -= 22
    y = line(y, "Ganancia por enajenación de acciones", gain)
    y = line(y, "Pérdida por enajenación de acciones", loss)
    y = line(y, "Resultado neto (ganancia)", net)
    y = line(y, "ISR retenido por enajenación", D("0.00"))
    y -= 16
    doc.fine(40, y, [
        "El contribuyente deberá acumular los intereses reales y los dividendos en su declaración anual y podrá acreditar el ISR",
        "retenido. La ganancia neta por enajenación de acciones en bolsa causa el impuesto a la tasa del 10% que se paga en la",
        "declaración anual (Artículo 129, fracción I LISR); las pérdidas pueden disminuirse de las ganancias de los 5 años siguientes.",
        "Los montos se expresan en pesos mexicanos. Esta constancia se emite en cumplimiento de los artículos 136 y 140 de la LISR",
        "y de la regla 3.16.11 de la Resolución Miscelánea Fiscal. Consulte a su asesor fiscal.",
    ], 6, 8)
    doc.rule(50)
    doc.text(36, 40, "GBM Grupo Bursátil Mexicano, S.A. de C.V., Casa de Bolsa. Av. Insurgentes Sur 1605 piso 31, CDMX.", 5.5)
    doc.right(W - 36, 40, "Página 1 de 1", 7)
    doc.save()
    truth = {k: {f: str(v) for f, v in block.items()} for k, block in CONSTANCIA.items() if k != "sales"}
    truth["enajenacion"] = {"gain": str(gain), "loss": str(loss), "net": str(net), "isr_withheld": "0.00"}
    return {"institution": "GBM", "tax_year": 2025, "issued_on": "2026-02-15", "contract_last4": "7832",
            "currency": "MXN", **truth}


# ============================================================================ Schwab 1099 composite 2025

DIV_1099 = {"1a": ("Total Ordinary Dividends", D("1284.56")), "1b": ("Qualified Dividends", D("1102.33")),
            "2a": ("Total Capital Gain Distributions", D("86.20")), "2b": ("Unrecap. Sec. 1250 Gain", D("0.00")),
            "3": ("Nondividend Distributions", D("0.00")), "4": ("Federal Income Tax Withheld", D("0.00")),
            "5": ("Section 199A Dividends", D("45.10")), "6": ("Investment Expenses", D("0.00")),
            "7": ("Foreign Tax Paid", D("12.45")), "12": ("Exempt-Interest Dividends", D("0.00"))}
INT_1099 = {"1": ("Interest Income", D("412.87")), "2": ("Early Withdrawal Penalty", D("0.00")),
            "3": ("Interest on U.S. Savings Bonds & Treasury Obligations", D("215.30")),
            "4": ("Federal Income Tax Withheld", D("0.00")), "6": ("Foreign Tax Paid", D("0.00")),
            "8": ("Tax-Exempt Interest", D("0.00"))}
LOTS_1099B = {
    ("short", "A"): [("TESLA INC", "88160R101", "TSLA", "25.0000", "03/14/2025", "06/02/2025", D("4812.50"),
                      D("6105.25"), D("1292.75")),
                     ("NVIDIA CORP", "67066G104", "NVDA", "40.0000", "01/21/2025", "09/15/2025", D("7020.00"),
                      D("5480.40"), None),
                     ("TESLA INC", "88160R101", "TSLA", "10.0000", "06/05/2025", "11/20/2025", D("3450.80"),
                      D("3901.25"), None)],
    ("long", "D"): [("VANGUARD TOTAL STOCK MARKET ETF", "922908769", "VTI", "50.0000", "02/10/2021", "08/12/2025",
                     D("15410.00"), D("10225.50"), None),
                    ("MICROSOFT CORP", "594918104", "MSFT", "15.0000", "05/03/2022", "12/08/2025", D("7125.45"),
                     D("4188.00"), None)],
    ("long", "E"): [("COCA-COLA CO", "191216100", "KO", "100.0000", "04/22/2010", "10/03/2025", D("6980.00"),
                     D("2450.00"), None)],
}
DIV_DETAIL = [  # security, cusip, date, kind, amount
    ("APPLE INC", "037833100", "02/13/2025", "Qualified Dividend", D("61.44")),
    ("SCHWAB US DIVIDEND EQUITY ETF", "808524797", "03/26/2025", "Qualified Dividend", D("412.18")),
    ("VANGUARD FTSE DEVELOPED MKTS ETF", "921943858", "03/25/2025", "Qualified Dividend", D("305.62")),
    ("VANGUARD FTSE DEVELOPED MKTS ETF", "921943858", "03/25/2025", "Foreign Tax Paid", D("-12.45")),
    ("VANGUARD TOTAL STOCK MARKET ETF", "922908769", "06/27/2025", "Qualified Dividend", D("323.09")),
    ("SCHWAB US DIVIDEND EQUITY ETF", "808524797", "12/10/2025", "Nonqualified Dividend", D("137.13")),
    ("VANGUARD REAL ESTATE ETF", "922908553", "12/22/2025", "Nonqualified Dividend", D("45.10")),
    ("VANGUARD REAL ESTATE ETF", "922908553", "12/22/2025", "Long-Term Capital Gain Distribution", D("86.20")),
]


def schwab_1099(path: Path) -> dict:
    pages = 3
    doc = Doc(path, title="2025 Form 1099 Composite", author="Charles Schwab")

    def header(number: int, title: str) -> None:
        doc.text(36, 752, "charles", 13)
        doc.text(36, 739, "SCHWAB", 16, True)
        doc.right(W - 36, 754, "TAX YEAR 2025 FORM 1099 COMPOSITE", 9.5, True)
        doc.right(W - 36, 743, title, 8)
        doc.right(W - 36, 732, f"Account Number: XXXX-4321        Page {number} of {pages}", 7)
        doc.rule(724, width=1)

    def footer() -> None:
        doc.rule(48)
        doc.fine(36, 40, [
            "This is important tax information and is being furnished to the Internal Revenue Service (except as indicated). If you "
            "are required to file a return, a negligence penalty or other sanction may be imposed",
            "on you if this income is taxable and the IRS determines that it has not been reported.   Schwab One® Account of "
            f"{PERSON_US}   (0126-4321)",
        ], 5, 6.5)

    def lot_totals(lots) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        proceeds = sum(l[6] for l in lots)
        cost = sum(l[7] for l in lots)
        wash = sum(l[8] or D(0) for l in lots)
        return proceeds, cost, wash, proceeds - cost + wash

    # ---- page 1: summary
    header(1, "& YEAR-END SUMMARY")
    doc.text(36, 700, f"Schwab One® Account of", 8)
    doc.text(36, 689, PERSON_US, 9, True)
    for i, line in enumerate(("AV ALVARO OBREGON 185 INT 4", "COL ROMA NORTE", "CIUDAD DE MEXICO CDMX 06700 MEXICO")):
        doc.text(36, 678 - 11 * i, line, 8)
    doc.text(340, 700, "Date Prepared: February 6, 2026", 8)
    doc.text(340, 689, "Recipient's TIN: ***-**-6789", 8)
    doc.text(340, 678, "Payer: Charles Schwab & Co., Inc.", 8)
    doc.text(340, 667, "3000 Schwab Way, Westlake, TX 76262", 8)
    doc.text(340, 656, "Payer's Federal ID No: 94-1737782", 8)
    y = 620
    doc.shade(36, y - 3, W - 72, 14, 0.88)
    doc.text(40, y + 1, "Summary of Form 1099 Information", 9, True)
    y -= 18
    doc.text(40, y, "Form 1099-DIV", 8, True)
    doc.text(310, y, "Form 1099-INT", 8, True)
    left = [("Total Ordinary Dividends (Box 1a)", DIV_1099["1a"][1]), ("Qualified Dividends (Box 1b)", DIV_1099["1b"][1]),
            ("Total Capital Gain Distributions (Box 2a)", DIV_1099["2a"][1]),
            ("Foreign Tax Paid (Box 7)", DIV_1099["7"][1])]
    right = [("Interest Income (Box 1)", INT_1099["1"][1]),
             ("Interest on U.S. Treasury Obligations (Box 3)", INT_1099["3"][1]),
             ("Federal Income Tax Withheld (Box 4)", INT_1099["4"][1])]
    for i, (label, value) in enumerate(left):
        doc.text(44, y - 12 - 11 * i, label, 7.5)
        doc.right(290, y - 12 - 11 * i, money(value, dollar=True), 7.5)
    for i, (label, value) in enumerate(right):
        doc.text(314, y - 12 - 11 * i, label, 7.5)
        doc.right(W - 40, y - 12 - 11 * i, money(value, dollar=True), 7.5)
    y -= 72
    doc.text(40, y, "Form 1099-B  Summary of Proceeds, Gains & Losses", 8, True)
    scols = [(40, "L"), (270, "R"), (340, "R"), (410, "R"), (480, "R"), (W - 40, "R")]
    y -= 13
    doc.row(y, [(x, a, t) for (x, a), t in zip(scols, ["Term", "Proceeds", "Cost Basis", "Market Discount",
                                                         "Wash Sale Loss", "Realized Gain"])], 7, True)
    doc.row(y - 9, [(x, a, t) for (x, a), t in zip(scols, ["", "", "", "", "Disallowed", "or (Loss)"])], 7, True)
    doc.rule(y - 12)
    y -= 23
    names = {("short", "A"): "Short-Term (Box A) covered", ("long", "D"): "Long-Term (Box D) covered",
             ("long", "E"): "Long-Term (Box E) noncovered"}
    grand = [D(0)] * 4
    for key, lots in LOTS_1099B.items():
        totals = lot_totals(lots)
        grand = [g + t for g, t in zip(grand, totals)]
        doc.row(y, [(x, a, t) for (x, a), t in zip(scols, [names[key], money(totals[0]), money(totals[1]), "0.00",
                                                             money(totals[2]), money(totals[3], parens=True)])])
        y -= 11
    doc.rule(y + 7, 200)
    doc.row(y - 2, [(x, a, t) for (x, a), t in zip(scols, ["Total", money(grand[0]), money(grand[1]), "0.00",
                                                             money(grand[2]), money(grand[3], parens=True)])], 7.5, True)
    y -= 34
    doc.fine(40, y, [
        "Your Form 1099 Composite may include the following Internal Revenue Service (IRS) forms: 1099-DIV, 1099-INT, 1099-B and",
        "1099-MISC. Only those forms that are applicable to your account are included. The Year-End Summary information is not",
        "provided to the IRS; it is provided to assist you with tax return preparation. Schwab may issue a corrected 1099.",
        "Non-U.S. address on file: foreign tax credit and treaty questions should be reviewed with your tax advisor.",
    ], 6, 8)
    footer()
    doc.page()

    # ---- page 2: 1099-DIV and 1099-INT
    header(2, "Dividends and Distributions / Interest Income")
    y = 704
    doc.shade(36, y - 3, W - 72, 14, 0.88)
    doc.text(40, y + 1, "2025 Form 1099-DIV   Dividends and Distributions", 9, True)
    doc.right(W - 40, y + 1, "Copy B for Recipient  OMB No. 1545-0110", 7)
    y -= 20
    boxes = list(DIV_1099.items())
    half = (len(boxes) + 1) // 2
    for i in range(half):
        box, (label, value) = boxes[i]
        doc.text(44, y, f"{box}  {label}", 7.5)
        doc.right(290, y, money(value, dollar=True), 7.5)
        if i + half < len(boxes):
            box, (label, value) = boxes[i + half]
            doc.text(314, y, f"{box}  {label}", 7.5)
            doc.right(W - 40, y, money(value, dollar=True), 7.5)
        y -= 12
    doc.text(314, y, "8  Foreign Country or U.S. Possession   VARIOUS", 7.5)
    y -= 30
    doc.shade(36, y - 3, W - 72, 14, 0.88)
    doc.text(40, y + 1, "2025 Form 1099-INT   Interest Income", 9, True)
    doc.right(W - 40, y + 1, "Copy B for Recipient  OMB No. 1545-0112", 7)
    y -= 20
    boxes = list(INT_1099.items())
    half = (len(boxes) + 1) // 2
    for i in range(half):
        box, (label, value) = boxes[i]
        doc.text(44, y, f"{box}  {label}", 7.5)
        doc.right(290, y, money(value, dollar=True), 7.5)
        if i + half < len(boxes):
            box, (label, value) = boxes[i + half]
            doc.text(314, y, f"{box}  {label}", 7.5)
            doc.right(W - 40, y, money(value, dollar=True), 7.5)
        y -= 12
    y -= 24
    doc.shade(36, y - 3, W - 72, 14, 0.88)
    doc.text(40, y + 1, "2025 Form 1099-MISC   Miscellaneous Information", 9, True)
    y -= 18
    doc.text(44, y, "No reportable Form 1099-MISC information for 2025.", 7.5)
    y -= 40
    doc.fine(40, y, [
        "Box 1a includes the amounts in boxes 1b and 5. Box 1b qualified dividends are eligible for the capital gains tax rates if the",
        "holding period requirement is met. Box 7 shows foreign tax paid by a regulated investment company; you may be able to claim",
        "it as a deduction or a credit on Form 1040. See the Instructions for Form 1116.",
    ], 6, 8)
    footer()
    doc.page()

    # ---- page 3: 1099-B
    header(3, "Proceeds from Broker and Barter Exchange Transactions")
    y = 704
    doc.shade(36, y - 3, W - 72, 14, 0.88)
    doc.text(40, y + 1, "2025 Form 1099-B   Proceeds from Broker and Barter Exchange Transactions", 9, True)
    doc.right(W - 40, y + 1, "OMB No. 1545-0715", 7)
    y -= 16
    doc.fine(40, y, ["Box 1a Description of property, 1b Date acquired, 1c Date sold or disposed, 1d Proceeds, 1e Cost or other "
                     "basis, 1f Accrued market discount, 1g Wash sale loss disallowed."], 6, 8)
    bcols = [(40, "L"), (150, "R"), (200, "R"), (250, "R"), (310, "R"), (370, "R"), (420, "R"), (476, "R"), (W - 40, "R")]
    headings = {
        ("short", "A"): ("SHORT-TERM TRANSACTIONS FOR COVERED TAX LOTS", "Report on Form 8949, Part I, with Box A checked. "
                                                                         "Basis is reported to the IRS."),
        ("long", "D"): ("LONG-TERM TRANSACTIONS FOR COVERED TAX LOTS", "Report on Form 8949, Part II, with Box D checked. "
                                                                       "Basis is reported to the IRS."),
        ("long", "E"): ("LONG-TERM TRANSACTIONS FOR NONCOVERED TAX LOTS", "Report on Form 8949, Part II, with Box E checked. "
                                                                          "Basis is not reported to the IRS."),
    }
    y -= 12
    number = 3
    for key, lots in LOTS_1099B.items():
        if y < 190:
            footer()
            doc.page()
            number += 1
            header(number, "Proceeds from Broker and Barter Exchange Transactions (continued)")
            y = 700
        title, note = headings[key]
        doc.text(40, y, title, 8, True)
        doc.text(40, y - 9, note, 6.5)
        y -= 22
        doc.row(y, [(x, a, t) for (x, a), t in zip(bcols, ["CUSIP / Symbol", "Quantity", "Date", "Date Sold", "Proceeds",
                                                             "Cost Basis", "Market", "Wash Sale Loss", "Realized Gain"])],
                6.5, True)
        doc.row(y - 8, [(x, a, t) for (x, a), t in zip(bcols, ["", "", "Acquired", "or Disposed", "", "", "Discount",
                                                                 "Disallowed", "or (Loss)"])], 6.5, True)
        doc.rule(y - 11)
        y -= 22
        for name, cusip, symbol, qty, acquired, sold, proceeds, cost, wash in lots:
            gain = proceeds - cost + (wash or D(0))
            doc.text(40, y, name, 7, True)
            y -= 9
            doc.row(y, [(x, a, t) for (x, a), t in zip(bcols, [
                f"{cusip} / {symbol}", qty, acquired, sold, money(proceeds), money(cost), "--",
                (money(wash) + " W") if wash else "--", money(gain, parens=True)])], 7)
            y -= 12
        totals = lot_totals(lots)
        doc.rule(y + 6, 280)
        label = "Total Short-Term" if key[0] == "short" else "Total Long-Term"
        doc.row(y - 3, [(x, a, t) for (x, a), t in zip(bcols, [
            label, "", "", "", money(totals[0]), money(totals[1]), "0.00", money(totals[2]),
            money(totals[3], parens=True)])], 7, True)
        y -= 28
    doc.fine(40, y, [
        "W = Wash sale loss disallowed. The disallowed loss is added to the cost basis of the replacement shares.",
        "Noncovered securities: cost basis, acquisition date and holding period are provided for your information only and are",
        "not reported to the IRS.",
    ], 6, 8)

    # ---- detail of dividends (the Year-End Summary part of the composite, not reported to the IRS)
    y -= 30
    if y < 250:
        footer()
        doc.page()
        number += 1
        header(number, "Detail Information of Dividends and Distributions")
        y = 700
    doc.shade(36, y - 3, W - 72, 14, 0.88)
    doc.text(40, y + 1, "Detail Information of Dividends and Distributions", 9, True)
    y -= 18
    dcols = [(40, "L"), (220, "L"), (290, "L"), (380, "L"), (W - 40, "R")]
    doc.row(y, [(x, a, t) for (x, a), t in zip(dcols, ["Description", "CUSIP", "Date Paid", "Transaction", "Amount"])],
            7, True)
    doc.rule(y - 3)
    y -= 13
    for name, cusip, when, kind, amount in DIV_DETAIL:
        doc.row(y, [(x, a, t) for (x, a), t in zip(dcols, [name, cusip, when, kind, money(amount, parens=True)])], 7)
        y -= 11
    doc.rule(y + 7, 300)
    doc.row(y - 2, [(380, "L", "Total Ordinary Dividends"), (W - 40, "R", money(DIV_1099["1a"][1]))], 7, True)
    doc.row(y - 13, [(380, "L", "Total Qualified Dividends"), (W - 40, "R", money(DIV_1099["1b"][1]))], 7, True)
    footer()
    doc.save()
    b = {}
    for key, lots in LOTS_1099B.items():
        p, c, w, g = lot_totals(lots)
        b[f"{key[0]}_{key[1]}"] = {"proceeds": str(p), "cost_basis": str(c), "wash": str(w), "gain": str(g)}
    all_lots = [l for lots in LOTS_1099B.values() for l in lots]
    return {"institution": "Charles Schwab", "tax_year": 2025, "account_last4": "4321", "issued_on": "2026-02-06",
            "div": {"ordinary": "1284.56", "qualified": "1102.33", "capital_gain_distributions": "86.20",
                    "foreign_tax_paid": "12.45", "federal_tax_withheld": "0.00", "section_199a": "45.10"},
            "int": {"interest": "412.87", "us_treasury_interest": "215.30", "federal_tax_withheld": "0.00"},
            "b_by_box": b, "b_lots": len(all_lots),
            "b_short_term_gain": b["short_A"]["gain"],
            "b_long_term_gain": str(D(b["long_D"]["gain"]) + D(b["long_E"]["gain"])),
            "b_proceeds": str(sum(l[6] for l in all_lots)), "b_cost_basis": str(sum(l[7] for l in all_lots)),
            "b_wash": "1292.75"}


# ============================================================================ Fidelity 5498

def fidelity_5498(path: Path) -> dict:
    doc = Doc(path, title="2025 Form 5498", author="Fidelity Investments")
    doc.text(36, 750, "Fidelity", 20, True)
    doc.text(36, 738, "INVESTMENTS", 7, True)
    doc.right(W - 36, 754, "2025 Form 5498", 11, True)
    doc.right(W - 36, 742, "IRA Contribution Information", 8.5)
    doc.right(W - 36, 731, "Copy B for Participant  OMB No. 1545-0747", 7)
    doc.rule(722, width=1)
    doc.text(36, 704, "TRUSTEE'S or ISSUER'S name, address and telephone", 6.5, True)
    for i, line in enumerate(("FIDELITY MANAGEMENT TRUST COMPANY", "900 SALEM STREET", "SMITHFIELD, RI 02917",
                              "800-544-6666")):
        doc.text(36, 693 - 10 * i, line, 7.5)
    doc.text(36, 640, "TRUSTEE'S or ISSUER'S TIN: 04-3022712", 7)
    doc.text(36, 628, "PARTICIPANT'S TIN: ***-**-6789", 7)
    doc.text(36, 610, "PARTICIPANT'S name and address", 6.5, True)
    for i, line in enumerate((PERSON_US, "AV ALVARO OBREGON 185 INT 4", "COL ROMA NORTE", "CIUDAD DE MEXICO 06700 MX")):
        doc.text(36, 599 - 10 * i, line, 7.5)
    doc.text(36, 548, "Account Number: 237-481926", 7.5, True)
    doc.text(36, 536, "Account Type: ROTH IRA", 7.5)
    doc.text(36, 524, "Date Prepared: 05/15/2026", 7.5)

    boxes = [("1", "IRA contributions (other than amounts in boxes 2-4, 8-10, 13a, 14a and 15a)", "0.00"),
             ("2", "Rollover contributions", "0.00"), ("3", "Roth IRA conversion amount", "0.00"),
             ("4", "Recharacterized contributions", "0.00"), ("5", "Fair market value of account", "48,215.62"),
             ("6", "Life insurance cost included in box 1", "0.00"), ("8", "SEP contributions", "0.00"),
             ("9", "SIMPLE contributions", "0.00"), ("10", "Roth IRA contributions", "7,000.00"),
             ("12b", "RMD amount", "0.00"), ("13a", "Postponed/late contrib.", "0.00"), ("14a", "Repayments", "0.00")]
    y = 704
    for box, label, value in boxes:
        doc.box(300, y - 5, W - 336, 18)
        doc.text(304, y, f"{box}  {label}", 6.8)
        doc.right(W - 42, y, f"${value}", 7.5)
        y -= 20
    doc.text(304, y, "7  IRA [ ]   SEP [ ]   SIMPLE [ ]   ROTH IRA [X]", 7)
    doc.text(304, y - 12, "11  Check if RMD for 2026 [ ]", 7)
    y -= 50
    doc.shade(36, y - 3, W - 72, 14, 0.9)
    doc.text(40, y + 1, "Additional information", 8.5, True)
    y -= 16
    doc.fine(40, y, [
        "Your 2025 Roth IRA contributions include $3,500.00 made from 01/01/2026 through 04/15/2026 for tax year 2025.",
        "Box 5 shows the fair market value of your Roth IRA on December 31, 2025.",
        "This information is being furnished to the IRS. Do not attach Form 5498 to your tax return; keep it for your records.",
        "Contributions to a Roth IRA may be limited by your modified adjusted gross income and by earned income. If you exclude",
        "foreign earned income (Form 2555), that income does not count as compensation for IRA contribution purposes.",
    ], 6.5, 9)
    doc.rule(50)
    doc.text(36, 40, "Fidelity Brokerage Services LLC, Member NYSE, SIPC. 900 Salem Street, Smithfield, RI 02917.", 5.5)
    doc.right(W - 36, 40, "Page 1 of 1", 7)
    doc.save()
    return {"institution": "Fidelity", "tax_year": 2025, "account_last4": "1926", "roth_contributions": "7000.00",
            "fair_market_value": "48215.62", "ira_contributions": "0.00", "late_contribution_note": "3500.00"}


# ============================================================================ IBKR activity statement CSV

def ibkr_csv(path: Path) -> dict:
    positions = [("Stocks", "USD", "VT", "120", "1", "104.2050", "12504.60", "118.45", "14214.00", "1709.40"),
                 ("Stocks", "USD", "SGOV", "200", "1", "100.3100", "20062.00", "100.52", "20104.00", "42.00"),
                 ("Stocks", "USD", "AMZN", "15", "1", "188.4000", "2826.00", "232.10", "3481.50", "655.50")]
    stocks = sum(D(p[8]) for p in positions)
    cash_usd = D("2318.44")
    cash_mxn = D("25000.00")
    usd_mxn = D("0.053694")  # IBKR prints MXN in base units per MXN
    cash_mxn_usd = (cash_mxn * usd_mxn).quantize(Decimal("0.01"))
    cash = cash_usd + cash_mxn_usd
    total = stocks + cash
    lines = [
        "﻿Statement,Header,Field Name,Field Value",
        "Statement,Data,BrokerName,Interactive Brokers LLC",
        "Statement,Data,BrokerAddress,\"Two Pickwick Plaza, Greenwich, CT 06830\"",
        "Statement,Data,Title,Activity Statement",
        "Statement,Data,Period,\"August 1, 2026 - August 31, 2026\"",
        "Statement,Data,WhenGenerated,\"2026-09-02, 04:17:09 EDT\"",
        "Account Information,Header,Field Name,Field Value",
        f"Account Information,Data,Name,{PERSON_US.title()}",
        "Account Information,Data,Account,U8812345",
        "Account Information,Data,Account Type,Individual",
        "Account Information,Data,Customer Type,Individual",
        "Account Information,Data,Account Capabilities,Cash",
        "Account Information,Data,Base Currency,USD",
        "Net Asset Value,Header,Asset Class,Prior Total,Current Long,Current Short,Current Total,Change",
        f"Net Asset Value,Data,Cash ,1905.12,{cash},0,{cash},{cash - D('1905.12')}",
        f"Net Asset Value,Data,Stock,37011.20,{stocks},0,{stocks},{stocks - D('37011.20')}",
        "Net Asset Value,Data,Interest Accruals,4.10,5.02,0,5.02,0.92",
        f"Net Asset Value,Data,Total,38920.42,{total + D('5.02')},0,{total + D('5.02')},{total + D('5.02') - D('38920.42')}",
        "Net Asset Value,Header,Time Weighted Rate of Return",
        "Net Asset Value,Data,1.37%",
        "Change in NAV,Header,Field Name,Field Value",
        "Change in NAV,Data,Starting Value,38920.42",
        "Change in NAV,Data,Mark-to-Market,512.66",
        "Change in NAV,Data,Deposits & Withdrawals,1500",
        "Change in NAV,Data,Dividends,31.20",
        "Change in NAV,Data,Withholding Tax,-3.12",
        "Change in NAV,Data,Interest,7.05",
        "Change in NAV,Data,Commissions,-1.00",
        "Change in NAV,Data,Other FX Translations,-164.77",
        f"Change in NAV,Data,Ending Value,{total + D('5.02')}",
        "Cash Report,Header,Currency Summary,Currency,Total,Securities,Futures,Month to Date,Year to Date",
        f"Cash Report,Data,Starting Cash,Base Currency Summary,1905.12,1905.12,0,,",
        f"Cash Report,Data,Ending Cash,Base Currency Summary,{cash},{cash},0,,",
        f"Cash Report,Data,Ending Cash,MXN,{cash_mxn},{cash_mxn},0,,",
        f"Cash Report,Data,Ending Cash,USD,{cash_usd},{cash_usd},0,,",
        "Open Positions,Header,DataDiscriminator,Asset Category,Currency,Symbol,Quantity,Mult,Cost Price,Cost Basis,"
        "Close Price,Value,Unrealized P/L,Code",
        *[f"Open Positions,Data,Summary,{','.join(p)}," for p in positions],
        f"Open Positions,Total,,Stocks,USD,,,,,{sum(D(p[6]) for p in positions)},,{stocks},"
        f"{sum(D(p[9]) for p in positions)},",
        f"Open Positions,Total,,Total,,,,,,,,{stocks},,",
        "Forex Balances,Header,Asset Category,Currency,Description,Quantity,Cost Price,Cost Basis in USD,Close Price,"
        "Value in USD,Unrealized P/L in USD,Code",
        f"Forex Balances,Data,Forex,USD,MXN,{cash_mxn},0.054120,-1353.00,{usd_mxn},{cash_mxn_usd},"
        f"{cash_mxn_usd - D('1353.00')},",
        "Trades,Header,DataDiscriminator,Asset Category,Currency,Symbol,Date/Time,Quantity,T. Price,C. Price,"
        "Proceeds,Comm/Fee,Basis,Realized P/L,MTM P/L,Code",
        "Trades,Data,Order,Stocks,USD,VT,\"2026-08-06, 09:41:22\",10,116.80,118.45,-1168,-1,1169,0,16.50,O",
        "Trades,SubTotal,,Stocks,USD,VT,,10,,,-1168,-1,1169,0,16.50,",
        "Trades,Total,,Stocks,USD,,,,,,-1168,-1,1169,0,16.50,",
        "Deposits & Withdrawals,Header,Currency,Settle Date,Description,Amount",
        "Deposits & Withdrawals,Data,USD,2026-08-04,Electronic Fund Transfer,1500",
        "Deposits & Withdrawals,Data,Total,,,1500",
        "Dividends,Header,Currency,Date,Description,Amount",
        "Dividends,Data,USD,2026-08-07,SGOV(US46436E7186) Cash Dividend USD 0.156 per Share (Ordinary Dividend),31.20",
        "Dividends,Data,Total,,,31.20",
        "Withholding Tax,Header,Currency,Date,Description,Amount,Code",
        "Withholding Tax,Data,USD,2026-08-07,SGOV(US46436E7186) Cash Dividend USD 0.156 per Share - US Tax,-3.12,",
        "Withholding Tax,Data,Total,,,-3.12,",
        "Interest,Header,Currency,Date,Description,Amount",
        "Interest,Data,USD,2026-08-05,USD Credit Interest for Jul-2026,7.05",
        "Interest,Data,Total,,,7.05",
        "Financial Instrument Information,Header,Asset Category,Symbol,Description,Conid,Security ID,Listing Exch,"
        "Multiplier,Type,Code",
        "Financial Instrument Information,Data,Stocks,AMZN,AMAZON.COM INC,3691937,US0231351067,NASDAQ,1,COMMON,",
        "Financial Instrument Information,Data,Stocks,SGOV,ISHARES 0-3 MONTH TREASURY BOND ETF,475245066,US46436E7186,"
        "ARCA,1,ETF,",
        "Financial Instrument Information,Data,Stocks,VT,VANGUARD TOT WORLD STK ETF,52197301,US9220427424,ARCA,1,ETF,",
        "Codes,Header,Code,Meaning",
        "Codes,Data,O,Opening Trade",
        "Notes/Legal Notes,Header,Type,Note",
        "Notes/Legal Notes,Data,Notes,\"Cash dividends are presented net of any applicable fees. Withholding tax on "
        "dividends paid to U.S. persons is not withheld unless backup withholding applies.\"",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"institution": "Interactive Brokers", "account_last4": "2345", "currency": "USD",
            "stocks": str(stocks), "cash_usd": str(cash_usd), "cash_mxn": str(cash_mxn), "cash_total_usd": str(cash),
            "nav_excluding_accruals": str(total), "nav": str(total + D("5.02")),
            "positions": {p[2]: {"quantity": p[3], "value": p[8], "cost_basis": p[6]} for p in positions},
            "dividends": "31.20", "withholding": "-3.12", "interest": "7.05", "deposit": "1500"}


BUILDERS = {
    "gbm_estado_de_cuenta_2026-08.pdf": gbm_statement,
    "gbm_estado_de_cuenta_2026-08_split.pdf": lambda path: gbm_statement(path, split_sic=True),
    "bbva_estado_de_cuenta_2026-08.pdf": bbva_statement,
    "banorte_tdc_2026-08.pdf": banorte_card,
    "gbm_constancia_2025.pdf": gbm_constancia,
    "schwab_1099_2025.pdf": schwab_1099,
    "fidelity_5498_2025.pdf": fidelity_5498,
    "ibkr_activity_2026-08.csv": ibkr_csv,
}


def build(directory: Path = HERE) -> dict:
    truth = {name: builder(directory / name) for name, builder in BUILDERS.items()}
    (directory / "ground_truth.json").write_text(json.dumps(truth, indent=2, ensure_ascii=False) + "\n",
                                                 encoding="utf-8")
    return truth


if __name__ == "__main__":
    for name, facts in build().items():
        print(name, "->", ", ".join(f"{k}={v}" for k, v in list(facts.items())[:6]))
