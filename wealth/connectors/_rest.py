"""Shared plumbing for the read-only JSON connectors (Alpaca, Cuenca).

* :class:`Secret` holds a credential that never prints, pickles or copies.
* :func:`load_secret` reads one value from an environment variable, else the OS
  keychain (macOS ``security``; ``secret-tool`` on Linux) with an argument
  list, never a shell.
* :class:`ReadOnlyClient` is the only way the connectors talk to a provider.
  Every request goes through :meth:`ReadOnlyClient.guard`, which refuses any
  method but ``GET`` and any path outside the connector's allowlist, and the
  default transport refuses non-GET again.  Nothing here can place an order,
  move money or change an account.
* Retries: HTTP 429 and 5xx (and network errors) are retried with exponential
  backoff, honouring ``Retry-After`` or a rate-limit reset header when the
  provider sends one, spaced by a per-connector minimum interval and bounded by
  an overall deadline.
* Every error message is scrubbed of the credentials, ``Authorization`` values
  and credential-looking query parameters.
"""

from __future__ import annotations

import base64
from email.utils import parsedate_to_datetime
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
from typing import Any, Callable, Iterable, Mapping
import urllib.error
import urllib.parse
import urllib.request


MAX_RESPONSE_BYTES = 20 * 1024 * 1024


# -- credentials ------------------------------------------------------------

class Secret:
    """A credential that never prints itself."""

    __slots__ = ("_value", "source", "label")

    def __init__(self, value: str, source: str, label: str = "credential"):
        if not isinstance(value, str) or not value.strip() or re.search(r"[\s\x00-\x1f]", value.strip()):
            raise ValueError(f"the {label} is empty or malformed")
        self._value = value.strip()
        self.source = source
        self.label = label

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return f"Secret({self.label!r}, source={self.source!r}, value='****')"

    __str__ = __repr__

    def __reduce__(self):  # keep it out of pickles, caches and copies to disk
        raise TypeError("Secret cannot be serialized")


def keychain_command(service: str, account: str, platform: str | None = None) -> list[str] | None:
    platform = platform or sys.platform
    if platform == "darwin":
        return ["security", "find-generic-password", "-s", service, "-a", account, "-w"]
    if platform.startswith("linux") and shutil.which("secret-tool"):
        return ["secret-tool", "lookup", "service", service, "account", account]
    return None


def load_secret(env_name: str, service: str, account: str, *, label: str,
                environ: Mapping[str, str] | None = None, runner: Callable[..., Any] | None = None,
                platform: str | None = None) -> Secret | None:
    """``env_name`` if set, else the keychain item (service, account), else ``None``."""
    environ = os.environ if environ is None else environ
    value = (environ.get(env_name) or "").strip()
    if value:
        return Secret(value, "env", label)
    command = keychain_command(service, account, platform)
    if command is None:
        return None
    try:
        done = (runner or subprocess.run)(command, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    secret = (getattr(done, "stdout", "") or "").strip()
    if getattr(done, "returncode", 1) != 0 or not secret:
        return None
    try:
        return Secret(secret, "keychain", label)
    except ValueError:
        return None


_SECRET_PARAMS = re.compile(r"(?i)([?&;](?:t|token|key|secret|api_key|api_secret|apikey|password)=)[^&\s\"'<>]+")
_AUTH_HEADER = re.compile(r"(?i)((?:authorization|apca-api-key-id|apca-api-secret-key|x-cuenca-token)\s*[:=]\s*)"
                          r"(?:basic\s+|bearer\s+)?[^\s,;\"'}]+")


def scrub(text: Any, *secrets: str | None) -> str:
    """Remove credentials (raw, URL-quoted and inside Basic auth) from text meant for people or logs."""
    value = str(text)
    for secret in secrets:
        if not secret:
            continue
        for form in {secret, urllib.parse.quote(secret, safe=""), urllib.parse.quote_plus(secret)}:
            value = value.replace(form, "****")
    value = _AUTH_HEADER.sub(r"\1****", value)
    return _SECRET_PARAMS.sub(r"\1****", value)


def basic_auth(user: Secret, password: Secret) -> str:
    return "Basic " + base64.b64encode(f"{user.reveal()}:{password.reveal()}".encode()).decode()


# -- errors -----------------------------------------------------------------

class ConnectorError(Exception):
    """A provider failure whose message never holds a credential."""

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False,
                 secrets: Iterable[str | None] = ()):
        super().__init__(scrub(message, *secrets))
        self.status = status
        self.retryable = retryable


