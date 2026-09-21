"""Read-only Interactive Brokers connector over the IBKR Flex Web Service.

The person creates an *Activity Flex Query* (XML) in the IBKR portal and a Flex
Web Service token.  :class:`IbkrFlexConnector` asks IBKR to run that query
(``SendRequest``), polls for the generated statement (``GetStatement``) and
turns it into the same :class:`wealth.ingest.IngestProposal` a statement upload
produces.  Nothing is saved here; the person confirms the summary first.

Nothing in this module can trade, transfer or change the account: the Flex Web
Service only returns reports.

Credentials
    The token comes only from ``WEALTH_IBKR_FLEX_TOKEN`` or the OS keychain
    (macOS ``security``, service ``wealth-ibkr-flex``; ``secret-tool`` on
    Linux).  It is wrapped in :class:`FlexToken`, whose ``repr``/``str`` never
    show it, and is scrubbed from every error message (including request URLs).
    Query ids are not secret.

IBKR sources (read 2026-09-21)
    * Flex Web Service v3 (SendRequest/GetStatement, ``t``/``q``/``v`` params,
      Success/Fail responses with ReferenceCode/Url/ErrorCode/ErrorMessage, the
      User-Agent requirement):
      https://www.ibkrguides.com/complianceportal/complianceportal/flexwebserviceversion3.htm
    * Current endpoint host and request walkthrough:
      https://www.interactivebrokers.com/campus/ibkr-api-page/flex-web-service/
    * Error codes 1001-1021, which are "try again shortly", and the rate limit
      attached to 1018 ("one request per second, 10 requests per minute (per
      token)"); 1019 is "Statement generation in progress":
      https://www.ibkrguides.com/clientportal/performanceandstatements/flex3error.htm
    * Token (Flex Web Service Configuration, expiry, IP restriction):
      https://www.ibkrguides.com/clientportal/performanceandstatements/flex-web-service.htm
    * Creating an Activity Flex Query (sections, XML format, period):
      https://www.ibkrguides.com/brokerportal/performanceandstatements/activityflex.htm
    * Section and field reference:
      https://www.ibkrguides.com/reportingreference/reportguide/activity%20flex%20query%20reference.htm
      https://www.ibkrguides.com/reportingreference/reportguide/open%20positionsfq.htm
      https://www.ibkrguides.com/reportingreference/reportguide/tradesfq.htm
      https://www.ibkrguides.com/reportingreference/reportguide/cash%20transactionsfq.htm
      https://ibkrguides.com/reportingreference/reportguide/corporate%20actionsfq.htm
      https://www.ibkrguides.com/reportingreference/reportguide/currency%20conversion%20ratefq.htm
      https://www.ibkrguides.com/reportingreference/reportguide/changeinnav_fq.htm

XML elements read: ``FlexStatement`` (accountId, fromDate, toDate,
whenGenerated), ``AccountInformation`` (base currency, accountType),
``EquitySummaryByReportDateInBase`` (NAV by date) or ``ChangeInNAV``,
``CashReportCurrency`` (ending cash per currency), ``OpenPosition`` (SUMMARY
and LOT rows), ``Trade`` (EXECUTION rows; ``assetCategory="CASH"`` rows such
as ``EUR.USD`` are FX conversions), ``CashTransaction`` (DETAIL rows),
``CorporateAction`` and ``ConversionRate``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
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
import xml.etree.ElementTree as ET

from ..ingest.common import envelope, out, slug
from ..ingest.model import _summary, build_proposal, diff_proposals, proposal_digest
from ..ingest.redact import last4, mask_account


NAME = "ibkr_flex"
INSTITUTION = "Interactive Brokers"
SEND_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest"
GET_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement"
API_VERSION = "3"
# IBKR's v3 guide: "Programmatic access requires the User-Agent HTTP header to be set.
# Accepted values are: Blackberry or Java."
USER_AGENT = "Java"
MAX_RESPONSE_BYTES = 50 * 1024 * 1024
TIMEOUT_SECONDS = 120.0
KEYCHAIN_SERVICE = "wealth-ibkr-flex"
TOKEN_ENV = "WEALTH_IBKR_FLEX_TOKEN"

ERRORS: dict[int, str] = {
    1001: "Statement could not be generated at this time. Please try again shortly.",
    1003: "Statement is not available.",
    1004: "Statement is incomplete at this time. Please try again shortly.",
    1005: "Settlement data is not ready at this time. Please try again shortly.",
    1006: "FIFO P/L data is not ready at this time. Please try again shortly.",
    1007: "MTM P/L data is not ready at this time. Please try again shortly.",
    1008: "MTM and FIFO P/L data is not ready at this time. Please try again shortly.",
    1009: "The server is under heavy load. Statement could not be generated at this time. Please try again shortly.",
    1010: "Legacy Flex Queries are no longer supported. Please convert over to Activity Flex.",
    1011: "Service account is inactive.",
    1012: "Token has expired.",
    1013: "IP restriction.",
    1014: "Query is invalid.",
    1015: "Token is invalid.",
    1016: "Account is invalid.",
    1017: "Reference code is invalid.",
    1018: "Too many requests have been made from this token. Please try again shortly.",
    1019: "Statement generation in progress. Please try again shortly.",
    1020: "Invalid request or unable to validate request.",
    1021: "Statement could not be retrieved at this time. Please try again shortly.",
}
THROTTLED = 1018      # rate limit: 1 request/second, 10 requests/minute per token
IN_PROGRESS = 1019    # the statement is still being generated; poll again
RETRYABLE = frozenset({1001, 1004, 1005, 1006, 1007, 1008, 1009, THROTTLED, IN_PROGRESS, 1021})
_HINTS = {
    1010: "Recreate it as an Activity Flex Query.",
    1011: "Enable the Flex Web Service in the IBKR portal (Reporting > Flex Queries > Flex Web Service Configuration).",
    1012: "Generate a new token in Flex Web Service Configuration and store it in the keychain again.",
    1013: "The token is restricted to another IP address; clear or update the restriction in Flex Web Service Configuration.",
    1014: "Check the query id shown next to the Activity Flex Query in the IBKR portal.",
    1015: "Generate a new token in Flex Web Service Configuration and store it in the keychain again.",
    1020: "Check the token and the query id.",
}
_MIN_SPACING = 1.0     # never call faster than one request per second
_THROTTLE_WAIT = 10.0  # after 1018, wait long enough to fall back under 10 requests/minute


# -- credentials ------------------------------------------------------------

class FlexToken:
    """A Flex Web Service token that never prints itself."""

    __slots__ = ("_value", "source")

    def __init__(self, value: str, source: str):
        if not isinstance(value, str) or not value.strip() or re.search(r"[\s\x00-\x1f]", value.strip()):
            raise ValueError("the IBKR Flex token is empty or malformed")
        self._value = value.strip()
        self.source = source

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return f"FlexToken(source={self.source!r}, value='****')"

    __str__ = __repr__

    def __reduce__(self):  # keep it out of pickles, caches and copies to disk
        raise TypeError("FlexToken cannot be serialized")


def load_token(environ: Mapping[str, str] | None = None, runner: Callable[..., Any] | None = None,
               platform: str | None = None) -> FlexToken | None:
    """The token from ``WEALTH_IBKR_FLEX_TOKEN``, else the OS keychain, else ``None``.

    The keychain is read with an argument list (no shell); its output never
    reaches an error message.
    """
    environ = os.environ if environ is None else environ
    value = (environ.get(TOKEN_ENV) or "").strip()
    if value:
        return FlexToken(value, "env")
    runner = runner or subprocess.run
    platform = platform or sys.platform
    if platform == "darwin":
        command = ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"]
    elif platform.startswith("linux") and shutil.which("secret-tool"):
        command = ["secret-tool", "lookup", "service", KEYCHAIN_SERVICE]
    else:
        return None
    try:
        done = runner(command, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    secret = (getattr(done, "stdout", "") or "").strip()
    if getattr(done, "returncode", 1) != 0 or not secret:
        return None
    try:
        return FlexToken(secret, "keychain")
    except ValueError:
        return None


def token_source(environ: Mapping[str, str] | None = None, runner: Callable[..., Any] | None = None,
                 platform: str | None = None) -> str | None:
    token = load_token(environ, runner, platform)
    return token.source if token else None


def scrub(text: Any, *secrets: str | None) -> str:
    """Remove secrets and any ``t=`` URL parameter from text meant for people or logs."""
    value = str(text)
    for secret in secrets:
        if secret:
            value = value.replace(secret, "****")
            quoted = urllib.parse.quote(secret, safe="")
            if quoted != secret:
                value = value.replace(quoted, "****")
    return re.sub(r"(?i)([?&;]t=)[^&\s\"'<>]+", r"\1****", value)


# -- errors -----------------------------------------------------------------

class FlexError(Exception):
    """An IBKR Flex failure.  The message is always free of the token."""

    def __init__(self, message: str, *, code: int | None = None, retryable: bool = False):
        super().__init__(scrub(message))
        self.code = code
        self.retryable = retryable

    @classmethod
    def from_code(cls, code: int, server_message: str | None = None) -> "FlexError":
        text = ERRORS.get(code) or scrub(server_message or "Unknown IBKR Flex error.")
        hint = _HINTS.get(code)
        return cls(f"IBKR Flex error {code}: {text}" + (f" {hint}" if hint else ""), code=code,
                   retryable=code in RETRYABLE)


class FlexTransportError(FlexError):
    """Network, TLS, HTTP or size failure talking to IBKR."""


class FlexTimeout(FlexError):
    """IBKR did not return the statement within the overall deadline."""


# -- transport --------------------------------------------------------------

Transport = Callable[[str, float], bytes]


class _HttpsOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        if parsed.scheme != "https" or not _ibkr_host(parsed.hostname):
            raise FlexTransportError("IBKR redirected to a non-IBKR or non-HTTPS address; refused.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _ibkr_host(host: str | None) -> bool:
    host = (host or "").lower()
    return host == "interactivebrokers.com" or host.endswith(".interactivebrokers.com")


def urllib_transport(url: str, timeout: float) -> bytes:
    """HTTPS GET with certificate verification, IBKR's User-Agent and a 50 MB cap."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not _ibkr_host(parsed.hostname):
        raise FlexTransportError("Refusing to call a non-IBKR or non-HTTPS address.")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/xml, text/xml"})
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ssl.create_default_context()), _HttpsOnly())
    failure: FlexTransportError | None = None
    chunks: list[bytes] = []
    try:
        with opener.open(request, timeout=timeout) as response:
            size = 0
            while True:
                chunk = response.read(1 << 16)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    failure = FlexTransportError(f"IBKR returned more than {MAX_RESPONSE_BYTES // (1024 * 1024)} MB; refused.")
                    break
                chunks.append(chunk)
    except FlexTransportError as exc:
        failure = FlexTransportError(str(exc))
    except urllib.error.HTTPError as exc:  # message only: the exception object carries the URL
        failure = FlexTransportError(f"IBKR answered HTTP {exc.code}.", retryable=exc.code >= 500 or exc.code == 429)
    except ssl.SSLError:
        failure = FlexTransportError("TLS verification with IBKR failed.")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        failure = FlexTransportError(f"Could not reach IBKR ({type(exc).__name__}).", retryable=True)
    if failure is not None:
        raise failure  # raised outside the except block, so no chained exception holds the URL
    return b"".join(chunks)


