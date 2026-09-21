"""Alpaca Trading API v2 order client for guarded, person-confirmed execution.

This is the only module in Wealth that can place or cancel an order, and only
:func:`wealth.execution.tickets.confirm` / :func:`~wealth.execution.tickets.cancel`
call its write methods.  Nothing reachable from the model (MCP tools, the CLI
``dispatch`` operations, ``WealthService.run``) calls those entry points.  The
read-only account connector (``wealth/connectors/alpaca.py``) stays GET-only.

Environments
    Paper is the default.  Live needs ``WEALTH_TRADING_LIVE=alpaca`` in the
    server's environment (plus a typed confirmation in the app the first time).
    Base URLs are constants chosen by mode, never by an input:
    paper ``https://paper-api.alpaca.markets``, live ``https://api.alpaca.markets``;
    latest prices come from ``https://data.alpaca.markets``.

Credentials
    Shared with the read-only connector and loaded with its helpers
    (:func:`wealth.connectors.alpaca.load_keys`, built on
    :func:`wealth.connectors._rest.load_secret`): env ``WEALTH_ALPACA_KEY_ID`` /
    ``WEALTH_ALPACA_SECRET`` or the OS keychain service ``wealth-alpaca``
    (accounts ``key_id`` and ``secret``), with ``WEALTH_ALPACA_PAPER`` saying
    they are paper keys.  Keys serve only the mode they belong to: paper keys
    only ever reach the paper URL, live keys only the live URL.  Someone who
    syncs a live account and wants to paper trade may add dedicated paper keys:
    env ``WEALTH_ALPACA_PAPER_KEY_ID`` / ``WEALTH_ALPACA_PAPER_SECRET`` or
    keychain service ``wealth-alpaca-paper``.  Keys never print or pickle,
    travel only in the ``APCA-API-KEY-ID`` / ``APCA-API-SECRET-KEY`` headers,
    and every error and audit payload is scrubbed of them
    (:func:`wealth.connectors._rest.scrub`).

Allowlist
    Unlike the connector's GET-only client, this client may write, but only
    ``POST /v2/orders`` and ``DELETE /v2/orders/{id}`` (one order); everything
    else is a GET on :data:`ALLOWED`.  Cancel-all, closing positions, account
    configuration and transfers are refused before a request is built, and the
    default transport checks the list again.

Alpaca sources (read 2026-09-21)
    * Orders at Alpaca (order types; time in force; fractional orders are
      ``day`` only; notional orders cannot be replaced; the lifecycle statuses
      new, partially_filled, filled, done_for_day, canceled, expired, replaced,
      pending_cancel, pending_replace, accepted, pending_new, rejected, ...;
      an order can be canceled until it is filled, canceled or expired;
      sub-penny rule: limit prices >= $1 take 2 decimals, < $1 take 4; open
      buy orders reduce buying power immediately):
      https://docs.alpaca.markets/docs/orders-at-alpaca
    * Create an order, ``POST /v2/orders`` (symbol, qty, notional, side, type,
      time_in_force, limit_price, extended_hours, ``client_order_id`` "<= 128
      characters", 403 "Buying power or shares is not sufficient", 422 input
      not recognized; qty and notional cannot be combined):
      https://docs.alpaca.markets/reference/postorder
    * Fractional trading (``fractionable`` asset flag; up to 9 decimals; limit
      orders are supported for fractional and notional orders, ``day`` only;
      no fractional short sales).  The order reference says fractional qty and
      notional work only with market/day orders; the two pages disagree, so
      Wealth never sends ``notional``: it converts a dollar amount into a qty at
      the limit price and sends limit/day:
      https://docs.alpaca.markets/docs/fractional-trading
    * Account (``status`` ACTIVE, ``trading_blocked``, ``account_blocked``,
      ``trade_suspended_by_user``, ``buying_power``,
      ``non_marginable_buying_power``, ``cash``):
      https://docs.alpaca.markets/reference/getaccount-1
    * Asset (``tradable``, ``fractionable``, ``status`` active/inactive,
      ``GET /v2/assets/{symbol_or_asset_id}``; paper and live servers):
      https://docs.alpaca.markets/reference/get-v2-assets-symbol_or_asset_id
    * Paper trading (separate paper keys; same API spec; paper base URL):
      https://docs.alpaca.markets/docs/paper-trading
    * Rate limit: 200 requests per minute per account, HTTP 429 beyond it:
      https://alpaca.markets/support/usage-limit-api-calls
    * Also used: ``GET /v2/clock`` (is_open, next_open), ``GET /v2/orders/{id}``,
      ``GET /v2/orders:by_client_order_id``, ``DELETE /v2/orders/{id}``,
      ``GET /v2/positions``, ``GET /v2/account/activities/FILL`` (id, order_id,
      qty, price, side, symbol, transaction_time) and
      ``GET /v2/stocks/{symbol}/trades/latest`` on the market data host.
"""

