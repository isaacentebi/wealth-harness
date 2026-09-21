"""Alpaca Trading API v2 order client for guarded, person-confirmed execution.

This is the only module in Wealth that can place or cancel an order, and only
:func:`wealth.execution.tickets.confirm` / :func:`~wealth.execution.tickets.cancel`
call its write methods.  Nothing reachable from the model (MCP tools, the CLI
``dispatch`` operations, ``WealthService.run``) imports those entry points.
The read-only account connector (``wealth/connectors/alpaca.py``) is separate
and never places orders.

Environments
    Paper is the default.  Live needs ``WEALTH_TRADING_LIVE=alpaca`` in the
    server's environment (plus a typed confirmation in the app the first time).
    Base URLs are constants chosen by mode, never by an input:
    paper ``https://paper-api.alpaca.markets``, live ``https://api.alpaca.markets``;
    latest prices come from ``https://data.alpaca.markets``.

Credentials
    Paper keys: ``WEALTH_ALPACA_PAPER_KEY_ID`` / ``WEALTH_ALPACA_PAPER_SECRET``
    or the OS keychain service ``wealth-alpaca-paper`` (accounts ``key_id`` and
    ``secret``); ``WEALTH_ALPACA_KEY_ID``/``WEALTH_ALPACA_SECRET`` are used for
    paper only when ``WEALTH_ALPACA_PAPER`` is set (the read connector's
    convention for paper keys).  Live keys: ``WEALTH_ALPACA_KEY_ID`` /
    ``WEALTH_ALPACA_SECRET`` (without ``WEALTH_ALPACA_PAPER``) or keychain
    service ``wealth-alpaca``.  Keys are wrapped so ``repr`` never shows them,
    they travel only in the ``APCA-API-KEY-ID``/``APCA-API-SECRET-KEY`` headers
    and every error and audit payload is scrubbed of them.

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
      not recognized; qty/notional cannot be combined):
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
      ``GET /v2/positions/{symbol}``, ``GET /v2/account/activities/FILL``
      (id, order_id, qty, price, side, symbol, transaction_time) and
      ``GET /v2/stocks/{symbol}/trades/latest`` on the market data host.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Mapping

NAME = "alpaca"
PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"
HOSTS = frozenset({"paper-api.alpaca.markets", "api.alpaca.markets", "data.alpaca.markets"})
RATE_LIMIT_PER_MINUTE = 200
MIN_INTERVAL = 60.0 / RATE_LIMIT_PER_MINUTE
TIMEOUT_SECONDS = 20.0
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_CLIENT_ORDER_ID = 128
TERMINAL = frozenset({"filled", "canceled", "expired", "rejected", "replaced", "done_for_day_final"})
CANCELABLE = frozenset({"new", "accepted", "pending_new", "partially_filled", "accepted_for_bidding",
                        "done_for_day", "held", "calculated", "stopped", "suspended"})
_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
_ORDER_ID = re.compile(r"^[0-9a-fA-F-]{8,64}$")
_CLIENT_ORDER_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

PAPER_ENV = ("WEALTH_ALPACA_PAPER_KEY_ID", "WEALTH_ALPACA_PAPER_SECRET")
LIVE_ENV = ("WEALTH_ALPACA_KEY_ID", "WEALTH_ALPACA_SECRET")
PAPER_FLAG = "WEALTH_ALPACA_PAPER"
KEYCHAIN = {"paper": "wealth-alpaca-paper", "live": "wealth-alpaca"}

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


class _Secret:
    __slots__ = ("_value",)

    def __init__(self, value: str):
        value = (value or "").strip()
        if not value or re.search(r"[\s\x00-\x1f]", value):
            raise ValueError("an Alpaca credential is empty or malformed")
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "'****'"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("credentials cannot be serialized")


class AlpacaKeys:
    """A key id and secret for one mode; never printed or pickled."""

    __slots__ = ("mode", "source", "_key", "_secret")

    def __init__(self, key_id: str, secret: str, *, mode: str, source: str):
        self.mode, self.source = mode, source
        self._key, self._secret = _Secret(key_id), _Secret(secret)

    def headers(self) -> dict[str, str]:
        return {"APCA-API-KEY-ID": self._key.reveal(), "APCA-API-SECRET-KEY": self._secret.reveal()}

    def secrets(self) -> tuple[str, str]:
        return self._key.reveal(), self._secret.reveal()

    def __repr__(self) -> str:
        return f"AlpacaKeys(mode={self.mode!r}, source={self.source!r}, key_id='****', secret='****')"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("AlpacaKeys cannot be serialized")


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _keychain(service: str, account: str, runner: Callable[..., Any] | None, platform: str | None) -> str | None:
    runner = runner or subprocess.run
    platform = platform or sys.platform
    if platform == "darwin":
        command = ["security", "find-generic-password", "-s", service, "-a", account, "-w"]
    elif platform.startswith("linux") and shutil.which("secret-tool"):
        command = ["secret-tool", "lookup", "service", service, "account", account]
    else:
        return None
    try:
        done = runner(command, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    value = (getattr(done, "stdout", "") or "").strip()
    return value if getattr(done, "returncode", 1) == 0 and value else None


def load_keys(mode: str, environ: Mapping[str, str] | None = None, runner: Callable[..., Any] | None = None,
              platform: str | None = None) -> AlpacaKeys | None:
    """Keys for ``mode`` ('paper' or 'live') from the environment or the OS keychain, else ``None``."""
    if mode not in ("paper", "live"):
        raise ValueError("mode must be paper or live")
    environ = os.environ if environ is None else environ
    paper_flag = _truthy(environ.get(PAPER_FLAG))
    candidates = [PAPER_ENV] if mode == "paper" else []
    if (mode == "paper") == paper_flag:
        candidates.append(LIVE_ENV)  # the read connector's variables, when they hold keys of this mode
    for key_name, secret_name in candidates:
        key, secret = (environ.get(key_name) or "").strip(), (environ.get(secret_name) or "").strip()
        if key and secret:
            try:
                return AlpacaKeys(key, secret, mode=mode, source="env")
            except ValueError:
                return None
    service = KEYCHAIN[mode]
    key = _keychain(service, "key_id", runner, platform)
    secret = _keychain(service, "secret", runner, platform) if key else None
    if key and secret:
        try:
            return AlpacaKeys(key, secret, mode=mode, source="keychain")
        except ValueError:
            return None
    return None


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
    """HTTPS with certificate verification, an Alpaca host allowlist, no redirects and a size cap."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in HOSTS:
        raise BrokerError("Refusing to call a non-Alpaca or non-HTTPS address.")
    request = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ssl.create_default_context()),
                                         _NoRedirect())
    failure: BrokerError | None = None
    status, data = 0, b""
    try:
        with opener.open(request, timeout=timeout) as response:
            status = response.status
            data = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
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
        raise failure
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

