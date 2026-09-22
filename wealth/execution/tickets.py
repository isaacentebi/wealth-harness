"""Order tickets: the model proposes, the person confirms in the app, the broker executes.

Principle, enforced here rather than in prompts:

* :func:`create_ticket` (service task ``order_ticket``) stores the exact orders
  plus their pre-trade checks and returns a ticket id and a short summary.  It
  only ever *reads* from the broker.  Its result never contains the nonce.
* :func:`confirm` is the only function that submits orders.  It is called only
  by the local web route ``POST /api/orders/<ticket_id>/confirm`` (session token
  plus local Host/Origin) and needs the one-time nonce shown on the card (only its
  hash is stored; each time the card is listed a fresh code replaces it) and an
  unexpired ticket; it re-runs every check on fresh broker data first.  It is
  not a :class:`~wealth.service.WealthService` method, not a CLI operation and
  not an MCP tool, so a model — or a "yes" typed in chat — cannot reach it.
* :func:`refresh` reconciles order status and posts fills to the ledger through
  :func:`wealth.ledger.post` with Alpaca fill ids as external ids, the same ids
  the read-only Alpaca connector uses, so a later sync dedupes them.  It reads
  every page of fills, retries a failed read on the next refresh, keeps a
  ticket open until every filled share is posted, and reconciles a ticket left
  "submitting" by a crash, looking each line up by its ``client_order_id``.
* The live daily limit is enforced by a reservation taken in the same write
  transaction that claims the ticket; lines that never reach the broker give
  their share back.
* A last trade older than 15 minutes (market open) or one trading day (closed)
  is not a price: the order is blocked with a plain reason.
* Every request and response is written, redacted, to the append-only
  ``orders`` audit table.

Brokers are reached only through :class:`~wealth.execution.brokers.base.OrderBroker`;
the account on the orders picks Alpaca, IBKR (the local Client Portal gateway)
or a manual, place-it-yourself ticket (:func:`_route`).

Paper trading is the default.  Live trading needs ``WEALTH_TRADING_LIVE=alpaca``
(or ``ibkr``; for IBKR, the gateway logged into a non-``DU`` account) in the
server's environment, a typed confirmation the first time, and stays under
per-order and daily notional limits (USD 1,000 and 5,000 by default;
``WEALTH_TRADING_MAX_ORDER_USD`` and ``WEALTH_TRADING_MAX_DAILY_USD``).
Broker facts are cited in the adapter modules under :mod:`wealth.execution.brokers`.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from ..ingest_posting import EXECUTION_BATCH_PREFIX  # so a first connector sync still posts opening balances
from .brokers import alpaca_orders, ibkr_gateway, manual
from .brokers.base import (BrokerError, OrderBroker, institution_label, kind_for_account_id, kind_for_institution, mask,
                           mentioned_institutions)

BROKER = "alpaca"
TICKET_TTL = timedelta(minutes=10)
SOURCES = ("rebalance", "manager_mirror", "user_request")
MAX_ORDERS = 20
MAX_NONCE_FAILURES = 5
KEEP_TICKETS = 60                     # stored history: the oldest settled (incl. expired) tickets go first
DEFAULT_MAX_ORDER = Decimal("1000")
DEFAULT_MAX_DAILY = Decimal("5000")
DEFAULT_COLLAR = Decimal("0.01")      # a limit may sit at most 1% through the last price
# At confirm, a line whose re-priced amount or quantity moved more than this from what the card showed (or whose
# limit moved more than the collar) is not sent: the card shows the new lines and waits for a fresh tap.
PRICE_MOVE_TOLERANCE = Decimal("0.02")
# A last trade older than this is no price at all (a halted or illiquid symbol): while the market is open,
# 15 minutes; while it is closed, one trading day (weekdays; exchange holidays are not modelled, so the first
# session after a holiday may need a fresh trade first).  WEALTH_TRADING_MAX_TRADE_AGE_MINUTES and
# WEALTH_TRADING_MAX_TRADE_AGE_DAYS change them.
DEFAULT_TRADE_AGE_MINUTES = Decimal(15)
DEFAULT_TRADE_AGE_DAYS = Decimal(1)
# A "submitting" ticket older than this with no live submission in this process was interrupted (a crash or a
# kill mid-submit); refresh() reconciles it with the broker by client_order_id.
SUBMIT_LEASE = timedelta(minutes=2)
_IN_FLIGHT: set[str] = set()  # tickets this process is submitting right now
FAR_FROM_MARKET = Decimal("0.05")     # a passive limit 5% away may never fill: said quietly
LIVE_ENV = "WEALTH_TRADING_LIVE"
TYPED_LIVE = ("LIVE", "EN VIVO")
_EASTERN = ZoneInfo("America/New_York")
_TICKET_ID = re.compile(r"^t[0-9a-f]{16}$")
_ORDER_FIELDS = frozenset({
    "symbol", "instrument_id", "side", "qty", "quantity", "notional", "estimated_amount", "type", "limit_price",
    "time_in_force", "account_id", "account", "estimated_tax", "estimated_cost", "asset_class", "sleeve",
    "domicile", "tags", "reason", "lots", "tax_regime", "exchange",
})
_FINAL = frozenset({"filled", "canceled", "expired", "rejected", "failed", "unconfirmed", "partial_unconfirmed"})
# A place-it-yourself card lives longer (the person switches to their broker's app), and a trade the person marked
# placed waits this long for a statement or sync to show it before it is left "unconfirmed".
MANUAL_TTL = timedelta(minutes=60)
MANUAL_CONFIRM_DAYS = 30
MANUAL_QTY_TOLERANCE = Decimal("0.02")
_LABELS = {"alpaca": "Alpaca", "ibkr": "Interactive Brokers", "manual": "your broker"}


class ConfirmError(Exception):
    """Why a confirmation or cancellation did not go through; ``status`` is the HTTP status."""

    def __init__(self, kind: str, message: str, status: int = 409, ticket: dict | None = None):
        super().__init__(message)
        self.kind, self.status, self.ticket = kind, status, ticket


# -- configuration ---------------------------------------------------------------

def live_enabled(kind: str = BROKER, environ: Mapping[str, str] | None = None) -> bool:
    """Whether the server's environment opts ``kind`` into live trading.

    Alpaca: ``WEALTH_TRADING_LIVE`` names ``alpaca`` (its live keys may exist only for syncing, so ``1`` is not
    enough).  IBKR: it names ``ibkr`` or is ``1``.  Manual brokers are never "live" for Wealth: it sends nothing.
    """
    environ = os.environ if environ is None else environ
    if kind == "ibkr":
        return ibkr_gateway.live_opted_in(environ)
    if kind == "alpaca":
        opted = {part.strip().lower() for part in (environ.get(LIVE_ENV) or "").split(",")}
        return "alpaca" in opted
    return False


def trading_mode(environ: Mapping[str, str] | None = None) -> str:
    """Alpaca's mode: 'live' only when ``WEALTH_TRADING_LIVE`` names it; paper otherwise.

    IBKR's mode is the account the gateway is logged into (``DU...`` is paper); manual tickets are ``manual``.
    """
    return "live" if live_enabled("alpaca", environ) else "paper"


def _env_decimal(environ: Mapping[str, str], name: str, default: Decimal) -> Decimal:
    try:
        value = Decimal(str(environ.get(name) or default))
    except InvalidOperation:
        return default
    return value if value.is_finite() and value > 0 else default


def limits(environ: Mapping[str, str] | None = None) -> dict[str, Decimal]:
    environ = os.environ if environ is None else environ
    return {"max_order": _env_decimal(environ, "WEALTH_TRADING_MAX_ORDER_USD", DEFAULT_MAX_ORDER),
            "max_daily": _env_decimal(environ, "WEALTH_TRADING_MAX_DAILY_USD", DEFAULT_MAX_DAILY),
            "collar": min(_env_decimal(environ, "WEALTH_TRADING_COLLAR", DEFAULT_COLLAR), Decimal("0.1")),
            "trade_age_minutes": _env_decimal(environ, "WEALTH_TRADING_MAX_TRADE_AGE_MINUTES", DEFAULT_TRADE_AGE_MINUTES),
            "trade_age_days": _env_decimal(environ, "WEALTH_TRADING_MAX_TRADE_AGE_DAYS", DEFAULT_TRADE_AGE_DAYS)}


def broker_for(mode: str, environ: Mapping[str, str] | None = None, *, existing: bool = False, kind: str = BROKER,
               route: Mapping[str, Any] | None = None, store: Any = None,
               client_id: str | None = None) -> OrderBroker | None:
    """The :class:`~wealth.execution.brokers.base.OrderBroker` for a ticket; the address follows the kind and mode.

    ``alpaca``: the client for ``mode`` when keys exist.  A live client for new work (pricing or confirming a
    ticket) needs the live opt-in (``WEALTH_TRADING_LIVE``); without it there is none.  ``existing=True`` is only
    for tickets already sent to the live broker: :func:`refresh` and :func:`cancel` may still reach them after the
    opt-in is withdrawn, so their orders can be reconciled, their fills posted, and an open order cancelled.
    Nothing new is ever placed that way.

    ``ibkr``: the local gateway (unresolved; its mode is the logged-in account, checked by the caller).
    ``manual``: a broker without an API, priced from the market-data cache and the ledger.
    """
    environ = os.environ if environ is None else environ
    if kind == "ibkr":
        return ibkr_gateway.broker(environ=environ)
    if kind == "manual":
        route = route or {}
        ledger: dict[str, Any] = {}
        if store is not None and client_id:
            try:
                ledger = store.ledger(client_id)
            except Exception:  # noqa: BLE001 - a missing ledger only leaves cash and holdings unknown
                ledger = {}
        account = next((a for a in ledger.get("accounts") or [] if a.get("id") == route.get("account_id")), None)
        if account is None:
            account = {"id": route.get("account_id"), "currency": route.get("currency"),
                       "country": route.get("country")}
            account = {k: v for k, v in account.items() if v}
        return manual.ManualBroker(institution=route.get("institution"), account=account, ledger=ledger,
                                   db_path=getattr(store, "path", None))
    if mode == "live" and not existing and not live_enabled("alpaca", environ):
        return None
    return alpaca_orders.broker(mode, environ=environ)


def _ticket_broker(ticket: Mapping[str, Any], environ: Mapping[str, str] | None, store: Any, client_id: str,
                   *, existing: bool) -> tuple[OrderBroker | None, str | None]:
    """``(broker, why_not)`` for a stored ticket, resolved to the same account the ticket was priced on."""
    kind = ticket.get("broker") or BROKER
    route = ticket.get("route") or {}
    api = broker_for(ticket["mode"], environ, existing=existing, kind=kind, route=route, store=store,
                     client_id=client_id)
    if kind != "ibkr" or api is None:
        return api, None
    try:
        api.resolve()
    except BrokerError as exc:
        return None, str(exc)
    stored = ticket.get("account_fingerprint")
    if stored:
        if ticket.get("fingerprint_v") == 2:
            key = _fingerprint_key(store, client_id) if store is not None and client_id else None
            current = api.fingerprint(key)
        else:
            current = api.legacy_fingerprint()  # a ticket stored before the keyed fingerprint
        same = hmac.compare_digest(str(current), str(stored))
    else:
        same = True
    if api.mode != ticket["mode"] or not same:
        return None, "account_changed"
    return api, None


def _fingerprint_key(store: Any, client_id: str) -> bytes:
    """The profile's random key for account fingerprints (created once, kept in its database, never shown)."""
    state = store.auxiliary(client_id, "execution")
    key = state.get("fingerprint_key")
    if not isinstance(key, str) or len(key) < 32:
        def create(current: dict) -> dict:
            if isinstance(current.get("fingerprint_key"), str) and len(current["fingerprint_key"]) >= 32:
                return current
            return {**current, "fingerprint_key": secrets.token_hex(32)}
        key = store.update_auxiliary(client_id, "execution", create)["fingerprint_key"]
    return bytes.fromhex(key)


def _label(kind: str, broker: Any = None, ticket: Mapping[str, Any] | None = None) -> str:
    if broker is not None and getattr(broker, "label", None):
        return broker.label
    if ticket and ticket.get("broker_label"):
        return ticket["broker_label"]
    return _LABELS.get(kind, kind)


def _unavailable(kind: str, mode: str, reason: str | None = None) -> str:
    if kind == "alpaca":
        keys = "paper" if mode == "paper" else "live"
        return (f"Alpaca {keys} keys are not configured, so prices, buying power and tradability could not be "
                "checked.")
    if kind == "ibkr":
        return (f"The IBKR gateway could not be read ({reason or 'not running'}), so prices, cash and "
                "tradability could not be checked. Start the Client Portal Gateway and log in.")
    return "Prices, cash and holdings could not be read for this account."


# -- small helpers -----------------------------------------------------------------

def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse(moment: str) -> datetime:
    return datetime.fromisoformat(moment.replace("Z", "+00:00"))


def _dec(value: Any, field: str, *, positive: bool = True) -> Decimal:
    if isinstance(value, bool) or value in (None, ""):
        raise ValueError(f"{field} must be a number")
    try:
        number = Decimal(str(value).strip().replace(",", ""))
    except InvalidOperation:
        raise ValueError(f"{field} must be a number") from None
    if not number.is_finite() or (positive and number <= 0):
        raise ValueError(f"{field} must be a positive number")
    return number