from __future__ import annotations

import json
from collections import deque
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Mapping

from ...connectors import _rest
from ...connectors import alpaca as _connector

NAME = "alpaca"
PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"
HOSTS = frozenset({"paper-api.alpaca.markets", "api.alpaca.markets", "data.alpaca.markets"})
RATE_LIMIT_PER_MINUTE = 200
WINDOW_BUDGET = RATE_LIMIT_PER_MINUTE - 20  # per client, leaving room for the read connector
TIMEOUT_SECONDS = 20.0
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_CLIENT_ORDER_ID = 128
# Optional dedicated paper keys; the shared keys belong to the read connector.
PAPER_ENV = ("WEALTH_ALPACA_PAPER_KEY_ID", "WEALTH_ALPACA_PAPER_SECRET")
PAPER_KEYCHAIN = "wealth-alpaca-paper"
_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
_ORDER_ID = re.compile(r"^[0-9a-fA-F-]{8,64}$")
_CLIENT_ORDER_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
# The only requests this client can make: (method, host kind, path pattern).
ALLOWED: tuple[tuple[str, str, re.Pattern[str]], ...] = tuple(
    (method, host, re.compile(pattern)) for method, host, pattern in (
        ("GET", "api", r"/v2/account"),
        ("GET", "api", r"/v2/clock"),
        ("GET", "api", r"/v2/assets/[A-Z][A-Z0-9.]{0,9}"),
        ("GET", "api", r"/v2/positions"),
        ("GET", "api", r"/v2/orders"),
        ("GET", "api", r"/v2/orders/[0-9a-fA-F-]{8,64}"),
        ("GET", "api", r"/v2/orders:by_client_order_id"),
        ("GET", "api", r"/v2/account/activities/FILL"),
        ("GET", "data", r"/v2/stocks/[A-Z][A-Z0-9.]{0,9}/trades/latest"),
        ("POST", "api", r"/v2/orders"),
        ("DELETE", "api", r"/v2/orders/[0-9a-fA-F-]{8,64}"),
    ))

# (method, url, headers, body, timeout) -> (status, body bytes)
Transport = Callable[[str, str, Mapping[str, str], "bytes | None", float], "tuple[int, bytes]"]


class BrokerError(Exception):
    """An Alpaca failure; the message never contains a credential."""

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False,
                 body: Any = None):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.body = body


def allowed(method: str, host_kind: str, path: str) -> bool:
    """Whether ``method path`` on the api or data host is a request this client may make."""
    return any(m == method and h == host_kind and p.fullmatch(path) for m, h, p in ALLOWED)


# -- credentials ---------------------------------------------------------------

class AlpacaKeys:
    """A key id and secret for one mode; never printed or pickled."""

    __slots__ = ("mode", "source", "_key", "_secret")

    def __init__(self, key_id: Any, secret: Any, *, mode: str, source: str):
        self.mode, self.source = mode, source
        self._key = key_id if isinstance(key_id, _rest.Secret) else _rest.Secret(str(key_id), source, "Alpaca key id")
        self._secret = secret if isinstance(secret, _rest.Secret) else _rest.Secret(str(secret), source,
                                                                                   "Alpaca secret")

    def headers(self) -> dict[str, str]:
        return {"APCA-API-KEY-ID": self._key.reveal(), "APCA-API-SECRET-KEY": self._secret.reveal()}

    def secrets(self) -> tuple[str, str]:
        return self._key.reveal(), self._secret.reveal()

    def __repr__(self) -> str:
        return f"AlpacaKeys(mode={self.mode!r}, source={self.source!r}, key_id='****', secret='****')"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("AlpacaKeys cannot be serialized")


