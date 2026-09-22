"""The order-broker interface every adapter implements, and how an account picks its adapter.

:mod:`wealth.execution.tickets` talks to brokers only through :class:`OrderBroker`,
in Wealth's own shapes (never a broker's raw JSON), so adding a broker means one
adapter module and one entry in :func:`kind_for_institution`:

* ``alpaca`` — :class:`wealth.execution.brokers.alpaca_orders.AlpacaBroker`
  (Trading API v2; paper by default).
* ``ibkr`` — :class:`wealth.execution.brokers.ibkr_gateway.IbkrBroker`
  (the Client Portal Web API through the gateway the person runs on this computer).
* ``manual`` — :class:`wealth.execution.brokers.manual.ManualBroker`: brokers
  without an order API (GBM, Vest, Schwab without OAuth, Fidelity, ...).  It
  prices and checks the ticket from Wealth's own data and never sends
  anything; the person places the order and taps "Ya la puse".

Shapes (every number is a decimal string):

``account_state()``
    ``{active, buying_power, currency, number, ledger_account_id}``; ``number``
    is the broker's account id, used only to address requests and never shown
    or audited unmasked.
``market_clock()``
    ``{is_open: bool | None, next_open}``.
``instrument(symbol)``
    ``{symbol, tradable, fractionable, exchange, currency}`` or ``None`` when
    the broker does not know it.
``latest_trade(symbol)``
    ``{price, at}`` (``at`` RFC 3339) or ``None``.
``positions()``
    ``[{symbol, qty, market_value}]``.
``open_orders()``
    ``[{symbol, side, client_order_id}]``.
``submit(order)`` / ``order(id)`` / ``order_by_client_id(cid)``
    ``{id, status, state, filled_qty, filled_avg_price, submitted_at}`` where
    ``status`` is the broker's word and ``state`` Wealth's line state
    (:data:`LINE_STATES`).
``fills(after)``
    ``[{external_id, order_id, client_order_id, symbol, side, qty, price, date}]``.
``ledger_account()``
    the ledger account row fills post to.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

KINDS = ("alpaca", "ibkr", "manual")
# Wealth's line states; "awaiting" is a manual order the person says they placed, until a statement shows it.
LINE_STATES = ("proposed", "sent", "partial", "filled", "canceled", "expired", "rejected", "failed", "unknown",
               "awaiting", "unconfirmed")


class BrokerError(Exception):
    """A broker failure; the message never contains a credential."""

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False,
                 body: Any = None, kind: str | None = None):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.body = body
        self.kind = kind


@runtime_checkable
class OrderBroker(Protocol):
    """One broker account, in one mode, as the ticket code sees it."""

    name: str        # "alpaca" | "ibkr" | "manual"
    label: str       # what the card calls the broker: "Alpaca", "Interactive Brokers", "GBM"
    mode: str        # "paper" | "live" | "manual"
    submits: bool    # False: Wealth never sends this broker anything (a "place it yourself" card)
    posts_fills: bool  # whether refresh posts this broker's fills to the ledger itself
    audit: Callable[[str, dict[str, Any]], None] | None

    def account_state(self) -> dict[str, Any]: ...
    def market_clock(self) -> dict[str, Any]: ...
    def instrument(self, symbol: str) -> dict[str, Any] | None: ...
    def latest_trade(self, symbol: str) -> dict[str, Any] | None: ...
    def positions(self) -> list[dict[str, Any]]: ...
    def open_orders(self) -> list[dict[str, Any]]: ...
    def submit(self, order: Mapping[str, Any]) -> dict[str, Any]: ...
    def order(self, order_id: str) -> dict[str, Any]: ...
    def order_by_client_id(self, client_order_id: str) -> dict[str, Any] | None: ...
    def cancel(self, order_id: str) -> None: ...
    def fills(self, after: str | None = None) -> list[dict[str, Any]]: ...
    def ledger_account(self) -> dict[str, Any]: ...


# -- which adapter an account uses ------------------------------------------------------

_IBKR = re.compile(r"\b(ibkr|interactive\s*brokers?|ib\s*gateway)\b")
_ALPACA = re.compile(r"\balpaca\b")
# Institutions whose orders trade on the Mexican exchanges (BMV, BIVA and the SIC for foreign listings), in MXN.
_MX_BROKERS = {
    "gbm": "GBM", "gbm+": "GBM", "gbm homebroker": "GBM", "bursanet": "Bursanet", "actinver": "Actinver",
    "kuspit": "Kuspit", "monex": "Monex", "banorte": "Banorte Casa de Bolsa", "santander": "Santander Casa de Bolsa",
    "bbva": "BBVA Casa de Bolsa", "vector": "Vector", "valmex": "Valmex", "finamex": "Finamex", "cibanco": "CIBanco",
    "intercam": "Intercam", "invex": "Invex", "scotia": "Scotia Casa de Bolsa", "hey banco": "Hey Banco",
    "flink": "Flink",
}
_US_BROKERS = {"vest": "Vest", "schwab": "Charles Schwab", "charles schwab": "Charles Schwab", "fidelity": "Fidelity",
               "vanguard": "Vanguard", "robinhood": "Robinhood", "e*trade": "E*TRADE", "etrade": "E*TRADE",
               "td ameritrade": "TD Ameritrade", "merrill": "Merrill", "firstrade": "Firstrade", "webull": "Webull",
               "hapi": "Hapi", "public": "Public"}


def _fold(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def kind_for_institution(institution: Any) -> str:
    """``alpaca``, ``ibkr`` or ``manual`` for an account's institution; anything unknown is manual."""
    text = _fold(institution)
    if _ALPACA.search(text):
        return "alpaca"
    if _IBKR.search(text):
        return "ibkr"
    return "manual"


def institution_profile(institution: Any, country: Any = None) -> dict[str, Any]:
    """``{label, market: MX | US, currency, known}`` for a manual broker (an unknown name is taken at its word).

    The market decides the listing: an MX broker buys foreign shares through the SIC and Mexican ones on the BMV,
    in pesos; a US broker trades US listings in dollars.
    """
    text = re.sub(r"[^a-z0-9+*&]+", " ", _fold(institution)).strip()  # "gbm-4321" reads as "gbm 4321"
    for key, label in _MX_BROKERS.items():
        if text == key or text.startswith(key + " ") or f" {key} " in f" {text} ":
            return {"label": label, "market": "MX", "currency": "MXN", "known": True}
    for key, label in _US_BROKERS.items():
        if text == key or text.startswith(key + " ") or f" {key} " in f" {text} ":
            return {"label": label, "market": "US", "currency": "USD", "known": True}
    mexican = str(country or "").upper() == "MX" or "casa de bolsa" in text
    label = str(institution or "").strip()[:40] or "your broker"
    return {"label": label, "market": "MX" if mexican else "US", "currency": "MXN" if mexican else "USD",
            "known": False}


def mask(number: Any) -> str:
    """An account number as the card and the audit may show it: its last four characters."""
    text = re.sub(r"[^A-Za-z0-9]", "", str(number or ""))
    return f"****{text[-4:]}" if len(text) >= 4 else "****"


__all__ = ["BrokerError", "KINDS", "LINE_STATES", "OrderBroker", "institution_profile", "kind_for_institution", "mask"]
