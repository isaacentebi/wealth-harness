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

Paper trading is the default.  Live trading needs ``WEALTH_TRADING_LIVE=alpaca``
in the server's environment, a typed confirmation the first time, and stays
under per-order and daily notional limits (USD 1,000 and 5,000 by default;
``WEALTH_TRADING_MAX_ORDER_USD`` and ``WEALTH_TRADING_MAX_DAILY_USD``).
Broker facts are cited in :mod:`wealth.execution.brokers.alpaca_orders`.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from ..connectors.alpaca import _external_id  # the read connector's FILL ids, so a later sync dedupes
from ..ingest_posting import EXECUTION_BATCH_PREFIX  # so a first connector sync still posts opening balances
from .brokers import alpaca_orders
from .brokers.alpaca_orders import AlpacaOrders, BrokerError

BROKER = "alpaca"
TICKET_TTL = timedelta(minutes=10)
SOURCES = ("rebalance", "manager_mirror", "user_request")
MAX_ORDERS = 20
MAX_NONCE_FAILURES = 5
KEEP_TICKETS = 60
DEFAULT_MAX_ORDER = Decimal("1000")
DEFAULT_MAX_DAILY = Decimal("5000")
DEFAULT_COLLAR = Decimal("0.01")      # a limit may sit at most 1% through the last price
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
    "domicile", "tags", "reason", "lots", "tax_regime",
})
_LINE_STATE = {
    "new": "sent", "accepted": "sent", "pending_new": "sent", "accepted_for_bidding": "sent", "calculated": "sent",
    "held": "sent", "pending_replace": "sent", "pending_cancel": "sent", "done_for_day": "sent", "stopped": "sent",
    "suspended": "sent", "partially_filled": "partial", "filled": "filled", "canceled": "canceled",
    "expired": "expired", "rejected": "rejected", "replaced": "canceled",
}
_FINAL = frozenset({"filled", "canceled", "expired", "rejected", "failed"})


class ConfirmError(Exception):
    """Why a confirmation or cancellation did not go through; ``status`` is the HTTP status."""

    def __init__(self, kind: str, message: str, status: int = 409, ticket: dict | None = None):
        super().__init__(message)
        self.kind, self.status, self.ticket = kind, status, ticket


# -- configuration ---------------------------------------------------------------

def trading_mode(environ: Mapping[str, str] | None = None) -> str:
    """'live' only when ``WEALTH_TRADING_LIVE`` names this broker; paper otherwise."""
    environ = os.environ if environ is None else environ
    opted = {part.strip().lower() for part in (environ.get(LIVE_ENV) or "").split(",")}
    return "live" if BROKER in opted else "paper"


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


def broker_for(mode: str, environ: Mapping[str, str] | None = None) -> AlpacaOrders | None:
    """The broker client for ``mode`` when keys exist; the base URL follows the mode, never an input."""
    return alpaca_orders.client(mode, environ=environ)


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