def load_keys(mode: str, environ: Mapping[str, str] | None = None, runner: Callable[..., Any] | None = None,
              platform: str | None = None) -> AlpacaKeys | None:
    """Keys for ``mode`` ('paper' or 'live'), or ``None``.

    The shared connector keys count only for the mode ``WEALTH_ALPACA_PAPER``
    says they belong to; paper mode may also use dedicated paper keys.
    """
    if mode not in ("paper", "live"):
        raise ValueError("mode must be paper or live")
    environ = os.environ if environ is None else environ
    if mode == "paper":
        key = _rest.load_secret(PAPER_ENV[0], PAPER_KEYCHAIN, "key_id", label="Alpaca paper key id",
                                environ=environ, runner=runner, platform=platform)
        secret = _rest.load_secret(PAPER_ENV[1], PAPER_KEYCHAIN, "secret", label="Alpaca paper secret",
                                   environ=environ, runner=runner, platform=platform) if key else None
        if key and secret:
            return AlpacaKeys(key, secret, mode="paper", source=key.source)
    if _connector.paper_default(environ) != (mode == "paper"):
        return None
    shared = _connector.load_keys(environ, runner=runner, platform=platform)
    if shared is None:
        return None
    return AlpacaKeys(shared.key_id, shared.secret, mode=mode, source=shared.source)


def base_url(mode: str) -> str:
    if mode == "paper":
        return PAPER_URL
    if mode == "live":
        return LIVE_URL
    raise ValueError("mode must be paper or live")


# -- transport ---------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BrokerError("Alpaca answered with a redirect; refused.")