class ReadOnlyViolation(ConnectorError):
    """A request that could change something was attempted; it was never sent."""


class ConnectorTimeout(ConnectorError):
    """The provider did not answer within the overall deadline."""


# -- transport --------------------------------------------------------------

# (method, url, headers, timeout) -> (status, response headers, body)
Transport = Callable[[str, str, Mapping[str, str], float], tuple[int, Mapping[str, str], bytes]]


def https_transport(allowed_hosts: Iterable[str]) -> Transport:
    """HTTPS GET with certificate verification, a size cap and no redirects off ``allowed_hosts``."""
    hosts = frozenset(h.lower() for h in allowed_hosts)

    def allowed(url: str) -> bool:
        parsed = urllib.parse.urlsplit(url)
        return parsed.scheme == "https" and (parsed.hostname or "").lower() in hosts

    class _Redirects(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if not allowed(newurl) or req.get_method() != "GET":
                raise ConnectorError("The provider redirected to another address; refused.")
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    def transport(method: str, url: str, headers: Mapping[str, str], timeout: float):
        if method != "GET":
            raise ReadOnlyViolation(f"Refusing a {method} request: this connector is read-only.")
        if not allowed(url):
            raise ConnectorError("Refusing to call an address outside the provider's API host or without HTTPS.")
        request = urllib.request.Request(url, headers=dict(headers), method="GET")
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ssl.create_default_context()),
                                             _Redirects())
        failure: ConnectorError | None = None
        status, reply_headers, chunks = 0, {}, []
        try:
            response = opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:  # keep status and body; the exception object carries the URL
            response = exc
        except ConnectorError as exc:
            failure = ConnectorError(str(exc))
            response = None
        except ssl.SSLError:
            failure, response = ConnectorError("TLS verification with the provider failed."), None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            failure, response = ConnectorError(f"Could not reach the provider ({type(exc).__name__}).",
                                               retryable=True), None
        if response is not None:
            try:
                status = int(getattr(response, "status", None) or response.getcode())
                reply_headers = {k.lower(): v for k, v in response.headers.items()}
                size = 0
                while True:
                    chunk = response.read(1 << 16)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        failure = ConnectorError("The provider returned more than 20 MB; refused.")
                        break
                    chunks.append(chunk)
            except (OSError, TimeoutError) as exc:
                failure = ConnectorError(f"The provider connection failed ({type(exc).__name__}).", retryable=True)
            finally:
                response.close()
        if failure is not None:
            raise failure  # raised outside the except block, so no chained exception holds the URL or headers
        return status, reply_headers, b"".join(chunks)

    return transport


def default_sleep(seconds: float) -> None:
    time.sleep(seconds)


def default_clock() -> float:
    return time.monotonic()


# -- client -----------------------------------------------------------------