def _normalize_order(raw: Any, index: int) -> dict[str, Any]:
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
    symbol = order_symbol(raw.get("symbol") or raw.get("instrument_id"))
    if symbol is None:
        errors.append(f"{field}.symbol must be a US ticker such as VTI or BRK.B")
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
    for name in ("estimated_tax", "estimated_cost"):
        value = _opt(raw.get(name))
        if value is not None:
            line[name] = _s(_money(abs(value)))
    for name in ("asset_class", "sleeve", "domicile"):
        if isinstance(raw.get(name), str):
            line[name] = raw[name][:40]
    if isinstance(raw.get("tags"), list):
        line["tags"] = [str(t)[:40] for t in raw["tags"][:10]]
    return line


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
                 account_id: str, now: datetime) -> list[dict]:
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
                "currency": "USD", "funding": f"cash:{account_id}"}
    for name in ("asset_class", "sleeve", "domicile", "tags"):
        if line.get(name) is not None:
            proposal[name] = line[name]
    portfolio = {"currency": "USD", "positions": [
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


def run_checks(lines: list[dict], *, mode: str, broker: AlpacaOrders | None, snapshot: Mapping[str, Any] | None,
               store: Any = None, client_id: str | None = None, ticket_id: str | None = None,
               environ: Mapping[str, str] | None = None, now: datetime | None = None) -> tuple[list[dict], list[dict]]:
    """Price every line and return ``(lines, checks)``.

    Check status: ``pass``; ``warn`` (a quiet line); ``violation`` (the IPS; the
    person may override it on the card, which is recorded); ``block`` (never
    overridable); ``unknown`` (the fact could not be read: it blocks submission,
    because an unknown is never assumed to be fine).
    """
    now = _now(now)
    config = limits(environ)
    lines = [dict(line) for line in lines]
    checks: list[dict] = []
    if broker is None:
        keys = "paper" if mode == "paper" else "live"
        checks.append(_check("broker_unavailable", "unknown",
                             f"Alpaca {keys} keys are not configured, so prices, buying power and tradability "
                             "could not be checked.", mode=mode))
    account = positions = clock = open_orders = None
    if broker is not None:
        account, error = _safe(broker.account)
        if error:
            checks.append(_check("account_unknown", "unknown", f"The Alpaca account could not be read: {error}"))
        positions, _ = _safe(broker.positions)
        clock, _ = _safe(broker.clock)
        open_orders, _ = _safe(broker.open_orders)
    positions = positions or []
    account_id = _ledger_account_id(store, client_id, account, mode)
    market_open = clock.get("is_open") if isinstance(clock, Mapping) else None
    trade_cutoff = _trade_cutoff(now, market_open is True, config)

    seen: dict[tuple[str, str], int] = {}
    for line in lines:
        i, symbol = line["index"], line["symbol"]
        key = (symbol, line["side"])
        if key in seen:
            checks.append(_check("duplicate_line", "block", f"{symbol} {line['side']} appears twice in this ticket.",
                                 i, symbol=symbol))
        seen[key] = i
        asset = last = None
        stale = False
        if broker is not None:
            asset, error = _safe(lambda: broker.asset(symbol))
            if error:
                checks.append(_check("asset_unknown", "unknown", f"Whether {symbol} is tradable could not be read.",
                                     i, symbol=symbol))
            elif asset is None or not asset.get("tradable") or str(asset.get("status", "active")) != "active":
                checks.append(_check("not_tradable", "block", f"{symbol} is not tradable at Alpaca.", i,
                                     symbol=symbol))
            trade, error = _safe(lambda: broker.latest_trade(symbol))
            last = _opt((trade or {}).get("price")) if trade else None
            if last is not None:
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

        # price: a limit by default, collared around the last trade
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
            if limit is None and stale:
                pass  # already said, plainly, by price_stale
            elif limit is None:
                checks.append(_check("price_unknown", "unknown", f"The last price of {symbol} could not be read, so "
                                     "no limit price was set.", i, symbol=symbol))
            elif last is not None:
                through = (limit - last) / last if line["side"] == "buy" else (last - limit) / last
                if through > config["collar"]:
                    checks.append(_check("collar", "block",
                                         f"The {symbol} limit {limit} is more than {config['collar'] * 100:.1f}% "
                                         f"through the last price {last}.", i, symbol=symbol, limit=limit,
                                         last=last, collar=config["collar"]))
                elif -through > FAR_FROM_MARKET:
                    checks.append(_check("far_from_market", "warn", f"The {symbol} limit is far from the last price "
                                         f"{last}; it may not fill.", i, symbol=symbol, last=last))
            elif line.get("limit_input") is not None and not stale:
                checks.append(_check("price_unknown", "unknown", f"The last price of {symbol} could not be read to "
                                     "check the limit.", i, symbol=symbol))

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
                    checks.append(_check("too_small", "block", f"USD {notional} buys less than one {symbol} share.",
                                         i, symbol=symbol))
                    qty = None
        line["order_qty"] = _s(qty)
        if qty is not None and not _whole(qty):
            if asset is not None and not fractionable:
                checks.append(_check("not_fractionable", "block", f"{symbol} trades in whole shares only.", i,
                                     symbol=symbol))
            if line["time_in_force"] != "day":
                checks.append(_check("fractional_day_only", "block", "Fractional orders are day orders only.", i))
        amount = _money(qty * price) if qty is not None and price is not None else None
        line["estimated_amount"] = _s(amount)
        if amount is None and line.get("notional") is None:
            checks.append(_check("amount_unknown", "unknown", f"The amount of the {symbol} order is unknown.", i,
                                 symbol=symbol))

        # sells never exceed what is held (no shorting)
        if line["side"] == "sell" and broker is not None and qty is not None:
            held = next((p for p in positions if str(p.get("symbol", "")).upper() == symbol), None)
            held_qty = _opt((held or {}).get("qty_available", (held or {}).get("qty"))) or Decimal(0)
            if qty > held_qty:
                checks.append(_check("sell_exceeds_position", "block",
                                     f"Selling {qty} {symbol} is more than the {held_qty} held.", i, symbol=symbol,
                                     qty=qty, held=held_qty))

        # live per-order limit
        if mode == "live" and amount is not None and amount > config["max_order"]:
            checks.append(_check("live_order_limit", "block", f"USD {amount} is above the live per-order limit of "
                                 f"USD {config['max_order']}.", i, amount=amount, limit=config["max_order"]))

        # open orders for the same symbol and side
        for order in open_orders or []:
            if str(order.get("symbol", "")).upper() == symbol and order.get("side") == line["side"] \
                    and not str(order.get("client_order_id", "")).startswith(f"wealth-{ticket_id}-"):
                checks.append(_check("open_order", "warn", f"An open {line['side']} order for {symbol} is already "
                                     "at Alpaca.", i, symbol=symbol))
                break

        checks += _policy_rows(snapshot, line, amount, positions, account_id, now)

    # the account as a whole
    if isinstance(account, Mapping):
        if str(account.get("status", "")).upper() != "ACTIVE" or any(
                account.get(flag) for flag in ("trading_blocked", "account_blocked", "trade_suspended_by_user")):
            checks.append(_check("account_blocked", "block", "The Alpaca account cannot place orders right now."))
        buys = [Decimal(l["estimated_amount"]) for l in lines if l["side"] == "buy" and l.get("estimated_amount")]
        power = _opt(account.get("non_marginable_buying_power"))
        if power is None:
            power = _opt(account.get("cash"))
        if buys and power is None:
            checks.append(_check("buying_power_unknown", "unknown", "Buying power could not be read."))
        elif buys and sum(buys) > power:
            checks.append(_check("buying_power", "block", f"The buys need USD {_money(sum(buys))}; cash buying power "
                                 f"is USD {_money(power)} (Wealth never uses margin).", need=_money(sum(buys)),
                                 have=_money(power)))
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
            if other.get("id") == ticket_id or other.get("status") not in ("pending", "submitted", "submitting"):
                continue
            if _parse(other["created_at"]) < recent:
                continue
            for line in lines:
                if any(o["symbol"] == line["symbol"] and o["side"] == line["side"] for o in other.get("lines", [])):
                    checks.append(_check("duplicate_ticket", "warn", f"Another recent ticket also has {line['side']} "
                                         f"{line['symbol']}.", line["index"], symbol=line["symbol"]))
    checks += _guardrail_rows(snapshot, now)
    return lines, _condense_policy(checks)


def _blocking(checks: list[dict], override: bool) -> list[dict]:
    return [c for c in checks if c["status"] in ("block", "unknown") or (c["status"] == "violation" and not override)]


def _ledger_account_id(store: Any, client_id: str | None, account: Mapping[str, Any] | None, mode: str) -> str:
    """The ledger account Alpaca fills post to: the connector's ``alpaca-<last4>`` convention."""
    digits = re.sub(r"\D", "", str((account or {}).get("account_number") or ""))
    wanted = f"alpaca-{digits[-4:]}" if len(digits) >= 4 else f"alpaca-{mode}"
    return wanted


# -- tickets -------------------------------------------------------------------------

def _totals(lines: list[dict]) -> dict[str, Any]:
    """``amount``: net cash, buys minus sells (negative means money comes in); ``gross``: all orders."""
    amounts = [_opt(l.get("estimated_amount")) for l in lines]
    known = all(a is not None for a in amounts)
    net = sum((a if l["side"] == "buy" else -a for a, l in zip(amounts, lines)), Decimal(0)) if known else None
    gross = sum(amounts, Decimal(0)) if known else None
    result: dict[str, Any] = {"amount": _s(net), "gross": _s(gross), "currency": "USD"}
    for name in ("estimated_tax", "estimated_cost"):
        values = [_opt(l.get(name)) for l in lines if l.get(name) is not None]
        result[name] = _s(sum(values, Decimal(0))) if values else None
    return result


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
        row = {k: line.get(k) for k in ("index", "side", "symbol", "type", "time_in_force", "limit_price",
                                         "last_price", "estimated_amount", "estimated_tax", "estimated_cost",
                                         "account", "state", "broker_order_id", "filled_qty", "filled_avg_price",
                                         "reason")}
        row["qty"] = line.get("order_qty") or line.get("qty")
        row["notional"] = line.get("notional")
        lines.append({k: v for k, v in row.items() if v is not None})
    view = {"id": ticket["id"], "broker": ticket["broker"], "mode": ticket["mode"], "status": status,
            "source": ticket["source"], "created_at": ticket["created_at"], "expires_at": ticket["expires_at"],
            "total": _totals(ticket["lines"]), "lines": lines, "notices": notices,
            "blocked": bool(_blocking(checks, override=True)),
            "needs_override": any(c["status"] == "violation" for c in checks),
            "needs_typed": ticket["mode"] == "live" and not live_acknowledged,
            "override": ticket.get("override")}
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


def _summary(view: Mapping[str, Any]) -> str:
    total = view["total"]["amount"]
    count = len(view["lines"])
    parts = [f"{'LIVE' if view['mode'] == 'live' else 'PAPER'} ticket {view['id']}: {count} order(s)"]
    if total is None:
        parts.append("amount not yet known")
    else:
        parts.append(f"net about USD {total} to pay" if Decimal(total) >= 0 else f"net about USD {total[1:]} to receive")
    notes = [n for n in view["notices"] if n["status"] in ("violation", "block", "unknown")]
    text = ", ".join(parts) + "."
    if notes:
        text += f" {len(notes)} issue(s) would stop it: " + "; ".join(n["message"] for n in notes[:3])
    return (text + " Nothing has been sent. The person reviews it on the order card in the Wealth app and taps "
            "to place it; it expires at " + view["expires_at"] + ".")


def _audit(store: Any, client_id: str, ticket: Mapping[str, Any], event: str, payload: Mapping[str, Any],
           line: dict | None = None, **extra: Any) -> None:
    store.record_order_event(client_id, {
        "ticket_id": ticket["id"], "event": event, "broker": ticket["broker"], "mode": ticket["mode"],
        "line": line["index"] if line else None,
        "client_order_id": line.get("client_order_id") if line else None,
        "broker_order_id": (line or {}).get("broker_order_id") or extra.get("broker_order_id"),
        "payload": alpaca_orders.redact(dict(payload)),
    })


def create_ticket(store: Any, client_id: str | None, inputs: Mapping[str, Any], *, snapshot: Mapping[str, Any] | None,
                  environ: Mapping[str, str] | None = None, broker: AlpacaOrders | None = None,
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
    lines = []
    for i, raw in enumerate(orders):
        try:
            lines.append(_normalize_order(raw, i))
        except ValueError as exc:
            errors.append(str(exc))
    if errors:
        raise ValueError("; ".join(errors))
    mode = trading_mode(environ)
    ticket_id = "t" + secrets.token_hex(8)
    if broker is None and store is not None:  # a preview without a client stays offline
        broker = broker_for(mode, environ)
    lines, checks = run_checks(lines, mode=mode, broker=broker, snapshot=snapshot, store=store,
                               client_id=client_id, ticket_id=ticket_id, environ=environ, now=now)
    for line in lines:
        line["client_order_id"] = client_order_id(ticket_id, line["index"])
    nonce = secrets.token_hex(4).upper()
    ticket = {"id": ticket_id, "broker": BROKER, "mode": mode, "status": "pending", "source": source,
              "rationale": rationale.strip()[:600], "created_at": _iso(now), "expires_at": _iso(now + TICKET_TTL),
              "lines": lines, "checks": checks, "nonce": None, "nonce_hash": _nonce_hash(ticket_id, nonce),
              "failures": 0}
    warnings: list[str] = []
    if store is not None and client_id:
        def update(state: dict) -> dict:
            tickets = dict(state.get("tickets") or {})
            tickets[ticket_id] = ticket
            if len(tickets) > KEEP_TICKETS:  # oldest settled tickets go first; the audit trail keeps them
                settled = sorted((t["created_at"], tid) for tid, t in tickets.items()
                                 if t["status"] not in ("pending", "submitting", "submitted"))
                for _, tid in settled[:len(tickets) - KEEP_TICKETS]:
                    tickets.pop(tid)
            return {**state, "tickets": tickets}

        store.update_auxiliary(client_id, "execution", update)
        _audit(store, client_id, ticket, "ticket", {"source": source, "rationale": ticket["rationale"],
                                                    "orders": [dict(o) for o in orders]})
        _audit(store, client_id, ticket, "checks", {"phase": "proposed", "checks": checks,
                                                    "lines": [_line_audit(l) for l in lines]})
        state = store.auxiliary(client_id, "execution")
        acknowledged = bool((state.get("live_ack") or {}).get(BROKER))
    else:
        acknowledged = True
        warnings.append("No client was given, so this ticket is a preview only: it was not stored and cannot be "
                        "confirmed.")
    view = public_ticket(ticket, now=now, live_acknowledged=acknowledged)
    unknown_facts = [c for c in checks if c["status"] == "unknown"]
    if store is None:
        view["id"] = None
    summary = _summary(view) if store is not None else "Preview only; nothing was stored or sent."
    return {"status": "partial" if unknown_facts or store is None else "ready",
            "result": {"ticket": view, "summary": summary,
                       "next_step": "Explain the ticket briefly and tell the person to review and confirm it on the "
                                    "order card. Never say an order was placed or filled until order status says so."},
            "missing": [], "warnings": warnings,
            "sources": [{"title": "Alpaca account, assets and latest trades", "mode": mode}] if broker else [],
            "assumptions": [f"Limit prices default to within {limits(environ)['collar'] * 50:.1f}% of the last trade; "
                            "dollar amounts become a quantity at the limit price.",
                            "Buys must fit cash buying power; Wealth never uses margin or sells short."]}


def _line_audit(line: Mapping[str, Any]) -> dict[str, Any]:
    return {k: line.get(k) for k in ("index", "symbol", "side", "type", "time_in_force", "qty", "notional",
                                     "order_qty", "limit_price", "last_price", "estimated_amount",
                                     "client_order_id")}


def ticket_status(store: Any, client_id: str, ticket_id: str, *, now: datetime | None = None) -> dict[str, Any]:
    """The stored ticket as the model may see it (no nonce, no broker calls)."""
    state = store.auxiliary(client_id, "execution")
    ticket = (state.get("tickets") or {}).get(ticket_id)
    if ticket is None:
        raise LookupError("That order ticket is no longer there.")
    return public_ticket(ticket, now=now, live_acknowledged=bool((state.get("live_ack") or {}).get(BROKER)))


def list_tickets(store: Any, client_id: str, *, include_nonce: bool = False, now: datetime | None = None,
                 since_hours: int = 24) -> list[dict[str, Any]]:
    """Recent tickets; with ``include_nonce`` (the card, token holder only) each pending one gets a fresh code.

    Plain codes are never stored, so showing the card again issues a new code and the earlier one stops
    working (a second open page must reload to confirm).
    """
    now = _now(now)
    state = store.auxiliary(client_id, "execution")
    acknowledged = bool((state.get("live_ack") or {}).get(BROKER))
    horizon = now - timedelta(hours=since_hours)
    rows = [t for t in (state.get("tickets") or {}).values()
            if _parse(t["created_at"]) >= horizon or t["status"] in ("submitting", "submitted")]
    rows.sort(key=lambda t: t["created_at"])
    codes = _issue_nonces(store, client_id, [t["id"] for t in rows if t["status"] == "pending"], now) \
        if include_nonce else {}
    return [public_ticket(t, include_nonce=include_nonce, now=now, live_acknowledged=acknowledged,
                          nonce=codes.get(t["id"])) for t in rows]


# -- confirmation: the only path that submits ------------------------------------------

def confirm(store: Any, client_id: str, ticket_id: str, *, nonce: Any, override: bool = False,
            typed: Any = None, snapshot: Mapping[str, Any] | None = None, environ: Mapping[str, str] | None = None,
            broker: AlpacaOrders | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Submit a ticket the person confirmed on its card.

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
    acknowledged = bool((state.get("live_ack") or {}).get(BROKER))
    if ticket["status"] != "pending":
        raise ConfirmError("used", "This ticket was already handled.", 409, public_ticket(ticket, now=now))
    if _parse(ticket["expires_at"]) <= now:
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
    if mode == "live" and trading_mode(environ) != "live":
        raise ConfirmError("live_disabled", "Live trading is turned off on this computer.", 403,
                           public_ticket(ticket, now=now, live_acknowledged=acknowledged))
    typed_ok = isinstance(typed, str) and typed.strip().upper() in TYPED_LIVE
    if mode == "live" and not acknowledged and not typed_ok:
        raise ConfirmError("typed", "Type LIVE (or EN VIVO) to place your first live orders.", 400,
                           public_ticket(ticket, now=now, live_acknowledged=False))

    # Fresh data: every check runs again right before anything is sent.
    if broker is None:
        broker = broker_for(mode, environ)
    lines, checks = run_checks([{k: v for k, v in l.items() if k not in ("limit_price", "order_qty", "last_price",
                                                                         "estimated_amount")}
                                for l in ticket["lines"]],
                               mode=mode, broker=broker, snapshot=snapshot, store=store, client_id=client_id,
                               ticket_id=ticket_id, environ=environ, now=now)
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

    # Claim the ticket atomically so two taps cannot both submit.
    violations = [c for c in checks if c["status"] == "violation"]
    record = {"at": _iso(now), "codes": sorted({c.get("params", {}).get("rule", c["code"]) for c in violations})} \
        if violations and override else None

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
            result["live_ack"] = {**(current.get("live_ack") or {}), BROKER: _iso(now)}
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
            broker.audit = lambda kind, payload, _line=line: _audit(store, client_id, ticket, kind, payload, _line)
            try:
                sent_any = True
                order = broker.submit(body)
            except BrokerError as exc:
                line["state"] = "unknown" if exc.retryable else "failed"
                line["reason"] = str(exc)[:200]
            else:
                line["broker_order_id"] = str(order.get("id") or "") or None
                line["broker_status"] = order.get("status")
                line["state"] = _LINE_STATE.get(str(order.get("status")), "sent")
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
                failed = [{**l, "state": "failed", "reason": "Submission stopped before anything reached Alpaca."}
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


# -- status, fills and cancellation --------------------------------------------------------

def _update_line(line: dict, order: Mapping[str, Any]) -> dict:
    line = dict(line)
    line["broker_order_id"] = str(order.get("id") or line.get("broker_order_id") or "") or None
    line["broker_status"] = order.get("status")
    line["state"] = _LINE_STATE.get(str(order.get("status")), line.get("state", "sent"))
    line["filled_qty"] = order.get("filled_qty")
    line["filled_avg_price"] = order.get("filled_avg_price")
    return line


def _fill_date(activity: Mapping[str, Any]) -> str | None:
    raw = str(activity.get("transaction_time") or "")
    try:
        return _parse(raw).astimezone(_EASTERN).date().isoformat()
    except ValueError:
        return None


def post_fills(store: Any, client_id: str, ticket: Mapping[str, Any], line: Mapping[str, Any],
               broker: AlpacaOrders, now: datetime) -> tuple[list[str], Decimal]:
    """Post this order's new fills to the ledger; returns ``(fill ids, quantity)``.

    The ledger skips a fill it already holds under the same external id, e.g. one
    the read-only Alpaca connector synced first.
    """
    from .. import ledger as ledger_module

    order_id = line.get("broker_order_id")
    if not order_id:
        return [], Decimal(0)
    after = (_parse(line["submitted_at"]) - timedelta(days=1)).date().isoformat() if line.get("submitted_at") else None
    # Every page (Alpaca pages FILL activities by page_token); a read error raises BrokerError to the caller,
    # which keeps the line open and tries again later: an unreadable page is never "no fills".
    activities = broker.all_fills(after)
    if not activities:
        return [], Decimal(0)
    fills = [a for a in activities if str(a.get("order_id")) == order_id and _external_id(a)]
    posted = set(line.get("posted_fills") or [])
    fresh = [a for a in fills if _external_id(a) not in posted]
    if not fresh:
        return [], Decimal(0)
    account, _ = _safe(broker.account)
    account_id = _ledger_account_id(store, client_id, account, ticket["mode"])
    ledger = store.ledger(client_id)
    existing_account = next((a for a in ledger.get("accounts", []) if a.get("id") == account_id), None)
    account_row = existing_account or {
        "id": account_id, "institution": "Alpaca", "type": "brokerage", "currency": "USD",
        "owners": [{"person_id": "self", "share": "1"}],
        "name": "Alpaca paper account" if ticket["mode"] == "paper" else "Alpaca brokerage account"}
    account_row = {k: v for k, v in account_row.items() if k not in ("updated_at",)}
    symbol = line["symbol"]
    instrument = next((i for i in ledger.get("instruments", []) if i.get("id") == symbol), None) or \
        {"id": symbol, "symbol": symbol, "currency": "USD"}
    transactions = []
    for activity in fresh:
        qty, price = _opt(activity.get("qty")), _opt(activity.get("price"))
        when = _fill_date(activity)
        if qty is None or price is None or qty <= 0 or when is None:
            continue
        gross = _money(qty * price)
        kind = "buy" if str(activity.get("side", line["side"])).lower() == "buy" else "sell"
        transactions.append({"kind": kind, "account_id": account_id, "date": when, "instrument_id": instrument["id"],
                             "quantity": _s(qty), "price": _s(price), "currency": "USD",
                             "amount": _s(-gross if kind == "buy" else gross), "external_id": _external_id(activity),
                             "description": f"Alpaca {kind} {_s(qty)} {symbol} (order {order_id[:8]})"})
    if not transactions:
        return [], Decimal(0)
    ids = sorted(t["external_id"] for t in transactions)
    batch = {"batch_id": EXECUTION_BATCH_PREFIX + hashlib.sha256("|".join(ids).encode()).hexdigest()[:24],
             "source": {"kind": "tool", "ref": f"alpaca-orders:{ticket['mode']}:{order_id}",
                        "observed_on": now.astimezone(_EASTERN).date().isoformat()},
             "confidence": "reported", "accounts": [account_row], "instruments": [instrument],
             "transactions": transactions}
    receipt = ledger_module.post(store, client_id, batch)
    _audit(store, client_id, ticket, "fill_posted", {"fill_ids": ids, "posted": len(receipt["posted"]),
                                                     "duplicates": len(receipt["duplicates"]),
                                                     "held": len(receipt["held"]), "account_id": account_id}, line)
    return ids, sum((Decimal(t["quantity"]) for t in transactions), Decimal(0))


def refresh(store: Any, client_id: str, ticket_ids: list[str] | None = None, *, broker: AlpacaOrders | None = None,
            environ: Mapping[str, str] | None = None, now: datetime | None = None) -> list[dict[str, Any]]:
    """Read open orders' status from the broker, post new fills, and return the updated tickets."""
    now = _now(now)
    state = store.auxiliary(client_id, "execution")
    tickets = state.get("tickets") or {}

    def interrupted(ticket: Mapping[str, Any]) -> bool:
        """A "submitting" ticket nobody is submitting any more: a crash or kill mid-submit."""
        if ticket["status"] != "submitting" or ticket["id"] in _IN_FLIGHT:
            return False
        started = ticket.get("confirmed_at")
        return bool(ticket.get("interrupted")) or not started or _parse(started) + SUBMIT_LEASE <= now

    targets = [t for tid, t in tickets.items() if (ticket_ids is None or tid in ticket_ids)
               and (t["status"] == "submitted" or interrupted(t))]
    clients: dict[str, AlpacaOrders | None] = {}
    for ticket in targets:
        mode = ticket["mode"]
        if mode not in clients:
            clients[mode] = broker if broker is not None and broker.mode == mode else broker_for(mode, environ)
        api = clients[mode]
        if api is None:
            continue
        lines = []
        reconciling = ticket["status"] == "submitting"
        unresolved = False
        for line in ticket["lines"]:
            line = dict(line)
            filled = _opt(line.get("filled_qty")) or Decimal(0)
            needs_fills = filled > (_opt(line.get("posted_qty")) or Decimal(0))
            if line.get("state") not in _FINAL or line.get("state") == "unknown" or needs_fills:
                api.audit = lambda kind, payload, _line=line: _audit(store, client_id, ticket, kind, payload, _line)
                try:
                    if line.get("broker_order_id"):
                        order = api.order(line["broker_order_id"])
                    else:
                        order = api.order_by_client_id(line["client_order_id"])
                    if order:
                        line = _update_line(line, order)
                        line.setdefault("submitted_at", order.get("submitted_at") or _iso(now))
                    elif line.get("state") in ("unknown", "proposed"):
                        # looked up by its client_order_id: Alpaca never saw it (lost response, or the process
                        # stopped before sending it)
                        line["state"] = "failed"
                        line["reason"] = "Alpaca has no record of this order; it was not placed."
                except (BrokerError, ValueError):
                    unresolved = unresolved or line.get("state") == "proposed"
                finally:
                    api.audit = None
                if (_opt(line.get("filled_qty")) or Decimal(0)) > (_opt(line.get("posted_qty")) or Decimal(0)):
                    api.audit = lambda kind, payload, _line=line: _audit(store, client_id, ticket, kind, payload,
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
        # Done only when every order is final AND every filled share is in the ledger.
        done = not unresolved and all(
            l.get("state") in _FINAL
            and (_opt(l.get("posted_qty")) or Decimal(0)) >= (_opt(l.get("filled_qty")) or Decimal(0))
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
           broker: AlpacaOrders | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Discard a pending ticket (nothing was sent) or cancel one of its submitted orders at the broker."""
    now = _now(now)
    state = store.auxiliary(client_id, "execution")
    tickets = state.get("tickets") or {}
    if isinstance(target, str) and _TICKET_ID.match(target):
        ticket = tickets.get(target)
        if ticket is None:
            raise ConfirmError("missing", "That order ticket is no longer there.", 404)
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
                if line.get("state") in _FINAL:
                    raise ConfirmError("final", "This order is already final.", 409)
                api = broker if broker is not None and broker.mode == ticket["mode"] else broker_for(ticket["mode"], environ)
                if api is None:
                    raise ConfirmError("broker", "Alpaca keys are not configured.", 503)
                api.audit = lambda kind, payload, _line=line: _audit(store, client_id, ticket, kind, payload, _line)
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
    """Mode, opt-in, whether keys exist (never the keys), limits and today's live usage."""
    now = _now(now)
    environ = os.environ if environ is None else environ
    config = limits(environ)
    mode = trading_mode(environ)
    result: dict[str, Any] = {
        "broker": BROKER, "mode": mode, "live_opt_in": mode == "live",
        "credentials": {m: alpaca_orders.load_keys(m, environ) is not None for m in ("paper", "live")},
        "limits": {"per_order_usd": _s(config["max_order"]), "daily_usd": _s(config["max_daily"]),
                   "collar": _s(config["collar"]), "applies_to": "live"},
        "ticket_minutes": int(TICKET_TTL.total_seconds() // 60),
        "submission": "Only the person, on the order card in the Wealth app, can place orders.",
    }
    if store is not None and client_id:
        state = store.auxiliary(client_id, "execution")
        result["live_acknowledged"] = bool((state.get("live_ack") or {}).get(BROKER))
        result["live_used_today_usd"] = _s(_money(_live_used_today(store, client_id, now)))
        result["pending_tickets"] = sum(1 for t in (state.get("tickets") or {}).values()
                                        if t["status"] == "pending" and _parse(t["expires_at"]) > now)
    return result


__all__ = ["BROKER", "ConfirmError", "SOURCES", "TICKET_TTL", "broker_for", "cancel", "client_order_id", "confirm",
           "create_ticket", "execution_status", "limits", "list_tickets", "post_fills", "public_ticket", "refresh",
           "run_checks", "ticket_status", "trading_mode"]