# Looked up at call time so tests (and hosts) can substitute them.
default_transport: Transport = urllib_transport


def default_sleep(seconds: float) -> None:
    time.sleep(seconds)


def default_clock() -> float:
    return time.monotonic()


# -- XML --------------------------------------------------------------------

def _xml(body: bytes) -> ET.Element:
    head = body[:4096].lstrip()
    if b"<!DOCTYPE" in body or b"<!ENTITY" in body:
        raise FlexError("IBKR response contains a DTD or entity declaration; refused.")
    if not head.startswith(b"<"):
        raise FlexError("IBKR returned something that is not XML (is the query's format set to XML?).")
    try:
        return ET.fromstring(body)
    except ET.ParseError:
        raise FlexError("IBKR returned malformed XML.") from None


def _response(root: ET.Element) -> dict[str, Any]:
    """Read a FlexStatementResponse: Success/Warn/Fail, ReferenceCode, Url, ErrorCode."""
    def text(tag: str) -> str:
        node = root.find(tag)
        return (node.text or "").strip() if node is not None else ""

    code_text = text("ErrorCode")
    code = int(code_text) if code_text.isdigit() else None
    return {"status": text("Status").lower(), "reference": text("ReferenceCode"), "url": text("Url"),
            "code": code, "message": text("ErrorMessage")}


# -- fetch ------------------------------------------------------------------

def _query_id(value: Any) -> str:
    text = str(value).strip() if isinstance(value, (str, int)) and not isinstance(value, bool) else ""
    if not re.fullmatch(r"\d{1,15}", text):
        raise ValueError("query_id must be the numeric Activity Flex Query id from the IBKR portal")
    return text


def _delay(attempt: int, code: int | None) -> float:
    base = min(20.0, 2.0 * 1.5 ** attempt)
    return max(base, _THROTTLE_WAIT) if code == THROTTLED else max(base, _MIN_SPACING)


def fetch_statement(token: FlexToken, query_id: Any, *, transport: Transport | None = None,
                    sleep: Callable[[float], None] | None = None, clock: Callable[[], float] | None = None,
                    timeout: float = TIMEOUT_SECONDS, send_url: str = SEND_URL) -> bytes:
    """Run the Flex query and return the statement XML (``FlexQueryResponse``).

    SendRequest is retried on IBKR's "try again shortly" codes; GetStatement is
    polled with backoff while IBKR answers 1019 (generation in progress) or
    another retryable code.  1018 (too many requests) waits at least 10 s.
    Everything stops after ``timeout`` seconds.
    """
    if not isinstance(token, FlexToken):
        raise TypeError("token must be a FlexToken")
    query = _query_id(query_id)
    transport = transport or default_transport
    sleep = sleep or default_sleep
    clock = clock or default_clock
    secret = token.reveal()
    deadline = clock() + timeout

    def wait(seconds: float) -> None:
        if clock() + seconds > deadline:
            raise FlexTimeout(f"IBKR did not return the statement within {int(timeout)} seconds; try again in a few minutes.",
                              retryable=True)
        sleep(seconds)

    def call(url: str) -> bytes:
        remaining = deadline - clock()
        if remaining <= 0:
            raise FlexTimeout(f"IBKR did not return the statement within {int(timeout)} seconds; try again in a few minutes.",
                              retryable=True)
        failure: FlexError | None = None
        try:
            body = transport(url, min(remaining, 60.0))
        except FlexError as exc:
            failure = type(exc)(scrub(str(exc), secret), code=exc.code, retryable=exc.retryable)
        except Exception as exc:  # a host transport: unknown semantics, so not retried; its message may echo the URL
            failure = FlexTransportError(scrub(f"Could not reach IBKR ({type(exc).__name__}: {exc})", secret))
        if failure is not None:
            raise failure
        if not isinstance(body, (bytes, bytearray)):
            raise FlexTransportError("The transport returned no bytes.")
        if len(body) > MAX_RESPONSE_BYTES:
            raise FlexTransportError(f"IBKR returned more than {MAX_RESPONSE_BYTES // (1024 * 1024)} MB; refused.")
        return bytes(body)

    def url_for(base: str, q: str) -> str:
        return base + "?" + urllib.parse.urlencode({"t": secret, "q": q, "v": API_VERSION})

    attempt = 0
    while True:
        try:
            root = _xml(call(url_for(send_url, query)))
        except FlexTimeout:
            raise
        except FlexError as exc:
            if not exc.retryable:
                raise
            wait(_delay(attempt, None))
            attempt += 1
            continue
        reply = _response(root)
        if reply["status"] == "success" and reply["reference"]:
            break
        code = reply["code"]
        if code is None:
            raise FlexError("IBKR rejected the request without an error code.")
        if code not in RETRYABLE:
            raise FlexError.from_code(code, reply["message"])
        wait(_delay(attempt, code))
        attempt += 1

    reference = reply["reference"]
    if not re.fullmatch(r"[A-Za-z0-9]{1,40}", reference):
        raise FlexError("IBKR returned an unexpected reference code.")
    parsed = urllib.parse.urlsplit(reply["url"]) if reply["url"] else None
    get_url = GET_URL
    if parsed and parsed.scheme == "https" and _ibkr_host(parsed.hostname):
        get_url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))

    attempt = 0
    while True:
        wait(_delay(attempt, None) if attempt else _MIN_SPACING)
        try:
            body = call(url_for(get_url, reference))
            root = _xml(body)
        except FlexTimeout:
            raise
        except FlexError as exc:
            if not exc.retryable:
                raise
            attempt += 1
            continue
        if root.tag == "FlexQueryResponse":
            return body
        if root.tag != "FlexStatementResponse":
            raise FlexError(f"IBKR returned an unexpected document <{root.tag[:40]}>.")
        reply = _response(root)
        code = reply["code"]
        if code is None:
            raise FlexError("IBKR returned neither a statement nor an error code.")
        if code not in RETRYABLE:
            raise FlexError.from_code(code, reply["message"])
        if code == THROTTLED:
            wait(_THROTTLE_WAIT)
        attempt += 1