def urllib_transport(method: str, url: str, headers: Mapping[str, str], body: bytes | None,
                     timeout: float) -> tuple[int, bytes]:
    """HTTPS with certificate verification, the host and request allowlists, no redirects and a size cap."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in HOSTS:
        raise BrokerError("Refusing to call a non-Alpaca or non-HTTPS address.")
    if not allowed(method, "data" if parsed.hostname == "data.alpaca.markets" else "api", parsed.path):
        raise BrokerError(f"Refusing {method} {parsed.path}: not an order request Wealth makes.")
    request = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ssl.create_default_context()),
                                         _NoRedirect())
    failure: BrokerError | None = None
    status, data = 0, b""
    try:
        with opener.open(request, timeout=timeout) as response:
            status = response.status
            data = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:  # keep status and body; the exception object carries the URL
        status = exc.code
        try:
            data = exc.read(MAX_RESPONSE_BYTES + 1)
        except OSError:
            data = b""
    except BrokerError as exc:
        failure = BrokerError(str(exc))
    except ssl.SSLError:
        failure = BrokerError("TLS verification with Alpaca failed.", retryable=True)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        failure = BrokerError(f"Could not reach Alpaca ({type(exc).__name__}).", retryable=True)
    if failure is not None:
        raise failure  # raised outside the except block, so no chained exception holds the URL or headers
    if len(data) > MAX_RESPONSE_BYTES:
        raise BrokerError("Alpaca returned an oversized response; refused.")
    return status, data


# Looked up at call time so tests substitute a fake (tests never reach the network).
default_transport: Transport = urllib_transport


def default_sleep(seconds: float) -> None:
    time.sleep(seconds)


def default_clock() -> float:
    return time.monotonic()


# -- redaction -----------------------------------------------------------------

_REDACT_KEYS = frozenset({"apca-api-key-id", "apca-api-secret-key", "authorization", "key_id", "secret",
                          "api_key", "token", "nonce", "nonce_hash"})


def redact(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    """A JSON value with account numbers masked, credential fields removed and secrets scrubbed."""
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered == "account_number":
                digits = re.sub(r"\D", "", str(item or ""))
                out[key] = f"****{digits[-4:]}" if len(digits) >= 4 else "****"
            elif lowered in _REDACT_KEYS:
                out[key] = "****"
            else:
                out[key] = redact(item, secrets)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(item, secrets) for item in value]
    if isinstance(value, str):
        return _rest.scrub(value, *secrets)
    return value


# -- client --------------------------------------------------------------------

class AlpacaOrders:
    """A thin, audited Alpaca REST client for one mode.

    ``audit(kind, payload)`` receives every order request and response
    (redacted, without headers); ``tickets`` writes it to the append-only
    ``orders`` table.
    """

    def __init__(self, keys: AlpacaKeys, *, transport: Transport | None = None,
                 sleep: Callable[[float], None] | None = None, clock: Callable[[], float] | None = None,
                 audit: Callable[[str, dict[str, Any]], None] | None = None, timeout: float = TIMEOUT_SECONDS):
        if not isinstance(keys, AlpacaKeys):
            raise TypeError("keys must be AlpacaKeys")
        self.keys = keys
        self.mode = keys.mode
        self.base = base_url(keys.mode)
        self._transport = transport
        self._sleep = sleep
        self._clock = clock or default_clock
        self._sent: deque[float] = deque()
        self.audit = audit
        self.timeout = timeout

    def __repr__(self) -> str:
        return f"AlpacaOrders(mode={self.mode!r})"

    def _scrub(self, value: Any) -> Any:
        return redact(value, self.keys.secrets())

    def _request(self, method: str, path: str, *, query: Mapping[str, Any] | None = None,
                 body: Mapping[str, Any] | None = None, data_host: bool = False,
                 audit_as: str | None = None) -> tuple[int, Any]:
        if not allowed(method, "data" if data_host else "api", path):
            raise BrokerError(f"Refusing {method} {path}: not an order request Wealth makes.")
        url = (DATA_URL if data_host else self.base) + path + (("?" + urllib.parse.urlencode(query)) if query else "")
        payload = json.dumps(body, separators=(",", ":")).encode() if body is not None else None
        headers = {**self.keys.headers(), "Accept": "application/json", "User-Agent": "wealth-harness/0.2 (orders)"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        # Stay under Alpaca's documented 200 requests a minute (sliding window, with headroom); a ticket's
        # few dozen requests go out without waiting.
        now = self._clock()
        while self._sent and now - self._sent[0] >= 60.0:
            self._sent.popleft()
        if len(self._sent) >= WINDOW_BUDGET:
            (self._sleep or default_sleep)(60.0 - (now - self._sent[0]))
            self._sent.popleft()
        self._sent.append(self._clock())
        if audit_as and self.audit:
            self.audit("request", {"action": audit_as, "method": method, "path": path, "query": dict(query or {}),
                                   "body": self._scrub(dict(body)) if body is not None else None})
        try:
            status, raw = (self._transport or default_transport)(method, url, headers, payload, self.timeout)
        except BrokerError as exc:
            if audit_as and self.audit:
                self.audit("response", {"action": audit_as, "method": method, "path": path,
                                        "error": self._scrub(str(exc))})
            raise BrokerError(self._scrub(str(exc)), retryable=exc.retryable) from None
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, ValueError):
            parsed = {"message": "unparseable response"}
        clean = self._scrub(parsed)
        if audit_as and self.audit:
            self.audit("response", {"action": audit_as, "method": method, "path": path, "status": status,
                                    "body": clean})
        return status, clean

    def _ok(self, method: str, path: str, **kwargs: Any) -> Any:
        status, body = self._request(method, path, **kwargs)
        if 200 <= status < 300:
            return body
        message = body.get("message") if isinstance(body, Mapping) else None
        hint = {401: "Check the Alpaca keys (paper and live keys differ).",
                403: "Alpaca refused: buying power or shares are not sufficient, or the key cannot trade.",
                404: "Alpaca does not know this item.",
                422: "Alpaca rejected the order's fields.",
                429: "Alpaca's rate limit was reached; try again in a minute."}.get(status, "")
        raise BrokerError(f"Alpaca answered HTTP {status}. {hint} {message or ''}".strip(), status=status,
                          retryable=status == 429 or status >= 500, body=body)

    # reads (used by the pre-trade checks and status refresh)
    def account(self) -> dict[str, Any]:
        return self._ok("GET", "/v2/account")

    def clock(self) -> dict[str, Any]:
        return self._ok("GET", "/v2/clock")

    def asset(self, symbol: str) -> dict[str, Any] | None:
        try:
            return self._ok("GET", f"/v2/assets/{_symbol(symbol)}")
        except BrokerError as exc:
            if exc.status == 404:
                return None
            raise

    def positions(self) -> list[dict[str, Any]]:
        value = self._ok("GET", "/v2/positions")
        return value if isinstance(value, list) else []

    def open_orders(self) -> list[dict[str, Any]]:
        value = self._ok("GET", "/v2/orders", query={"status": "open", "limit": 100})
        return value if isinstance(value, list) else []

    def last_price(self, symbol: str) -> str | None:
        body = self._ok("GET", f"/v2/stocks/{_symbol(symbol)}/trades/latest", data_host=True)
        trade = body.get("trade") if isinstance(body, Mapping) else None
        price = (trade or {}).get("p")
        return str(price) if price not in (None, "") else None

    def order(self, order_id: str) -> dict[str, Any]:
        if not _ORDER_ID.match(str(order_id)):
            raise ValueError("order id is malformed")
        return self._ok("GET", f"/v2/orders/{order_id}", audit_as="status")

    def order_by_client_id(self, client_order_id: str) -> dict[str, Any] | None:
        if not _CLIENT_ORDER_ID.match(str(client_order_id)):
            raise ValueError("client_order_id is malformed")
        try:
            return self._ok("GET", "/v2/orders:by_client_order_id",
                            query={"client_order_id": client_order_id}, audit_as="lookup")
        except BrokerError as exc:
            if exc.status == 404:
                return None
            raise

    def fills(self, after: str | None = None) -> list[dict[str, Any]]:
        query: dict[str, Any] = {"direction": "asc", "page_size": 100}
        if after:
            query["after"] = after
        value = self._ok("GET", "/v2/account/activities/FILL", query=query)
        return value if isinstance(value, list) else []

    # writes: reachable only from tickets.confirm / tickets.cancel, i.e. the person's tap in the app
    def submit(self, order: Mapping[str, Any]) -> dict[str, Any]:
        """POST one order; a repeated ``client_order_id`` resolves to the existing order, never a second one."""
        body = dict(order)
        cid = str(body.get("client_order_id") or "")
        if not _CLIENT_ORDER_ID.match(cid) or len(cid) > MAX_CLIENT_ORDER_ID:
            raise ValueError("every order needs a deterministic client_order_id")
        if "notional" in body:
            raise ValueError("Wealth sends qty with a limit price, never notional")
        existing = self.order_by_client_id(cid)
        if existing:
            return existing
        try:
            return self._ok("POST", "/v2/orders", body=body, audit_as="submit")
        except BrokerError as exc:
            # A timeout, a 5xx or a duplicate-id rejection may hide an accepted order: look it up first.
            if exc.retryable or exc.status in (None, 409, 422):
                found = self.order_by_client_id(cid)
                if found:
                    return found
            raise

    def cancel(self, order_id: str) -> None:
        if not _ORDER_ID.match(str(order_id)):
            raise ValueError("order id is malformed")
        self._ok("DELETE", f"/v2/orders/{order_id}", audit_as="cancel")


def _symbol(value: Any) -> str:
    symbol = str(value or "").strip().upper()
    if not _SYMBOL.match(symbol):
        raise ValueError("symbol must be a US ticker such as VTI or BRK.B")
    return symbol


def client(mode: str, *, environ: Mapping[str, str] | None = None, transport: Transport | None = None,
           audit: Callable[[str, dict[str, Any]], None] | None = None, runner: Callable[..., Any] | None = None,
           platform: str | None = None, **kwargs: Any) -> AlpacaOrders | None:
    """A client for ``mode`` when its keys are configured, else ``None``."""
    keys = load_keys(mode, environ, runner=runner, platform=platform)
    return AlpacaOrders(keys, transport=transport, audit=audit, **kwargs) if keys else None


__all__ = ["ALLOWED", "AlpacaKeys", "AlpacaOrders", "BrokerError", "DATA_URL", "LIVE_URL", "PAPER_URL", "allowed",
           "base_url", "client", "load_keys", "redact", "urllib_transport"]