_REDACT_KEYS = frozenset({"account_number", "apca-api-key-id", "apca-api-secret-key", "authorization",
                          "key_id", "secret", "api_key", "token"})


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
        text = value
        for secret in secrets:
            if secret:
                text = text.replace(secret, "****")
        return text
    return value


# -- client --------------------------------------------------------------------

class AlpacaOrders:
    """A thin, audited Alpaca REST client for one mode.

    ``audit(kind, payload)`` receives every request and response (redacted,
    without headers); ``tickets`` writes it to the append-only ``orders`` table.
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
        self._sleep = sleep or default_sleep
        self._clock = clock or default_clock
        self._last: float | None = None
        self.audit = audit
        self.timeout = timeout

    def __repr__(self) -> str:
        return f"AlpacaOrders(mode={self.mode!r})"

    # plumbing
    def _scrub(self, value: Any) -> Any:
        return redact(value, self.keys.secrets())

    def _request(self, method: str, path: str, *, query: Mapping[str, Any] | None = None,
                 body: Mapping[str, Any] | None = None, data_host: bool = False,
                 audit_as: str | None = None) -> tuple[int, Any]:
        base = DATA_URL if data_host else self.base
        url = base + path + (("?" + urllib.parse.urlencode(query)) if query else "")
        payload = json.dumps(body, separators=(",", ":")).encode() if body is not None else None
        headers = {**self.keys.headers(), "Accept": "application/json", "User-Agent": "wealth-harness/0.2 (orders)"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if self._last is not None:
            wait = MIN_INTERVAL - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        self._last = self._clock()
        if audit_as and self.audit:
            self.audit("request", {"method": method, "path": path, "query": dict(query or {}),
                                   "body": self._scrub(dict(body)) if body is not None else None})
        transport = self._transport or default_transport
        try:
            status, raw = transport(method, url, headers, payload, self.timeout)
        except BrokerError as exc:
            if audit_as and self.audit:
                self.audit("response", {"method": method, "path": path, "error": self._scrub(str(exc))})
            raise BrokerError(self._scrub(str(exc)), retryable=exc.retryable) from None
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, ValueError):
            parsed = {"message": "unparseable response"}
        clean = self._scrub(parsed)
        if audit_as and self.audit:
            self.audit("response", {"method": method, "path": path, "status": status, "body": clean})
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

    # reads (used by the pre-trade checks; safe from any path)
    def account(self) -> dict[str, Any]:
        return self._ok("GET", "/v2/account")

    def clock(self) -> dict[str, Any]:
        return self._ok("GET", "/v2/clock")

    def asset(self, symbol: str) -> dict[str, Any] | None:
        symbol = _symbol(symbol)
        try:
            return self._ok("GET", f"/v2/assets/{symbol}")
        except BrokerError as exc:
            if exc.status == 404:
                return None
            raise

    def position(self, symbol: str) -> dict[str, Any] | None:
        symbol = _symbol(symbol)
        try:
            return self._ok("GET", f"/v2/positions/{symbol}")
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
        symbol = _symbol(symbol)
        body = self._ok("GET", f"/v2/stocks/{symbol}/trades/latest", data_host=True)
        trade = (body or {}).get("trade") if isinstance(body, Mapping) else None
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

    # writes (reachable only from tickets.confirm / tickets.cancel, i.e. the web confirmation route)
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
           audit: Callable[[str, dict[str, Any]], None] | None = None, **kwargs: Any) -> AlpacaOrders | None:
    """A client for ``mode`` when its keys are configured, else ``None``."""
    keys = load_keys(mode, environ, runner=kwargs.pop("runner", None), platform=kwargs.pop("platform", None))
    return AlpacaOrders(keys, transport=transport, audit=audit, **kwargs) if keys else None


__all__ = ["AlpacaKeys", "AlpacaOrders", "BrokerError", "CANCELABLE", "DATA_URL", "LIVE_URL", "PAPER_URL",
           "TERMINAL", "base_url", "client", "load_keys", "redact", "urllib_transport"]
