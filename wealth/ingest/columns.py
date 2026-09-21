"""Configurable column-header aliases shared by PDF tables and CSV/XLSX exports.

Headers are matched after :func:`~wealth.ingest.common.fold` (lowercase, no
accents, punctuation collapsed) and after removing a currency marker, which is
kept as a column currency hint.  Matching is exact, so ``Price Change`` never
maps to ``price``.
"""

from __future__ import annotations

import re
from typing import Iterable, Mapping

from .common import fold


HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "symbol": ("symbol", "ticker", "symbol cusip", "emisora", "clave", "clave de pizarra", "simbolo", "instrumento",
               "emisora serie", "emisora y serie", "valor emisora", "security id", "financial instrument"),
    "serie": ("serie",),
    "description": ("description", "security description", "security", "name", "investment name", "security name",
                    "investment", "descripcion", "nombre", "nombre del valor", "holding", "asset", "activo",
                    "descripcion del valor", "fondo"),
    "quantity": ("quantity", "qty", "qty quantity", "shares", "units", "position", "titulos", "titulos disponibles",
                 "cantidad", "acciones", "no de titulos", "num titulos", "numero de titulos", "shares quantity",
                 "share quantity", "titulos a valor de mercado", "posicion"),
    "price": ("price", "last price", "share price", "close price", "closing price", "market price", "current price",
              "mark price", "markprice", "price per share", "precio", "precio de mercado", "precio mercado",
              "precio actual", "precio de cierre", "precio unitario", "precio de valuacion", "precio valuacion",
              "precio al cierre", "unit price", "nav"),
    "value": ("market value", "mkt val market value", "mkt val", "current value", "total value", "value",
              "position value", "positionvalue", "ending value", "ending market value", "valor de mercado",
              "valor mercado", "importe", "valuacion", "valor", "valor actual", "saldo", "monto", "valor total",
              "importe de mercado", "valor a mercado", "valor de la posicion", "valuacion a mercado", "balance",
              "market value total"),
    "cost_basis": ("cost basis", "cost basis total", "total cost", "total cost basis", "costbasismoney",
                   "costo total", "costo de adquisicion", "importe de compra", "costo", "cost", "book value",
                   "costo de compra", "inversion inicial"),
    "avg_cost": ("average cost", "avg cost", "average cost basis", "cost price", "unit cost", "costo promedio",
                 "precio promedio", "precio de compra promedio", "precio de compra", "costo unitario",
                 "costo promedio unitario", "average price", "avg price", "costbasisprice", "precio promedio de compra"),
    "gain": ("unrealized gain loss", "gain loss", "gain loss dollar", "unrealized p l", "unrealized pl",
             "total gain loss dollar", "plusvalia minusvalia", "plusvalia", "minusvalia", "utilidad perdida",
             "ganancia perdida", "plus minusvalia", "fifopnlunrealized", "plusvalia o minusvalia"),
    "currency": ("currency", "ccy", "moneda", "divisa", "currencyprimary"),
    "account": ("account number", "account", "account name number", "cuenta", "contrato", "clientaccountid",
                "account id", "numero de cuenta", "no de contrato", "numero de contrato"),
    "account_name": ("account name", "nombre de la cuenta", "account description", "registration"),
    "asset_type": ("security type", "asset class", "asset category", "assetclass", "tipo de valor",
                   "clase de activo", "tipo de instrumento", "security class"),
    "acquired_on": ("date acquired", "acquired", "open date", "acquisition date", "fecha de compra",
                    "fecha de adquisicion", "opendatetime"),
    "maturity": ("maturity", "maturity date", "vencimiento", "fecha de vencimiento"),
    "rate": ("rate", "coupon", "tasa", "tasa de rendimiento", "yield", "tasa bruta"),
}
_MARKER = re.compile(r"\b(usd|mxn|mn|m n|dls|dlls|pesos|dolares|udis?)\b|\$")
_CCY_OF = {"usd": "USD", "dls": "USD", "dlls": "USD", "dolares": "USD", "mxn": "MXN", "mn": "MXN", "m n": "MXN",
           "pesos": "MXN", "udi": "UDI", "udis": "UDI"}


def build_alias_index(extra: Mapping[str, Iterable[str]] | None = None) -> dict[str, str]:
    """Map folded alias -> canonical field, optionally extended by the caller."""
    index: dict[str, str] = {}
    for field, aliases in HEADER_ALIASES.items():
        for alias in aliases:
            index[fold(alias)] = field
    for field, aliases in (extra or {}).items():
        if field not in HEADER_ALIASES:
            raise ValueError(f"unknown column field for alias: {field}")
        for alias in aliases:
            index[fold(alias)] = field
    return index


def match_header(cell: str, index: Mapping[str, str]) -> tuple[str | None, str | None]:
    """Return (canonical field, currency hint) for a header cell."""
    folded = fold(cell).replace("%", "").strip()
    if not folded:
        return None, None
    if folded in index:
        return index[folded], None
    marker = _MARKER.search(folded)
    if marker:
        stripped = " ".join(_MARKER.sub(" ", folded).split())
        if stripped in index:
            return index[stripped], _CCY_OF.get(marker.group(1) or "")
    return None, None
