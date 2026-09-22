"""Guarded trade execution: the model proposes an order ticket; the person places it in the app.

* :mod:`.tickets` — tickets, pre-trade checks, confirmation (the only submit
  path, reached solely from the local web route), status, fills and cancel.
* :mod:`.brokers.base` — the ``OrderBroker`` interface and which adapter an
  account's institution uses.
* :mod:`.brokers.alpaca_orders` — the Alpaca Trading API v2 client (paper by
  default) with its sources.
* :mod:`.brokers.ibkr_gateway` — Interactive Brokers through the Client Portal
  gateway the person runs (paper when logged into a ``DU`` account).
* :mod:`.brokers.manual` — place-it-yourself tickets for brokers without an API.

See ``docs/trading.md`` for the threat model and setup.
"""

from .tickets import ConfirmError, create_ticket, execution_status, list_tickets, ticket_status, trading_mode

__all__ = ["ConfirmError", "create_ticket", "execution_status", "list_tickets", "ticket_status", "trading_mode"]