# -- parsing ----------------------------------------------------------------

_SECTIONS = {
    "AccountInformation": "account_information", "EquitySummaryByReportDateInBase": "equity_summary",
    "ChangeInNAV": "change_in_nav", "CashReportCurrency": "cash_report", "OpenPosition": "open_positions",
    "Trade": "trades", "CashTransaction": "cash_transactions", "CorporateAction": "corporate_actions",
    "ConversionRate": "conversion_rates", "Lot": "closed_lots",
}


def _dec(value: Any) -> Decimal | None:
    text = str(value or "").strip().replace(",", "")
    if not text or text.lower() in {"--", "n/a", "na"}:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _date(value: Any) -> str | None:
    """Flex dates: yyyyMMdd, yyyy-MM-dd, MM/dd/yyyy, optionally with ;HHmmss or a time."""
    text = str(value or "").strip()
    if not text:
        return None
    head = re.split(r"[;,T ]", text)[0]
    for pattern in ("%Y%m%d", "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d-%b-%y"):
        try:
            return datetime.strptime(head, pattern).date().isoformat()
        except ValueError:
            continue
    return None


def parse_flex(body: bytes) -> list[dict[str, Any]]:
    """Split a ``FlexQueryResponse`` into one dict of section rows per account statement."""
    root = _xml(body)
    if root.tag != "FlexQueryResponse":
        raise FlexError("This is not an Activity Flex Query statement (FlexQueryResponse).")
    statements = []
    for node in root.iter("FlexStatement"):
        entry: dict[str, Any] = {name: [] for name in _SECTIONS.values()}
        entry["account_id"] = node.get("accountId") or ""
        entry["from_date"] = _date(node.get("fromDate"))
        entry["to_date"] = _date(node.get("toDate"))
        entry["when_generated"] = node.get("whenGenerated") or ""
        entry["sections"] = sorted({child.tag for child in node})
        for element in node.iter():
            name = _SECTIONS.get(element.tag)
            if name:
                entry[name].append(dict(element.attrib))
        statements.append(entry)
    if not statements:
        raise FlexError("The Flex statement contains no accounts.")
    return statements


# -- mapping ----------------------------------------------------------------

_US_EXCHANGES = frozenset({
    "NYSE", "NASDAQ", "ARCA", "NYSEARCA", "AMEX", "NYSEAMERICAN", "BATS", "CBOE", "BYX", "EDGX", "EDGEA", "IEX",
    "ISLAND", "PINK", "OTC", "OTCBB", "PSX", "BEX", "CHX", "NYSENAT", "MEMX", "LTSE", "IBKRATS", "SMART",
})
_LEDGER_LISTING = {"NYSE": "NYSE", "NASDAQ": "NASDAQ", "ARCA": "ARCA", "NYSEARCA": "ARCA", "PINK": "OTC", "OTC": "OTC",
                   "OTCBB": "OTC", "LSE": "LSE", "LSEETF": "LSE", "LSEIOB1": "LSE", "MEXI": "BMV", "BMV": "BMV"}
_DERIVATIVES = frozenset({"OPT", "FOP", "FUT", "WAR", "CFD", "IOPT", "FSFOP", "FSOPT", "BAG"})
_ASSET_TYPE = {"STK": "stock", "ETF": "etf", "FUND": "fund", "BOND": "bond", "BILL": "bond", "OPT": "options",
               "FOP": "options", "FUT": "futures", "WAR": "warrants", "CMDTY": "commodity"}
_CORPORATE = {
    "FS": "forward split", "RS": "reverse split", "SD": "stock dividend", "SO": "spin-off", "TC": "merger",
    "CD": "cash dividend", "BC": "bond conversion", "BM": "bond maturity", "DI": "dividend rights issue",
    "ED": "expired dividend right", "RI": "rights issue", "SR": "subscribable rights issue", "TO": "tender",
    "HI": "choice dividend issue", "HD": "choice dividend delivery", "IC": "issue change", "DW": "delisted",
    "OR": "subscribe rights", "PI": "share purchase issue", "PV": "proxy vote", "CO": "contract consolidation",
    "CC": "contract soulte", "CH": "CUSIP change", "GV": "asset purchase", "CA": "contract adjustment",
}
_SPLIT = re.compile(r"(?i)\bSPLIT\s+(\d+(?:\.\d+)?)\s+FOR\s+(\d+(?:\.\d+)?)\b")


def _norm(text: Any) -> str:
    return " ".join(str(text or "").split())


class _Scrubber:
    """Masks the full account id and the account holder's name in descriptions."""

    def __init__(self, statements: list[dict[str, Any]]):
        self.pairs: list[tuple[str, str]] = []
        for statement in statements:
            account = statement["account_id"]
            if account:
                self.pairs.append((account, mask_account(account) or "****"))
            for info in statement["account_information"]:
                for field in ("name", "acctAlias", "primaryEmail"):
                    value = _norm(info.get(field))
                    if len(value) >= 3:
                        self.pairs.append((value, "[account holder]"))
        self.pairs.sort(key=lambda pair: -len(pair[0]))

    def __call__(self, text: Any) -> str:
        value = _norm(text)
        for secret, mask in self.pairs:
            value = re.sub(re.escape(secret), mask, value, flags=re.IGNORECASE)
        return value


def _account_ids(statements: list[dict[str, Any]]) -> list[str]:
    """Replicates build_proposal's id rule so transactions can name their account."""
    used: set[str] = set()
    ids = []
    for index, statement in enumerate(statements):
        tail = last4(statement["account_id"])
        base = f"ibkr-{tail}" if tail else f"ibkr-{slug(_label(statement), 20)}" if _label(statement) else f"ibkr-{index + 1}"
        candidate, counter = base, 2
        while candidate in used:
            candidate, counter = f"{base}-{counter}", counter + 1
        used.add(candidate)
        ids.append(candidate)
    return ids


def _label(statement: dict[str, Any]) -> str:
    info = statement["account_information"][0] if statement["account_information"] else {}
    kind = _norm(info.get("accountType"))
    return f"{INSTITUTION} {kind}".strip()


def _base_currency(statement: dict[str, Any]) -> str | None:
    for row in statement["account_information"] + statement["equity_summary"]:
        ccy = _norm(row.get("currency")).upper()
        if re.fullmatch(r"[A-Z]{3}", ccy):
            return ccy
    return None


