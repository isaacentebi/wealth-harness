"""Synthetic annual tax documents (fpdf2): fictional people, generic RFC and masked numbers only.

Figures match the ``tax_pack`` catalog examples, so the pack's reconciliation
against the ledger can be checked end to end.
"""

from __future__ import annotations

from .statements import render, table


def _line(label: str, amount: str, indent: int = 6) -> tuple:
    return ("row", [("", indent, "L"), (label, 124, "L"), (amount, 40, "R")])


def gbm_constancia(net: str = "2,587.00") -> bytes:
    return render([
        ("text", "GBM Grupo Bursatil Mexicano, S.A. de C.V., Casa de Bolsa"),
        ("text", "Constancia Fiscal Anual de Retenciones e Información de Operaciones"),
        ("text", "Ejercicio fiscal: 2025"),
        ("text", "Fecha de emisión: 13/02/2026"),
        ("text", "Contribuyente: ANA EJEMPLO FICTICIA        RFC: XAXX010101000"),
        ("text", "Contrato: 00012345678"),
        ("text", "Moneda: Pesos mexicanos (MXN)"),
        ("blank",),
        ("text", "Enajenación de acciones (Art. 129 LISR)"),
        _line("Ganancia en enajenación de acciones", "5,417.00"),
        _line("Pérdida en enajenación de acciones", "2,830.00"),
        _line("Resultado neto (ganancia - pérdida)", net),
        ("blank",),
        ("text", "Intereses (Arts. 133 a 136 LISR)"),
        _line("Intereses nominales", "4,500.00"),
        _line("Ajuste anual por inflación", "2,100.00"),
        _line("Intereses reales", "2,400.00"),
        _line("Pérdida real", "0.00"),
        _line("ISR retenido", "400.00"),
        ("blank",),
        ("text", "Dividendos (Art. 140 LISR)"),
        _line("Dividendos de personas morales residentes en México", "1,200.00"),
        _line("Dividendos de fuente extranjera", "0.00"),
        _line("ISR retenido (10% adicional)", "120.00"),
        _line("Total de dividendos", "1,200.00"),
        ("blank",),
        ("text", "Este documento es un ejemplo ficticio generado para pruebas."),
    ])


def bbva_interest_constancia() -> bytes:
    return render([
        ("text", "BBVA México, S.A., Institución de Banca Múltiple"),
        ("text", "Constancia Anual de Intereses y Retenciones"),
        ("text", "Ejercicio 2025"),
        ("text", "Fecha de expedición: 10/02/2026"),
        ("text", "Titular: ANA EJEMPLO FICTICIA     RFC: XAXX010101000"),
        ("text", "Cuenta: ****4821"),
        ("text", "Periodo: 01/01/2025 al 31/12/2025"),
        ("blank",),
        ("text", "Intereses"),
        _line("Saldo promedio diario", "80,004.11"),
        _line("Intereses nominales pagados", "1,800.00"),
        _line("Ajuste anual por inflación", "2,718.20"),
        _line("Intereses reales", "0.00"),
        _line("Pérdida real", "918.20"),
        _line("ISR retenido", "300.00"),
        ("blank",),
        ("text", "Documento ficticio para pruebas."),
    ])


LOT_COLUMNS = [("Description", 46, "L"), ("Symbol", 12, "L"), ("Quantity", 14, "R"), ("Acquired", 18, "R"),
               ("Sold", 18, "R"), ("Proceeds", 20, "R"), ("Cost Basis", 20, "R"), ("Wash Sale", 16, "R"),
               ("Gain/(Loss)", 20, "R")]


