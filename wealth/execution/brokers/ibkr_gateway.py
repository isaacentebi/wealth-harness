"""Interactive Brokers orders through the Client Portal Web API gateway the person runs on this computer.

The person downloads IBKR's Client Portal Gateway, starts it (``bin/run.sh
root/conf.yaml``), and logs in at https://localhost:5000 with the account they
want Wealth to trade: the **paper** login (account ids ``DU...``) or the live
one.  Wealth holds no IBKR credential: every request rides on that gateway
session, and the only address it ever calls is ``https://localhost:<port>/v1/api``.

Paper or live
    Decided by the account the gateway is logged into, read at every ticket and
    again at every confirmation: an account id starting with ``DU`` is paper.
    Anything else is live and needs ``WEALTH_TRADING_LIVE=ibkr`` (or ``1``) in
    the server's environment, plus the same live limits as Alpaca (per-order and
    daily caps, limit orders only, a typed confirmation the first time).  If the
    gateway is logged into a different account at confirmation than when the
    card was drawn, nothing is sent.

The gateway's certificate
    The gateway serves a self-signed certificate.  Either pin it,
    ``WEALTH_IBKR_GATEWAY_CERT_SHA256=<sha256 of the DER certificate>`` (the
    connection is refused unless the gateway presents exactly that certificate),
    or allow it without verification for the loopback address only with
    ``WEALTH_IBKR_GATEWAY_INSECURE_LOCALHOST=1``.  With neither, the certificate
    must verify against the system's trust store, which the stock gateway's does
    not, and the error says which setting to add.  ``WEALTH_IBKR_GATEWAY_PORT``
    (default 5000) is the only part of the address that can change; the host is
    always ``localhost``.

Order replies
    ``POST /iserver/account/{acct}/orders`` may answer with a question
    (``[{id, message: [...], messageIds: [...]}]``) that must be confirmed with
    ``POST /iserver/reply/{id}``.  Wealth confirms it as part of the person's
    single tap only when every message id is in :data:`BENIGN_REPLIES` —
    precautionary notices about price and size that Wealth's own collar and
    limits already cover.  Any other message is answered ``confirmed: false``,
    nothing is placed, and the card shows IBKR's words.

Allowlist
    GETs on a fixed list, ``POST`` of one order, ``POST`` of a reply and
    ``DELETE`` of one order (:data:`ALLOWED`).  Nothing else (transfers,
    account settings, modify, cancel-all) can be built, and the transport checks
    the list again.

Fills
    IBKR fills are not posted from here: the Client Portal's execution ids are
    not the trade ids the Flex connector and statements use, so posting both
    would double-count.  A line's state (filled, average price) comes from order
    status; the trade itself reaches the ledger with the next Flex sync or
    statement.

IBKR sources (Client Portal Web API v1; re-check before relying on a detail)
    * Overview, the gateway, its self-signed certificate and session:
      https://www.interactivebrokers.com/campus/ibkr-api-page/cpapi-v1/
    * Endpoints used: ``/iserver/auth/status``, ``/iserver/accounts``,
      ``/iserver/secdef/search``, ``/iserver/marketdata/snapshot`` (field 31,
      last price; a ``C`` prefix is the previous close, ``H`` a halted symbol),
      ``/iserver/account/{acct}/orders``, ``/iserver/reply/{replyId}``,
      ``/iserver/account/orders``, ``/iserver/account/order/status/{orderId}``,
      ``DELETE /iserver/account/{acct}/order/{orderId}``, ``/portfolio/accounts``
      (must precede other portfolio calls), ``/portfolio/{acct}/ledger`` and
      ``/portfolio/{acct}/positions/0``:
      https://www.interactivebrokers.com/campus/ibkr-api-page/cpapi-v1/#endpoints
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import ssl
import urllib.parse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from .base import BrokerError, mask

NAME = "ibkr"
HOST = "localhost"
DEFAULT_PORT = 5000
API_PREFIX = "/v1/api"
TIMEOUT_SECONDS = 20.0
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_REPLIES = 5  # a question may follow a question; more than this is not a flow Wealth knows
PORT_ENV = "WEALTH_IBKR_GATEWAY_PORT"
PIN_ENV = "WEALTH_IBKR_GATEWAY_CERT_SHA256"
INSECURE_ENV = "WEALTH_IBKR_GATEWAY_INSECURE_LOCALHOST"
LIVE_ENV = "WEALTH_TRADING_LIVE"
PAPER_PREFIX = "DU"
US_EXCHANGES = frozenset({"NYSE", "NASDAQ", "ARCA", "AMEX", "BATS", "NYSE ARCA", "NYSEARCA", "CBOE", "IEX", "PINK"})
# Order-reply message ids Wealth confirms within the person's tap.  Each is a precaution Wealth's own checks already
# cover: o163 "the limit price is more than X% from the market" (the collar is tighter), o383 "the order size
# exceeds the size limit" and o451 "the order value exceeds the total value limit" (the per-order and daily caps).
# Anything else (e.g. o354 "no market data", o10151 "outside regular trading hours", a margin or short-sale notice)
# stops the order and is shown to the person.
BENIGN_REPLIES = frozenset({"o163", "o383", "o451"})
_ACCOUNT = r"[A-Z]{1,3}[0-9]{4,12}"
_ORDER_ID = re.compile(r"^[0-9]{1,20}$")
_REPLY_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_CLIENT_ORDER_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
ALLOWED: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (method, re.compile(API_PREFIX + pattern)) for method, pattern in (
        ("GET", r"/iserver/auth/status"),
        ("GET", r"/iserver/accounts"),
        ("GET", r"/iserver/secdef/search"),
        ("GET", r"/iserver/marketdata/snapshot"),
        ("GET", r"/iserver/account/orders"),
        ("GET", r"/iserver/account/order/status/[0-9]{1,20}"),
        ("GET", r"/portfolio/accounts"),
        ("GET", rf"/portfolio/{_ACCOUNT}/ledger"),
        ("GET", rf"/portfolio/{_ACCOUNT}/positions/0"),
        ("POST", rf"/iserver/account/{_ACCOUNT}/orders"),
        ("POST", r"/iserver/reply/[A-Za-z0-9-]{1,64}"),
        ("DELETE", rf"/iserver/account/{_ACCOUNT}/order/[0-9]{{1,20}}"),
    ))
# IBKR order statuses in Wealth's line states (a working order with a fill is "partial").
LINE_STATE = {"pendingsubmit": "sent", "presubmitted": "sent", "submitted": "sent", "apipending": "sent",
              "pendingcancel": "sent", "filled": "filled", "cancelled": "canceled", "apicancelled": "canceled",
              "inactive": "rejected"}
_EASTERN = ZoneInfo("America/New_York")

# (method, url, headers, body, timeout) -> (status, body bytes)
Transport = Callable[[str, str, Mapping[str, str], "bytes | None", float], "tuple[int, bytes]"]


def allowed(method: str, path: str) -> bool:
    return any(m == method and p.fullmatch(path) for m, p in ALLOWED)


def live_opted_in(environ: Mapping[str, str] | None = None) -> bool:
    """``WEALTH_TRADING_LIVE`` names ``ibkr`` (or is ``1``, which opts in IBKR only)."""
    environ = os.environ if environ is None else environ
    parts = {part.strip().lower() for part in (environ.get(LIVE_ENV) or "").split(",")}
    return "ibkr" in parts or "1" in parts


def is_paper_account(account_id: Any) -> bool:
    return str(account_id or "").upper().startswith(PAPER_PREFIX)


# -- TLS and transport -------------------------------------------------------------------

class TlsPolicy:
    """How the gateway's certificate is trusted: a pinned SHA-256, loopback-only unverified, or the trust store."""

    __slots__ = ("pin", "insecure_localhost")

    def __init__(self, pin: str | None = None, insecure_localhost: bool = False):
        self.pin = pin
        self.insecure_localhost = insecure_localhost

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "TlsPolicy":
        environ = os.environ if environ is None else environ
        raw = re.sub(r"[^0-9a-fA-F]", "", environ.get(PIN_ENV) or "").lower()
        pin = raw if len(raw) == 64 else None
        insecure = (environ.get(INSECURE_ENV) or "").strip().lower() in ("1", "true", "yes")
        return cls(pin, insecure)

    def describe(self) -> str:
        return "pinned" if self.pin else ("localhost-unverified" if self.insecure_localhost else "system-trust")

    def context(self) -> ssl.SSLContext:
        if self.pin or self.insecure_localhost:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE  # replaced by the pin check, or allowed for loopback only
            return context
        return ssl.create_default_context()