def _instrument_facts(row: Mapping[str, Any]) -> dict[str, Any]:
    symbol = _norm(row.get("symbol")).upper()
    exchange = _norm(row.get("listingExchange")).upper()
    isin = _norm(row.get("isin") or (row.get("securityID") if row.get("securityIDType") == "ISIN" else "")).upper()
    facts: dict[str, Any] = {"symbol": symbol, "underlying_symbol": _norm(row.get("underlyingSymbol")).upper() or symbol}
    if exchange:
        facts["listing_exchange"] = exchange
        facts["venue"] = "us" if exchange in _US_EXCHANGES else "bmv" if exchange in {"MEXI", "BMV"} else "other"
    if re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}\d", isin):
        facts["isin"] = isin
        facts["issuer_domicile"] = {"US": "US", "IE": "IE", "MX": "MX"}.get(isin[:2], "other")
    if row.get("description"):
        facts["name"] = _norm(row["description"])[:120]
    category = _norm(row.get("assetCategory")).upper()
    sub = _norm(row.get("subCategory")).upper()
    facts["asset_category"] = category
    if category in _DERIVATIVES:
        facts["asset_class"] = "derivative"
    elif sub == "ETF" or category in {"ETF", "FUND"}:
        facts["asset_class"] = "fund"
    elif category == "STK":
        facts["asset_class"] = "equity"
    elif category in {"BOND", "BILL"}:
        facts["asset_class"] = "fixed_income"
    return facts


def _external_id(prefix: str, identifier: Any, fallback: Iterable[Any], warnings: list[str]) -> str:
    text = _norm(identifier)
    if re.fullmatch(r"[A-Za-z0-9.\-]{1,40}", text):
        return f"IBKR-{prefix}{text}"
    content = "|".join(str(part) for part in fallback)
    warnings.append("An IBKR row had no transaction id; it was identified by its content, so a corrected re-sync may "
                    "not match it.")
    return f"IBKR-H{hashlib.sha256(content.encode()).hexdigest()[:20]}"


def _tx(account_id: str, identifier: str, kind: str, when: str, amount: Decimal | None, currency: str,
        description: str, **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": identifier, "dedupe_hash": identifier, "account_id": account_id, "date": when,
        "settlement_date": extra.pop("settlement_date", None), "description": description,
        "amount": out(amount) if amount is not None else None, "currency": currency, "type": kind,
        "symbol": extra.pop("symbol", None), "quantity": extra.pop("quantity", None), "price": extra.pop("price", None),
        "fees": extra.pop("fees", None), "balance": None, "page": None, "installment": None,
    }
    row.update({k: v for k, v in extra.items() if v is not None})
    return row


def _closed_lots(statement) -> dict[str, list[dict[str, str]]]:
    """CLOSED_LOT rows by the closing trade's transactionID: which lots a sale relieved."""
    lots: dict[str, list[dict[str, str]]] = {}
    for row in statement["closed_lots"]:
        acquired = _date(row.get("openDateTime")) or _date(row.get("tradeDate"))
        quantity, cost = _dec(row.get("quantity")), _dec(row.get("cost"))
        key = _norm(row.get("transactionID"))
        if key and acquired and quantity is not None and cost is not None:
            lots.setdefault(key, []).append({"acquired_on": acquired, "quantity": out(abs(quantity)),
                                             "cost_basis": out(abs(cost))})
    return lots


def _trades(statement, account_id, scrub_text, warnings) -> list[dict[str, Any]]:
    rows = statement["trades"]
    closed = _closed_lots(statement)
    details = {(_norm(r.get("levelOfDetail")).upper() or "EXECUTION") for r in rows}
    level = "EXECUTION" if "EXECUTION" in details else "ORDER"
    result = []
    for row in rows:
        if (_norm(row.get("levelOfDetail")).upper() or "EXECUTION") != level:
            continue
        when = _date(row.get("tradeDate")) or _date(row.get("dateTime"))
        currency = _norm(row.get("currency")).upper()
        symbol = _norm(row.get("symbol")).upper()
        quantity, price = _dec(row.get("quantity")), _dec(row.get("tradePrice"))
        proceeds, net_cash = _dec(row.get("proceeds")), _dec(row.get("netCash"))
        commission = _dec(row.get("ibCommission")) or Decimal(0)
        taxes = _dec(row.get("taxes")) or Decimal(0)
        commission_ccy = _norm(row.get("ibCommissionCurrency")).upper() or currency
        side = _norm(row.get("buySell")).upper()
        category = _norm(row.get("assetCategory")).upper()
        identifier = _external_id("T", row.get("tradeID") or row.get("transactionID"),
                                  (account_id, when, symbol, quantity, price, side), warnings)
        settle = _date(row.get("settleDateTarget"))
        base = {"settlement_date": settle, "symbol": symbol or None}
        if not when or quantity is None or not currency:
            warnings.append(f"An IBKR trade in {symbol or 'an unknown symbol'} has no date, quantity or currency; skipped.")
            continue
        if "(CA.)" in side or _norm(row.get("transactionType")).lower() in {"tradecancel", "tradecorrect"}:
            result.append(_tx(account_id, identifier, "trade_correction", when, None, currency,
                              scrub_text(f"{side} {symbol}"), **base,
                              not_posted_reason="IBKR trade cancellation or correction; check the original trade by hand"))
            continue
        if category == "CASH":
            pair = symbol.split(".")
            if len(pair) != 2 or proceeds is None or quantity == 0:
                result.append(_tx(account_id, identifier, "fx", when, None, currency, scrub_text(symbol), **base,
                                  not_posted_reason="FX trade without a currency pair or proceeds"))
                continue
            first, second = pair
            if quantity > 0 and proceeds < 0:
                sold, sold_ccy, bought, bought_ccy = proceeds, second, quantity, first
            elif quantity < 0 and proceeds > 0:
                sold, sold_ccy, bought, bought_ccy = quantity, first, proceeds, second
            else:
                result.append(_tx(account_id, identifier, "fx", when, None, currency, scrub_text(symbol), **base,
                                  not_posted_reason="FX trade whose legs have the same sign"))
                continue
            result.append(_tx(account_id, identifier, "fx", when, sold, sold_ccy,
                              f"FX {side} {out(abs(quantity))} {symbol} @ {out(price) if price is not None else '?'}",
                              settlement_date=settle, to_amount=out(abs(bought)), to_currency=bought_ccy))
            if commission:
                result.append(_tx(account_id, identifier + "-fee", "fee", when, -abs(commission), commission_ccy,
                                  f"Commission on FX {symbol}", settlement_date=settle))
            continue
        facts = _instrument_facts(row)
        if category in _DERIVATIVES:
            result.append(_tx(account_id, identifier, "buy" if side.startswith("BUY") else "sell", when, net_cash,
                              currency, scrub_text(f"{side} {symbol}"), **base, instrument=facts,
                              not_posted_reason=f"{category} trade: the ledger has no contract multiplier for derivatives"))
            continue
        kind = "buy" if side.startswith("BUY") or (not side and quantity > 0) else "sell"
        same_ccy = commission_ccy == currency
        amount = net_cash if net_cash is not None and same_ccy else (
            (proceeds + taxes) if proceeds is not None else None)
        if amount is None and price is not None:
            gross = abs(quantity) * price
            amount = -(gross + abs(commission)) if kind == "buy" else gross - abs(commission)
        result.append(_tx(account_id, identifier, kind, when, amount, currency,
                          scrub_text(f"{side or kind.upper()} {symbol} {facts.get('name') or ''}"),
                          settlement_date=settle, symbol=symbol, quantity=out(abs(quantity)),
                          price=out(abs(price)) if price is not None else None,
                          fees=out(abs(commission)) if same_ccy and commission else None, instrument=facts,
                          closed_lots=closed.get(_norm(row.get("transactionID"))) if kind == "sell" else None))
        if commission and not same_ccy:
            result.append(_tx(account_id, identifier + "-fee", "fee", when, -abs(commission), commission_ccy,
                              f"Commission on {symbol}", settlement_date=settle))
    return result