def _opt(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() else None


def _s(value: Decimal | None) -> str | None:
    if value is None:
        return None
    text = format(value.normalize(), "f")
    return "0" if text in ("-0", "") else text


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


def _tick(price: Decimal, rounding: str) -> Decimal:
    """Round to Alpaca's price increment: cents at or above $1, hundredths of a cent below."""
    step = Decimal("0.01") if price >= 1 else Decimal("0.0001")
    return price.quantize(step, rounding=rounding)


def _valid_tick(price: Decimal) -> bool:
    return _tick(price, ROUND_DOWN) == price


def _whole(qty: Decimal) -> bool:
    return qty == qty.to_integral_value()


def _check(code: str, status: str, message: str, line: int | None = None, **params: Any) -> dict[str, Any]:
    row: dict[str, Any] = {"code": code, "status": status, "message": message}
    if line is not None:
        row["line"] = line
    if params:
        row["params"] = {k: (_s(v) if isinstance(v, Decimal) else v) for k, v in params.items()}
    return row


def client_order_id(ticket_id: str, index: int) -> str:
    """Deterministic per ticket line, so a retried submission can never create a second order."""
    if not _TICKET_ID.match(ticket_id) or not isinstance(index, int) or index < 0:
        raise ValueError("ticket id or line is malformed")
    return f"wealth-{ticket_id}-{index}"


def _nonce_hash(ticket_id: str, nonce: str) -> str:
    return hashlib.sha256(f"{ticket_id}:{nonce}".encode()).hexdigest()


# -- normalising the model's proposal --------------------------------------------------

def order_symbol(raw: Any) -> str | None:
    """Alpaca's form of a US ticker: ``BRK-B``, ``BRK/B``, ``brk b`` and ``BRK.B`` are all ``BRK.B``.

    Built on :func:`wealth.household.normalize_symbol`, but only separators may
    change: a venue suffix (``VOD.L``, ``WALMEX.MX``) names another listing, so
    it is refused rather than stripped into a different US security.
    """
    from ..household import normalize_symbol

    text = str(raw or "").strip().upper()
    if not text:
        return None
    normal = normalize_symbol(text)
    if not normal:
        return None
    symbol = normal.replace("-", ".")
    if re.sub(r"[^A-Z0-9]", "", symbol) != re.sub(r"[^A-Z0-9]", "", text):
        return None
    return symbol if re.fullmatch(r"[A-Z][A-Z0-9.]{0,9}", symbol) else None


def _manual_symbol(raw: Any, market: str) -> str | None:
    """A symbol for a place-it-yourself card: a US ticker at a US broker; at a Mexican one, a BMV/SIC emisora."""
    if market != "MX":
        return order_symbol(raw)
    symbol = manual.clean_symbol(raw)
    return symbol if re.fullmatch(r"[A-Z][A-Z0-9&.\-]{0,13}", symbol) else None


def _normalize_order(raw: Any, index: int, route: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """One order line; raises ValueError listing every problem with it at once."""
    field = f"orders[{index}]"
    if not isinstance(raw, Mapping):
        raise ValueError(f"{field} must be an object")
    errors: list[str] = []

    def attempt(call: Callable[[], Any]) -> Any:
        try:
            return call()
        except ValueError as exc:
            errors.append(str(exc))
            return None

    unknown = sorted(set(raw) - _ORDER_FIELDS)
    if unknown:
        errors.append(f"{field} has unknown fields {unknown}; orders carry {sorted(_ORDER_FIELDS)}")
    route = route or {}
    if route.get("kind") == "manual":
        symbol = _manual_symbol(raw.get("symbol") or raw.get("instrument_id"), route.get("market") or "US")
    else:
        symbol = order_symbol(raw.get("symbol") or raw.get("instrument_id"))
    if symbol is None:
        errors.append(f"{field}.symbol must be a US ticker such as VTI or BRK.B" if route.get("market") != "MX"
                      else f"{field}.symbol must be a BMV or SIC symbol such as WALMEX, NAFTRAC or VOO")
    side = str(raw.get("side") or "").strip().lower()
    if side not in ("buy", "sell"):
        errors.append(f"{field}.side must be buy or sell")
    kind = str(raw.get("type") or "limit").strip().lower()
    if kind not in ("limit", "market"):
        errors.append(f"{field}.type must be limit (default) or market")
    tif = str(raw.get("time_in_force") or "day").strip().lower()
    if tif not in ("day", "gtc"):
        errors.append(f"{field}.time_in_force must be day (default) or gtc")
    qty_raw = raw.get("qty", raw.get("quantity"))
    notional_raw = raw.get("notional")
    if isinstance(notional_raw, str):
        notional_raw = notional_raw.strip().lstrip("$").strip()  # "$500" is USD 500
    if qty_raw is None and notional_raw is None and raw.get("estimated_amount") is not None:
        amount = attempt(lambda: _dec(raw["estimated_amount"], f"{field}.estimated_amount", positive=False))
        notional_raw = abs(amount) if amount is not None else None
    if qty_raw is None and notional_raw is None or qty_raw is not None and notional_raw is not None:
        errors.append(f"{field} needs exactly one of qty or notional (USD)")
    qty = attempt(lambda: _s(_dec(qty_raw, f"{field}.qty"))) if qty_raw is not None else None
    notional = attempt(lambda: _s(_money(_dec(notional_raw, f"{field}.notional")))) \
        if notional_raw is not None else None
    line: dict[str, Any] = {"index": index, "symbol": symbol, "side": side, "type": kind, "time_in_force": tif,
                            "qty": qty, "notional": notional, "limit_input": None, "state": "proposed"}
    if raw.get("limit_price") is not None:
        if kind == "market":
            errors.append(f"{field}: a market order has no limit_price")
        price = attempt(lambda: _dec(raw["limit_price"], f"{field}.limit_price"))
        if price is not None and not _valid_tick(price):
            errors.append(f"{field}.limit_price: at or above $1 use cents; below $1 at most 4 decimals")
        elif price is not None:
            line["limit_input"] = _s(price)
    if errors:
        raise ValueError("; ".join(errors))
    account = raw.get("account_id") or raw.get("account")
    if account is not None:
        line["account"] = str(account)[:40]
    if raw.get("exchange") is not None:
        exchange = str(raw["exchange"]).strip().upper()
        if exchange not in manual.LISTINGS:
            raise ValueError(f"{field}.exchange must be one of {sorted(manual.LISTINGS)}")
        line["exchange_input"] = exchange
    for name in ("estimated_tax", "estimated_cost"):
        value = _opt(raw.get(name))
        if value is not None:
            line[name] = _s(_money(abs(value)))
    for name in ("asset_class", "sleeve", "domicile"):
        if isinstance(raw.get(name), str):
            line[name] = raw[name][:40]
    if isinstance(raw.get("tags"), list):
        line["tags"] = [str(t)[:40] for t in raw["tags"][:10]]
    if raw.get("lots") is not None:
        line.update(_normalize_lots(raw["lots"], field, side, qty))
    return line


MAX_LOTS = 50
_LOT_FIELDS = frozenset({"lot_id", "quantity", "estimated_tax_saving", "repurchase_not_before", "character",
                         "account_id"})


def _normalize_lots(raw: Any, field: str, side: str, qty: str | None) -> dict[str, Any]:
    """The tax lots a sell relieves (specific identification), each with its estimated saving.

    ``lots`` is ``[{lot_id, quantity?, estimated_tax_saving?, repurchase_not_before?, character?, account_id?}]``,
    typically copied from ``tax`` ``harvest_report`` (``result.order_tickets[].inputs``).  The line then carries
    the lots, the sum of their savings (``null`` when any lot's saving is unknown) and the latest wash-sale
    ``repurchase_not_before`` date.  Nothing here changes what is sent to the broker.
    """
    if side != "sell":
        raise ValueError(f"{field}.lots: only a sell relieves tax lots")
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_LOTS:
        raise ValueError(f"{field}.lots must be a list of 1-{MAX_LOTS} lots")
    lots: list[dict[str, Any]] = []
    errors: list[str] = []
    for j, item in enumerate(raw):
        path = f"{field}.lots[{j}]"
        if not isinstance(item, Mapping):
            errors.append(f"{path} must be an object")
            continue
        unknown = sorted(set(item) - _LOT_FIELDS)
        if unknown:
            errors.append(f"{path} has unknown fields {unknown}; lots carry {sorted(_LOT_FIELDS)}")
        lot_id = item.get("lot_id")
        if not isinstance(lot_id, str) or not lot_id.strip() or len(lot_id) > 64:
            errors.append(f"{path}.lot_id must be a lot id (text, at most 64 characters)")
            continue
        lot: dict[str, Any] = {"lot_id": lot_id.strip()}
        try:
            if item.get("quantity") is not None:
                lot["quantity"] = _s(_dec(item["quantity"], f"{path}.quantity"))
            saving = item.get("estimated_tax_saving")
            lot["estimated_tax_saving"] = None if saving is None else _s(
                _money(_dec(saving, f"{path}.estimated_tax_saving", positive=False)))
        except ValueError as exc:
            errors.append(str(exc))
        day = item.get("repurchase_not_before")
        if day is not None:
            try:
                lot["repurchase_not_before"] = datetime.strptime(str(day), "%Y-%m-%d").date().isoformat()
            except ValueError:
                errors.append(f"{path}.repurchase_not_before must be YYYY-MM-DD")
        if item.get("character") is not None:
            if item["character"] not in ("short_term", "long_term"):
                errors.append(f"{path}.character must be short_term or long_term")
            else:
                lot["character"] = item["character"]
        if isinstance(item.get("account_id"), str):
            lot["account_id"] = item["account_id"][:40]
        lots.append(lot)
    ids = [lot["lot_id"] for lot in lots]
    if len(set(ids)) != len(ids):
        errors.append(f"{field}.lots lists a lot twice")
    if not errors and qty is not None and all("quantity" in lot for lot in lots):
        total = sum((Decimal(lot["quantity"]) for lot in lots), Decimal(0))
        if total != Decimal(qty):
            errors.append(f"{field}.lots quantities add up to {_s(total)}, not the order qty {qty}")
    if errors:
        raise ValueError("; ".join(errors))
    savings = [lot.get("estimated_tax_saving") for lot in lots]
    dates = [lot["repurchase_not_before"] for lot in lots if lot.get("repurchase_not_before")]
    return {"lots": lots,
            "estimated_tax_saving": None if any(v is None for v in savings)
            else _s(sum((Decimal(v) for v in savings), Decimal(0))),
            "repurchase_not_before": max(dates) if dates else None}


# -- the pre-trade checks ------------------------------------------------------------

def _safe(call: Callable[[], Any]) -> tuple[Any, str | None]:
    try:
        return call(), None
    except BrokerError as exc:
        return None, str(exc)
    except ValueError as exc:
        return None, str(exc)


def _trade_cutoff(now: datetime, market_open: bool | None, config: Mapping[str, Decimal]) -> datetime:
    """The oldest last-trade time that still counts as a price."""
    if market_open:
        return now - timedelta(minutes=float(config["trade_age_minutes"]))
    cutoff, days = now.astimezone(_EASTERN), int(config["trade_age_days"].to_integral_value(rounding=ROUND_CEILING))
    while days > 0:  # walk back whole weekdays: Monday before the open reaches back to Friday
        cutoff -= timedelta(days=1)
        if cutoff.weekday() < 5:
            days -= 1
    return cutoff


def _reserved_today(state: Mapping[str, Any], today: str) -> Decimal:
    return sum((_opt(r.get("notional")) or Decimal(0) for r in (state.get("live_reserved") or {}).values()
                if r.get("date_et") == today), Decimal(0))


def _legacy_confirmed_today(store: Any, client_id: str, today: str, reserved: Mapping[str, Any]) -> Decimal:
    """Live confirms recorded before reservations existed (tickets with no reservation row)."""
    used = Decimal(0)
    for row in store.order_events(client_id):
        payload = row["payload"]
        if row["event"] == "confirm" and row["mode"] == "live" and payload.get("date_et") == today \
                and row["ticket_id"] not in reserved:
            used += _opt(payload.get("notional")) or Decimal(0)
    return used


def _live_used_today(store: Any, client_id: str, now: datetime) -> Decimal:
    """Live notional reserved today (US Eastern date).

    Each live confirmation reserves its notional inside the same write
    transaction that claims the ticket (:func:`confirm`), so two confirmations
    racing each other cannot both fit under the daily limit; lines that never
    reached the broker give their share back.
    """
    if store is None or not client_id:
        return Decimal(0)
    today = now.astimezone(_EASTERN).date().isoformat()
    state = store.auxiliary(client_id, "execution")
    reserved = state.get("live_reserved") or {}
    return _reserved_today(state, today) + _legacy_confirmed_today(store, client_id, today, reserved)


def _reservation_after(lines: list[dict]) -> Decimal:
    """What a submitted ticket still holds of the daily limit: every line except those never placed."""
    return _money(sum((_opt(l.get("estimated_amount")) or Decimal(0) for l in lines if l.get("state") != "failed"),
                      Decimal(0)))


def _release(state: dict, ticket_id: str, lines: list[dict]) -> dict:
    """Shrink a ticket's live reservation to the lines that reached the broker (called inside a write)."""
    reserved = dict(state.get("live_reserved") or {})
    if ticket_id not in reserved:
        return state
    row = dict(reserved[ticket_id])
    keep = _reservation_after(lines)
    if (_opt(row.get("notional")) or Decimal(0)) > keep:
        row["released"] = _s(_money((_opt(row.get("notional")) or Decimal(0)) - keep + (_opt(row.get("released")) or 0)))
        row["notional"] = _s(keep)
        reserved[ticket_id] = row
    return {**state, "live_reserved": reserved}


def _policy_rows(snapshot: Mapping[str, Any] | None, line: dict, amount: Decimal | None, positions: list,
                 account_id: str, now: datetime, currency: str = "USD") -> list[dict]:
    from .. import policy

    if not snapshot or not snapshot.get("facts"):
        return [_check("no_policy", "warn", "No accepted investment policy; this order was not checked against one.",
                       line["index"])]
    ips = policy.current(snapshot, now.date())
    if ips is None:
        return [_check("no_policy", "warn", "No accepted investment policy; this order was not checked against one.",
                       line["index"])]
    if amount is None:
        return [_check("policy_unchecked", "unknown", "The policy check needs the order's amount.", line["index"])]
    proposal = {"kind": "trade", "action": line["side"], "symbol": line["symbol"], "amount": float(amount),
                "currency": currency, "funding": f"cash:{account_id}"}
    for name in ("asset_class", "sleeve", "domicile", "tags"):
        if line.get(name) is not None:
            proposal[name] = line[name]
    portfolio = {"currency": currency, "positions": [
        {"symbol": str(p.get("symbol") or ""), "value": float(_opt(p.get("market_value")) or 0)}
        for p in positions if p.get("symbol")]} if positions else None
    try:
        result = policy.check(ips, proposal, portfolio)
    except ValueError as exc:
        return [_check("policy_unchecked", "warn", f"The policy check could not run: {exc}", line["index"])]
    rows, unchecked = [], 0
    for rule in result["rules"]:
        extra = {"rule": rule["rule"], "symbol": line["symbol"]}
        if rule.get("sleeve"):
            extra["sleeve"] = rule["sleeve"]
        if rule["status"] == "violation":
            rows.append(_check("policy", "violation", rule["explanation"], line["index"], **extra))
        elif rule["status"] == "warn" and rule["explanation"].startswith("Not checked"):
            unchecked += 1
        elif rule["status"] == "warn":
            rows.append(_check("policy", "warn", rule["explanation"], line["index"], **extra))
    if unchecked:
        rows.append(_check("policy_unchecked", "warn",
                           f"{unchecked} policy rule(s) could not be checked for {line['symbol']}.", line["index"],
                           count=unchecked))
    return rows


def _condense_policy(checks: list[dict]) -> list[dict]:
    """One quiet line per kind of policy note, so the card stays calm; violations stay per order."""
    out: list[dict] = []
    seen: set[tuple] = set()
    unchecked = 0
    band = False
    for check in checks:
        code, params = check["code"], check.get("params") or {}
        if code == "no_policy":
            key = ("no_policy",)
            check = _check("no_policy", "warn", "No accepted investment policy; the orders were not checked "
                           "against one.")
        elif code == "policy_unchecked" and check["status"] == "warn":
            unchecked += int(params.get("count") or 1)
            continue
        elif code == "policy" and check["status"] == "warn" and params.get("rule") == "allocation_band":
            band = True
            continue
        elif code == "policy":
            key = ("policy", check["status"], params.get("rule"), check.get("line") if check["status"] == "violation"
                   else params.get("symbol"))
        else:
            out.append(check)
            continue
        if key not in seen:
            seen.add(key)
            out.append(check)
    if band:
        out.append(_check("policy_band", "warn", "Part of the portfolio is already outside its policy ranges."))
    if unchecked:
        out.append(_check("policy_unchecked", "warn", f"{unchecked} policy check(s) could not run for lack of data.",
                          count=unchecked))
    return out


def _guardrail_rows(snapshot: Mapping[str, Any] | None, now: datetime) -> list[dict]:
    """Wealth's cool-off flag (:func:`wealth.guardrails.cool_off`) as quiet lines; guardrails never block."""
    from .. import guardrails

    zone = None
    for fact in (snapshot or {}).get("facts") or []:
        if fact.get("key") == "client.profile" and isinstance(fact.get("value"), Mapping):
            zone = fact["value"].get("timezone")
    try:
        result = guardrails.cool_off(now=now.isoformat(), tz=zone if isinstance(zone, str) else None)
    except ValueError:
        return []
    return [_check("cool_off", "warn", flag["en"], es=flag["es"]) for flag in result["flags"]]


def _route_checks(route: Mapping[str, Any] | None, *, kind: str, mode: str, account: Mapping[str, Any] | None,
                  label: str) -> list[dict]:
    """Whether the account the orders name is the account the broker would place them in.

    * An account Wealth has no record of goes to the default broker: a quiet line on paper, a block live.
    * For Alpaca and IBKR, a named account must be the broker's own ledger account
      (``account_state().ledger_account_id``).  A route read from a model-writable ``account.<id>`` fact, or from
      the id's form alone, is never enough by itself: the broker must agree, or nothing is sent.
    """
    if not route:
        return []
    rows: list[dict] = []
    account_id = route.get("account_id")
    if account_id and not route.get("known"):
        rows.append(_check("account_unmatched", "block" if mode == "live" else "warn",
                           f"Wealth has no record of account {account_id}, so this ticket goes to "
                           f"{_LABELS.get(kind, kind)}, the default broker"
                           + (", and a live order never goes to an account Wealth cannot identify. " if mode == "live"
                              else ". ")
                           + "If the account is elsewhere, add it (with its institution) and ask again.",
                           account_id=account_id))
    elif account_id and kind in ("alpaca", "ibkr") and isinstance(account, Mapping):
        reached = account.get("ledger_account_id")
        if reached != account_id:
            rows.append(_check("account_mismatch", "block",
                               f"The orders are for account {account_id}, but {label} is connected to "
                               f"{'ledger account ' + str(reached) if reached else 'an account Wealth cannot match'}"
                               f" (number {mask(account.get('number')) if account.get('number') else 'unknown'}), "
                               "so nothing will be sent. Log into the right account, or ask for a ticket for the "
                               "account that is connected.", account_id=account_id, connected=reached,
                               source=route.get("source")))
    return rows


def run_checks(lines: list[dict], *, mode: str, broker: OrderBroker | None, snapshot: Mapping[str, Any] | None,
               store: Any = None, client_id: str | None = None, ticket_id: str | None = None,
               environ: Mapping[str, str] | None = None, now: datetime | None = None, kind: str | None = None,
               unavailable: str | None = None,
               route: Mapping[str, Any] | None = None) -> tuple[list[dict], list[dict]]:
    """Price every line and return ``(lines, checks)``.

    Check status: ``pass``; ``warn`` (a quiet line); ``violation`` (the IPS; the
    person may override it on the card, which is recorded); ``block`` (never
    overridable); ``unknown`` (the fact could not be read: it blocks submission,
    because an unknown is never assumed to be fine).

    A manual ticket (``kind="manual"``) is priced from reference closes and the
    ledger; what Wealth cannot see there (cash, holdings, a live quote) is a
    quiet line, because the person places the order and sees their broker's screen.
    """
    now = _now(now)
    config = limits(environ)
    kind = kind or (broker.name if broker is not None else BROKER)
    label = _label(kind, broker)
    by_hand = kind == "manual"
    lines = [dict(line) for line in lines]
    checks: list[dict] = []
    if broker is None:
        checks.append(_check("broker_unavailable", "unknown", unavailable or _unavailable(kind, mode), mode=mode,
                             broker=kind))
    if mode == "live" and not live_enabled(kind, environ):
        checks.append(_check("live_disabled", "block", f"{label} is logged into a live account and live trading is "
                             "not turned on here (WEALTH_TRADING_LIVE). Log into the paper account to practise.",
                             broker=kind))
    account = positions = clock = open_orders = None
    positions_error = None
    if broker is not None and callable(getattr(broker, "use_clock", None)):
        broker.use_clock(lambda: now)  # the broker's market hours and freshness read the same moment as the checks
    if broker is not None:
        account, error = _safe(broker.account_state)
        if error:
            checks.append(_check("account_unknown", "warn" if by_hand else "unknown",
                                 f"The {label} account could not be read: {error}"))
        positions, positions_error = _safe(broker.positions)
        clock, _ = _safe(broker.market_clock)
        open_orders, _ = _safe(broker.open_orders)
    positions = positions or []
    account_id = (account or {}).get("ledger_account_id") or f"{kind}-{mode}"
    currency = str((account or {}).get("currency") or getattr(broker, "currency", None) or "USD").upper()
    market_open = clock.get("is_open") if isinstance(clock, Mapping) else None
    trade_cutoff = _trade_cutoff(now, market_open is True, config)
    usd_rate = None
    if by_hand and broker is not None and currency != "USD":
        usd_rate, _ = _safe(broker.fx_to_usd)
    checks += _route_checks(route, kind=kind, mode=mode, account=account, label=label)

    seen: dict[tuple[str, str], int] = {}
    for line in lines:
        i, symbol = line["index"], line["symbol"]
        line["currency"] = currency
        key = (symbol, line["side"])
        if key in seen:
            checks.append(_check("duplicate_line", "block", f"{symbol} {line['side']} appears twice in this ticket.",
                                 i, symbol=symbol))
        seen[key] = i
        asset = last = trade = None
        stale = reference = False
        if broker is not None and by_hand:
            listing, assumed = broker.listing(symbol, line.get("exchange_input"))
            line["exchange"] = listing
            if listing == "SIC":
                line["listing_note"] = "Compra en el SIC"
            if assumed:
                checks.append(_check("listing_assumed", "warn", f"{symbol} is taken to be the {listing} listing; "
                                     "say BMV or SIC if you meant the other.", i, symbol=symbol, listing=listing))
            asset = broker.instrument(symbol)
            trade, _ = _safe(lambda: broker.latest_trade(symbol, listing))
        elif broker is not None:
            asset, error = _safe(lambda: broker.instrument(symbol))
            if error:
                checks.append(_check("asset_unknown", "unknown", f"Whether {symbol} is tradable could not be read.",
                                     i, symbol=symbol))
            elif asset is None or not asset.get("tradable"):
                checks.append(_check("not_tradable", "block", f"{symbol} is not tradable at {label}.", i,
                                     symbol=symbol))
            trade, error = _safe(lambda: broker.latest_trade(symbol))
            if asset and asset.get("exchange") and kind == "ibkr":
                line["exchange"] = asset["exchange"]
        last = _opt((trade or {}).get("price")) if trade else None
        old_close = None
        if last is not None and trade.get("fresh") is False:
            # The broker says this price is not current (a prior close, delayed or frozen data, a halt).
            checks.append(_check("price_stale", "unknown",
                                 f"The {symbol} price {last} from {label} is not current: "
                                 f"{trade.get('stale_reason') or 'the data is not real-time'}. No order is priced "
                                 "from it.", i, symbol=symbol, last=last, reason=trade.get("stale_reason")))
            last, stale = None, True
        elif last is not None and trade.get("reference"):
            # A manual ticket's price is the last close: shown with its date, never taken for a live quote.
            reference = True
            line["reference_date"] = trade.get("at")
            try:
                closed_on = datetime.strptime(str(trade.get("at") or "")[:10], "%Y-%m-%d").date()
            except ValueError:
                closed_on = None
            if closed_on is None or (now.date() - closed_on).days > manual.REFERENCE_MAX_AGE_DAYS:
                checks.append(_check("price_reference_old", "warn", f"The last {symbol} close Wealth has is from "
                                     f"{trade.get('at') or 'an unknown date'}; check the price at {label}.", i,
                                     symbol=symbol))
                old_close, last, stale = last, None, True
        elif last is not None:
            try:
                traded_at = _parse(str(trade.get("at") or ""))
                if traded_at.tzinfo is None:
                    raise ValueError
            except ValueError:
                traded_at = None
            if traded_at is None or traded_at < trade_cutoff:
                when = traded_at.astimezone(_EASTERN).strftime("%Y-%m-%d %H:%M ET") if traded_at else "an unknown time"
                age = (f"{int(config['trade_age_minutes'])} minutes" if market_open
                       else f"{int(config['trade_age_days'])} trading day(s)")
                checks.append(_check("price_stale", "unknown",
                                     f"The last {symbol} trade was at {when}, older than {age}, so its price "
                                     "is not current (the symbol may be halted or thinly traded). No order is "
                                     "priced from it.", i, symbol=symbol, traded_at=trade.get("at"),
                                     last=last))
                last, stale = None, True
        line["last_price"] = _s(last)
        fractionable = bool(asset and asset.get("fractionable"))

        # price: a limit by default, collared around the last trade (a manual card: around the last close)
        limit = _opt(line.get("limit_input"))
        if line["type"] == "market":
            line["limit_price"] = None
            if mode == "live":
                checks.append(_check("live_needs_limit", "block", "Live orders must be limit orders.", i))
            else:
                checks.append(_check("market_order", "warn", f"{symbol} is a market order: the price is not capped.",
                                     i, symbol=symbol))
        elif limit is None and last is not None:
            limit = _tick(last * (1 + config["collar"] / 2), ROUND_CEILING) if line["side"] == "buy" \
                else _tick(last * (1 - config["collar"] / 2), ROUND_FLOOR)
        if line["type"] == "limit":
            line["limit_price"] = _s(limit)
            if limit is None and stale and not by_hand:
                pass  # already said, plainly, by price_stale
            elif limit is None and by_hand:
                # The person places it themselves and sees their broker's price: a quiet line, never a block.
                reason = (trade or {}).get("reason")
                line["price_note"] = "precio al momento de colocar"
                checks.append(_check("price_unknown", "warn", f"Wealth has no current {symbol} price"
                                     + (f" ({reason})" if reason else "") + f", so no limit was set: use the price "
                                     f"{label} shows when you place it (precio al momento de colocar), or give a "
                                     "limit price.", i, symbol=symbol, es="Precio al momento de colocar."))
            elif limit is None:
                checks.append(_check("price_unknown", "unknown", f"The last price of {symbol} could not be read, so "
                                     "no limit price was set.", i, symbol=symbol))
            elif last is not None:
                through = (limit - last) / last if line["side"] == "buy" else (last - limit) / last
                if through > config["collar"]:
                    checks.append(_check("collar", "warn" if reference else "block",
                                         f"The {symbol} limit {limit} is more than {config['collar'] * 100:.1f}% "
                                         f"through the last {'close' if reference else 'price'} {last}.", i,
                                         symbol=symbol, limit=limit, last=last, collar=config["collar"]))
                elif -through > FAR_FROM_MARKET:
                    checks.append(_check("far_from_market", "warn", f"The {symbol} limit is far from the last price "
                                         f"{last}; it may not fill.", i, symbol=symbol, last=last))
            elif line.get("limit_input") is not None and not stale:
                checks.append(_check("price_unknown", "warn" if by_hand else "unknown",
                                     f"The last price of {symbol} could not be read to check the limit.", i,
                                     symbol=symbol))

        # quantity: dollar amounts become a quantity at the limit price
        price = limit if limit is not None else last
        qty = _opt(line.get("qty"))
        if line.get("notional") is not None:
            notional = Decimal(line["notional"])
            if price is None:
                qty = None
            else:
                step = Decimal("0.000001") if fractionable else Decimal(1)
                qty = (notional / price).quantize(step, rounding=ROUND_DOWN)
                if qty <= 0:
                    checks.append(_check("too_small", "block", f"{currency} {notional} buys less than one {symbol} "
                                         "share.", i, symbol=symbol))
                    qty = None
        line["order_qty"] = _s(qty)
        if qty is not None and not _whole(qty):
            if asset is not None and not fractionable:
                checks.append(_check("not_fractionable", "warn" if by_hand else "block",
                                     f"{symbol} trades in whole shares only" + (f" unless {label} offers fractions."
                                                                                if by_hand else "."), i,
                                     symbol=symbol))
            if line["time_in_force"] != "day":
                checks.append(_check("fractional_day_only", "block", "Fractional orders are day orders only.", i))
        amount = _money(qty * price) if qty is not None and price is not None else None
        line["estimated_amount"] = _s(amount)
        if amount is None and by_hand and old_close is not None and qty is not None:
            # No current price, but an older close: a rough range (±10%) instead of an amount.
            line["estimated_range"] = [_s(_money(qty * old_close * Decimal("0.9"))),
                                       _s(_money(qty * old_close * Decimal("1.1")))]
            line["reference_close"] = _s(old_close)
        if amount is None and line.get("notional") is None:
            checks.append(_check("amount_unknown", "warn" if by_hand else "unknown",
                                 f"The amount of the {symbol} order is unknown"
                                 + (" until it is placed at the broker's price." if by_hand else "."), i,
                                 symbol=symbol))
        if by_hand and broker is not None:
            fee, basis = broker.fee(amount)
            line["estimated_fee"], line["fee_basis"] = _s(fee), basis
            if usd_rate and amount is not None:
                line["fx"] = {"pair": f"USD/{currency}", "rate": _s(usd_rate),
                              "usd_amount": _s(_money(amount / usd_rate))}
                if getattr(broker, "fx_as_of", None):
                    line["fx"]["as_of"] = broker.fx_as_of

        # sells never exceed what is held (no shorting)
        if line["side"] == "sell" and broker is not None and qty is not None:
            if by_hand and positions_error:
                checks.append(_check("position_unknown", "warn", f"Wealth has no holdings on record at {label}, so "
                                     f"it could not check that {qty} {symbol} are there to sell.", i, symbol=symbol))
            else:
                match = manual.symbol_key if by_hand else (lambda value: str(value or "").upper())
                held = next((p for p in positions if match(p.get("symbol")) == match(symbol)), None)
                held_qty = _opt((held or {}).get("qty")) or Decimal(0)
                if qty > held_qty:
                    checks.append(_check("sell_exceeds_position", "block",
                                         f"Selling {qty} {symbol} is more than the {held_qty} held.", i,
                                         symbol=symbol, qty=qty, held=held_qty))

        # live per-order limit
        if mode == "live" and amount is not None and amount > config["max_order"]:
            checks.append(_check("live_order_limit", "block", f"USD {amount} is above the live per-order limit of "
                                 f"USD {config['max_order']}.", i, amount=amount, limit=config["max_order"]))

        # open orders for the same symbol and side
        for order in open_orders or []:
            if str(order.get("symbol", "")).upper() == symbol and order.get("side") == line["side"] \
                    and not str(order.get("client_order_id", "")).startswith(f"wealth-{ticket_id}-"):
                checks.append(_check("open_order", "warn", f"An open {line['side']} order for {symbol} is already "
                                     f"at {label}.", i, symbol=symbol))
                break

        checks += _policy_rows(snapshot, line, amount, positions, account_id, now, currency)

    # the account as a whole
    if isinstance(account, Mapping):
        if not account.get("active", True):
            checks.append(_check("account_blocked", "block", f"The {label} account cannot place orders right now."))
        buys = [Decimal(l["estimated_amount"]) for l in lines if l["side"] == "buy" and l.get("estimated_amount")]
        power = _opt(account.get("buying_power"))
        if buys and power is None:
            checks.append(_check("buying_power_unknown", "warn" if by_hand else "unknown",
                                 f"Wealth cannot see the cash in this account; make sure {currency} "
                                 f"{_money(sum(buys))} is available." if by_hand else "Buying power could not be read."))
        elif buys and sum(buys) > power:
            if by_hand:
                checks.append(_check("buying_power", "warn", f"The buys need {currency} {_money(sum(buys))}; the "
                                     f"ledger shows {currency} {_money(power)} in this account.",
                                     need=_money(sum(buys)), have=_money(power)))
            else:
                checks.append(_check("buying_power", "block", f"The buys need USD {_money(sum(buys))}; cash buying "
                                     f"power is USD {_money(power)} (Wealth never uses margin).",
                                     need=_money(sum(buys)), have=_money(power)))
    if mode == "live":
        total = sum((Decimal(l["estimated_amount"]) for l in lines if l.get("estimated_amount")), Decimal(0))
        used = _live_used_today(store, client_id or "", now)
        if total + used > config["max_daily"]:
            checks.append(_check("live_daily_limit", "block", f"This would bring today's live orders to USD "
                                 f"{_money(total + used)}, above the daily limit of USD {config['max_daily']}.",
                                 total=_money(total + used), limit=config["max_daily"]))

    # market hours
    if isinstance(clock, Mapping) and clock.get("is_open") is False:
        checks.append(_check("market_closed", "warn", "The market is closed; day orders wait for the next open.",
                             next_open=clock.get("next_open")))

    # other tickets proposing the same thing
    if store is not None and client_id:
        state = store.auxiliary(client_id, "execution")
        recent = now - timedelta(hours=24)
        for other in (state.get("tickets") or {}).values():
            if other.get("id") == ticket_id or other.get("status") not in ("pending", "submitted", "submitting",
                                                                            "placed"):
                continue
            if other["status"] == "pending" and _parse(other["expires_at"]) <= now:
                continue
            if _parse(other["created_at"]) < recent:
                continue
            for line in lines:
                if any(o["symbol"] == line["symbol"] and o["side"] == line["side"] for o in other.get("lines", [])):
                    checks.append(_check("duplicate_ticket", "warn", f"Another recent ticket also has {line['side']} "
                                         f"{line['symbol']}.", line["index"], symbol=line["symbol"]))
    checks += _guardrail_rows(snapshot, now)
    return lines, _condense_policy(checks)


def _relative_move(shown: Decimal, fresh: Decimal) -> Decimal:
    return abs(fresh - shown) / abs(shown) if shown else (Decimal(0) if fresh == shown else Decimal(1))


def _price_moves(shown_lines: list[dict], fresh_lines: list[dict], *, config: Mapping[str, Decimal],
                 mode: str) -> list[dict]:
    """``price_moved`` notices for lines re-priced beyond tolerance since the card showed them.

    A line moved when its limit moved more than the collar, its quantity or amount more than
    :data:`PRICE_MOVE_TOLERANCE`, its amount crossed the live per-order limit, or the card showed no
    limit, quantity or amount where one now exists (the person never saw a price).
    """
    shown_by_index = {l["index"]: l for l in shown_lines}
    rows: list[dict] = []
    for fresh in fresh_lines:
        shown = shown_by_index.get(fresh["index"], {})
        reasons: list[str] = []
        # last_price: the fresh price against the one the card displayed (a limit the person gave stays put, so
        # without this a market that moved would not show).
        for field, tolerance in (("last_price", config["collar"]), ("limit_price", config["collar"]),
                                 ("order_qty", PRICE_MOVE_TOLERANCE), ("estimated_amount", PRICE_MOVE_TOLERANCE)):
            before, after = _opt(shown.get(field)), _opt(fresh.get(field))
            if after is None:
                continue  # an unknown fresh value is already a blocking check
            if before is None or _relative_move(before, after) > tolerance:
                reasons.append(field)
        before, after = _opt(shown.get("estimated_amount")), _opt(fresh.get("estimated_amount"))
        if mode == "live" and after is not None and after > config["max_order"] and "estimated_amount" not in reasons \
                and (before is None or before <= config["max_order"]):
            reasons.append("estimated_amount")
        if reasons:
            symbol = fresh["symbol"]
            rows.append(_check("price_moved", "warn",
                               f"{symbol} was re-priced since the card was shown: price "
                               f"{shown.get('last_price') or '-'} -> {fresh.get('last_price') or '-'}, limit "
                               f"{shown.get('limit_price') or '-'}"
                               f" -> {fresh.get('limit_price') or '-'}, quantity {shown.get('order_qty') or '-'} -> "
                               f"{fresh.get('order_qty') or '-'}, about USD {shown.get('estimated_amount') or '-'} -> "
                               f"{fresh.get('estimated_amount') or '-'}. Tap again to place it at the new price.",
                               fresh["index"], symbol=symbol, fields=reasons,
                               shown_limit=shown.get("limit_price"), limit=fresh.get("limit_price"),
                               shown_qty=shown.get("order_qty"), qty=fresh.get("order_qty"),
                               shown_amount=shown.get("estimated_amount"), amount=fresh.get("estimated_amount")))
    return rows


def _blocking(checks: list[dict], override: bool) -> list[dict]:
    return [c for c in checks if c["status"] in ("block", "unknown") or (c["status"] == "violation" and not override)]


# -- tickets -------------------------------------------------------------------------

def _totals(lines: list[dict], currency: str = "USD") -> dict[str, Any]:
    """``amount``: net cash, buys minus sells (negative means money comes in); ``gross``: all orders."""
    amounts = [_opt(l.get("estimated_amount")) for l in lines]
    known = all(a is not None for a in amounts)
    net = sum((a if l["side"] == "buy" else -a for a, l in zip(amounts, lines)), Decimal(0)) if known else None
    gross = sum(amounts, Decimal(0)) if known else None
    result: dict[str, Any] = {"amount": _s(net), "gross": _s(gross), "currency": currency}
    for name in ("estimated_tax", "estimated_cost", "estimated_fee"):
        values = [_opt(l.get(name)) for l in lines if l.get(name) is not None]
        result[name] = _s(sum(values, Decimal(0))) if values else None
    return result


_SETTLED_OPEN = ("pending", "submitting", "submitted", "placed")  # everything else is settled


def _settle_expired(tickets: Mapping[str, Any], now: datetime) -> tuple[dict[str, Any], bool]:
    """Pending tickets past ``expires_at`` become ``expired`` (their card code is dropped); ``(tickets, changed)``."""
    out, changed = dict(tickets), False
    for tid, item in tickets.items():
        if item.get("status") == "pending" and _parse(item["expires_at"]) <= now:
            out[tid] = {**item, "status": "expired", "nonce": None, "nonce_hash": None}
            changed = True
    return out, changed


def _prune(tickets: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Settle expired tickets, then keep at most KEEP_TICKETS, dropping the oldest settled first.

    The append-only ``orders`` audit table keeps every dropped ticket's history.
    """
    tickets, _ = _settle_expired(tickets, now)
    if len(tickets) > KEEP_TICKETS:
        settled = sorted((t["created_at"], tid) for tid, t in tickets.items() if t["status"] not in _SETTLED_OPEN)
        for _, tid in settled[:len(tickets) - KEEP_TICKETS]:
            tickets.pop(tid)
    return tickets


def _acknowledged(state: Mapping[str, Any], ticket: Mapping[str, Any]) -> bool:
    """Whether the person already typed LIVE once for this ticket's broker."""
    return bool((state.get("live_ack") or {}).get(ticket.get("broker") or BROKER))


_LINE_FIELDS = ("index", "side", "symbol", "type", "time_in_force", "limit_price", "last_price", "estimated_amount",
                "estimated_tax", "estimated_cost", "account", "state", "broker_order_id", "filled_qty",
                "filled_avg_price", "reason", "lots", "estimated_tax_saving", "repurchase_not_before", "exchange",
                "listing_note", "currency", "reference_date", "estimated_fee", "fee_basis", "fx", "confirmed_by",
                "price_note", "estimated_range", "reference_close")


def manual_instructions(ticket: Mapping[str, Any]) -> dict[str, str]:
    """The place-it-yourself text the card copies: one line per order, in English and Spanish."""
    route = ticket.get("route") or {}
    label = ticket.get("broker_label") or _LABELS["manual"]
    account = route.get("account_id")
    en, es = [f"{label}" + (f" · account {account}" if account else "")], \
        [f"{label}" + (f" · cuenta {account}" if account else "")]
    for line in ticket["lines"]:
        currency = line.get("currency") or ticket.get("currency") or "USD"
        qty = line.get("order_qty") or line.get("qty")
        what_en = what_es = f"{qty} {line['symbol']}" if qty else None
        if not qty and line.get("notional"):
            what_en, what_es = (f"{currency} {line['notional']} of {line['symbol']}",
                                f"{currency} {line['notional']} de {line['symbol']}")
        what_en, what_es = what_en or f"? {line['symbol']}", what_es or f"? {line['symbol']}"
        venue = line.get("exchange") or ""
        if line.get("limit_price"):
            price_en, price_es = f"limit {currency} {line['limit_price']}", f"límite {currency} {line['limit_price']}"
        elif line.get("type") == "limit":
            price_en, price_es = "limit at the price when you place it", "precio al momento de colocar"
        else:
            price_en, price_es = "market", "a mercado"
        where_en = {"SIC": " on the SIC", "BMV": " on the BMV", "BIVA": " on BIVA"}.get(venue, "")
        where_es = {"SIC": " en el SIC", "BMV": " en la BMV", "BIVA": " en BIVA"}.get(venue, "")
        tif = (line.get("time_in_force") or "day").upper()
        tif_es = "del día" if tif == "DAY" else "hasta cancelar"
        side_en, side_es = ("Buy", "Compra") if line["side"] == "buy" else ("Sell", "Venta")
        amount = f" ≈ {currency} {line['estimated_amount']}" if line.get("estimated_amount") else ""
        if not amount and line.get("estimated_range"):
            low, high = line["estimated_range"]
            amount = f" ≈ {currency} {low}–{high}"
        en.append(f"{side_en} {what_en}{where_en} · {price_en} · {tif}{amount}")
        es.append(f"{side_es} {what_es}{where_es} · {price_es} · {tif_es}{amount}")
    return {"en": "\n".join(en), "es": "\n".join(es)}


def public_ticket(ticket: Mapping[str, Any], *, include_nonce: bool = False, now: datetime | None = None,
                  live_acknowledged: bool = True, nonce: str | None = None) -> dict[str, Any]:
    """What the card and the model see.  The nonce is only for the card (``include_nonce``).

    Only the nonce's hash is stored; ``nonce`` is the plain code just issued (or just
    presented by the card) for this one response.
    """
    now = _now(now)
    status = ticket["status"]
    if status == "pending" and _parse(ticket["expires_at"]) <= now:
        status = "expired"
    checks = ticket.get("checks") or []
    notices = [c for c in checks if c["status"] != "pass"]
    lines = []
    for line in ticket["lines"]:
        row = {k: line.get(k) for k in _LINE_FIELDS}
        row["qty"] = line.get("order_qty") or line.get("qty")
        row["notional"] = line.get("notional")
        lines.append({k: v for k, v in row.items() if v is not None})
    kind = ticket.get("broker") or BROKER
    route = ticket.get("route") or {}
    view = {"id": ticket["id"], "broker": kind, "broker_label": _label(kind, ticket=ticket), "mode": ticket["mode"],
            "status": status, "source": ticket["source"], "created_at": ticket["created_at"],
            "expires_at": ticket["expires_at"], "total": _totals(ticket["lines"], ticket.get("currency") or "USD"),
            "lines": lines, "notices": notices,
            "blocked": bool(_blocking(checks, override=True)),
            "needs_override": any(c["status"] == "violation" for c in checks),
            "needs_typed": ticket["mode"] == "live" and not live_acknowledged,
            "override": ticket.get("override")}
    if route.get("account_id") or ticket.get("account_number"):
        view["account"] = {k: v for k, v in {"id": route.get("account_id"),
                                             "number": ticket.get("account_number")}.items() if v}
    if kind == "manual":
        view["manual"] = {**manual_instructions(ticket), "placed_at": ticket.get("placed_at"),
                          "placed_via": ticket.get("placed_via"),
                          "verified": bool(ticket.get("verified")) if ticket.get("placed_at") else None}
    if include_nonce and status == "pending":
        view["nonce"] = nonce
    return view


def _issue_nonces(store: Any, client_id: str, ticket_ids: list[str], now: datetime) -> dict[str, str]:
    """A fresh card code for each still-pending ticket; only its hash is stored and the old code stops working."""
    if not ticket_ids:
        return {}
    issued: dict[str, str] = {}

    def rotate(state: dict) -> dict:
        tickets = dict(state.get("tickets") or {})
        for ticket_id in ticket_ids:
            item = tickets.get(ticket_id)
            if item is None or item["status"] != "pending" or _parse(item["expires_at"]) <= now:
                continue
            code = secrets.token_hex(4).upper()
            tickets[ticket_id] = {**item, "nonce": None, "nonce_hash": _nonce_hash(ticket_id, code)}
            issued[ticket_id] = code
        return {**state, "tickets": tickets}

    store.update_auxiliary(client_id, "execution", rotate)
    return issued


def in_wealth_app(environ: Mapping[str, str] | None = None) -> bool:
    """Whether this server runs under the Wealth launcher, whose chat shows the order card."""
    return (os.environ if environ is None else environ).get("WEALTH_BEHAVIOR_IN_HOST") == "1"


_ELSEWHERE = ("The ticket is stored; placing it needs the Wealth web app (`wealth-chat`), where the person reviews "
              "and confirms it; no other host or tool can place it")


_PLACED_CALL = "wealth_run task=order_ticket inputs {{\"ticket_id\": \"{id}\", \"placed\": true}}"


def _summary(view: Mapping[str, Any], app: bool = True) -> str:
    total = view["total"]["amount"]
    currency = view["total"].get("currency") or "USD"
    count = len(view["lines"])
    manual_card = view["mode"] == "manual"
    head = {"live": "LIVE", "paper": "PAPER"}.get(view["mode"], "PLACE-IT-YOURSELF")
    parts = [f"{head} {view.get('broker_label') or ''} ticket {view['id']}: {count} order(s)".replace("  ", " ")]
    if total is None:
        parts.append("amount not yet known")
    else:
        parts.append(f"net about {currency} {total} to pay" if Decimal(total) >= 0
                     else f"net about {currency} {total[1:]} to receive")
    notes = [n for n in view["notices"] if n["status"] in ("violation", "block", "unknown")]
    text = ", ".join(parts) + "."
    if notes:
        text += f" {len(notes)} issue(s) would stop it: " + "; ".join(n["message"] for n in notes[:3])
    if manual_card:
        label = view.get("broker_label")
        if app:
            return (text + f" Wealth cannot send orders to {label}: the card shows exactly what to place there, and "
                    "the person taps 'Ya la puse / I placed it' afterwards; the next statement or sync confirms it. "
                    "It expires at " + view["expires_at"] + ".")
        return (text + f" Wealth cannot send orders to {label}: the person places them there exactly as listed ("
                + view["manual"]["en"].replace("\n", "; ") + "). Once the person says they placed it, record that "
                f"with {_PLACED_CALL.format(id=view['id'])}; the next statement or sync confirms it. The ticket "
                "stays open for that until " + view["expires_at"] + ".")
    where = ("The person reviews it on the order card in the Wealth app and taps to place it" if app
             else _ELSEWHERE)
    return text + " Nothing has been sent. " + where + "; it expires at " + view["expires_at"] + "."


def _audit(store: Any, client_id: str, ticket: Mapping[str, Any], event: str, payload: Mapping[str, Any],
           line: dict | None = None, **extra: Any) -> None:
    # The audit table's mode is paper or live; a manual ticket is real money the person places, so it is "live"
    # there, and its broker column ("manual") tells it apart.
    store.record_order_event(client_id, {
        "ticket_id": ticket["id"], "event": event, "broker": ticket["broker"],
        "mode": "live" if ticket["mode"] == "manual" else ticket["mode"],
        "line": line["index"] if line else None,
        "client_order_id": line.get("client_order_id") if line else None,
        "broker_order_id": (line or {}).get("broker_order_id") or extra.get("broker_order_id"),
        "payload": alpaca_orders.redact(dict(payload)),
    })


class AccountNeeded(ValueError):
    """The orders name no account and the broker is ambiguous: the model must ask which account (``needs_input``)."""

    def __init__(self, message: str, accounts: list[dict[str, Any]]):
        super().__init__(message)
        self.accounts = accounts


_TRADING_TYPES = frozenset({"brokerage", "taxable"})


def _same_institution(account: Mapping[str, Any], label: str) -> bool:
    exact = institution_label(account.get("institution"))
    if exact is not None:
        return exact[0] == label
    profile = manual.institution_profile(account.get("institution"), account.get("country"))
    return profile["known"] and profile["label"] == label


def _account_choice(accounts: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [{k: a.get(k) for k in ("id", "institution", "currency", "type") if a.get(k)} for a in accounts[:12]]


def _route_for_row(account_id: str, row: Mapping[str, Any], source: str) -> dict[str, Any]:
    institution = row.get("institution")
    kind = kind_for_institution(institution)
    route: dict[str, Any] = {"kind": kind, "account_id": account_id, "institution": str(institution or account_id)[:60],
                             "known": True, "source": source}
    for name in ("currency", "country"):
        if row.get(name):
            route[name] = str(row[name]).upper()
    return _manual_route(route) if kind == "manual" else route


def _manual_route(route: dict[str, Any]) -> dict[str, Any]:
    profile = manual.institution_profile(route["institution"], route.get("country"))
    route.update(label=profile["label"], market=profile["market"])
    route.setdefault("currency", profile["currency"])
    if route["market"] == "MX" and route["currency"] == "USD":
        route["market"] = "US"  # a dollar account at a Mexican broker (GBM "Trading USA") trades US listings in USD
    return route


def _route(store: Any, client_id: str | None, orders: list, snapshot: Mapping[str, Any] | None,
           rationale: Any = "") -> dict[str, Any]:
    """Which broker a ticket goes to: its account's institution (Alpaca, IBKR, or manual for everyone else).

    * A named account: its ledger row, else an ``account.<id>`` fact, picks the institution; the institution
      must be exactly one of the canonical aliases (:func:`~wealth.execution.brokers.base.kind_for_institution`)
      to reach an API broker.  A fact is model-writable, so for Alpaca and IBKR :func:`run_checks` also requires
      the broker's own account state to name the same ledger account (``account_mismatch`` blocks otherwise).
    * No account named: the broker the request names ("compra 10 VOO en GBM"), else the client's single
      brokerage account; more than one candidate raises :class:`AccountNeeded`.  With no brokerage account on
      record at all, Alpaca (paper by default), as before.

    One ticket holds one account.
    """
    named = sorted({str(raw.get("account_id") or raw.get("account")).strip()[:40] for raw in orders
                    if isinstance(raw, Mapping) and (raw.get("account_id") or raw.get("account"))})
    if len(named) > 1:
        raise ValueError(f"orders name {len(named)} accounts ({', '.join(named)}); a ticket holds one account's "
                         "orders, so make one ticket per account")
    accounts: list[Mapping[str, Any]] = []
    if store is not None and client_id:
        try:
            accounts = list(store.ledger(client_id).get("accounts") or [])
        except Exception:  # noqa: BLE001 - an unreadable ledger routes by the account's name below
            accounts = []
    if not named:
        text = " ".join([str(rationale or "")] + [str(raw.get("reason") or "") for raw in orders
                                                   if isinstance(raw, Mapping)])
        mentioned = mentioned_institutions(text)
        trading = [a for a in accounts if a.get("type") in _TRADING_TYPES and a.get("id")]
        if len(mentioned) > 1:
            candidates = [a for a in trading if any(_same_institution(a, label) for label in mentioned)] or trading
            raise AccountNeeded(f"the request names {len(mentioned)} brokers ({', '.join(sorted(mentioned))}); "
                                "set orders[].account_id to the one account this ticket is for",
                                _account_choice(candidates))
        if mentioned:
            label, kind = next(iter(mentioned.items()))
            at = [a for a in trading if _same_institution(a, label)]
            if len(at) == 1:
                return _route_for_row(at[0]["id"], at[0], "named_broker")
            if len(at) > 1:
                raise AccountNeeded(f"there are {len(at)} {label} accounts; set orders[].account_id to one of them",
                                    _account_choice(at))
            if kind == "manual":
                return _manual_route({"kind": "manual", "account_id": None, "institution": label, "known": True,
                                      "source": "named_broker"})
            return {"kind": kind, "account_id": None, "institution": label, "known": True, "source": "named_broker"}
        if len(trading) == 1:
            return _route_for_row(trading[0]["id"], trading[0], "single_account")
        if len(trading) > 1:
            raise AccountNeeded(f"the orders name no account and there are {len(trading)} brokerage accounts; set "
                                "orders[].account_id to the one this ticket is for", _account_choice(trading))
        return {"kind": BROKER, "account_id": None, "institution": "Alpaca", "known": True, "source": "default"}
    account_id = named[0]
    row = next((a for a in accounts if a.get("id") == account_id), None)
    if row is not None:
        return _route_for_row(account_id, row, "ledger")
    for fact in (snapshot or {}).get("facts") or []:
        value = fact.get("value")
        if fact.get("key") in (f"account.{account_id}", account_id) and isinstance(value, Mapping) \
                and value.get("institution"):
            return _route_for_row(account_id, value, "fact")
    hinted = kind_for_account_id(account_id)
    if hinted:
        # "ibkr-4567": only a hint; the broker's own account must be ledger account ibkr-4567 (run_checks).
        return {"kind": hinted, "account_id": account_id, "institution": _LABELS[hinted], "known": True,
                "source": "account_id"}
    if manual.institution_profile(account_id)["known"]:
        return _manual_route({"kind": "manual", "account_id": account_id, "institution": account_id[:60],
                              "known": True, "source": "account_id"})
    # An account Wealth has no record of, whose name says nothing: the default broker, said plainly (a warning on
    # paper; on a live account it blocks).
    return {"kind": BROKER, "account_id": account_id, "institution": "Alpaca", "known": False, "source": "default"}


def _account_needed(exc: AccountNeeded) -> dict[str, Any]:
    """``needs_input``: which account the ticket is for (nothing was stored; never a silent default broker)."""
    return {"status": "needs_input",
            "result": {"accounts": exc.accounts,
                       "next_step": ("Ask the person which account this is for (list these accounts by institution "
                                     "and id), then call order_ticket again with orders[].account_id set.")},
            "missing": [{"key": "orders[].account_id", "reason": "ambiguous", "detail": str(exc)}],
            "warnings": [], "sources": [], "assumptions": []}


def _ttl(kind: str) -> timedelta:
    return MANUAL_TTL if kind == "manual" else TICKET_TTL


def create_ticket(store: Any, client_id: str | None, inputs: Mapping[str, Any], *, snapshot: Mapping[str, Any] | None,
                  environ: Mapping[str, str] | None = None, broker: OrderBroker | None = None,
                  now: datetime | None = None) -> dict[str, Any]:
    """Store a proposed ticket with its checks (``store`` may be ``None`` for a preview without a client)."""
    now = _now(now)
    if not isinstance(inputs, Mapping):
        raise ValueError("order_ticket inputs must be an object")
    errors: list[str] = []  # every schema problem at once, so one retry can fix them all
    unknown = sorted(set(inputs) - {"orders", "rationale", "source"})
    if unknown:
        errors.append(f"order_ticket inputs: unknown {unknown}; expected {{orders, rationale, source}}. "
                      "Tickets are only proposals: the person confirms them in the app.")
    orders = inputs.get("orders")
    if isinstance(orders, Mapping):
        orders = [orders]  # one order given without its list
    if not isinstance(orders, list) or not 1 <= len(orders) <= MAX_ORDERS:
        errors.append(f"orders must be a list of 1-{MAX_ORDERS} orders")
        orders = []
    source = inputs.get("source") or "user_request"
    if source not in SOURCES:
        errors.append(f"source must be one of {', '.join(SOURCES)}")
    rationale = inputs.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        errors.append("rationale is required: one or two sentences the person will read")
    route: dict[str, Any] = {"kind": BROKER, "account_id": None, "institution": "Alpaca", "known": True,
                             "source": "default"}
    if broker is None:
        try:
            route = _route(store, client_id, orders, snapshot, rationale)
        except AccountNeeded as exc:
            if errors:
                raise ValueError("; ".join(errors + [str(exc)])) from None
            return _account_needed(exc)
        except ValueError as exc:
            errors.append(str(exc))
    else:
        try:
            route = _route(store, client_id, orders, snapshot, rationale)
        except ValueError:
            route = {**route, "account_id": next((str(o.get("account_id") or o.get("account"))[:40] for o in orders
                                                  if isinstance(o, Mapping) and (o.get("account_id")
                                                                                 or o.get("account"))), None)}
    if broker is not None:
        route = {**route, "kind": broker.name}
    kind = route["kind"]
    lines = []
    for i, raw in enumerate(orders):
        try:
            lines.append(_normalize_order(raw, i, route))
        except ValueError as exc:
            errors.append(str(exc))
    if errors:
        raise ValueError("; ".join(errors))
    ticket_id = "t" + secrets.token_hex(8)
    unavailable = fingerprint = account_number = None
    if kind == "manual":
        mode = "manual"
    elif kind == "ibkr":
        mode = broker.mode if broker is not None and broker.mode else "paper"
    else:
        mode = broker.mode if broker is not None else trading_mode(environ)
    if broker is None and store is not None:  # a preview without a client stays offline
        broker = broker_for(mode, environ, kind=kind, route=route, store=store, client_id=client_id)
    if kind == "ibkr" and broker is not None:
        try:
            broker.resolve()
            key = _fingerprint_key(store, client_id) if store is not None and client_id else None
            mode, fingerprint, account_number = broker.mode, broker.fingerprint(key), mask(broker.account_id)
        except BrokerError as exc:
            unavailable, broker = _unavailable("ibkr", mode, str(exc)), None
    lines, checks = run_checks(lines, mode=mode, broker=broker, snapshot=snapshot, store=store,
                               client_id=client_id, ticket_id=ticket_id, environ=environ, now=now, kind=kind,
                               unavailable=unavailable, route=route)
    for line in lines:
        line["client_order_id"] = client_order_id(ticket_id, line["index"])
    currency = next((l["currency"] for l in lines if l.get("currency")), "USD")
    nonce = secrets.token_hex(4).upper()
    ticket = {"id": ticket_id, "broker": kind, "broker_label": _label(kind, broker) if broker is not None
              else route.get("label") or _LABELS.get(kind, kind), "route": route, "mode": mode,
              "currency": currency, "status": "pending", "source": source,
              "rationale": rationale.strip()[:600], "created_at": _iso(now), "expires_at": _iso(now + _ttl(kind)),
              "lines": lines, "checks": checks, "nonce": None, "nonce_hash": _nonce_hash(ticket_id, nonce),
              "failures": 0}
    if fingerprint:
        ticket["account_fingerprint"], ticket["account_number"] = fingerprint, account_number
        ticket["fingerprint_v"] = 2
    warnings: list[str] = []
    if store is not None and client_id:
        def update(state: dict) -> dict:
            tickets = dict(state.get("tickets") or {})
            tickets[ticket_id] = ticket
            return {**state, "tickets": _prune(tickets, now)}

        store.update_auxiliary(client_id, "execution", update)
        _audit(store, client_id, ticket, "ticket", {"source": source, "rationale": ticket["rationale"],
                                                    "orders": [dict(o) for o in orders],
                                                    "route": {k: route.get(k) for k in ("kind", "account_id",
                                                                                        "institution")}})
        _audit(store, client_id, ticket, "checks", {"phase": "proposed", "checks": checks,
                                                    "lines": [_line_audit(l) for l in lines]})
        acknowledged = _acknowledged(store.auxiliary(client_id, "execution"), ticket)
    else:
        acknowledged = True
        warnings.append("No client was given, so this ticket is a preview only: it was not stored and cannot be "
                        "confirmed.")
    view = public_ticket(ticket, now=now, live_acknowledged=acknowledged)
    unknown_facts = [c for c in checks if c["status"] == "unknown"]
    if store is None:
        view["id"] = None
    app = in_wealth_app(environ)
    summary = _summary(view, app) if store is not None else "Preview only; nothing was stored or sent."
    label = view["broker_label"]
    if kind == "manual" and app:
        next_step = ("Explain the ticket briefly and tell the person to place it at " + label + " exactly as the "
                     "card shows, then tap 'Ya la puse / I placed it' (or tell you they placed it: then call "
                     + _PLACED_CALL.format(id=view["id"]) + "). Never say it was placed or filled until its line "
                     "state says so.")
    elif kind == "manual":
        next_step = ("Show the person the orders to place at " + label + " (result.ticket.manual.es / .en) exactly "
                     "as listed. When the person tells you they placed it, call " + _PLACED_CALL.format(id=view["id"])
                     + " (it returns needs_person with a code: show it, and only on their yes call again with "
                     "confirm=true and confirmation_code inside inputs). Wealth sends nothing to " + label + ". "
                     "Never say it was placed or filled until its line state says so.")
    else:
        next_step = (("Explain the ticket briefly and tell the person to review and confirm it on the order card."
                      if app else f"Explain the ticket briefly. Tell the person: {_ELSEWHERE}.")
                     + " Never say an order was placed or filled until order status says so.")
    sources = []
    if broker is not None:
        sources = [{"title": f"{label} account, instruments and latest trades", "mode": mode}] if kind != "manual" \
            else [{"title": "Wealth's market-data cache (last closes) and ledger", "mode": mode}]
    assumptions = [f"Limit prices default to within {limits(environ)['collar'] * 50:.1f}% of the last "
                   f"{'close' if kind == 'manual' else 'trade'}; dollar amounts become a quantity at the limit price."]
    assumptions.append("Fees are estimates; the broker's contract governs." if kind == "manual" else
                       "Buys must fit cash buying power; Wealth never uses margin or sells short.")
    return {"status": "partial" if unknown_facts or store is None else "ready",
            "result": {"ticket": view, "summary": summary, "next_step": next_step},
            "missing": [], "warnings": warnings, "sources": sources, "assumptions": assumptions}


def _line_audit(line: Mapping[str, Any]) -> dict[str, Any]:
    return {k: line.get(k) for k in ("index", "symbol", "side", "type", "time_in_force", "qty", "notional",
                                     "order_qty", "limit_price", "last_price", "estimated_amount",
                                     "client_order_id", "exchange", "currency")}


def ticket_status(store: Any, client_id: str, ticket_id: str, *, now: datetime | None = None) -> dict[str, Any]:
    """The stored ticket as the model may see it (no nonce, no broker calls)."""
    state = store.auxiliary(client_id, "execution")
    ticket = (state.get("tickets") or {}).get(ticket_id)
    if ticket is None:
        raise LookupError("That order ticket is no longer there.")
    return public_ticket(ticket, now=now, live_acknowledged=_acknowledged(state, ticket))


def list_tickets(store: Any, client_id: str, *, include_nonce: bool = False, now: datetime | None = None,
                 since_hours: int = 24) -> list[dict[str, Any]]:
    """Recent tickets; with ``include_nonce`` (the card, token holder only) each pending one gets a fresh code.

    Plain codes are never stored, so showing the card again issues a new code and the earlier one stops
    working (a second open page must reload to confirm).
    """
    now = _now(now)
    state = store.auxiliary(client_id, "execution")
    if _settle_expired(state.get("tickets") or {}, now)[1]:  # mark expired tickets on read, once
        state = store.update_auxiliary(client_id, "execution",
                                       lambda current: {**current, "tickets": _prune(dict(current.get("tickets") or {}),
                                                                                     now)})
    horizon = now - timedelta(hours=since_hours)
    rows = [t for t in (state.get("tickets") or {}).values()
            if _parse(t["created_at"]) >= horizon or t["status"] in ("submitting", "submitted", "placed")]
    rows.sort(key=lambda t: t["created_at"])
    codes = _issue_nonces(store, client_id, [t["id"] for t in rows if t["status"] == "pending"], now) \
        if include_nonce else {}
    return [public_ticket(t, include_nonce=include_nonce, now=now, live_acknowledged=_acknowledged(state, t),
                          nonce=codes.get(t["id"])) for t in rows]


# -- confirmation: the only path that submits ------------------------------------------

# Line fields run_checks computes afresh at confirmation.
_REPRICED = frozenset({"limit_price", "order_qty", "last_price", "estimated_amount", "price_note", "estimated_range",
                       "reference_close", "fx", "estimated_fee", "fee_basis", "reference_date"})

def confirm(store: Any, client_id: str, ticket_id: str, *, nonce: Any, override: bool = False,
            typed: Any = None, snapshot: Mapping[str, Any] | None = None, environ: Mapping[str, str] | None = None,
            broker: OrderBroker | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Submit a ticket the person confirmed on its card (a manual ticket: record that they placed it).

    Called only by the web route ``POST /api/orders/<ticket_id>/confirm`` after
    its session-token and local-origin checks.  Raises :class:`ConfirmError`.
    """
    now = _now(now)
    if not isinstance(ticket_id, str) or not _TICKET_ID.match(ticket_id):
        raise ConfirmError("missing", "That order ticket is no longer there.", 404)
    state = store.auxiliary(client_id, "execution")
    ticket = (state.get("tickets") or {}).get(ticket_id)
    if ticket is None:
        raise ConfirmError("missing", "That order ticket is no longer there.", 404)
    kind = ticket.get("broker") or BROKER
    acknowledged = _acknowledged(state, ticket)
    if ticket["status"] not in ("pending", "expired"):
        raise ConfirmError("used", "This ticket was already handled.", 409, public_ticket(ticket, now=now))
    if ticket["status"] == "expired" or _parse(ticket["expires_at"]) <= now:
        raise ConfirmError("expired", "This ticket expired. Ask for a new one.", 410, public_ticket(ticket, now=now))
    if not isinstance(nonce, str) or not ticket.get("nonce_hash") or not secrets.compare_digest(
            _nonce_hash(ticket_id, nonce.strip().upper()), ticket["nonce_hash"]):
        def fail(current: dict) -> dict:
            tickets = dict(current.get("tickets") or {})
            item = dict(tickets[ticket_id])
            item["failures"] = int(item.get("failures", 0)) + 1
            if item["failures"] >= MAX_NONCE_FAILURES:
                item["status"], item["nonce"], item["nonce_hash"] = "void", None, None
            tickets[ticket_id] = item
            return {**current, "tickets": tickets}
        store.update_auxiliary(client_id, "execution", fail)
        _audit(store, client_id, ticket, "nonce_rejected", {"at": _iso(now)})
        raise ConfirmError("nonce", "This confirmation does not match the card. Reload the page.", 403)
    if not isinstance(override, bool):
        raise ConfirmError("invalid", "override must be true or false.", 400)
    mode = ticket["mode"]
    label = _label(kind, ticket=ticket)
    if mode == "live" and not live_enabled(kind, environ):
        raise ConfirmError("live_disabled", "Live trading is turned off on this computer.", 403,
                           public_ticket(ticket, now=now, live_acknowledged=acknowledged))
    typed_ok = isinstance(typed, str) and typed.strip().upper() in TYPED_LIVE
    if mode == "live" and not acknowledged and not typed_ok:
        raise ConfirmError("typed", "Type LIVE (or EN VIVO) to place your first live orders.", 400,
                           public_ticket(ticket, now=now, live_acknowledged=False))

    # Fresh data: every check runs again right before anything is sent, on the same account the card showed.
    unavailable = None
    if broker is None:
        broker, why = _ticket_broker(ticket, environ, store, client_id, existing=False)
        if why == "account_changed":
            _audit(store, client_id, ticket, "blocked", {"reasons": ["account_changed"], "at": _iso(now)})
            raise ConfirmError("account_changed", f"{label} is now logged into a different account than when this "
                               "card was drawn, so nothing was sent. Ask for a new ticket.", 409,
                               public_ticket(ticket, now=now, live_acknowledged=acknowledged))
        if why:
            unavailable = _unavailable(kind, mode, why)
    lines, checks = run_checks([{k: v for k, v in l.items() if k not in _REPRICED} for l in ticket["lines"]],
                               mode=mode, broker=broker, snapshot=snapshot, store=store, client_id=client_id,
                               ticket_id=ticket_id, environ=environ, now=now, kind=kind, unavailable=unavailable,
                               route=ticket.get("route"))
    for line in lines:
        line["client_order_id"] = client_order_id(ticket_id, line["index"])
    _audit(store, client_id, ticket, "checks", {"phase": "confirm", "checks": checks, "override": override,
                                                "lines": [_line_audit(l) for l in lines]})
    blocking = _blocking(checks, override)
    if blocking:
        def refresh_checks(current: dict) -> dict:
            tickets = dict(current.get("tickets") or {})
            tickets[ticket_id] = {**tickets[ticket_id], "lines": lines, "checks": checks}
            return {**current, "tickets": tickets}
        store.update_auxiliary(client_id, "execution", refresh_checks)
        _audit(store, client_id, ticket, "blocked", {"reasons": [c["code"] for c in blocking]})
        fresh = {**ticket, "lines": lines, "checks": checks}
        raise ConfirmError("blocked", blocking[0]["message"], 409,
                           public_ticket(fresh, include_nonce=True, now=now, live_acknowledged=acknowledged,
                                         nonce=nonce.strip().upper()))

    # The tap confirms what the card showed, not the ticket id: if re-pricing moved a line beyond tolerance,
    # nothing is sent; the card shows the new lines and waits for a fresh tap.
    moved = _price_moves(ticket["lines"], lines, config=limits(environ), mode=mode)
    if moved:
        shown = checks + moved

        def reprice(current: dict) -> dict:
            tickets = dict(current.get("tickets") or {})
            if tickets.get(ticket_id, {}).get("status") != "pending":
                raise ConfirmError("used", "This ticket was already handled.", 409)
            tickets[ticket_id] = {**tickets[ticket_id], "lines": lines, "checks": shown}
            return {**current, "tickets": tickets}
        store.update_auxiliary(client_id, "execution", reprice)
        _audit(store, client_id, ticket, "blocked", {
            "reasons": ["price_moved"], "at": _iso(now),
            "moved": [{"line": c["line"], **(c.get("params") or {})} for c in moved]})
        fresh = {**ticket, "lines": lines, "checks": shown}
        raise ConfirmError("price_moved", "The price moved since this card was shown, so nothing was sent. Review "
                           "the new amounts and tap again to place them.", 409,
                           public_ticket(fresh, include_nonce=True, now=now, live_acknowledged=acknowledged,
                                         nonce=nonce.strip().upper()))

    violations = [c for c in checks if c["status"] == "violation"]
    record = {"at": _iso(now), "codes": sorted({c.get("params", {}).get("rule", c["code"]) for c in violations})} \
        if violations and override else None
    if kind == "manual":
        return _mark_placed(store, client_id, ticket, lines, checks, record, now)

    # Claim the ticket atomically so two taps cannot both submit.
    notional = _money(sum((Decimal(l["estimated_amount"]) for l in lines if l.get("estimated_amount")), Decimal(0)))
    today_et = now.astimezone(_EASTERN).date().isoformat()
    config = limits(environ)
    legacy = Decimal(0)
    if mode == "live":  # confirms from before reservations existed; they cannot grow any more
        legacy = _legacy_confirmed_today(store, client_id, today_et,
                                         store.auxiliary(client_id, "execution").get("live_reserved") or {})

    def claim(current: dict) -> dict:
        # One write transaction (BEGIN IMMEDIATE): claim the ticket AND, for live orders, re-check and reserve
        # its notional against the daily limit, so two confirmations racing each other cannot both fit.
        tickets = dict(current.get("tickets") or {})
        item = tickets.get(ticket_id)
        if item is None or item["status"] != "pending":
            raise ConfirmError("used", "This ticket was already handled.", 409)
        result = dict(current)
        if mode == "live":
            used = _reserved_today(current, today_et) + legacy
            if used + notional > config["max_daily"]:
                raise ConfirmError("blocked", f"This would bring today's live orders to USD {_money(used + notional)}, "
                                   f"above the daily limit of USD {config['max_daily']}.", 409)
            result["live_reserved"] = {
                **{k: v for k, v in (current.get("live_reserved") or {}).items()
                   if v.get("date_et", today_et) >= (now - timedelta(days=7)).astimezone(_EASTERN).date().isoformat()},
                ticket_id: {"date_et": today_et, "notional": _s(notional), "at": _iso(now)}}
        tickets[ticket_id] = {**item, "status": "submitting", "lines": lines, "checks": checks,
                              "nonce": None, "nonce_hash": None, "confirmed_at": _iso(now), "override": record}
        result["tickets"] = tickets
        if mode == "live" and not acknowledged:
            result["live_ack"] = {**(current.get("live_ack") or {}), kind: _iso(now)}
        return result

    try:
        store.update_auxiliary(client_id, "execution", claim)
    except ConfirmError as exc:
        if exc.kind != "blocked":
            raise
        _audit(store, client_id, ticket, "blocked", {"reasons": ["live_daily_limit"], "at": _iso(now)})
        fresh = {**ticket, "lines": lines, "checks": checks + [_check("live_daily_limit", "block", str(exc))]}
        raise ConfirmError("blocked", str(exc), 409,
                           public_ticket(fresh, include_nonce=True, now=now, live_acknowledged=acknowledged,
                                         nonce=nonce.strip().upper())) from None
    _IN_FLIGHT.add(ticket_id)
    ticket = {**ticket, "lines": lines, "checks": checks, "override": record}
    submitted: list[dict] = []
    sent_any = False
    try:
        if mode == "live" and not acknowledged:
            _audit(store, client_id, ticket, "live_acknowledged", {"typed": True, "at": _iso(now)})
        _audit(store, client_id, ticket, "confirm", {
            "at": _iso(now), "date_et": today_et, "notional": _s(notional), "reserved": mode == "live",
            "override": record, "lines": [_line_audit(l) for l in lines]})
        for line in lines:
            line = dict(line)
            body = {"symbol": line["symbol"], "qty": line["order_qty"], "side": line["side"], "type": line["type"],
                    "time_in_force": line["time_in_force"], "client_order_id": line["client_order_id"]}
            if line["type"] == "limit":
                body["limit_price"] = line["limit_price"]
            broker.audit = lambda kind_, payload, _line=line: _audit(store, client_id, ticket, kind_, payload, _line)
            try:
                sent_any = True
                order = broker.submit(body)
            except BrokerError as exc:
                line["state"] = "unknown" if exc.retryable else "failed"
                line["reason"] = str(exc)[:400]
                if exc.kind == "reply_blocked":
                    _audit(store, client_id, ticket, "reply_blocked", {"body": exc.body, "at": _iso(now)}, line)
            else:
                line["broker_order_id"] = order.get("id") or None
                line["broker_status"] = order.get("status")
                line["state"] = order.get("state") or "sent"
                line["filled_qty"] = order.get("filled_qty")
                line["filled_avg_price"] = order.get("filled_avg_price")
                line["submitted_at"] = order.get("submitted_at") or _iso(now)
            finally:
                broker.audit = None
            submitted.append(line)
    except BaseException:
        # Interrupted mid-submit.  If nothing was sent, the ticket failed cleanly and gives its reservation back;
        # otherwise it stays "submitting", marked interrupted, and refresh() reconciles it by client_order_id.
        def interrupted(current: dict) -> dict:
            items = dict(current.get("tickets") or {})
            item = dict(items[ticket_id])
            if not sent_any:
                failed = [{**l, "state": "failed", "reason": f"Submission stopped before anything reached {label}."}
                          for l in lines]
                item.update(status="done", lines=failed)
                current = _release(current, ticket_id, failed)
            else:
                known = {l["index"]: l for l in submitted}
                item.update(interrupted=_iso(now), lines=[known.get(l["index"], l) for l in lines])
            items[ticket_id] = item
            return {**current, "tickets": items}
        try:
            store.update_auxiliary(client_id, "execution", interrupted)
        except Exception:
            pass  # the lease on "submitting" still lets refresh() reconcile it
        raise
    finally:
        _IN_FLIGHT.discard(ticket_id)

    def finish(current: dict) -> dict:
        tickets = dict(current.get("tickets") or {})
        tickets[ticket_id] = {**tickets[ticket_id], "status": "submitted", "lines": submitted}
        return _release({**current, "tickets": tickets}, ticket_id, submitted)

    store.update_auxiliary(client_id, "execution", finish)
    ticket = {**ticket, "status": "submitted", "lines": submitted}
    if any(l.get("filled_qty") not in (None, "0", 0) for l in submitted):
        refresh(store, client_id, [ticket_id], broker=broker, environ=environ, now=now)
    return ticket_status(store, client_id, ticket_id, now=now)


def _mark_placed(store: Any, client_id: str, ticket: Mapping[str, Any], lines: list[dict], checks: list[dict],
                 record: dict | None, now: datetime, *, via: str = "app",
                 statuses: tuple[str, ...] = ("pending",)) -> dict[str, Any]:
    """The person says they placed a manual ticket ("Ya la puse"): its orders wait for a statement to show them.

    ``via`` is ``app`` (the card's tap) or ``mcp`` (the person told a chat host, through the consent gate);
    either way the ticket is ``verified: false`` until reconciliation matches it with a statement or sync.
    """
    ticket_id = ticket["id"]
    awaiting = [{**l, "state": "awaiting"} for l in lines]

    def claim(current: dict) -> dict:
        tickets = dict(current.get("tickets") or {})
        item = tickets.get(ticket_id)
        if item is None or item["status"] not in statuses or item.get("broker") != "manual":
            raise ConfirmError("used", "This ticket was already handled.", 409)
        tickets[ticket_id] = {**item, "status": "placed", "lines": awaiting, "checks": checks, "nonce": None,
                              "nonce_hash": None, "placed_at": _iso(now), "override": record, "placed_via": via,
                              "verified": False}
        return {**current, "tickets": tickets}

    store.update_auxiliary(client_id, "execution", claim)
    _audit(store, client_id, ticket, "placed_manually", {"at": _iso(now), "override": record, "via": via,
                                                         "lines": [_line_audit(l) for l in awaiting]})
    refresh(store, client_id, [ticket_id], now=now)  # a statement already in the ledger may confirm it at once
    return ticket_status(store, client_id, ticket_id, now=now)


def placeable_manually(ticket: Mapping[str, Any] | None) -> bool:
    """Whether a stored ticket is a place-it-yourself ticket the person can still mark placed."""
    return bool(ticket) and ticket.get("broker") == "manual" and ticket.get("status") in ("pending", "expired")


def mark_placed(store: Any, client_id: str, ticket_id: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """Record that the person placed a place-it-yourself ticket, told through a chat host (``placed_via: mcp``).

    Reached only through ``wealth_run task=order_ticket inputs {ticket_id, placed: true}``, whose MCP tool
    gates it on the person's own words (in a Wealth turn) or on the two-step ``needs_person`` code (elsewhere).
    It never talks to a broker and refuses any ticket Wealth would send (Alpaca, IBKR): it only marks a manual
    ticket's orders ``awaiting`` a statement, like the card's "Ya la puse".  A card that expired is accepted
    (the person may say so after the card's hour); the checks are the ones the card last showed.
    """
    now = _now(now)
    if not isinstance(ticket_id, str) or not _TICKET_ID.match(ticket_id):
        raise ValueError("ticket_id is not an order ticket id")
    state = store.auxiliary(client_id, "execution")
    ticket = (state.get("tickets") or {}).get(ticket_id)
    if ticket is None:
        raise ValueError("unknown ticket_id for this client")
    if ticket.get("broker") != "manual":
        raise ValueError(f"ticket {ticket_id} is a {_label(ticket.get('broker') or BROKER, ticket=ticket)} ticket that "
                         "Wealth places itself; only the person's tap on its card in the Wealth app can place it, "
                         "and nothing here sends orders")
    if not placeable_manually(ticket):
        raise ValueError(f"ticket {ticket_id} is {ticket.get('status')}, not waiting to be placed")
    checks = list(ticket.get("checks") or [])
    violations = [c for c in checks if c["status"] == "violation"]
    record = {"at": _iso(now), "codes": sorted({c.get("params", {}).get("rule", c["code"]) for c in violations}),
              "via": "mcp"} if violations else None
    try:
        return _mark_placed(store, client_id, ticket, [dict(l) for l in ticket["lines"]], checks, record, now,
                            via="mcp", statuses=("pending", "expired"))
    except ConfirmError as exc:
        raise ValueError(str(exc)) from None


# -- status, fills and cancellation --------------------------------------------------------

def _update_line(line: dict, order: Mapping[str, Any]) -> dict:
    line = dict(line)
    line["broker_order_id"] = order.get("id") or line.get("broker_order_id") or None
    line["broker_status"] = order.get("status")
    line["state"] = order.get("state") or line.get("state", "sent")
    line["filled_qty"] = order.get("filled_qty")
    line["filled_avg_price"] = order.get("filled_avg_price")
    return line


def post_fills(store: Any, client_id: str, ticket: Mapping[str, Any], line: Mapping[str, Any],
               broker: OrderBroker, now: datetime) -> tuple[list[str], Decimal]:
    """Post this order's new fills to the ledger; returns ``(fill ids, quantity)``.

    The ledger skips a fill it already holds under the same external id, e.g. one
    the read-only connector synced first.
    """
    from .. import ledger as ledger_module

    order_id = line.get("broker_order_id")
    if not order_id or not broker.posts_fills:
        return [], Decimal(0)
    after = (_parse(line["submitted_at"]) - timedelta(days=1)).date().isoformat() if line.get("submitted_at") else None
    # Every page; a read error raises BrokerError to the caller, which keeps the line open and tries again later:
    # an unreadable page is never "no fills".
    activities = broker.fills(after)
    fills = [a for a in activities if a.get("external_id") and (
        str(a.get("order_id")) == order_id or (a.get("client_order_id") and
                                               a["client_order_id"] == line.get("client_order_id")))]
    posted = set(line.get("posted_fills") or [])
    fresh = [a for a in fills if a["external_id"] not in posted]
    if not fresh:
        return [], Decimal(0)
    account_row = broker.ledger_account()
    account_id = account_row["id"]
    ledger = store.ledger(client_id)
    existing_account = next((a for a in ledger.get("accounts", []) if a.get("id") == account_id), None)
    account_row = {k: v for k, v in (existing_account or account_row).items() if k not in ("updated_at",)}
    symbol = line["symbol"]
    instrument = next((i for i in ledger.get("instruments", []) if i.get("id") == symbol), None) or \
        {"id": symbol, "symbol": symbol, "currency": "USD"}
    transactions = []
    for fill in fresh:
        qty, price = _opt(fill.get("qty")), _opt(fill.get("price"))
        when = fill.get("date")
        if qty is None or price is None or qty <= 0 or not when:
            continue
        gross = _money(qty * price)
        side = "buy" if str(fill.get("side") or line["side"]).lower() in ("buy", "b") else "sell"
        transactions.append({"kind": side, "account_id": account_id, "date": when, "instrument_id": instrument["id"],
                             "quantity": _s(qty), "price": _s(price), "currency": "USD",
                             "amount": _s(-gross if side == "buy" else gross), "external_id": fill["external_id"],
                             "description": f"{broker.label} {side} {_s(qty)} {symbol} (order {order_id[:8]})"})
    if not transactions:
        return [], Decimal(0)
    ids = sorted(t["external_id"] for t in transactions)
    batch = {"batch_id": EXECUTION_BATCH_PREFIX + hashlib.sha256("|".join(ids).encode()).hexdigest()[:24],
             "source": {"kind": "tool", "ref": f"{broker.name}-orders:{ticket['mode']}:{order_id}",
                        "observed_on": now.astimezone(_EASTERN).date().isoformat()},
             "confidence": "reported", "accounts": [account_row], "instruments": [instrument],
             "transactions": transactions}
    receipt = ledger_module.post(store, client_id, batch)
    _audit(store, client_id, ticket, "fill_posted", {"fill_ids": ids, "posted": len(receipt["posted"]),
                                                     "duplicates": len(receipt["duplicates"]),
                                                     "held": len(receipt["held"]), "account_id": account_id}, line)
    return ids, sum((Decimal(t["quantity"]) for t in transactions), Decimal(0))


def _reconcile_manual(store: Any, client_id: str, ticket: Mapping[str, Any], claimed: set[str],
                      now: datetime) -> tuple[list[dict], str]:
    """Match a placed manual ticket's lines with ledger entries from a later statement or sync.

    An entry is a candidate for a line when it is the same side and symbol, in the ticket's account (or, when the
    ticket named no account Wealth knows, any account at the same institution), dated from the day before the tap
    on, and not already claimed by another line.  An entry for more than the line's quantity (beyond
    :data:`MANUAL_QTY_TOLERANCE`) is a different trade: it never fills the line.  One entry for the line's quantity
    is preferred (the closest wins); otherwise smaller entries add up (partial fills) without overshooting.
    Within the tolerance of the quantity the line is ``filled``; below it, ``partial``.  Past
    :data:`MANUAL_CONFIRM_DAYS` a line with no match is ``unconfirmed`` and a partial one ``partial_unconfirmed``
    (never ``filled``).  Returns ``(lines, status)``.
    """
    route = ticket.get("route") or {}
    placed = _parse(ticket.get("placed_at") or ticket["created_at"])
    earliest = (placed - timedelta(days=1)).date().isoformat()
    try:
        ledger = store.ledger(client_id)
    except Exception:  # noqa: BLE001
        return list(ticket["lines"]), "placed"
    accounts = ledger.get("accounts") or []
    if route.get("account_id") and any(a.get("id") == route["account_id"] for a in accounts):
        wanted = {route["account_id"]}
    else:
        label = route.get("label") or ticket.get("broker_label")
        wanted = {a["id"] for a in accounts
                  if manual.institution_profile(a.get("institution"), a.get("country"))["label"] == label}
    symbols = {i.get("id"): i.get("symbol") or i.get("id") for i in ledger.get("instruments") or []}
    entries = sorted((e for e in ledger.get("entries") or [] if e.get("account_id") in wanted
                      and e.get("kind") in ("buy", "sell") and str(e.get("date") or "") >= earliest),
                     key=lambda e: (e.get("date") or "", e.get("seq") or 0))
    expired = now - placed > timedelta(days=MANUAL_CONFIRM_DAYS)
    lines, open_left = [], False
    for line in ticket["lines"]:
        line = dict(line)
        if line.get("state") not in ("awaiting", "partial"):
            lines.append(line)
            continue
        want = _opt(line.get("order_qty") or line.get("qty"))
        ceiling = want * (1 + MANUAL_QTY_TOLERANCE) if want else None
        ids = list(line.get("confirmed_by") or [])
        candidates, oversized = [], []
        for entry in entries:
            key = str(entry.get("external_id") or entry.get("id") or entry.get("seq"))
            if key in claimed and key not in ids:
                continue
            if entry["kind"] != line["side"]:
                continue
            if manual.symbol_key(symbols.get(entry.get("instrument_id"), entry.get("instrument_id"))) \
                    != manual.symbol_key(line["symbol"]):
                continue
            qty = abs(_opt(entry.get("quantity")) or Decimal(0))
            if qty <= 0:
                continue
            if ceiling is not None and qty > ceiling and key not in ids:
                oversized.append((key, entry, qty))
                continue
            candidates.append((key, entry, qty))
        chosen: list[tuple[str, Mapping[str, Any], Decimal]] = [c for c in candidates if c[0] in ids]
        got = sum((c[2] for c in chosen), Decimal(0))
        if want and not chosen:
            # One entry for the whole quantity beats several pieces; the closest to the quantity wins.
            whole = [c for c in candidates if c[2] >= want * (1 - MANUAL_QTY_TOLERANCE)]
            if whole:
                chosen = [min(whole, key=lambda c: abs(c[2] - want))]
                got = chosen[0][2]
        for candidate in candidates:
            if candidate in chosen:
                continue
            if want and got >= want * (1 - MANUAL_QTY_TOLERANCE):
                break
            if ceiling is not None and got + candidate[2] > ceiling:
                continue  # would overshoot the order: another trade
            chosen.append(candidate)
            got += candidate[2]
        cost = Decimal(0)
        for key, entry, qty in chosen:
            price = _opt(entry.get("price"))
            cost += qty * price if price is not None else abs(_opt(entry.get("amount")) or Decimal(0))
            if key not in ids:
                ids.append(key)
                claimed.add(key)
        if oversized and not chosen:
            line["mismatch"] = [{"entry": key, "qty": _s(qty), "date": entry.get("date")}
                                for key, entry, qty in oversized[:3]]
            line["reason"] = (f"A statement shows a {line['side']} of {_s(oversized[0][2])} {line['symbol']}, more "
                              f"than this order's {_s(want)}; it was not counted as this order.")
        if got > 0:
            line.pop("mismatch", None)
            line["confirmed_by"] = ids
            line["filled_qty"] = _s(got)
            line["filled_avg_price"] = _s(_tick(cost / got, ROUND_DOWN)) if cost else None
            line["state"] = "filled" if not want or got >= want * (1 - MANUAL_QTY_TOLERANCE) else "partial"
            if line["state"] == "partial":
                line["reason"] = f"{_s(got)} of {_s(want)} shown so far."
            else:
                line.pop("reason", None)
        if line["state"] in ("awaiting", "partial") and expired:
            line["state"] = "unconfirmed" if got == 0 else "partial_unconfirmed"
            line["reason"] = (f"No statement or sync showed this trade within {MANUAL_CONFIRM_DAYS} days."
                              if got == 0 else f"Only {_s(got)} of {_s(want)} showed up within "
                              f"{MANUAL_CONFIRM_DAYS} days; the rest may not have filled.")
        if line["state"] in ("awaiting", "partial"):
            open_left = True
        lines.append(line)
    return lines, "placed" if open_left else "done"


def _missing_for_good(line: Mapping[str, Any], now: datetime) -> bool:
    """Whether an IBKR line is absent from enough listings to call it not placed (two reads, a lease apart)."""
    since = line.get("missing_since")
    return bool(since) and int(line.get("missing_reads") or 0) >= 1 and _parse(since) + SUBMIT_LEASE <= now


def refresh(store: Any, client_id: str, ticket_ids: list[str] | None = None, *, broker: OrderBroker | None = None,
            environ: Mapping[str, str] | None = None, now: datetime | None = None) -> list[dict[str, Any]]:
    """Read open orders' status from the broker, post new fills, reconcile manual tickets; return the tickets."""
    now = _now(now)
    state = store.auxiliary(client_id, "execution")
    tickets = state.get("tickets") or {}

    def interrupted(ticket: Mapping[str, Any]) -> bool:
        """A "submitting" ticket nobody is submitting any more: a crash or kill mid-submit."""
        if ticket["status"] != "submitting" or ticket["id"] in _IN_FLIGHT:
            return False
        started = ticket.get("confirmed_at")
        return bool(ticket.get("interrupted")) or not started or _parse(started) + SUBMIT_LEASE <= now

    # Manual tickets: match the orders the person placed with the ledger (statement uploads and connector syncs).
    placed = [t for tid, t in tickets.items() if (ticket_ids is None or tid in ticket_ids) and t["status"] == "placed"]
    if placed:
        claimed = {key for t in tickets.values() if t.get("broker") == "manual" for l in t.get("lines") or []
                   for key in l.get("confirmed_by") or []}
        for ticket in sorted(placed, key=lambda t: t.get("placed_at") or t["created_at"]):
            lines, status = _reconcile_manual(store, client_id, ticket, claimed, now)
            if lines == ticket["lines"] and status == ticket["status"]:
                continue
            _audit(store, client_id, ticket, "reconciled", {"status": status, "lines": [
                {"index": l["index"], "state": l.get("state"), "confirmed_by": l.get("confirmed_by")} for l in lines]})

            def settle(current: dict, _tid=ticket["id"], _lines=lines, _status=status) -> dict:
                items = dict(current.get("tickets") or {})
                if _tid in items and items[_tid]["status"] == "placed":
                    # verified: a statement or sync showed every order (until then the person's word only)
                    items[_tid] = {**items[_tid], "lines": _lines, "status": _status, "refreshed_at": _iso(now),
                                   "verified": all(l.get("state") == "filled" for l in _lines)}
                return {**current, "tickets": items}
            store.update_auxiliary(client_id, "execution", settle)

    targets = [t for tid, t in tickets.items() if (ticket_ids is None or tid in ticket_ids)
               and t.get("broker", BROKER) != "manual" and (t["status"] == "submitted" or interrupted(t))]
    clients: dict[tuple, OrderBroker | None] = {}
    for ticket in targets:
        kind, mode = ticket.get("broker") or BROKER, ticket["mode"]
        key = (kind, mode, ticket.get("account_fingerprint"))
        if key not in clients:
            if broker is not None and broker.name == kind and broker.mode == mode:
                clients[key] = broker
            else:
                clients[key], _ = _ticket_broker(ticket, environ, store, client_id, existing=True)
        api = clients[key]
        if api is None:
            continue
        label = _label(kind, api)
        lines = []
        reconciling = ticket["status"] == "submitting"
        unresolved = False
        for line in ticket["lines"]:
            line = dict(line)
            filled = _opt(line.get("filled_qty")) or Decimal(0)
            needs_fills = api.posts_fills and filled > (_opt(line.get("posted_qty")) or Decimal(0))
            if line.get("state") not in _FINAL or line.get("state") == "unknown" or needs_fills:
                api.audit = lambda kind_, payload, _line=line: _audit(store, client_id, ticket, kind_, payload, _line)
                try:
                    if line.get("broker_order_id"):
                        order = api.order(line["broker_order_id"])
                    else:
                        order = api.order_by_client_id(line["client_order_id"])
                    if order:
                        line = _update_line(line, order)
                        line.setdefault("submitted_at", order.get("submitted_at") or _iso(now))
                        line.pop("missing_since", None)
                        line.pop("missing_reads", None)
                    elif line.get("state") in ("unknown", "proposed") and kind == "ibkr" \
                            and not _missing_for_good(line, now):
                        # IBKR's listing is per gateway session and can lag: one silent listing is never "not
                        # placed".  The line stays unknown until two refreshes, SUBMIT_LEASE apart, miss it.
                        line["missing_since"] = line.get("missing_since") or _iso(now)
                        line["missing_reads"] = int(line.get("missing_reads") or 0) + 1
                        line["state"] = "unknown"
                        line["reason"] = (f"{label}'s order list does not show this order yet; Wealth checks "
                                          "again on the next refresh.")
                    elif line.get("state") in ("unknown", "proposed"):
                        # looked up by its client_order_id: the broker never saw it (lost response, or the process
                        # stopped before sending it)
                        line["state"] = "failed"
                        line["reason"] = f"{label} has no record of this order; it was not placed."
                except (BrokerError, ValueError):
                    unresolved = unresolved or line.get("state") == "proposed"
                finally:
                    api.audit = None
                if api.posts_fills and (_opt(line.get("filled_qty")) or Decimal(0)) > \
                        (_opt(line.get("posted_qty")) or Decimal(0)):
                    api.audit = lambda kind_, payload, _line=line: _audit(store, client_id, ticket, kind_, payload,
                                                                          _line)
                    try:
                        ids, qty = post_fills(store, client_id, ticket, line, api, now)
                    except (BrokerError, ValueError) as exc:
                        line["fills_error"] = str(exc)[:200]  # retried on the next refresh
                        _audit(store, client_id, ticket, "error", {"action": "fills", "error": str(exc)[:200]},
                               line)
                    else:
                        line.pop("fills_error", None)
                        if ids:
                            line["posted_fills"] = sorted(set(line.get("posted_fills") or []) | set(ids))
                            line["posted_qty"] = _s((_opt(line.get("posted_qty")) or Decimal(0)) + qty)
                    finally:
                        api.audit = None
            lines.append(line)
        # Done only when every order is final AND (where Wealth posts fills) every filled share is in the ledger.
        done = not unresolved and all(
            l.get("state") in _FINAL
            and (not api.posts_fills
                 or (_opt(l.get("posted_qty")) or Decimal(0)) >= (_opt(l.get("filled_qty")) or Decimal(0)))
            for l in lines)
        status = "done" if done else ("submitting" if unresolved and reconciling else "submitted")
        if reconciling:
            _audit(store, client_id, ticket, "status", {"action": "reconcile_interrupted", "status": status,
                                                        "lines": [{"index": l["index"], "state": l.get("state")}
                                                                  for l in lines]})

        def update(current: dict, _tid=ticket["id"], _lines=lines, _status=status) -> dict:
            items = dict(current.get("tickets") or {})
            if _tid in items:
                items[_tid] = {**items[_tid], "lines": _lines, "status": _status, "refreshed_at": _iso(now)}
                current = _release(current, _tid, _lines)
            return {**current, "tickets": items}

        store.update_auxiliary(client_id, "execution", update)
    return list_tickets(store, client_id, now=now)


def cancel(store: Any, client_id: str, target: str, *, environ: Mapping[str, str] | None = None,
           broker: OrderBroker | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Discard a pending ticket (nothing was sent), withdraw a manual ticket the person did not place after all,
    or cancel one of its submitted orders at the broker."""
    now = _now(now)
    state = store.auxiliary(client_id, "execution")
    tickets = state.get("tickets") or {}
    if isinstance(target, str) and _TICKET_ID.match(target):
        ticket = tickets.get(target)
        if ticket is None:
            raise ConfirmError("missing", "That order ticket is no longer there.", 404)
        if ticket["status"] == "expired":
            return ticket_status(store, client_id, target, now=now)  # nothing was sent; nothing to discard
        if ticket["status"] == "placed":
            def withdraw(current: dict) -> dict:
                items = dict(current.get("tickets") or {})
                item = items[target]
                items[target] = {**item, "status": "done", "lines": [
                    {**l, "state": "canceled", "reason": "The person said this order was not placed."}
                    if l.get("state") in ("awaiting", "partial") and not l.get("confirmed_by") else l
                    for l in item["lines"]]}
                return {**current, "tickets": items}

            store.update_auxiliary(client_id, "execution", withdraw)
            _audit(store, client_id, ticket, "withdrawn", {"at": _iso(now)})
            return ticket_status(store, client_id, target, now=now)
        if ticket["status"] != "pending":
            raise ConfirmError("used", "This ticket was already handled.", 409)

        def discard(current: dict) -> dict:
            items = dict(current.get("tickets") or {})
            items[target] = {**items[target], "status": "discarded", "nonce": None, "nonce_hash": None}
            return {**current, "tickets": items}

        store.update_auxiliary(client_id, "execution", discard)
        _audit(store, client_id, ticket, "discarded", {"at": _iso(now)})
        return ticket_status(store, client_id, target, now=now)
    for ticket in tickets.values():
        for line in ticket["lines"]:
            if line.get("broker_order_id") and line["broker_order_id"] == target:
                kind = ticket.get("broker") or BROKER
                if kind == "manual":
                    raise ConfirmError("manual", "Cancel it at your broker.", 409)
                if line.get("state") in _FINAL:
                    raise ConfirmError("final", "This order is already final.", 409)
                why = None
                if broker is not None and broker.name == kind and broker.mode == ticket["mode"]:
                    api = broker
                else:
                    api, why = _ticket_broker(ticket, environ, store, client_id, existing=True)
                if api is None:
                    message = {"alpaca": "Alpaca keys are not configured.",
                               "ibkr": "The IBKR gateway is not reachable or is logged into another account."}
                    raise ConfirmError("broker", message.get(kind, why or "The broker is not reachable."), 503)
                api.audit = lambda kind_, payload, _line=line: _audit(store, client_id, ticket, kind_, payload, _line)
                try:
                    api.cancel(target)
                except BrokerError as exc:
                    raise ConfirmError("broker", str(exc)[:200], 502) from None
                finally:
                    api.audit = None
                _audit(store, client_id, ticket, "cancel", {"at": _iso(now)}, line)
                refresh(store, client_id, [ticket["id"]], broker=api, environ=environ, now=now)
                return ticket_status(store, client_id, ticket["id"], now=now)
    raise ConfirmError("missing", "That order is not one Wealth placed.", 404)


def execution_status(store: Any, client_id: str | None, *, environ: Mapping[str, str] | None = None,
                     now: datetime | None = None) -> dict[str, Any]:
    """Mode, opt-in, whether keys exist (never the keys), limits and today's live usage; reads no broker."""
    now = _now(now)
    environ = os.environ if environ is None else environ
    config = limits(environ)
    mode = trading_mode(environ)
    tls = ibkr_gateway.TlsPolicy.from_env(environ)
    result: dict[str, Any] = {
        "broker": BROKER, "mode": mode, "live_opt_in": mode == "live",
        "credentials": {m: alpaca_orders.load_keys(m, environ) is not None for m in ("paper", "live")},
        "brokers": {
            "alpaca": {"mode": mode, "live_opt_in": mode == "live",
                       "credentials": {m: alpaca_orders.load_keys(m, environ) is not None for m in ("paper", "live")}},
            "ibkr": {"gateway": f"https://localhost:{ibkr_gateway.gateway_port(environ)}/v1/api",
                     "mode": "the logged-in account (DU... is paper)",
                     "live_opt_in": ibkr_gateway.live_opted_in(environ), "tls": tls.describe(),
                     "credentials": "the gateway session; Wealth stores none"},
            "manual": {"submission": "The person places the order at their broker and taps 'Ya la puse' (or, in "
                                     "another chat host, says so and order_ticket {ticket_id, placed: true} "
                                     "records it through the consent code); a later statement or sync confirms "
                                     "it."},
        },
        "limits": {"per_order_usd": _s(config["max_order"]), "daily_usd": _s(config["max_daily"]),
                   "collar": _s(config["collar"]), "applies_to": "live"},
        "ticket_minutes": int(TICKET_TTL.total_seconds() // 60),
        "submission": "Only the person, on the order card in the Wealth app, can place orders.",
    }
    if store is not None and client_id:
        state = store.auxiliary(client_id, "execution")
        result["live_acknowledged"] = bool((state.get("live_ack") or {}).get(BROKER))
        result["brokers"]["ibkr"]["live_acknowledged"] = bool((state.get("live_ack") or {}).get("ibkr"))
        result["live_used_today_usd"] = _s(_money(_live_used_today(store, client_id, now)))
        result["pending_tickets"] = sum(1 for t in (state.get("tickets") or {}).values()
                                        if t["status"] == "pending" and _parse(t["expires_at"]) > now)
        result["awaiting_statement"] = sum(1 for t in (state.get("tickets") or {}).values() if t["status"] == "placed")
    return result


__all__ = ["AccountNeeded", "BROKER", "ConfirmError", "MANUAL_TTL", "SOURCES", "TICKET_TTL", "broker_for", "cancel",
           "client_order_id", "confirm", "create_ticket", "execution_status", "limits", "list_tickets",
           "live_enabled", "manual_instructions", "post_fills", "public_ticket", "refresh", "run_checks",
           "ticket_status", "trading_mode", "mark_placed", "placeable_manually"]