def schwab_1099(short_proceeds: str = "12,100.00") -> bytes:
    return render([
        ("text", "Charles Schwab & Co., Inc."),
        ("text", "2025 Form 1099 Composite and Year-End Summary"),
        ("text", "Tax Year 2025"),
        ("text", "Date Prepared: February 13, 2026"),
        ("text", "Recipient: SAM EXAMPLE (fictional)      Recipient's TIN: ***-**-6789"),
        ("text", "Account Number: XXXX-5678"),
        ("blank",),
        ("text", "Form 1099-DIV  Dividends and Distributions"),
        _line("1a Total ordinary dividends", "105.00"),
        _line("1b Qualified dividends", "90.00"),
        _line("2a Total capital gain distributions", "0.00"),
        _line("4 Federal income tax withheld", "0.00"),
        _line("7 Foreign tax paid", "4.00"),
        ("blank",),
        ("text", "Form 1099-INT  Interest Income"),
        _line("1 Interest income", "40.00"),
        _line("3 Interest on U.S. Savings Bonds and Treasury obligations", "0.00"),
        _line("4 Federal income tax withheld", "0.00"),
        _line("6 Foreign tax paid", "0.00"),
        ("page",),
        ("text", "Form 1099-B  Proceeds From Broker and Barter Exchange Transactions"),
        ("text", "Short-term transactions for covered tax lots (Box A)"),
        *table(LOT_COLUMNS, [
            ["VANGUARD TOTAL STOCK", "VTI", "20.000", "01/15/2025", "03/10/2025", "5,600.00", "6,000.00",
             "400.00", "0.00"],
            ["VANGUARD TOTAL STOCK", "VTI", "20.000", "03/25/2025", "11/03/2025", "6,500.00", "6,100.00",
             "", "400.00"],
        ]),
        ("row", [("Total Short-Term", 108, "L"), (short_proceeds, 20, "R"), ("12,100.00", 20, "R"),
                 ("400.00", 16, "R"), ("400.00", 20, "R")]),
        ("blank",),
        ("text", "Long-term transactions for covered tax lots (Box D)"),
        *table(LOT_COLUMNS, [
            ["SCHWAB US DIVIDEND EQ", "SCHD", "50.000", "05/01/2023", "08/01/2025", "4,200.00", "3,500.00",
             "", "700.00"],
        ]),
        ("row", [("Total Long-Term", 108, "L"), ("4,200.00", 20, "R"), ("3,500.00", 20, "R"), ("0.00", 16, "R"),
                 ("700.00", 20, "R")]),
        ("blank",),
        ("row", [("Total 1099-B", 108, "L"), ("16,300.00", 20, "R"), ("15,600.00", 20, "R"), ("400.00", 16, "R"),
                 ("1,100.00", 20, "R")]),
        ("blank",),
        ("text", "This is a fictional document generated for tests."),
    ])


def schwab_5498() -> bytes:
    return render([
        ("text", "Charles Schwab & Co., Inc."),
        ("text", "2025 Form 5498  IRA Contribution Information"),
        ("text", "Tax Year 2025"),
        ("text", "Date Prepared: May 15, 2026"),
        ("text", "Participant: SAM EXAMPLE (fictional)"),
        ("text", "Account Number: XXXX-9012     Account type: Roth IRA"),
        ("blank",),
        _line("1 IRA contributions", "0.00"),
        _line("2 Rollover contributions", "0.00"),
        _line("3 Roth IRA conversion amount", "0.00"),
        _line("4 Recharacterized contributions", "0.00"),
        _line("5 Fair market value of account", "6,420.00"),
        _line("10 Roth IRA contributions", "6,000.00"),
    ])


def actinver_prose_constancia() -> bytes:
    """A constancia whose figures sit in prose, not label lines: needs the host extraction."""
    return render([
        ("text", "Actinver Casa de Bolsa, S.A. de C.V."),
        ("text", "Constancia de Retenciones Anual"),
        ("text", "Ejercicio 2025"),
        ("blank",),
        ("text", "Durante el ejercicio usted obtuvo por la venta de acciones una ganancia de"),
        ("text", "3,210 pesos con 50 centavos y no tuvo pérdidas."),
    ])