_CASH_TYPES = {
    "dividends": "dividend", "payment in lieu of dividends": "dividend",
    "withholding tax": "tax_withheld", "871(m) withholding": "tax_withheld",
    "broker interest received": "interest", "bond interest received": "interest",
    "broker interest paid": "fee", "bond interest paid": "fee",
    "deposits/withdrawals": "deposit_or_withdrawal", "deposits & withdrawals": "deposit_or_withdrawal",
    "deposits and withdrawals": "deposit_or_withdrawal",
    "other fees": "fee", "commission adjustments": "fee", "advisor fees": "fee", "client fees": "fee",
    "broker fees": "fee",
}
_SIGN = {"dividend": 1, "interest": 1, "tax_withheld": -1, "fee": -1}
_SIGN_REASON = {
    "dividend": "negative dividend (a reversal or correction); check it against the original dividend",
    "interest": "negative interest received (a reversal or correction)",
    "tax_withheld": "withholding tax refund; record it once the original withholding is identified",
    "fee": "fee credit or rebate",
}


def _cash_transactions(statement, account_id, scrub_text, warnings) -> list[dict[str, Any]]:
    result = []
    occurrences: dict[tuple, int] = {}
    for row in statement["cash_transactions"]:
        level = _norm(row.get("levelOfDetail")).upper()
        if level and level != "DETAIL":
            continue
        amount = _dec(row.get("amount"))
        when = _date(row.get("dateTime")) or _date(row.get("reportDate")) or _date(row.get("settleDate"))
        currency = _norm(row.get("currency")).upper()
        printed = _norm(row.get("type"))
        symbol = _norm(row.get("symbol")).upper() or None
        description = scrub_text(row.get("description") or printed)
        if amount is None or not when or not currency:
            warnings.append(f"An IBKR cash line ({printed or 'unknown type'}) has no amount, date or currency; skipped.")
            continue
        key = (when, str(amount), printed, description)
        occurrences[key] = occurrences.get(key, 0) + 1
        identifier = _external_id("C", row.get("transactionID"), (account_id, *key, occurrences[key]), warnings)
        kind = _CASH_TYPES.get(printed.lower())
        extra: dict[str, Any] = {"settlement_date": _date(row.get("settleDate")), "symbol": symbol}
        if symbol:
            extra["instrument"] = _instrument_facts(row)
        if kind == "deposit_or_withdrawal":
            kind = "deposit" if amount > 0 else "withdrawal"
        if kind is None:
            extra["not_posted_reason"] = f"IBKR cash type {printed!r} is not mapped to a ledger kind"
            kind = "other"
        elif kind in _SIGN and amount * _SIGN[kind] <= 0:
            extra["not_posted_reason"] = _SIGN_REASON[kind]
        elif kind == "dividend" and not symbol:
            extra["not_posted_reason"] = "dividend without a symbol"
        result.append(_tx(account_id, identifier, kind, when, amount, currency, description, **extra))
    return result


def _corporate_actions(statement, account_id, scrub_text, warnings) -> list[dict[str, Any]]:
    result = []
    rows = [r for r in statement["corporate_actions"]
            if (_norm(r.get("levelOfDetail")).upper() or "DETAIL") == "DETAIL"]
    per_action: dict[str, int] = {}
    for row in rows:
        action = _norm(row.get("actionID"))
        if action:
            per_action[action] = per_action.get(action, 0) + 1
    for row in rows:
        code = _norm(row.get("type")).upper()
        name = _CORPORATE.get(code, f"corporate action {code or '?'}")
        when = _date(row.get("dateTime")) or _date(row.get("reportDate"))
        symbol = _norm(row.get("symbol")).upper() or None
        currency = _norm(row.get("currency")).upper()
        quantity = _dec(row.get("quantity"))
        proceeds = _dec(row.get("proceeds")) or Decimal(0)
        text = scrub_text(row.get("description") or row.get("actionDescription") or name)
        identifier = _external_id("A", row.get("transactionID") or row.get("actionID"),
                                  (account_id, when, code, symbol, quantity), warnings)
        if not when or not currency:
            warnings.append(f"An IBKR corporate action ({name}) has no date or currency; skipped.")
            continue
        match = _SPLIT.search(_norm(row.get("description")) + " " + _norm(row.get("actionDescription")))
        single = per_action.get(_norm(row.get("actionID")), 1) <= 1
        facts = _instrument_facts(row)
        if code in {"FS", "RS"} and match and single and symbol and Decimal(match.group(2)) > 0:
            ratio = Decimal(match.group(1)) / Decimal(match.group(2))
            result.append(_tx(account_id, identifier, "split", when, proceeds if proceeds > 0 else None, currency, text,
                              symbol=symbol, quantity=out(quantity) if quantity is not None else None,
                              ratio=out(ratio), instrument=facts))
            continue
        reason = (f"IBKR {name} ({code}) is not posted automatically; check the holding after it"
                  if code not in {"FS", "RS"} else
                  f"IBKR {name} could not be read as one 'SPLIT x FOR y' row; check the holding after it")
        warnings.append(f"{symbol or 'A holding'}: {reason} (on {when}).")
        result.append(_tx(account_id, identifier, "corporate_action", when, None, currency, text, symbol=symbol,
                          quantity=out(quantity) if quantity is not None else None, corporate_action=code,
                          not_posted_reason=reason))
    return result


def _positions(statement, scrub_text, warnings) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    groups: dict[tuple, dict[str, list]] = {}
    for row in statement["open_positions"]:
        key = (_norm(row.get("conid")) or _norm(row.get("symbol")).upper(), _norm(row.get("currency")).upper())
        level = _norm(row.get("levelOfDetail")).upper() or "SUMMARY"
        groups.setdefault(key, {"SUMMARY": [], "LOT": []}).setdefault(level, []).append(row)
    rows, facts_by_symbol = [], {}
    for (_, currency), levels in groups.items():
        summary, lots = levels.get("SUMMARY", []), levels.get("LOT", [])
        sample = (summary or lots)[0]
        facts = _instrument_facts(sample)
        symbol = facts["symbol"]
        facts_by_symbol[symbol] = facts
        total = _dec(summary[0].get("position")) if summary else None
        lot_total = sum((_dec(l.get("position")) or Decimal(0) for l in lots), Decimal(0))
        use_lots = bool(lots) and (total is None or lot_total == total) and all(
            _date(l.get("openDateTime")) and _dec(l.get("costBasisMoney")) is not None for l in lots)
        if lots and not use_lots:
            warnings.append(f"IBKR lots for {symbol} do not add up to the position or lack dates/cost; the summary row was used.")
        chosen = lots if use_lots else summary[:1] or lots[:1]
        multiplier = _dec(sample.get("multiplier"))
        category = _norm(sample.get("assetCategory")).upper()
        for row in chosen:
            entry = {
                "symbol": symbol, "description": scrub_text(row.get("description")),
                "quantity": row.get("position"), "market_value": row.get("positionValue"),
                "cost_basis": row.get("costBasisMoney") or None, "currency": currency or None,
                "asset_type": _ASSET_TYPE.get("ETF" if _norm(sample.get("subCategory")).upper() == "ETF" else category),
                "currency_explicit": True,
            }
            if multiplier in (None, Decimal(1)):
                entry["price"] = row.get("markPrice")
            if use_lots:
                entry["acquired_on"] = _date(row.get("openDateTime"))
            rows.append(entry)
    return rows, facts_by_symbol