def gateway_port(environ: Mapping[str, str] | None = None) -> int:
    environ = os.environ if environ is None else environ
    try:
        port = int(str(environ.get(PORT_ENV) or DEFAULT_PORT))
    except ValueError:
        return DEFAULT_PORT
    return port if 1 <= port <= 65535 else DEFAULT_PORT


def https_transport(method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout: float, *,
                    tls: TlsPolicy) -> tuple[int, bytes]:
    """HTTPS to the local gateway only, with the TLS policy, the allowlist, no redirects and a size cap."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.hostname != HOST:
        raise BrokerError("Refusing to call anything but the IBKR gateway on localhost over HTTPS.")
    if not allowed(method, parts.path):
        raise BrokerError(f"Refusing {method} {parts.path}: not a request Wealth makes to IBKR.")
    target = parts.path + (f"?{parts.query}" if parts.query else "")
    connection = http.client.HTTPSConnection(HOST, parts.port or DEFAULT_PORT, timeout=timeout,
                                             context=tls.context())
    failure: BrokerError | None = None
    status, data = 0, b""
    try:
        connection.connect()
        if tls.pin:
            der = connection.sock.getpeercert(binary_form=True) if connection.sock else None
            if not der or hashlib.sha256(der).hexdigest() != tls.pin:
                raise BrokerError("The IBKR gateway's certificate does not match WEALTH_IBKR_GATEWAY_CERT_SHA256; "
                                  "nothing was sent.")
        connection.request(method, target, body=body, headers=dict(headers))
        response = connection.getresponse()
        status = response.status
        if 300 <= status < 400:
            raise BrokerError("The IBKR gateway answered with a redirect; refused.")
        data = response.read(MAX_RESPONSE_BYTES + 1)
    except BrokerError as exc:
        failure = BrokerError(str(exc))
    except ssl.SSLCertVerificationError:
        failure = BrokerError("The IBKR gateway's certificate is self-signed. Pin it with "
                              "WEALTH_IBKR_GATEWAY_CERT_SHA256, or allow it for localhost with "
                              "WEALTH_IBKR_GATEWAY_INSECURE_LOCALHOST=1.")
    except ssl.SSLError:
        failure = BrokerError("TLS with the IBKR gateway failed.", retryable=True)
    except (TimeoutError, OSError, http.client.HTTPException) as exc:
        failure = BrokerError(f"Could not reach the IBKR gateway on localhost ({type(exc).__name__}). Is it running "
                              "and logged in?", retryable=True)
    finally:
        connection.close()
    if failure is not None:
        raise failure
    if len(data) > MAX_RESPONSE_BYTES:
        raise BrokerError("The IBKR gateway returned an oversized response; refused.")
    return status, data


# Looked up at call time so tests substitute a fake (tests never reach a socket).
default_transport: Callable[..., tuple[int, bytes]] = https_transport


# -- small helpers ------------------------------------------------------------------------

def _dec(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value).replace(",", ""))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() else None


def _s(value: Decimal | None) -> str | None:
    if value is None:
        return None
    text = format(value.normalize(), "f")
    return "0" if text in ("-0", "") else text


def _ibkr_symbol(symbol: str) -> str:
    """Wealth's ``BRK.B`` is IBKR's ``BRK B``."""
    return symbol.replace(".", " ")


def _wealth_symbol(symbol: Any) -> str:
    return re.sub(r"\s+", ".", str(symbol or "").strip().upper())


def _us_session_open(now: datetime) -> bool:
    """Regular US equity hours, 09:30-16:00 New York, weekdays (exchange holidays are not modelled)."""
    local = now.astimezone(_EASTERN)
    minutes = local.hour * 60 + local.minute
    return local.weekday() < 5 and 570 <= minutes < 960


# -- the client and adapter ---------------------------------------------------------------

class IbkrBroker:
    """:class:`~wealth.execution.brokers.base.OrderBroker` over the Client Portal gateway (one logged-in account).

    ``mode`` is ``paper`` or ``live`` after :meth:`resolve` (the account id's
    ``DU`` prefix), ``None`` before.  ``audit(kind, payload)`` receives every
    order request and response (no headers; account ids masked).
    """

    name = NAME
    label = "Interactive Brokers"
    submits = True
    posts_fills = False  # the Flex connector or a statement posts IBKR trades (see the module notes)

    def __init__(self, *, environ: Mapping[str, str] | None = None, transport: Transport | None = None,
                 audit: Callable[[str, dict[str, Any]], None] | None = None, timeout: float = TIMEOUT_SECONDS,
                 clock: Callable[[], datetime] | None = None):
        environ = os.environ if environ is None else environ
        self.tls = TlsPolicy.from_env(environ)
        self.base = f"https://{HOST}:{gateway_port(environ)}{API_PREFIX}"
        self._transport = transport
        self.audit = audit
        self.timeout = timeout
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.account_id: str | None = None
        self.mode: str | None = None
        self._portfolio_ready = False
        self._conids: dict[str, dict[str, Any]] = {}

    def __repr__(self) -> str:
        return f"IbkrBroker(mode={self.mode!r}, account={mask(self.account_id) if self.account_id else None!r})"

    # -- plumbing
    def _scrub(self, value: Any) -> Any:
        if isinstance(value, Mapping):
            return {k: self._scrub(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._scrub(v) for v in value]
        if isinstance(value, str) and self.account_id and self.account_id in value:
            return value.replace(self.account_id, mask(self.account_id))
        return value

    def _call(self, method: str, url: str, headers: Mapping[str, str], payload: bytes | None) -> tuple[int, bytes]:
        transport = self._transport or default_transport
        if transport is https_transport:
            return https_transport(method, url, headers, payload, self.timeout, tls=self.tls)
        return transport(method, url, headers, payload, self.timeout)

    def _request(self, method: str, path: str, *, query: Mapping[str, Any] | None = None,
                 body: Any = None, audit_as: str | None = None) -> tuple[int, Any]:
        full = API_PREFIX + path
        if not allowed(method, full):
            raise BrokerError(f"Refusing {method} {path}: not a request Wealth makes to IBKR.")
        url = self.base + path + (("?" + urllib.parse.urlencode(query)) if query else "")
        payload = json.dumps(body, separators=(",", ":")).encode() if body is not None else None
        headers = {"Accept": "application/json", "User-Agent": "wealth-harness/0.2 (orders)"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        masked_path = self._scrub(path)
        if audit_as and self.audit:
            self.audit("request", {"action": audit_as, "method": method, "path": masked_path,
                                   "query": dict(query or {}), "body": self._scrub(body)})
        try:
            status, raw = self._call(method, url, headers, payload)
        except BrokerError as exc:
            if audit_as and self.audit:
                self.audit("response", {"action": audit_as, "method": method, "path": masked_path,
                                        "error": self._scrub(str(exc))})
            raise BrokerError(self._scrub(str(exc)), retryable=exc.retryable) from None
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, ValueError):
            parsed = {"error": "unparseable response"}
        if audit_as and self.audit:
            self.audit("response", {"action": audit_as, "method": method, "path": masked_path, "status": status,
                                    "body": self._scrub(parsed)})
        return status, parsed

    def _ok(self, method: str, path: str, **kwargs: Any) -> Any:
        status, body = self._request(method, path, **kwargs)
        if 200 <= status < 300:
            return body
        message = body.get("error") if isinstance(body, Mapping) else None
        hint = {401: "The IBKR gateway is not logged in: open https://localhost:5000 and log in.",
                403: "The IBKR gateway refused the request.",
                404: "IBKR does not know this item.",
                429: "The IBKR gateway's rate limit was reached; try again shortly."}.get(status, "")
        raise BrokerError(f"IBKR answered HTTP {status}. {hint} {self._scrub(message or '')}".strip(), status=status,
                          retryable=status == 429 or status >= 500, body=body)

    # -- the session and account
    def resolve(self) -> str:
        """Read the gateway session and the logged-in account; sets ``account_id`` and ``mode``."""
        if self.account_id:
            return self.account_id
        auth = self._ok("GET", "/iserver/auth/status")
        if not isinstance(auth, Mapping) or not auth.get("authenticated") or auth.get("competing"):
            raise BrokerError("The IBKR gateway is running but not logged in (or another session took over). Open "
                              "https://localhost:5000 and log in.", status=401)
        accounts = self._ok("GET", "/iserver/accounts")
        listed = [str(a) for a in (accounts or {}).get("accounts") or []] if isinstance(accounts, Mapping) else []
        selected = str((accounts or {}).get("selectedAccount") or "") if isinstance(accounts, Mapping) else ""
        account = selected or (listed[0] if len(listed) == 1 else "")
        if not account or not re.fullmatch(_ACCOUNT, account):
            raise BrokerError("The IBKR gateway did not name one trading account; log in with a single account.")
        self.account_id = account
        self.mode = "paper" if is_paper_account(account) else "live"
        return account

    def fingerprint(self) -> str:
        """A stable, non-reversible id of the logged-in account, stored on a ticket to detect a switched login."""
        return hashlib.sha256(f"wealth-ibkr:{self.resolve()}".encode()).hexdigest()[:16]

    def _portfolio(self) -> str:
        account = self.resolve()
        if not self._portfolio_ready:
            self._ok("GET", "/portfolio/accounts")  # IBKR requires this before other portfolio calls
            self._portfolio_ready = True
        return account

    def account_state(self) -> dict[str, Any]:
        account = self._portfolio()
        ledger = self._ok("GET", f"/portfolio/{account}/ledger")
        usd = (ledger or {}).get("USD") if isinstance(ledger, Mapping) else None
        if not isinstance(usd, Mapping):
            base = (ledger or {}).get("BASE") if isinstance(ledger, Mapping) else None
            usd = base if isinstance(base, Mapping) and str(base.get("currency", "")).upper() == "USD" else None
        cash = _dec((usd or {}).get("cashbalance")) if usd else None
        settled = _dec((usd or {}).get("settledcash")) if usd else None
        power = min(v for v in (cash, settled) if v is not None) if (cash is not None or settled is not None) else None
        return {"active": True, "buying_power": _s(power) if power is not None else None, "currency": "USD",
                "number": account, "ledger_account_id": f"ibkr-{account[-4:]}"}

    def market_clock(self) -> dict[str, Any]:
        return {"is_open": _us_session_open(self._clock()), "next_open": None}

    # -- instruments and prices
    def instrument(self, symbol: str) -> dict[str, Any] | None:
        symbol = str(symbol or "").upper()
        if not _SYMBOL.match(symbol):
            raise ValueError("symbol must be a US ticker such as VTI or BRK.B")
        if symbol in self._conids:
            return self._conids[symbol]
        self.resolve()
        found = self._ok("GET", "/iserver/secdef/search", query={"symbol": _ibkr_symbol(symbol), "secType": "STK"})
        match = None
        for item in found if isinstance(found, list) else []:
            if not isinstance(item, Mapping) or _wealth_symbol(item.get("symbol")) != symbol:
                continue
            sections = item.get("sections") or []
            stock = any(isinstance(s, Mapping) and s.get("secType") == "STK" for s in sections) or not sections
            exchange = str(item.get("description") or "").upper()
            if stock and exchange in US_EXCHANGES and str(item.get("conid") or "").isdigit():
                match = {"symbol": symbol, "conid": str(item["conid"]), "tradable": True, "fractionable": False,
                         "exchange": exchange, "currency": "USD"}
                break
        if match is not None:
            self._conids[symbol] = match
        return match

    def latest_trade(self, symbol: str) -> dict[str, Any] | None:
        info = self.instrument(symbol)
        if info is None:
            return None
        query = {"conids": info["conid"], "fields": "31"}
        row: Mapping[str, Any] = {}
        for _ in range(2):  # the first snapshot for a conid only subscribes; the second carries the fields
            data = self._ok("GET", "/iserver/marketdata/snapshot", query=query)
            row = next((r for r in data if isinstance(r, Mapping)), {}) if isinstance(data, list) else {}
            if row.get("31") not in (None, ""):
                break
        raw = str(row.get("31") or "").strip()
        if not raw:
            return None
        flag = raw[0] if raw[0] in "CH" else ""
        price = _dec(raw.lstrip("CH"))
        if price is None or price <= 0:
            return None
        at = None
        updated = row.get("_updated")
        if flag != "H" and isinstance(updated, (int, float)) and updated > 0:
            at = datetime.fromtimestamp(updated / 1000, tz=timezone.utc).isoformat()
        return {"price": _s(price), "at": at}

    # -- positions and orders
    def positions(self) -> list[dict[str, Any]]:
        account = self._portfolio()
        rows = self._ok("GET", f"/portfolio/{account}/positions/0")
        out = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, Mapping):
                continue
            symbol = _wealth_symbol(row.get("ticker") or row.get("contractDesc"))
            qty = _dec(row.get("position"))
            if symbol and qty is not None:
                out.append({"symbol": symbol, "qty": _s(qty), "market_value": _s(_dec(row.get("mktValue")))})
        return out

    def _orders(self) -> list[Mapping[str, Any]]:
        self.resolve()
        data = self._ok("GET", "/iserver/account/orders")
        rows = (data or {}).get("orders") if isinstance(data, Mapping) else data
        return [r for r in rows or [] if isinstance(r, Mapping)]

    @staticmethod
    def _view(order: Mapping[str, Any]) -> dict[str, Any]:
        status = str(order.get("order_status") or order.get("status") or "")
        filled = _dec(order.get("cum_fill", order.get("filledQuantity")))
        state = LINE_STATE.get(status.lower().replace(" ", ""), "sent")
        if state == "sent" and filled and filled > 0:
            state = "partial"
        return {"id": str(order.get("order_id") or order.get("orderId") or "") or None, "status": status or None,
                "state": state, "filled_qty": _s(filled) if filled is not None else None,
                "filled_avg_price": _s(_dec(order.get("average_price", order.get("avgPrice")))) or None,
                "submitted_at": None}

    def open_orders(self) -> list[dict[str, Any]]:
        working = []
        for row in self._orders():
            view = self._view(row)
            if view["state"] in ("sent", "partial"):
                working.append({"symbol": _wealth_symbol(row.get("ticker")), "side": str(row.get("side") or "").lower(),
                                "client_order_id": str(row.get("order_ref") or "")})
        return working

    def order_by_client_id(self, client_order_id: str) -> dict[str, Any] | None:
        if not _CLIENT_ORDER_ID.match(str(client_order_id)):
            raise ValueError("client_order_id is malformed")
        for row in self._orders():
            if str(row.get("order_ref") or "") == client_order_id:
                return self._view(row)
        return None

    def order(self, order_id: str) -> dict[str, Any]:
        if not _ORDER_ID.match(str(order_id)):
            raise ValueError("order id is malformed")
        self.resolve()
        return self._view(self._ok("GET", f"/iserver/account/order/status/{order_id}", audit_as="status") or {})

    def submit(self, order: Mapping[str, Any]) -> dict[str, Any]:
        """Place one order, confirming only benign replies; a repeated ``client_order_id`` never places a second."""
        cid = str(order.get("client_order_id") or "")
        if not _CLIENT_ORDER_ID.match(cid):
            raise ValueError("every order needs a deterministic client_order_id")
        account = self.resolve()
        info = self.instrument(str(order["symbol"]))
        if info is None:
            raise BrokerError(f"IBKR does not list {order['symbol']} as a US stock.", status=404)
        existing = self.order_by_client_id(cid)
        if existing:
            return existing
        body = {"acctId": account, "conid": int(info["conid"]), "cOID": cid, "side": str(order["side"]).upper(),
                "orderType": "LMT" if order["type"] == "limit" else "MKT", "tif": str(order["time_in_force"]).upper(),
                "quantity": float(Decimal(str(order["qty"]))), "outsideRTH": False}
        if order["type"] == "limit":
            body["price"] = float(Decimal(str(order["limit_price"])))
        try:
            answer = self._ok("POST", f"/iserver/account/{account}/orders", body={"orders": [body]}, audit_as="submit")
            for _ in range(MAX_REPLIES + 1):
                item = next((a for a in answer if isinstance(a, Mapping)), None) if isinstance(answer, list) \
                    else answer if isinstance(answer, Mapping) else None
                if item is None:
                    raise BrokerError("IBKR's order answer was empty.", retryable=True)
                if item.get("error"):
                    raise BrokerError(f"IBKR rejected the order: {self._scrub(str(item['error']))[:300]}", status=400)
                if item.get("order_id"):
                    view = self._view(item)
                    view["status"] = view["status"] or "Submitted"
                    return view
                reply_id, ids = str(item.get("id") or ""), [str(i) for i in item.get("messageIds") or []]
                messages = [self._scrub(str(m)) for m in item.get("message") or []]
                if not _REPLY_ID.match(reply_id):
                    raise BrokerError("IBKR answered the order with something Wealth does not know.", retryable=True)
                if not ids or any(i not in BENIGN_REPLIES for i in ids):
                    self._ok("POST", f"/iserver/reply/{reply_id}", body={"confirmed": False}, audit_as="reply")
                    raise BrokerError("IBKR asked to confirm: " + " ".join(messages)[:400] + " Wealth did not "
                                      "confirm it, so the order was not placed.", status=409, kind="reply_blocked",
                                      body={"message_ids": ids, "messages": messages})
                answer = self._ok("POST", f"/iserver/reply/{reply_id}", body={"confirmed": True}, audit_as="reply")
            raise BrokerError("IBKR kept asking questions; the order was not confirmed.", kind="reply_blocked")
        except BrokerError as exc:
            if exc.kind != "reply_blocked" and (exc.retryable or exc.status is None):
                found = self.order_by_client_id(cid)  # a lost answer may hide a placed order
                if found:
                    return found
            raise

    def cancel(self, order_id: str) -> None:
        if not _ORDER_ID.match(str(order_id)):
            raise ValueError("order id is malformed")
        account = self.resolve()
        self._ok("DELETE", f"/iserver/account/{account}/order/{order_id}", audit_as="cancel")

    def fills(self, after: str | None = None) -> list[dict[str, Any]]:
        return []  # posted by the Flex connector or a statement; see the module notes

    def ledger_account(self) -> dict[str, Any]:
        account = self.resolve()
        return {"id": f"ibkr-{account[-4:]}", "institution": "Interactive Brokers", "type": "brokerage",
                "currency": "USD", "owners": [{"person_id": "self", "share": "1"}],
                "name": "IBKR paper account" if self.mode == "paper" else "IBKR account"}


def broker(*, environ: Mapping[str, str] | None = None, **kwargs: Any) -> IbkrBroker:
    """An unresolved gateway broker; nothing is read until a method is called."""
    return IbkrBroker(environ=environ, **kwargs)


__all__ = ["ALLOWED", "BENIGN_REPLIES", "IbkrBroker", "TlsPolicy", "allowed", "broker", "gateway_port",
           "https_transport", "is_paper_account", "live_opted_in"]
