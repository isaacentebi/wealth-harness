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
# "partial_unconfirmed" is a manual order only partly shown by statements after the confirmation window.
LINE_STATES = ("proposed", "sent", "partial", "filled", "canceled", "expired", "rejected", "failed", "unknown",
               "awaiting", "unconfirmed", "partial_unconfirmed")


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

# The canonical alias table: an institution routes to an API broker only when its whole (normalised) name is one
# of these aliases.  Never a substring or a word inside a longer name: "GBM (not Interactive Brokers)" is not IBKR.
_API_ALIASES: dict[str, str] = {
    "alpaca": "alpaca", "alpaca securities": "alpaca", "alpaca markets": "alpaca", "alpaca securities llc": "alpaca",
    "alpaca markets inc": "alpaca", "alpaca paper": "alpaca", "alpaca paper trading": "alpaca",
    "ibkr": "ibkr", "interactive brokers": "ibkr", "interactive broker": "ibkr", "interactive brokers llc": "ibkr",
    "interactive brokers ireland": "ibkr", "interactive brokers uk": "ibkr", "interactive brokers u k": "ibkr",
    "interactive brokers canada": "ibkr", "interactive brokers central europe": "ibkr",
    "interactive brokers ibkr": "ibkr", "ibkr interactive brokers": "ibkr", "ib gateway": "ibkr",
    "ibkr lite": "ibkr", "ibkr pro": "ibkr", "interactivebrokers": "ibkr",
}
# Institutions whose orders trade on the Mexican exchanges (BMV, BIVA and the SIC for foreign listings), in MXN.
_MX_BROKERS = {
    "gbm": "GBM", "gbm+": "GBM", "gbm homebroker": "GBM", "gbm plus": "GBM", "gbm trading usa": "GBM",
    "grupo bursatil mexicano": "GBM", "bursanet": "Bursanet", "actinver": "Actinver",
    "kuspit": "Kuspit", "monex": "Monex", "banorte": "Banorte Casa de Bolsa", "santander": "Santander Casa de Bolsa",
    "bbva": "BBVA Casa de Bolsa", "vector": "Vector", "valmex": "Valmex", "finamex": "Finamex", "cibanco": "CIBanco",
    "intercam": "Intercam", "invex": "Invex", "scotia": "Scotia Casa de Bolsa", "hey banco": "Hey Banco",
    "flink": "Flink",
}
_US_BROKERS = {"vest": "Vest", "schwab": "Charles Schwab", "charles schwab": "Charles Schwab", "fidelity": "Fidelity",
               "vanguard": "Vanguard", "robinhood": "Robinhood", "e*trade": "E*TRADE", "etrade": "E*TRADE",
               "td ameritrade": "TD Ameritrade", "merrill": "Merrill", "firstrade": "Firstrade", "webull": "Webull",
               "hapi": "Hapi", "public": "Public"}
_API_LABELS = {"alpaca": "Alpaca", "ibkr": "Interactive Brokers"}


def _fold(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def _canonical(text: Any) -> str:
    """Lowercase, punctuation to spaces (keeping + * &), whitespace collapsed: "Interactive Brokers, LLC." ->
    "interactive brokers llc"."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9+*&]+", " ", _fold(text))).strip()


def kind_for_institution(institution: Any) -> str:
    """``alpaca``, ``ibkr`` or ``manual`` for an account's institution; anything unknown is manual.

    Only an exact alias from the canonical table picks an API broker; anything else (including a longer name that
    merely contains one) is manual, which never sends anything.
    """
    return _API_ALIASES.get(_canonical(institution), "manual")


def kind_for_account_id(account_id: Any) -> str | None:
    """``alpaca`` / ``ibkr`` for an id in the form Wealth itself gives those accounts (``ibkr-4567``, ``alpaca-1234``).

    Such an id is only a hint: :mod:`wealth.execution.tickets` still requires the broker's own account state to
    name the same ledger account before anything is sent.
    """
    match = re.fullmatch(r"(ibkr|alpaca)-[a-z0-9]{2,12}", str(account_id or "").strip().lower())
    return match.group(1) if match else None


def mentioned_institutions(text: Any) -> dict[str, str]:
    """``{label: kind}`` for every institution in the alias tables named as whole words in free text.

    Used when an order names no account: "compra 10 VOO en GBM" names GBM.  Aliases are matched as whole
    tokens only, longest first, so "GBM" in "GBMX" is not a mention.
    """
    folded = f" {_canonical(text)} "
    found: dict[str, str] = {}
    tables = [(alias, _API_LABELS[kind], kind) for alias, kind in _API_ALIASES.items()]
    tables += [(alias, label, "manual") for alias, label in {**_MX_BROKERS, **_US_BROKERS}.items()]
    for alias, label, kind in sorted(tables, key=lambda row: -len(row[0])):
        if alias in ("public", "vector"):
            continue  # ordinary words too; they must be named as an account's institution, not in passing
        if f" {alias} " in folded:
            found.setdefault(label, kind)
            folded = folded.replace(f" {alias} ", " ")
    return found


def institution_label(institution: Any) -> tuple[str, str] | None:
    """``(label, kind)`` when the institution is exactly one of the table's aliases, else ``None``."""
    text = _canonical(institution)
    if text in _API_ALIASES:
        kind = _API_ALIASES[text]
        return _API_LABELS[kind], kind
    for table in (_MX_BROKERS, _US_BROKERS):
        if text in table:
            return table[text], "manual"
    return None


def institution_profile(institution: Any, country: Any = None) -> dict[str, Any]:
    """``{label, market: MX | US, currency, known}`` for a manual broker (an unknown name is taken at its word).

    The market decides the listing: an MX broker buys foreign shares through the SIC and Mexican ones on the BMV,
    in pesos; a US broker trades US listings in dollars.
    """
    text = _canonical(institution)  # "gbm-4321" reads as "gbm 4321"
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


__all__ = ["BrokerError", "KINDS", "LINE_STATES", "OrderBroker", "institution_label", "institution_profile",
           "kind_for_account_id", "kind_for_institution", "mask", "mentioned_institutions"]