def _nav(statement, as_of: str | None) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[str]]:
    """(closing NAV row, opening NAV row, assumptions) from the equity summary or Change in NAV."""
    notes = []
    dated = sorted(((_date(r.get("reportDate")), r) for r in statement["equity_summary"] if _date(r.get("reportDate"))),
                   key=lambda item: item[0])
    closing = next((r for d, r in reversed(dated) if as_of is None or d <= as_of), None)
    opening = dated[0][1] if len(dated) > 1 else None

    def total(row: dict[str, Any] | None) -> Decimal | None:
        if row is None:
            return None
        value = _dec(row.get("total"))
        accruals = sum((_dec(row.get(k)) or Decimal(0) for k in ("interestAccruals", "dividendAccruals")), Decimal(0))
        if value is None:
            return None
        if accruals and row is closing:
            notes.append(f"IBKR net asset value {out(value)} includes {out(accruals)} of accrued interest/dividends that "
                         "is not yet cash; reconciliation compares holdings and cash with the value excluding accruals.")
        return value - accruals

    close_value, open_value = total(closing), total(opening)
    if closing is not None and close_value is not None:
        close_row = {"amount": out(close_value), "date": _date(closing.get("reportDate")),
                     "label": "IBKR net asset value (equity summary in base, excluding accruals)"}
    else:
        change = statement["change_in_nav"][0] if statement["change_in_nav"] else None
        ending = _dec(change.get("endingValue")) if change else None
        close_row = {"amount": out(ending), "date": as_of, "label": "IBKR ending value (change in NAV)"} if ending is not None else None
        starting = _dec(change.get("startingValue")) if change else None
        open_value = starting if starting is not None else open_value
        opening = change if starting is not None else opening
    open_row = ({"amount": out(open_value), "date": _date(opening.get("reportDate") or opening.get("fromDate"))}
                if opening is not None and open_value is not None else None)
    return close_row, open_row, notes