class ReadOnlyClient:
    """GET-only JSON client with an allowlist, backoff and scrubbed errors."""

    def __init__(self, *, provider: str, base_url: str, headers: Callable[[], Mapping[str, str]],
                 allowed_paths: Iterable[str], transport: Transport, secrets: Callable[[], Iterable[str]],
                 sleep: Callable[[float], None] | None = None, clock: Callable[[], float] | None = None,
                 timeout: float = 120.0, min_interval: float = 0.0, max_attempts: int = 6,
                 hints: Mapping[int, str] | None = None):
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("base_url must be an https URL")
        self.provider = provider
        self.base_url = base_url.rstrip("/")
        self._host = parsed.hostname.lower()
        self._headers = headers
        self._patterns = tuple(re.compile(p) for p in allowed_paths)
        self._transport = transport
        self._secrets = secrets
        self._sleep = sleep or default_sleep
        self._clock = clock or default_clock
        self._deadline = self._clock() + timeout
        self._timeout = timeout
        self._min_interval = min_interval
        self._max_attempts = max_attempts
        self._last: float | None = None
        self._hints = dict(hints or {})
        self.requests = 0

    def __repr__(self) -> str:
        return f"ReadOnlyClient({self.provider!r}, {self.base_url!r})"

    def guard(self, method: str, path: str) -> str:
        """The request path if it is a GET on the allowlist; otherwise :class:`ReadOnlyViolation`."""
        if method != "GET":
            raise ReadOnlyViolation(f"{self.provider}: refusing {method} {path.split('?')[0]}; the connector is read-only.")
        bare = urllib.parse.urlsplit(path)
        if bare.scheme or bare.netloc:
            if bare.scheme != "https" or (bare.hostname or "").lower() != self._host:
                raise ReadOnlyViolation(f"{self.provider}: refusing a request to another host.")
        route = "/" + bare.path.lstrip("/")
        if ".." in route.split("/") or not any(p.fullmatch(route) for p in self._patterns):
            raise ReadOnlyViolation(f"{self.provider}: refusing GET {route}; it is not a read endpoint this connector uses.")
        return route + (f"?{bare.query}" if bare.query else "")

    def _wait(self, seconds: float) -> None:
        if self._clock() + seconds > self._deadline:
            raise ConnectorTimeout(f"{self.provider} did not answer within {int(self._timeout)} seconds; try again later.",
                                   retryable=True)
        if seconds > 0:
            self._sleep(seconds)

    def _retry_after(self, headers: Mapping[str, str]) -> float | None:
        value = (headers.get("retry-after") or "").strip()
        if value:
            if re.fullmatch(r"\d+(\.\d+)?", value):
                return float(value)
            try:
                return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
            except (TypeError, ValueError):
                return None
        reset = (headers.get("x-ratelimit-reset") or "").strip()
        if re.fullmatch(r"\d+", reset):
            number = int(reset)
            return float(max(0, number - int(time.time()))) if number > 10**9 else float(number)
        return None

    def get_json(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        route = self.guard("GET", path)
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
        url = self.base_url + route + (("&" if "?" in route else "?") + query if query else "")
        secrets = [s for s in self._secrets() if s]
        attempt = 0
        while True:
            if self._last is not None and self._min_interval:
                gap = self._min_interval - (self._clock() - self._last)
                if gap > 0:
                    self._wait(gap)
            remaining = self._deadline - self._clock()
            if remaining <= 0:
                raise ConnectorTimeout(f"{self.provider} did not answer within {int(self._timeout)} seconds; "
                                       "try again later.", retryable=True)
            failure: ConnectorError | None = None
            status, headers, body = 0, {}, b""
            self._last = self._clock()
            self.requests += 1
            try:
                status, headers, body = self._transport("GET", url, self._headers(), min(remaining, 60.0))
            except ReadOnlyViolation:
                raise
            except ConnectorError as exc:
                failure = type(exc)(str(exc), status=exc.status, retryable=exc.retryable, secrets=secrets)
            except Exception as exc:  # a host transport: unknown semantics, not retried; its text may echo headers
                failure = ConnectorError(f"Could not reach {self.provider} ({type(exc).__name__}: {exc})", secrets=secrets)
            if failure is not None:
                if not failure.retryable or attempt + 1 >= self._max_attempts:
                    raise failure
                self._wait(min(30.0, 2.0 ** attempt))
                attempt += 1
                continue
            headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
            if status == 429 or status >= 500:
                if attempt + 1 >= self._max_attempts:
                    raise ConnectorError(f"{self.provider} answered HTTP {status} after {attempt + 1} attempts; "
                                         "try again later.", status=status, retryable=True)
                hinted = self._retry_after(headers)
                backoff = min(30.0, 2.0 ** attempt)
                self._wait(min(60.0, max(backoff, hinted or 0.0, self._min_interval)))
                attempt += 1
                continue
            if status >= 400 or status < 200:
                detail = ""
                try:
                    message = json.loads(body.decode("utf-8", "replace") or "{}")
                    if isinstance(message, dict):
                        detail = str(message.get("message") or message.get("error") or "")[:200]
                except ValueError:
                    pass
                hint = self._hints.get(status, "")
                raise ConnectorError(f"{self.provider} answered HTTP {status}" + (f": {detail}" if detail else "")
                                     + (f". {hint}" if hint else "."), status=status, secrets=secrets)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ConnectorError(f"{self.provider} returned more than 20 MB; refused.")
            try:
                return json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise ConnectorError(f"{self.provider} returned something that is not JSON.") from None


__all__ = [
    "ConnectorError", "ConnectorTimeout", "ReadOnlyClient", "ReadOnlyViolation", "Secret", "Transport",
    "basic_auth", "https_transport", "keychain_command", "load_secret", "scrub",
]