def proposal_from_flex(body: bytes, *, query_id: Any, owner_id: str = "self",
                       sic_listed: Mapping[str, bool] | Iterable[str] | None = None,
                       retrieved_at: str | None = None) -> dict[str, Any]:
    """Map an Activity Flex statement to an ingest proposal (nothing is saved)."""
    query = _query_id(query_id)
    statements = parse_flex(body)
    scrub_text = _Scrubber(statements)
    if isinstance(sic_listed, Mapping):
        sic = {str(k).upper(): bool(v) for k, v in sic_listed.items()}
    else:
        sic = {str(k).upper(): True for k in (sic_listed or [])}
    warnings: list[str] = []
    assumptions: list[str] = [
        "Positions, cash and NAV are IBKR's own figures from the Activity Flex Query; transactions carry IBKR "
        "trade/transaction ids so a later sync adds only new lines.",
    ]
    reasons: list[str] = []
    ends = sorted({s["to_date"] for s in statements if s["to_date"]})
    as_of = ends[-1] if ends else None
    if len(ends) > 1:
        reasons.append(f"The IBKR accounts end on different dates ({', '.join(ends)}); sync them together with one query.")
    starts = sorted({s["from_date"] for s in statements if s["from_date"]})
    account_ids = _account_ids(statements)
    accounts, transactions, facts, navs, fx = [], [], {}, {}, []
    seen_rates: set[tuple[str, str]] = set()
    sections: set[str] = set()
    for statement, account_id in zip(statements, account_ids):
        sections.update(statement["sections"])
        base = _base_currency(statement)
        if base is None:
            warnings.append(f"Account {account_id}: base currency missing; add Account Information (Currency) to the query.")
        positions, by_symbol = _positions(statement, scrub_text, warnings)
        facts[account_id] = by_symbol
        cash_rows = [r for r in statement["cash_report"] if _norm(r.get("currency")).upper() not in {"", "BASE_SUMMARY"}
                     and (_norm(r.get("levelOfDetail")).upper() in {"", "CURRENCY"})]
        cash = [{"amount": r.get("endingCash"), "currency": _norm(r.get("currency")).upper(), "label": "IBKR ending cash"}
                for r in cash_rows if (_dec(r.get("endingCash")) or Decimal(0)) != 0]
        close_nav, open_nav, nav_notes = _nav(statement, as_of)
        assumptions.extend(nav_notes)
        if not cash_rows:
            summary_cash = next((r for r in reversed(statement["equity_summary"]) if _date(r.get("reportDate")) == as_of), None)
            amount = _dec(summary_cash.get("cash")) if summary_cash else None
            if amount is not None and base:
                cash = [{"amount": out(amount), "currency": base, "label": "IBKR cash (base currency)"}] if amount else []
                warnings.append(f"Account {account_id}: the query has no Cash Report, so cash is the base-currency total; "
                                "add Cash Report (Ending Cash per currency) for balances by currency.")
            else:
                warnings.append(f"Account {account_id}: no cash balances in the query; add the Cash Report section.")
        if close_nav is None:
            warnings.append(f"Account {account_id}: no NAV in the query; add Net Asset Value (NAV) in Base so holdings "
                            "can be reconciled.")
        elif close_nav["date"] != as_of:
            reasons.append(f"Account {account_id}: the latest NAV is dated {close_nav['date']}, not {as_of}.")
        navs[account_id] = (close_nav, open_nav, base, statement["from_date"], statement["to_date"])
        accounts.append({
            "label": _label(statement), "number_last4": last4(statement["account_id"]), "type": "brokerage",
            "currency": base, "positions": positions, "cash": cash,
            "reported_total": ({"amount": close_nav["amount"], "currency": base, "label": close_nav["label"]}
                               if close_nav else None),
        })
        for rate in statement["conversion_rates"]:
            source, target = _norm(rate.get("fromCurrency")).upper(), _norm(rate.get("toCurrency")).upper()
            if _date(rate.get("reportDate")) == as_of and source != target and (source, target) not in seen_rates:
                seen_rates.add((source, target))
                fx.append({"from": source, "to": target, "rate": rate.get("rate")})
        transactions += _trades(statement, account_id, scrub_text, warnings)
        transactions += _cash_transactions(statement, account_id, scrub_text, warnings)
        transactions += _corporate_actions(statement, account_id, scrub_text, warnings)
    transactions.sort(key=lambda t: (t["account_id"], t["date"], t["id"]))
    for name in ("OpenPositions", "Trades", "CashTransactions", "ConversionRates"):
        if name not in sections:
            warnings.append(f"The Flex query has no {name} section; add it in the IBKR portal for a complete sync.")
    if any(s["open_positions"] for s in statements):
        assumptions.append(
            "IBKR data does not say whether a security is listed in Mexico's SIC. For a Mexican resident that decides "
            "10% (Art. 129) versus progressive tax on gains, so it stays 'unknown' per holding until the person or "
            "the SIC list confirms it.")
    provenance = {
        "kind": "connector", "provider": NAME, "ref": f"ibkr-flex:query-{query}", "query_id": query,
        "period_start": starts[0] if starts else None, "period_end": as_of,
        "accounts": [mask_account(s["account_id"]) for s in statements],
        "retrieved_at": retrieved_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    statement = {
        "institution": INSTITUTION, "institution_key": "ibkr", "as_of": as_of,
        "currency": next((a["currency"] for a in accounts if a["currency"]), None),
        "market": "us", "period_start": starts[0] if starts else None, "fx": fx, "accounts": accounts,
    }
    proposal = build_proposal(statement, kind="connector", provenance=provenance, owner_id=owner_id,
                              warnings=warnings, assumptions=assumptions, review_reasons=reasons,
                              transactions=transactions)
    result = proposal["result"]
    household = result["household"]
    if [a["id"] for a in household["accounts"]] != account_ids:
        raise FlexError("Internal error: IBKR account ids did not map one to one.")
    for position in household["positions"]:
        if str(position["instrument_id"]).startswith("CASH:"):
            continue
        info = facts.get(position["account_id"], {}).get(str(position.get("symbol") or "").upper())
        if not info:
            continue
        for field in ("venue", "listing_exchange", "underlying_symbol", "issuer_domicile", "isin"):
            if info.get(field):
                position[field] = info[field]
        if info.get("asset_class") and not position.get("asset_class"):
            position["asset_class"] = info["asset_class"]
        if position.get("asset_class") in {"equity", "fund"} and position.get("issuer_domicile") != "MX":
            position["sic_listed"] = sic.get(position["symbol"].upper(), "unknown")
    for account_id, (close_nav, open_nav, base, start, end) in navs.items():
        if close_nav is None or base is None:
            continue
        result["balance_assertions"].append({
            "account_id": account_id, "period_start": start, "period_end": end,
            "opening": open_nav["amount"] if open_nav else None, "closing": close_nav["amount"],
            "currency": base, "balance_kind": "nav", "page": None, "source": close_nav["label"],
        })
    result["connector"] = {"name": NAME, "query_id": query, "sections": sorted(sections),
                           "period": {"start": provenance["period_start"], "end": as_of}}
    result["proposal_id"] = proposal_digest(result)
    result["summary"] = _summary(result)
    return proposal


# -- ledger posting ---------------------------------------------------------

def ledger_batch(proposal: Mapping[str, Any], *, batch_id: str, ledger: Mapping[str, Any]) -> dict[str, Any]:
    """Posting batch for a confirmed IBKR proposal (same contract as ``ingest_posting.proposal_to_batch``).

    Differences from a statement upload: FX conversions post as
    ``fx_conversion`` with both legs, forward/reverse splits post with their
    ratio, trade-only instruments keep their venue/ISIN facts, opening lots
    come from IBKR's lot dates, and opening quantities are rolled back through
    the period's trades and splits.  Unposted IBKR rows keep their reason.
    """
    from ..ingest_posting import _instrument, _line, _s
    from ..ledger.model import LedgerInputError, normalize_transaction

    result = proposal["result"]
    household = result["household"]
    as_of = result["as_of"]
    provenance = result.get("provenance") or {}
    source = {"kind": "tool", "ref": provenance.get("ref") or f"ibkr-flex:{result['proposal_id'][:16]}",
              "observed_on": as_of}
    active = {e["account_id"] for e in ledger.get("entries", [])}
    notes: list[str] = []
    not_posted: list[dict[str, Any]] = []
    instruments: dict[str, dict[str, Any]] = {}
    symbols: dict[tuple[str, str], str] = {}
    prices: dict[str, list[dict[str, str]]] = {}
    zero = Decimal(0)

    def enrich(row: dict[str, Any], facts: Mapping[str, Any]) -> dict[str, Any]:
        listing = _LEDGER_LISTING.get(str(facts.get("listing_exchange") or ""))
        if listing:
            row["listing"] = listing
        if facts.get("isin"):
            row["isin"] = facts["isin"]
        if facts.get("venue") and "venue" not in row and not (facts["venue"] == "sic" and row["currency"] != "MXN"):
            row["venue"] = facts["venue"]
        return row

    for position in household["positions"]:
        if str(position["instrument_id"]).upper().startswith("CASH:") or position.get("asset_class") == "cash":
            continue
        instruments.setdefault(position["instrument_id"], enrich(_instrument(position), position))
        symbols[(position["account_id"], str(position.get("symbol") or "").upper())] = position["instrument_id"]
        value, quantity = Decimal(str(position["value"])), Decimal(str(position["quantity"]))
        if quantity:
            prices.setdefault(position["instrument_id"], []).append({"date": as_of, "price": str(value / quantity)})
    for tx in result.get("transactions") or []:
        facts = tx.get("instrument")
        symbol = str(tx.get("symbol") or "").upper()
        if facts and symbol and (tx["account_id"], symbol) not in symbols and symbol not in instruments:
            row = {"id": symbol, "symbol": symbol, "currency": tx["currency"]}
            for field in ("name", "asset_class", "underlying_symbol", "issuer_domicile"):
                if facts.get(field):
                    row[field] = facts[field]
            instruments[symbol] = enrich(row, facts)

    accounts = []
    for account in household["accounts"]:
        if account["currency"] == "XXX":
            notes.append(f"Account {account['id']} has no known currency; it was not posted to the ledger.")
            continue
        row = {"id": account["id"], "institution": account.get("institution") or INSTITUTION, "type": account["type"],
               "currency": account["currency"], "owners": [{"person_id": account.get("owner_id") or "self", "share": "1"}]}
        if account.get("name"):
            row["name"] = account["name"]
        accounts.append(row)
    known = {a["id"] for a in accounts}

    by_account: dict[str, list[dict[str, Any]]] = {}
    for tx in result.get("transactions") or []:
        if tx.get("account_id") not in known:
            continue
        reason = tx.get("not_posted_reason")
        line = None
        if reason is None and tx["type"] == "fx":
            line = {"kind": "fx_conversion", "account_id": tx["account_id"], "date": tx["date"], "amount": tx["amount"],
                    "currency": tx["currency"], "to_amount": tx["to_amount"], "to_currency": tx["to_currency"],
                    "external_id": tx["id"], "description": tx["description"], "settle_date": tx.get("settlement_date")}
        elif reason is None and tx["type"] == "split":
            symbol = str(tx["symbol"]).upper()
            line = {"kind": "split", "account_id": tx["account_id"], "date": tx["date"], "ratio": tx["ratio"],
                    "instrument_id": symbols.get((tx["account_id"], symbol), symbol), "currency": tx["currency"],
                    "amount": tx.get("amount"), "external_id": tx["id"], "description": tx["description"]}
        elif reason is None:
            line, reason = _line(tx, instruments, symbols)
        if line is not None and line["kind"] in {"fx_conversion", "split"}:
            line = {k: v for k, v in line.items() if v is not None}
            if line["kind"] == "split" and line["instrument_id"] not in instruments:
                instruments[line["instrument_id"]] = {"id": line["instrument_id"], "symbol": line["instrument_id"],
                                                      "currency": tx["currency"]}
            try:
                normalize_transaction(line, 0, source={"kind": "tool", "ref": "check"}, confidence=None)
            except LedgerInputError as exc:
                line, reason = None, str(exc).replace("transactions[0]", "line")
        if line is None:
            not_posted.append({"date": tx.get("date"), "description": tx.get("description"), "amount": tx.get("amount"),
                               "account_id": tx.get("account_id"), "reason": reason})
            continue
        by_account.setdefault(tx["account_id"], []).append({k: v for k, v in line.items() if v is not None})

    periods = {a["account_id"]: a for a in result.get("balance_assertions") or []}
    lines, assertions = [], []
    for account in accounts:
        account_id = account["id"]
        own = [p for p in household["positions"] if p["account_id"] == account_id]
        posted = sorted(by_account.get(account_id, []), key=lambda l: l["date"])
        cash: dict[str, Decimal] = {}
        for position in own:
            if str(position["instrument_id"]).startswith("CASH:"):
                cash[position["currency"]] = cash.get(position["currency"], zero) + Decimal(str(position["quantity"]))
        for liability in household["liabilities"]:
            if liability.get("account_id") == account_id and liability.get("value") not in (None, ""):
                ccy = liability.get("currency") or account["currency"]
                cash[ccy] = cash.get(ccy, zero) - Decimal(str(liability["value"]))
        period = periods.get(account_id) or {}
        start = period.get("period_start") or min([l["date"] for l in posted] or [as_of])
        end = period.get("period_end") or as_of
        closing_qty = {p["instrument_id"]: Decimal(str(p["quantity"])) for p in own
                       if not str(p["instrument_id"]).startswith("CASH:")}
        touched = [l["instrument_id"] for l in posted if l.get("instrument_id") and l["kind"] in {"buy", "sell", "split"}]
        if account_id not in active:
            flows: dict[str, Decimal] = {}
            for line in posted:
                if line.get("amount") is not None:
                    flows[line["currency"]] = flows.get(line["currency"], zero) + Decimal(line["amount"])
                if line["kind"] == "fx_conversion":
                    flows[line["to_currency"]] = flows.get(line["to_currency"], zero) + Decimal(line["to_amount"])
            opening: list[dict[str, Any]] = []
            for ccy in sorted(set(cash) | set(flows)):
                amount = cash.get(ccy, zero) - flows.get(ccy, zero)
                if amount != 0 or ccy in cash:
                    opening.append({"kind": "opening_balance", "account_id": account_id, "date": start, "amount": _s(amount),
                                    "currency": ccy, "description": "Opening balance derived from the IBKR sync"})
            for instrument in list(dict.fromkeys([*closing_qty, *touched])):
                quantity, factor, traded = closing_qty.get(instrument, zero), Decimal(1), False
                for line in reversed(posted):
                    if line.get("instrument_id") != instrument:
                        continue
                    if line["kind"] == "buy":
                        quantity, traded = quantity - Decimal(line["quantity"]), True
                    elif line["kind"] == "sell":
                        quantity, traded = quantity + Decimal(line["quantity"]), True
                    elif line["kind"] == "split":
                        quantity, factor = quantity / Decimal(line["ratio"]), factor * Decimal(line["ratio"])
                if quantity < 0:
                    notes.append(f"{instrument} in {account_id}: the period's trades exceed the closing quantity; "
                                 "opening quantity not posted.")
                    continue
                if quantity == 0:
                    continue
                currency = instruments.get(instrument, {}).get("currency") or account["currency"]
                base = {"kind": "opening_balance", "account_id": account_id, "date": start, "instrument_id": instrument,
                        "currency": currency}
                prior = [(Decimal(str(l["quantity"])) / factor, l["cost_basis"], l["acquired_on"])
                         for l in household["lots"] if l["account_id"] == account_id
                         and l["instrument_id"] == instrument and l["acquired_on"] < start]
                for tx in result.get("transactions") or []:  # lots a sale in the period relieved (IBKR CLOSED_LOT)
                    symbol = str(tx.get("symbol") or "").upper()
                    if tx.get("account_id") != account_id or tx.get("type") != "sell" or tx.get("not_posted_reason") \
                            or symbols.get((account_id, symbol), symbol) != instrument:
                        continue
                    before = Decimal(1)  # splits between the sale and the period start
                    for line in posted:
                        if line.get("instrument_id") == instrument and line["kind"] == "split" and line["date"] <= tx["date"]:
                            before *= Decimal(line["ratio"])
                    for lot in tx.get("closed_lots") or []:
                        if lot["acquired_on"] < start:
                            prior.append((Decimal(lot["quantity"]) / before, lot["cost_basis"], lot["acquired_on"]))
                if prior and sum((q for q, _, _ in prior), zero) == quantity:
                    merged: dict[str, list[Decimal]] = {}
                    for lot_qty, cost, acquired in prior:
                        slot = merged.setdefault(acquired, [zero, zero])
                        slot[0] += lot_qty
                        slot[1] += Decimal(str(cost))
                    for acquired, (lot_qty, cost) in sorted(merged.items()):
                        opening.append({**base, "quantity": _s(lot_qty), "cost_basis": _s(cost), "acquired_on": acquired,
                                        "description": f"Opening lot {instrument} acquired {acquired}"})
                    continue
                entry = {**base, "quantity": _s(quantity), "description": f"Opening position {instrument}"}
                position = next((p for p in own if p["instrument_id"] == instrument), None)
                if position and position.get("cost_basis") not in (None, "") and not traded:
                    entry["cost_basis"] = str(position["cost_basis"])
                elif traded:
                    notes.append(f"{instrument} in {account_id}: its opening cost basis is unknown because it was traded "
                                 "in the period and IBKR's remaining lots do not cover the opening quantity.")
                opening.append(entry)
            lines.extend(opening)
        elif not posted:
            notes.append(f"Account {account_id} is already in the ledger and this sync has no new lines; its closing "
                         "numbers were recorded as balance checks only.")
        lines.extend(posted)
        for ccy, balance in sorted(cash.items()):
            assertions.append({"account_id": account_id, "date": end if end >= start else as_of, "currency": ccy,
                               "balance": _s(balance)})
        for currency in sorted({l["currency"] for l in posted if l.get("currency")} - set(cash)):
            assertions.append({"account_id": account_id, "date": end if end >= start else as_of, "currency": currency,
                               "balance": "0"})
        for instrument in dict.fromkeys([*closing_qty, *touched]):
            assertions.append({"account_id": account_id, "date": as_of, "instrument_id": instrument,
                               "quantity": _s(closing_qty.get(instrument, zero))})
    fx = [{"date": item.get("as_of") or as_of, "base": item["from"], "quote": item["to"], "rate": str(item["rate"]),
           "source": f"IBKR conversion rates ({source['ref']})"} for item in household.get("fx") or []]
    batch = None
    if accounts and (lines or assertions):
        batch = {"batch_id": batch_id, "source": source, "accounts": accounts, "instruments": list(instruments.values()),
                 "transactions": lines, "balance_assertions": assertions, "fx": fx}
    return {"batch": batch, "not_posted": not_posted, "notes": notes, "prices": prices}


# -- connector --------------------------------------------------------------

SETUP = (
    "In the IBKR portal open Performance & Reports > Flex Queries and create an Activity Flex Query with Format XML, "
    "Period 'Last 365 Calendar Days' and date format yyyyMMdd.",
    "Sections: Account Information; Net Asset Value (NAV) in Base (Equity Summary); Cash Report; Open Positions "
    "(Summary and Lot); Trades (Executions); Cash Transactions (Detail); Corporate Actions; Conversion Rates. Select "
    "all fields in each section.",
    "Open Flex Web Service Configuration, enable it and copy the Current Token.",
    f"Store the token: security add-generic-password -U -s {KEYCHAIN_SERVICE} -a \"$USER\" -w  (macOS prompts for "
    f"it), or export {TOKEN_ENV} for one session.",
    "Sync with wealth ingest action=connector inputs={\"name\": \"ibkr_flex\", \"query_id\": \"<query id>\"}.",
)


class IbkrFlexConnector:
    """Pull an Activity Flex Query on demand and return an ingest proposal."""

    provider = NAME
    countries = ("US", "MX", "*")

    def __init__(self, query_id: Any, *, token: FlexToken | None = None, transport: Transport | None = None,
                 sleep: Callable[[float], None] | None = None, clock: Callable[[], float] | None = None,
                 timeout: float = TIMEOUT_SECONDS, sic_listed: Mapping[str, bool] | Iterable[str] | None = None,
                 token_loader: Callable[[], FlexToken | None] = load_token):
        self.query_id = _query_id(query_id)
        self._token = token
        self._transport = transport
        self._sleep, self._clock, self._timeout = sleep, clock, timeout
        self._sic_listed = sic_listed
        self._token_loader = token_loader

    @property
    def ref(self) -> str:
        return f"ibkr-flex:query-{self.query_id}"

    def __repr__(self) -> str:
        return f"IbkrFlexConnector(query_id={self.query_id!r})"

    def proposal(self, *, owner_id: str = "self", previous: dict[str, Any] | None = None) -> dict[str, Any]:
        token = self._token or self._token_loader()
        if token is None:
            return envelope("needs_input", {"setup": list(SETUP)}, missing=[{
                "key": "ibkr_flex.token", "reason": "missing",
                "detail": f"No IBKR Flex token in the keychain (service {KEYCHAIN_SERVICE}) or {TOKEN_ENV}. "
                          "The person stores it themselves; never paste it into the chat."}])
        try:
            body = fetch_statement(token, self.query_id, transport=self._transport or default_transport,
                                   sleep=self._sleep, clock=self._clock, timeout=self._timeout)
            proposal = proposal_from_flex(body, query_id=self.query_id, owner_id=owner_id, sic_listed=self._sic_listed)
        except FlexError as exc:
            message = scrub(str(exc), token.reveal())
            return envelope("rejected", {"error": {"code": exc.code, "retryable": exc.retryable, "message": message}},
                            warnings=[message], sources=[self.ref])
        if previous is not None:
            proposal["result"]["changes"] = diff_proposals(previous, proposal)
        return proposal


def status(environ: Mapping[str, str] | None = None, runner: Callable[..., Any] | None = None) -> dict[str, Any]:
    source = token_source(environ, runner)
    return {"name": NAME, "institution": INSTITUTION, "read_only": True, "token": source or "missing",
            "ready": source is not None, "needs": ["query_id"], "setup": list(SETUP)}


__all__ = [
    "ERRORS", "FlexError", "FlexTimeout", "FlexToken", "FlexTransportError", "IN_PROGRESS", "IbkrFlexConnector",
    "RETRYABLE", "THROTTLED", "fetch_statement", "ledger_batch", "load_token", "parse_flex", "proposal_from_flex",
    "scrub", "status", "urllib_transport",
]
