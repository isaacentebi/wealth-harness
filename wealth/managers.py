"""Follow a manager: public institutional portfolios from SEC Form 13F filings.

What this module does

* :func:`find` searches EDGAR for filers by name and returns candidates with
  their CIK and latest 13F filing.
* :func:`holdings` reads one quarter's 13F information table (amendments
  applied), maps CUSIPs to tickers with a confidence, separates options and
  debt from the long-equity book, diffs against the prior quarter, and always
  carries the lag and what a 13F leaves out.
* :func:`profile` interprets consecutive filings: turnover, holding period,
  concentration, conviction and sizing, sector drift and options usage, plus a
  one-paragraph ``character``.  :func:`compare` sets managers side by side.
* :func:`mirror` turns a manager's long-equity weights into target weights for
  a sleeve of the person's money inside their policy (concentration cap, IPS
  check, Mexico SIC and estate-situs flags), hands off to
  :func:`wealth.rebalance.plan` for a trade list, and never places orders.
  :func:`backtest` replays "copy the 13F at its filing date" on past quarters.

Sources (read 2026-09-21)

* EDGAR APIs, ``data.sec.gov/submissions/CIK##########.json``:
  https://www.sec.gov/search-filings/edgar-application-programming-interfaces
* Fair access: a descriptive User-Agent with a contact e-mail ("Sample Company
  Name AdminContact@<sample company domain>.com") and at most 10 requests per
  second: https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data
  and https://www.sec.gov/os/webmaster-faq#developers
* Filing folders expose ``index.json``:
  https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data
* Form 13F FAQ (who files, 45-day deadline, 13(f) securities, options as the
  underlying, amendments: restatement vs new holdings, 13F-NT, values rounded
  to the nearest dollar from 2023-01-03, previously thousands):
  https://www.sec.gov/rules-regulations/staff-guidance/division-investment-management-frequently-asked-questions/frequently-asked-questions-about-form-13f
* Issuer names and tickers: https://www.sec.gov/files/company_tickers.json
* CUSIP to ticker: OpenFIGI ``/v3/mapping`` (keyless: 25 requests a minute, 10
  jobs each; with ``X-OPENFIGI-APIKEY``: 25 per 6 seconds, 100 jobs):
  https://www.openfigi.com/api/documentation

Network access needs ``WEALTH_SEC_USER_AGENT``; without it EDGAR is never
called.  The transport is injectable and responses are cached on disk under
the data directory with per-kind TTLs.  ``Snapshot`` replays recorded pages
offline (the catalog examples and tests use it).
"""
from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import re
import statistics
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher, get_close_matches
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from ._common import envelope

# ------------------------------------------------------------------ constants

SEC_UA_ENV = "WEALTH_SEC_USER_AGENT"
OPENFIGI_KEY_ENV = "WEALTH_OPENFIGI_API_KEY"
CACHE_ENV = "WEALTH_SEC_CACHE"

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SUBMISSIONS_PAGE_URL = "https://data.sec.gov/submissions/{name}"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{folder}/"
ENTITY_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index?keysTyped={q}"
FULL_TEXT_URL = "https://efts.sec.gov/LATEST/search-index?q={q}&forms=13F-HR,13F-HR/A,13F-NT"
COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"
BROWSE_URL = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=13F&owner=include&count=40"

DOCS = {
    "edgar_apis": "https://www.sec.gov/search-filings/edgar-application-programming-interfaces",
    "fair_access": "https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data",
    "form_13f_faq": ("https://www.sec.gov/rules-regulations/staff-guidance/division-investment-management-"
                     "frequently-asked-questions/frequently-asked-questions-about-form-13f"),
    "openfigi": "https://www.openfigi.com/api/documentation",
    "company_tickers": COMPANY_TICKERS_URL,
}

# Values are whole dollars for filings made on or after this date, thousands before (13F FAQ).
VALUE_IN_DOLLARS_FROM = date(2023, 1, 3)
FORMS_13F = ("13F-HR", "13F-HR/A", "13F-NT", "13F-NT/A")
SEC_INTERVAL = 0.11                      # just under 10 requests a second
FIGI_INTERVAL = {False: 2.5, True: 0.25}  # keyless 25/minute; keyed 25 per 6 seconds
FIGI_BATCH = {False: 10, True: 100}
TTL = {"search": 86400, "submissions": 6 * 3600, "filing": 365 * 86400, "tickers": 7 * 86400,
       "figi": 90 * 86400, "issuer": 30 * 86400}
MIN_CONFIDENCE = 0.8
DEFAULT_CONCENTRATION = 0.10

# People are known by name; EDGAR knows their firms.  Search terms only: CIKs are always looked up.
ALIASES = {
    "leopold aschenbrenner": "Situational Awareness", "aschenbrenner": "Situational Awareness",
    "gavin baker": "Atreides Management", "warren buffett": "Berkshire Hathaway",
    "bill ackman": "Pershing Square", "william ackman": "Pershing Square", "ackman": "Pershing Square",
    "pat dorsey": "Dorsey Asset Management",
}

EXCLUDES = [
    "short positions (and written options)",
    "cash and money-market holdings",
    "shares listed only outside the US",
    "most bonds and other debt (only some convertibles appear, as principal amounts)",
    "private companies, private funds and other non-13(f) securities",
    "trades made during the quarter (only the quarter-end snapshot is shown)",
    "positions under 10,000 shares and US$200,000, and positions granted confidential treatment",
]
OPTIONS_NOTE = ("Options (puts and calls) are reported as the value and share count of the underlying stock, "
                "not the option premium paid, so a call can look like a large stock position. They are listed "
                "separately and left out of the long-equity weights.")
AMENDMENT_POLICY = ("13F-HR/A amendments are applied in filing order: a RESTATEMENT replaces the whole quarter; "
                    "a NEW HOLDINGS amendment adds its rows (often positions first withheld under confidential "
                    "treatment) to what was already filed.")

WHAT_IS_13F = {
    "en": ("What a 13F is: every US institutional investment manager with at least US$100 million in certain "
           "US-listed securities must file Form 13F with the SEC each quarter, within 45 days after the quarter "
           "ends. It lists the long positions it held in those securities on the last day of the quarter: "
           "US-listed stocks and ETFs, some convertible bonds and listed options. It leaves out short positions, "
           "cash, most bonds, shares listed only abroad, private investments and anything bought and sold within "
           "the quarter; small or confidential positions can be omitted. Values are market values on the "
           "quarter-end date, not what the manager paid and not a return: a position grows in value when its "
           "price rises even if nothing was bought. Options appear as the value of the underlying shares. So a "
           "13F cannot show a manager's full position (hedges, shorts, cash and foreign holdings are invisible) "
           "or what they own today, which can be up to four and a half months later."),
    "es": ("Qué es un 13F: todo administrador institucional de inversiones en EE.UU. con al menos 100 millones de "
           "dólares en ciertos valores listados en EE.UU. debe presentar el Formulario 13F ante la SEC cada "
           "trimestre, dentro de los 45 días posteriores al cierre. Muestra las posiciones largas que tenía en "
           "esos valores el último día del trimestre: acciones y ETF listados en EE.UU., algunos bonos "
           "convertibles y opciones listadas. No incluye posiciones cortas, efectivo, la mayoría de los bonos, "
           "acciones listadas sólo fuera de EE.UU., inversiones privadas ni lo que se compró y vendió dentro del "
           "trimestre; las posiciones pequeñas o confidenciales pueden omitirse. Los valores son de mercado al "
           "cierre del trimestre, no lo que pagó el administrador ni un rendimiento: una posición vale más si su "
           "precio sube aunque no se haya comprado nada. Las opciones aparecen con el valor de las acciones "
           "subyacentes. Por eso un 13F no puede mostrar la posición completa de un administrador (coberturas, "
           "cortos, efectivo y valores extranjeros no se ven) ni lo que tiene hoy, que puede ser hasta cuatro "
           "meses y medio después."),
}

TRACKING_CAVEATS = [
    "Lag: you see holdings as of the quarter end, published up to 45 days later; the manager may have sold or "
    "changed them since.",
    "Turnover: trades made within a quarter never appear, and a fast-trading manager's 13F can be stale by the "
    "time it is public.",
    "Missing positions: shorts, hedges, cash, foreign listings, bonds and private holdings are not in a 13F, so "
    "the copied portfolio carries risks the manager's real one may offset.",
    "Options are shown at the underlying stock's value; a call or put is not the same exposure as the stock.",
    "Taxes and costs: rebalancing each quarter realises gains, and commissions, spreads, FX and (in Mexico) the "
    "SIC premium and whole-share rounding all reduce the copy's return.",
    "Returns will differ from the manager's fund, which also has fees, leverage, cash drag and different timing.",
]

# ------------------------------------------------------------------ errors and transport


class ManagerDataError(ValueError):
    """A filing or response could not be used; the message says which and why."""


class UserAgentRequired(RuntimeError):
    """EDGAR is never called without a declared User-Agent with contact details."""

    def __init__(self) -> None:
        super().__init__(
            "EDGAR requires every automated request to say who is calling, with a contact e-mail. Set "
            f"{SEC_UA_ENV} to something like 'Jane Doe jane@example.com' (SEC fair-access policy: "
            f"{DOCS['fair_access']}). Without it the SEC blocks requests and may rate-limit this machine, so "
            "Wealth does not call EDGAR.")


class TransportError(RuntimeError):
    def __init__(self, status: int | None, url: str, detail: str = ""):
        self.status, self.url = status, url
        super().__init__(f"{url}: {'HTTP ' + str(status) if status else detail or 'unreachable'}")


Transport = Callable[[str, str, Mapping[str, str], "bytes | None"], str]


def http_transport(method: str, url: str, headers: Mapping[str, str], body: bytes | None = None) -> str:
    request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            if response.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise TransportError(exc.code, url) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TransportError(None, url, str(exc)) from None


class Snapshot:
    """Offline transport over recorded pages ``{url: body}``.

    ``body`` is text or a JSON-able object.  OpenFIGI answers come from the
    special key ``"openfigi"``: ``{cusip: [figi data rows]}``.
    """

    def __init__(self, pages: Mapping[str, Any]):
        if not isinstance(pages, Mapping):
            raise ValueError("snapshot must map URLs to recorded bodies")
        self.pages = pages

    def __call__(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None = None) -> str:
        if url == OPENFIGI_URL:
            table = self.pages.get("openfigi") or {}
            jobs = json.loads(body or b"[]")
            return json.dumps([{"data": table[job["idValue"]]} if table.get(job["idValue"])
                               else {"warning": "No identifier found."} for job in jobs])
        if url not in self.pages:
            raise TransportError(404, url)
        value = self.pages[url]
        return value if isinstance(value, str) else json.dumps(value)


_THROTTLE_LOCK = threading.Lock()
_LAST_CALL: dict[str, float] = {}


def _throttle(host: str, interval: float) -> None:
    with _THROTTLE_LOCK:
        wait = _LAST_CALL.get(host, 0.0) + interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _LAST_CALL[host] = time.monotonic()


def default_cache_dir() -> Path:
    configured = os.environ.get(CACHE_ENV)
    if configured:
        return Path(configured).expanduser()
    from .service import database_path
    return database_path().expanduser().parent / "sec-cache"


_DEFAULT = object()


class Edgar:
    """EDGAR and OpenFIGI access with a disk cache, polite pacing and a declared User-Agent."""

    def __init__(self, user_agent: str | None = None, *, transport: Transport | None = None,
                 cache_dir: Any = _DEFAULT, openfigi_key: str | None = None, pace: bool | None = None):
        self.user_agent = (user_agent if user_agent is not None else os.environ.get(SEC_UA_ENV, "")).strip()
        self.transport = transport or http_transport
        self.offline = isinstance(self.transport, Snapshot)
        if cache_dir is _DEFAULT:
            cache_dir = None if self.offline or transport is not None else default_cache_dir()
        self.cache_dir = Path(cache_dir).expanduser() if cache_dir else None
        self.openfigi_key = openfigi_key if openfigi_key is not None else os.environ.get(OPENFIGI_KEY_ENV) or None
        self.pace = (transport is None) if pace is None else pace
        self.memory: dict[str, str] = {}
        self.fetched: list[str] = []
        self.warnings: list[str] = []

    # -- cache
    def _path(self, kind: str, key: str) -> Path | None:
        if self.cache_dir is None:
            return None
        return self.cache_dir / kind / (hashlib.sha256(key.encode()).hexdigest()[:40] + ".json")

    def _cached(self, kind: str, key: str, fresh_only: bool = True) -> str | None:
        if key in self.memory:
            return self.memory[key]
        path = self._path(kind, key)
        if path is None or not path.exists():
            return None
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if fresh_only and time.time() - float(record.get("fetched_at", 0)) > TTL.get(kind, 86400):
            return None
        return record.get("body")

    def _store(self, kind: str, key: str, body: str) -> None:
        self.memory[key] = body
        path = self._path(kind, key)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"key": key, "fetched_at": time.time(), "body": body}), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass  # the cache is a convenience; never fail a read because of it

    def require_user_agent(self) -> None:
        if not self.offline and ("@" not in self.user_agent or len(self.user_agent) < 6):
            raise UserAgentRequired()

    # -- requests
    def text(self, url: str, kind: str) -> str:
        cached = self._cached(kind, url)
        if cached is not None:
            self._note(url)
            return cached
        self.require_user_agent()
        if self.pace:
            _throttle("sec", SEC_INTERVAL)
        headers = {"User-Agent": self.user_agent or "offline replay", "Accept-Encoding": "gzip"}
        try:
            body = self.transport("GET", url, headers, None)
        except TransportError:
            stale = self._cached(kind, url, fresh_only=False)
            if stale is None:
                raise
            self.warnings.append(f"EDGAR could not be reached; used the cached copy of {url}.")
            body = stale
        self._store(kind, url, body)
        self._note(url)
        return body

    def json(self, url: str, kind: str) -> Any:
        body = self.text(url, kind)
        try:
            return json.loads(body)
        except ValueError:
            raise ManagerDataError(f"{url} did not return JSON") from None

    def _note(self, url: str) -> None:
        if url not in self.fetched:
            self.fetched.append(url)

    def figi(self, cusips: list[str]) -> dict[str, dict[str, Any] | None]:
        """CUSIP -> best US listing from OpenFIGI (cached per CUSIP); None when OpenFIGI has no match."""
        out: dict[str, dict[str, Any] | None] = {}
        todo = []
        for cusip in cusips:
            cached = self._cached("figi", "figi:" + cusip)
            if cached is not None:
                out[cusip] = json.loads(cached)
            else:
                todo.append(cusip)
        keyed = bool(self.openfigi_key)
        size = FIGI_BATCH[keyed]
        for start in range(0, len(todo), size):
            batch = todo[start:start + size]
            headers = {"Content-Type": "application/json", "User-Agent": self.user_agent or "wealth"}
            if self.openfigi_key:
                headers["X-OPENFIGI-APIKEY"] = self.openfigi_key
            if self.pace:
                _throttle("openfigi", FIGI_INTERVAL[keyed])
            body = json.dumps([{"idType": "ID_CUSIP", "idValue": c} for c in batch]).encode()
            try:
                answer = json.loads(self.transport("POST", OPENFIGI_URL, headers, body))
            except (TransportError, ValueError) as exc:
                self.warnings.append(f"OpenFIGI was not available ({exc}); CUSIPs fall back to name matching.")
                break
            self._note(OPENFIGI_URL)
            for cusip, item in zip(batch, answer):
                best = _best_figi(item.get("data") or []) if isinstance(item, dict) else None
                out[cusip] = best
                self._store("figi", "figi:" + cusip, json.dumps(best))
        return out


def _best_figi(rows: list[Mapping[str, Any]]) -> dict[str, Any] | None:
    rows = [r for r in rows if isinstance(r, Mapping) and r.get("ticker")]
    if not rows:
        return None
    rows.sort(key=lambda r: (r.get("exchCode") != "US", r.get("marketSector") != "Equity"))
    best = rows[0]
    us_equity = best.get("exchCode") == "US" and best.get("marketSector") == "Equity"
    return {"ticker": str(best["ticker"]).replace("/", "-"), "name": best.get("name"), "us_equity": us_equity,
            "exch_code": best.get("exchCode"), "security_type": best.get("securityType"),
            "figi": best.get("compositeFIGI") or best.get("figi")}


# ------------------------------------------------------------------ small helpers


def normalize_cik(value: Any) -> str:
    text = str(value or "").strip().upper().removeprefix("CIK")
    if not text.isdigit() or len(text) > 10 or int(text) == 0:
        raise ValueError(f"cik must be a SEC Central Index Key (up to 10 digits), not {value!r}")
    return text.zfill(10)


def _date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%m-%d-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def normalize_period(value: Any) -> str | None:
    """'2026-06-30', '2026Q2', 'Q2 2026' -> '2026-06-30'."""
    if value in (None, ""):
        return None
    text = str(value).strip().upper().replace(" ", "")
    match = re.fullmatch(r"(\d{4})-?Q([1-4])|Q([1-4])-?(\d{4})", text)
    if match:
        year = int(match.group(1) or match.group(4))
        quarter = int(match.group(2) or match.group(3))
        return {1: f"{year}-03-31", 2: f"{year}-06-30", 3: f"{year}-09-30", 4: f"{year}-12-31"}[quarter]
    parsed = _date(value)
    if parsed is None:
        raise ValueError(f"period must be a quarter end (YYYY-MM-DD) or like 2026Q2, not {value!r}")
    return parsed.isoformat()


def _r(value: float | None, places: int = 6) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, places)


def _today(as_of: Any = None) -> date:
    return _date(as_of) or datetime.now(timezone.utc).date()


def _folder(accession: str) -> str:
    return accession.replace("-", "")


def _filing_url(cik: str, accession: str) -> str:
    return ARCHIVE_URL.format(cik=int(cik), folder=_folder(accession)) + f"{accession}-index.htm"


# ------------------------------------------------------------------ XML parsing

_DTD = re.compile(r"<!\s*(DOCTYPE|ENTITY)", re.I)


def _xml(text: str) -> ET.Element:
    if _DTD.search(text or ""):
        raise ManagerDataError("refusing an XML document that declares a DTD or entities")
    try:
        return ET.fromstring((text or "").strip().encode("utf-8"))
    except ET.ParseError as exc:
        raise ManagerDataError(f"malformed XML: {exc}") from None


def _local(tag: Any) -> str:
    return str(tag).rsplit("}", 1)[-1]


def _first(element: ET.Element, name: str) -> ET.Element | None:
    for node in element.iter():
        if _local(node.tag) == name:
            return node
    return None


def _child(element: ET.Element, name: str) -> ET.Element | None:
    for node in element:
        if _local(node.tag) == name:
            return node
    return None


def _txt(element: ET.Element | None, name: str | None = None) -> str | None:
    node = element if name is None or element is None else _first(element, name)
    if node is None or node.text is None:
        return None
    text = " ".join(node.text.split())
    return text or None


def _num(text: str | None) -> float | None:
    if text is None:
        return None
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def parse_infotable(xml_text: str, *, filing_date: Any = None, value_unit: str | None = None) -> dict[str, Any]:
    """Rows of a 13F information table with values in whole dollars.

    ``value_unit`` is ``dollars`` or ``thousands``; by default it follows the
    filing date (thousands before 2023-01-03).  Implied share prices that look
    like the other unit produce a warning, never a silent rescale.
    """
    root = _xml(xml_text)
    if _local(root.tag) != "informationTable":
        raise ManagerDataError("not a 13F information table (root element is " + _local(root.tag) + ")")
    filed = _date(filing_date)
    unit = value_unit or ("thousands" if filed and filed < VALUE_IN_DOLLARS_FROM else "dollars")
    if unit not in {"dollars", "thousands"}:
        raise ValueError("value_unit must be dollars or thousands")
    factor = 1000 if unit == "thousands" else 1
    rows, warnings = [], []
    for index, entry in enumerate(e for e in root if _local(e.tag) == "infoTable"):
        cusip = (_txt(entry, "cusip") or "").upper()
        raw_value = _num(_txt(entry, "value"))
        amount = _child(entry, "shrsOrPrnAmt")
        shares = _num(_txt(amount, "sshPrnamt")) if amount is not None else None
        if not cusip or raw_value is None or shares is None:
            warnings.append(f"Information table row {index + 1} is missing its CUSIP, value or amount and was skipped.")
            continue
        put_call = (_txt(entry, "putCall") or "").lower() or None
        voting = _child(entry, "votingAuthority")
        rows.append({
            "issuer": _txt(entry, "nameOfIssuer") or "", "class": _txt(entry, "titleOfClass") or "",
            "cusip": cusip, "figi": _txt(entry, "figi"), "value": int(round(raw_value * factor)),
            "shares": shares, "amount_type": (_txt(amount, "sshPrnamtType") or "SH").upper(),
            "put_call": put_call if put_call in {"put", "call"} else None,
            "discretion": _txt(entry, "investmentDiscretion"),
            "other_managers": _txt(entry, "otherManager"),
            "voting": {k.lower(): _num(_txt(voting, k)) for k in ("Sole", "Shared", "None")} if voting is not None else None,
        })
    prices = sorted(r["value"] / r["shares"] for r in rows
                    if r["amount_type"] == "SH" and not r["put_call"] and r["shares"] > 0)
    if prices:
        median = prices[len(prices) // 2]
        if unit == "dollars" and median < 0.5:
            warnings.append("Values look like thousands of dollars although this filing should report whole "
                            "dollars (median implied price below $0.50); check the filing before relying on values.")
        if unit == "thousands" and median > 20000:
            warnings.append("Values look like whole dollars although a pre-2023 filing reports thousands (median "
                            "implied price above $20,000); check the filing before relying on values.")
    return {"rows": rows, "value_unit": unit, "warnings": warnings}


def parse_primary_doc(xml_text: str) -> dict[str, Any]:
    """Cover and summary page of a 13F submission (report type, amendment, other managers, totals)."""
    root = _xml(xml_text)
    cover = _first(root, "coverPage")
    summary = _first(root, "summaryPage")
    amendment = _first(root, "amendmentInfo") if cover is not None else None
    others = []
    for node in root.iter():
        if _local(node.tag) == "otherManager" and len(node):
            cik = _txt(node, "cik")
            others.append({"cik": cik.zfill(10) if cik and cik.isdigit() else None, "name": _txt(node, "name"),
                           "file_number": _txt(node, "form13FFileNumber")})
    period = _date(_txt(root, "reportCalendarOrQuarter") or _txt(root, "periodOfReport"))
    return {
        "submission_type": _txt(root, "submissionType"),
        "report_type": (_txt(cover, "reportType") or "").upper() or None if cover is not None else None,
        "period": period.isoformat() if period else None,
        "is_amendment": (_txt(cover, "isAmendment") or "").lower() == "true" if cover is not None else False,
        "amendment_no": _txt(cover, "amendmentNo") if cover is not None else None,
        "amendment_type": (_txt(amendment, "amendmentType") or "").upper() or None if amendment is not None else None,
        "manager": _txt(_first(root, "filingManager"), "name") if _first(root, "filingManager") is not None else None,
        "table_entry_total": _num(_txt(summary, "tableEntryTotal")) if summary is not None else None,
        "table_value_total": _num(_txt(summary, "tableValueTotal")) if summary is not None else None,
        "other_managers": others,
        "additional_information": _txt(root, "additionalInformation"),
    }


# ------------------------------------------------------------------ EDGAR reads


def _submissions(client: Edgar, cik: str) -> dict[str, Any]:
    data = client.json(SUBMISSIONS_URL.format(cik=cik), "submissions")
    if not isinstance(data, dict) or "filings" not in data:
        raise ManagerDataError(f"EDGAR submissions for CIK {cik} are not in the expected shape")
    return data


def _columns(block: Mapping[str, Any]) -> list[dict[str, Any]]:
    forms = block.get("form") or []
    out = []
    for i, form in enumerate(forms):
        if form not in FORMS_13F:
            continue
        def col(name: str) -> Any:
            values = block.get(name) or []
            return values[i] if i < len(values) else None
        out.append({"form": form, "accession": col("accessionNumber"), "filing_date": col("filingDate"),
                    "period": col("reportDate") or None, "primary_document": col("primaryDocument")})
    return out


def filings_13f(client: Edgar, cik: str, *, periods_wanted: int = 1,
                submissions: Mapping[str, Any] | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """(submissions, 13F filings newest first), reading older pages only when more quarters are needed."""
    sub = submissions or _submissions(client, cik)
    rows = _columns(sub["filings"].get("recent") or {})
    for page in sub["filings"].get("files") or []:
        if len({r["period"] for r in rows if r["form"].startswith("13F-HR")}) >= periods_wanted + 1:
            break
        name = page.get("name") if isinstance(page, Mapping) else None
        if not name:
            continue
        try:
            rows += _columns(client.json(SUBMISSIONS_PAGE_URL.format(name=name), "submissions"))
        except TransportError:
            client.warnings.append(f"Older EDGAR filings page {name} was not available.")
            break
    rows.sort(key=lambda r: (r["filing_date"] or "", r["accession"] or ""), reverse=True)
    return sub, rows


def _documents(client: Edgar, cik: str, accession: str) -> tuple[str | None, str | None, str]:
    """(primary_doc XML, information table XML, folder URL) for one filing."""
    base = ARCHIVE_URL.format(cik=int(cik), folder=_folder(accession))
    index = client.json(base + "index.json", "filing")
    items = [i.get("name") for i in ((index.get("directory") or {}).get("item") or []) if isinstance(i, Mapping)]
    xmls = [n for n in items if isinstance(n, str) and n.lower().endswith(".xml")]
    primary = client.text(base + "primary_doc.xml", "filing") if "primary_doc.xml" in xmls else None
    table = None
    for name in [n for n in xmls if n != "primary_doc.xml"]:
        body = client.text(base + name, "filing")
        if re.search(r"<(\w+:)?informationTable[\s>]", body[:2000]):
            table = body
            break
    return primary, table, base


def _resolve_quarter(client: Edgar, cik: str, period: str, filings: list[dict[str, Any]]) -> dict[str, Any]:
    """One quarter's holdings with amendments applied in filing order."""
    ordered = sorted(filings, key=lambda f: (f["filing_date"] or "", f["accession"]))
    originals = [f for f in ordered if f["form"] in ("13F-HR", "13F-NT")]
    warnings: list[str] = []
    base = originals[-1] if originals else None
    if len(originals) > 1:
        warnings.append(f"{len(originals)} original 13F filings exist for {period}; the latest "
                        f"({base['accession']}) is used.")
    if base is None:
        base = ordered[0]
        warnings.append(f"The original 13F for {period} is outside the filings EDGAR listed; starting from the "
                        f"amendment {base['accession']}.")
    rows: list[dict[str, Any]] = []
    units: set[str] = set()
    amendments: list[dict[str, Any]] = []
    cover: dict[str, Any] = {}
    sources: list[str] = []
    notice = None
    for filing in [base] + [f for f in ordered if f["form"].endswith("/A") and f["filing_date"] >= base["filing_date"]
                            and f is not base]:
        primary, table, folder = _documents(client, cik, filing["accession"])
        sources.append(folder)
        doc = parse_primary_doc(primary) if primary else {}
        report_type = doc.get("report_type") or ("13F NOTICE" if filing["form"].startswith("13F-NT") else None)
        if filing is base:
            cover = doc
            if report_type == "13F NOTICE" or filing["form"].startswith("13F-NT"):
                notice = {"reported_by": [m for m in doc.get("other_managers") or []],
                          "note": doc.get("additional_information")}
                continue
            if table is None:
                raise ManagerDataError(f"{filing['accession']} has no information table")
            parsed = parse_infotable(table, filing_date=filing["filing_date"])
            rows = parsed["rows"]
            units.add(parsed["value_unit"])
            warnings += parsed["warnings"]
            continue
        kind = doc.get("amendment_type") or "RESTATEMENT"
        if table is None:
            amendments.append({"accession": filing["accession"], "form": filing["form"],
                               "filing_date": filing["filing_date"], "amendment_type": kind,
                               "effect": "no information table (cover page change only)"})
            continue
        parsed = parse_infotable(table, filing_date=filing["filing_date"])
        units.add(parsed["value_unit"])
        warnings += parsed["warnings"]
        if kind.startswith("NEW"):
            rows = rows + parsed["rows"]
            effect = f"added {len(parsed['rows'])} holdings"
        else:
            rows = parsed["rows"]
            effect = f"replaced the quarter with {len(parsed['rows'])} rows"
            notice = None
        amendments.append({"accession": filing["accession"], "form": filing["form"],
                           "filing_date": filing["filing_date"], "amendment_type": kind, "effect": effect})
    return {"period": period, "accession": base["accession"], "form": base["form"],
            "filing_date": base["filing_date"], "url": _filing_url(cik, base["accession"]),
            "report_type": cover.get("report_type"), "rows": rows, "value_unit": sorted(units),
            "amendments": amendments, "notice": notice, "warnings": warnings, "sources": sources,
            "table_value_total": cover.get("table_value_total"),
            "table_entry_total": cover.get("table_entry_total"),
            "manager": cover.get("manager")}


def _by_period(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row["period"]:
            grouped.setdefault(row["period"], []).append(row)
    return grouped


# ------------------------------------------------------------------ positions


def positions_from_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate rows by CUSIP into long equity, options and other (principal amounts)."""
    merged: dict[tuple, dict[str, Any]] = {}
    for row in rows:
        key = (row["cusip"], row.get("put_call") or "", row.get("amount_type") or "SH")
        item = merged.get(key)
        if item is None:
            merged[key] = {"issuer": row["issuer"], "class": row["class"], "cusip": row["cusip"],
                           "put_call": row.get("put_call"), "amount_type": row.get("amount_type") or "SH",
                           "shares": 0.0, "value": 0}
            item = merged[key]
        item["shares"] += row["shares"]
        item["value"] += row["value"]
    equity = [p for p in merged.values() if not p["put_call"] and p["amount_type"] == "SH"]
    options = [p for p in merged.values() if p["put_call"]]
    other = [p for p in merged.values() if not p["put_call"] and p["amount_type"] != "SH"]
    total = sum(p["value"] for p in merged.values())
    equity_total = sum(p["value"] for p in equity)
    for group in (equity, options, other):
        group.sort(key=lambda p: (-p["value"], p["cusip"]))
        for p in group:
            p["shares"] = int(p["shares"]) if float(p["shares"]).is_integer() else p["shares"]
            p["weight_of_reported"] = _r(p["value"] / total) if total else None
    for p in equity:
        p["weight"] = _r(p["value"] / equity_total) if equity_total else None
    return {"equity": equity, "options": options, "other": other, "total": total, "equity_total": equity_total,
            "calls": sum(p["value"] for p in options if p["put_call"] == "call"),
            "puts": sum(p["value"] for p in options if p["put_call"] == "put"),
            "other_total": sum(p["value"] for p in other)}


def diff(previous: list[dict[str, Any]], current: list[dict[str, Any]]) -> dict[str, Any]:
    """Quarter-over-quarter change of long-equity positions, keyed by CUSIP."""
    before = {p["cusip"]: p for p in previous}
    after = {p["cusip"]: p for p in current}
    out: dict[str, list[dict[str, Any]]] = {"new": [], "exited": [], "increased": [], "decreased": []}
    unchanged = 0
    for cusip in sorted(set(before) | set(after)):
        old, new = before.get(cusip), after.get(cusip)
        ref = new or old
        row = {"cusip": cusip, "issuer": ref["issuer"], "ticker": ref.get("ticker"),
               "shares": new["shares"] if new else 0, "previous_shares": old["shares"] if old else 0,
               "weight": new.get("weight", 0) if new else 0, "previous_weight": old.get("weight", 0) if old else 0,
               "value": new["value"] if new else 0, "previous_value": old["value"] if old else 0}
        row["share_change"] = row["shares"] - row["previous_shares"]
        row["share_change_pct"] = _r(row["share_change"] / row["previous_shares"], 4) if row["previous_shares"] else None
        row["weight_change"] = _r((row["weight"] or 0) - (row["previous_weight"] or 0))
        if old is None:
            out["new"].append(row)
        elif new is None:
            out["exited"].append(row)
        elif row["share_change"] > 0:
            out["increased"].append(row)
        elif row["share_change"] < 0:
            out["decreased"].append(row)
        else:
            unchanged += 1
    for rows in out.values():
        rows.sort(key=lambda r: -abs(r["weight_change"] or 0))
    return {**out, "unchanged": unchanged,
            "counts": {k: len(v) for k, v in out.items()} | {"unchanged": unchanged},
            "note": "Share changes compare quarter-end snapshots; trades within the quarter are invisible, and a "
                    "stock split shows as an increase in shares."}


# ------------------------------------------------------------------ CUSIP -> ticker

_NAME_NOISE = re.compile(
    r"\b(INC|INCORPORATED|CORP|CORPORATION|CO|COMPANY|LTD|LIMITED|PLC|LLC|L ?P|HLDGS?|HOLDINGS?|GROUP|GRP|THE|NEW|"
    r"DEL|DE|CL|CLASS|COM|COMMON|SHS|SH|ORD|N ?V|S ?A|AG|SE|SPONSORED|ADR|ADS|REG|STK)\b")


def _norm_name(name: Any) -> str:
    text = re.sub(r"[^A-Z0-9 ]", " ", str(name or "").upper().replace("&", " AND "))
    text = _NAME_NOISE.sub(" ", text)
    return " ".join(text.split())


def _class_letter(title: str) -> str | None:
    match = re.search(r"\bCL(?:ASS)?\s+([A-C])\b", title.upper())
    return match.group(1) if match else None


class _TickerIndex:
    def __init__(self, data: Any):
        self.by_name: dict[str, list[dict[str, Any]]] = {}
        self.by_ticker: dict[str, dict[str, Any]] = {}
        rows = data.values() if isinstance(data, Mapping) else data or []
        for row in rows:
            if not isinstance(row, Mapping) or not row.get("ticker"):
                continue
            entry = {"cik": str(row.get("cik_str") or row.get("cik") or "").zfill(10), "ticker": str(row["ticker"]),
                     "title": str(row.get("title") or "")}
            self.by_name.setdefault(_norm_name(entry["title"]), []).append(entry)
            self.by_ticker.setdefault(entry["ticker"].upper(), entry)

    def match(self, issuer: str, title_class: str) -> tuple[dict[str, Any] | None, float]:
        key = _norm_name(issuer)
        if not key:
            return None, 0.0
        score, entries = 1.0, self.by_name.get(key)
        if entries is None:
            close = get_close_matches(key, list(self.by_name), n=3, cutoff=0.75)
            if not close:
                return None, 0.0
            best = max(close, key=lambda c: SequenceMatcher(None, key, c).ratio())
            score, entries = SequenceMatcher(None, key, best).ratio(), self.by_name[best]
        letter = _class_letter(title_class)
        chosen = entries[0]
        if letter and len(entries) > 1:
            chosen = next((e for e in entries if re.search(rf"[.-]?{letter}$", e["ticker"])), chosen)
        return chosen, score


def _ticker_index(client: Edgar) -> _TickerIndex | None:
    try:
        return _TickerIndex(client.json(COMPANY_TICKERS_URL, "tickers"))
    except (TransportError, ManagerDataError) as exc:
        client.warnings.append(f"SEC company_tickers.json was not available ({exc}); no name matching.")
        return None


def map_cusips(client: Edgar, items: Iterable[Mapping[str, Any]],
               overrides: Mapping[str, str] | None = None) -> dict[str, dict[str, Any]]:
    """CUSIP -> {ticker, confidence, method}.

    Supplied mappings are 1.0; OpenFIGI CUSIP matches 0.99 for a US equity listing and 0.7 otherwise
    (a foreign line or a bond, which a US broker may not offer); SEC name matches at
    most 0.9 (0.9 x name similarity), and below 0.6 the name match is dropped.
    """
    wanted: dict[str, Mapping[str, Any]] = {}
    for item in items:
        wanted.setdefault(str(item["cusip"]).upper(), item)
    supplied = {str(k).upper(): str(v).upper() for k, v in (overrides or {}).items()}
    out: dict[str, dict[str, Any]] = {}
    for cusip in wanted:
        if cusip in supplied:
            out[cusip] = {"ticker": supplied[cusip], "confidence": 1.0, "method": "supplied"}
    todo = [c for c in wanted if c not in out]
    figi = client.figi(todo) if todo else {}
    for cusip in todo:
        hit = figi.get(cusip)
        if hit:
            us_equity = hit.get("us_equity", True)
            out[cusip] = {"ticker": hit["ticker"], "confidence": 0.99 if us_equity else 0.7,
                          "method": "openfigi" if us_equity else "openfigi_not_us_equity",
                          "security_type": hit.get("security_type")}
    rest = [c for c in wanted if c not in out]
    index = _ticker_index(client) if rest else None
    for cusip in rest:
        item = wanted[cusip]
        entry, score = index.match(str(item.get("issuer") or ""), str(item.get("class") or "")) if index else (None, 0.0)
        confidence = round(0.9 * score, 2)
        if entry and confidence >= 0.6:
            out[cusip] = {"ticker": entry["ticker"], "confidence": confidence, "method": "name_match",
                          "matched_name": entry["title"]}
        else:
            out[cusip] = {"ticker": None, "confidence": 0.0, "method": "unmapped"}
    return out


def _apply_mapping(groups: Iterable[list[dict[str, Any]]], mapping: Mapping[str, Mapping[str, Any]]) -> None:
    for group in groups:
        for p in group:
            m = mapping.get(p["cusip"]) or {"ticker": None, "confidence": 0.0, "method": "unmapped"}
            p["ticker"], p["ticker_confidence"], p["mapping_method"] = m["ticker"], m["confidence"], m["method"]
            if m.get("security_type"):
                p["security_type"] = m["security_type"]


# ------------------------------------------------------------------ quarters


def _load_quarters(client: Edgar, cik: str, count: int, *, period: str | None = None,
                   submissions: Mapping[str, Any] | None = None) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """(submissions, resolved quarters newest first, 13F filing rows).  Notice-only quarters are kept."""
    sub, rows = filings_13f(client, cik, periods_wanted=count + 1, submissions=submissions)
    grouped = _by_period(rows)
    periods = sorted(grouped, reverse=True)
    if period is not None:
        if period not in grouped:
            raise ManagerDataError(f"CIK {cik} has no 13F for the quarter ending {period}; filed quarters: "
                                   f"{', '.join(periods[:8]) or 'none'}")
        periods = [p for p in periods if p <= period]
    out = []
    for p in periods:
        if len([q for q in out if not q["notice"]]) >= count:
            break
        out.append(_resolve_quarter(client, cik, p, grouped[p]))
    return sub, out, rows


def _lag(quarter: Mapping[str, Any], today: date) -> dict[str, Any]:
    period, filed = _date(quarter["period"]), _date(quarter["filing_date"])
    return {"period_end": quarter["period"], "filed": quarter["filing_date"],
            "days_since_period_end": (today - period).days if period else None,
            "days_since_filing": (today - filed).days if filed else None,
            "filing_delay_days": (filed - period).days if period and filed else None,
            "deadline": "45 days after the quarter ends"}


def _meta(result: dict[str, Any]) -> dict[str, Any]:
    result.update(what_is_13f=WHAT_IS_13F, excludes=EXCLUDES, options_note=OPTIONS_NOTE,
                  amendment_policy=AMENDMENT_POLICY)
    return result


def _sources(client: Edgar, extra: Iterable[str] = ()) -> list[dict[str, str]]:
    urls = list(dict.fromkeys([*client.fetched, *extra]))
    out = [{"title": "SEC EDGAR" if "sec.gov" in u else "OpenFIGI" if "openfigi" in u else "source", "url": u}
           for u in urls]
    out += [{"title": "SEC Form 13F FAQ", "url": DOCS["form_13f_faq"]},
            {"title": "SEC EDGAR APIs and fair access", "url": DOCS["fair_access"]}]
    return out


def _needs(detail: str, missing: str, client: Edgar | None = None) -> dict[str, Any]:
    return envelope("needs_input", _meta({}), missing=[missing], warnings=[detail],
                    sources=_sources(client) if client else [])


def _client(client: Edgar | None) -> Edgar:
    return client if client is not None else Edgar()


# ------------------------------------------------------------------ find


def find(name: str, *, client: Edgar | None = None, limit: int = 8) -> dict[str, Any]:
    """Search EDGAR for a manager by name; candidates carry CIK and latest 13F filing."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name must be a manager or firm name")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
        raise ValueError("limit must be 1-20")
    client = _client(client)
    query = " ".join(name.split())
    terms = [query] + [firm for person, firm in ALIASES.items() if person in query.lower() and firm.lower()
                       not in query.lower()]
    order: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    try:
        for term in terms:
            q = urllib.parse.quote(term.lower())
            try:
                hits = client.json(FULL_TEXT_URL.format(q=urllib.parse.quote('"' + term.lower() + '"')), "search")
                for hit in (hits.get("hits") or {}).get("hits") or []:
                    source = hit.get("_source") or {}
                    for cik, label in zip(source.get("ciks") or [], source.get("display_names") or []):
                        order.setdefault(str(cik).zfill(10), {"name": re.sub(r"\s*\(CIK \d+\)\s*$", "", label).strip(),
                                                              "via": "13F full-text search"})
            except TransportError:
                warnings.append(f"EDGAR full-text search for {term!r} was not available.")
            try:
                hits = client.json(ENTITY_SEARCH_URL.format(q=q), "search")
                for hit in (hits.get("hits") or {}).get("hits") or []:
                    entity = str((hit.get("_source") or {}).get("entity") or "")
                    order.setdefault(str(hit.get("_id")).zfill(10), {"name": re.sub(r"\s*\([^)]*\)\s*$", "", entity).strip(),
                                                                     "via": "EDGAR company search"})
            except TransportError:
                warnings.append(f"EDGAR company search for {term!r} was not available.")
    except UserAgentRequired as exc:
        return _needs(str(exc), SEC_UA_ENV)
    wanted_terms = [_norm_name(t) for t in terms]  # an alias ranks by the firm's name as well

    def score(item: tuple[str, dict[str, Any]]) -> float:
        name = _norm_name(item[1]["name"])
        best = 0.0
        for wanted in wanted_terms:
            tokens = set(wanted.split())
            best = max(best, SequenceMatcher(None, wanted, name).ratio()
                       + (1.0 if tokens and tokens <= set(name.split()) else 0.0))
        return best

    ranked = sorted(order.items(), key=score, reverse=True)  # stable: search relevance breaks ties
    candidates = []
    queue = ranked[:limit]
    seen_ciks = {cik for cik, _ in queue}
    while queue:
        cik, meta = queue.pop(0)
        try:
            normalize_cik(cik)
            sub, rows = filings_13f(client, cik)
        except (TransportError, ManagerDataError, ValueError):
            warnings.append(f"Submissions for CIK {cik} could not be read.")
            continue
        except UserAgentRequired as exc:
            return _needs(str(exc), SEC_UA_ENV)
        latest = rows[0] if rows else None
        latest_hr = next((r for r in rows if r["form"].startswith("13F-HR")), None)
        item = {"cik": cik, "name": sub.get("name") or meta["name"], "files_13f": bool(latest_hr),
                "latest_13f": {k: latest[k] for k in ("form", "filing_date", "period", "accession")} if latest else None,
                "latest_holdings_report": {k: latest_hr[k] for k in ("form", "filing_date", "period", "accession")}
                if latest_hr else None,
                "found_via": meta["via"], "edgar_url": BROWSE_URL.format(cik=cik)}
        if latest and latest["form"].startswith("13F-NT"):
            try:
                primary, _, _ = _documents(client, cik, latest["accession"])
                doc = parse_primary_doc(primary) if primary else {}
                item["notice"] = {"reported_by": doc.get("other_managers") or [],
                                  "note": doc.get("additional_information") or
                                  "The latest quarter's holdings are reported in another manager's 13F."}
                for other in item["notice"]["reported_by"]:
                    if other.get("cik") and other["cik"] not in seen_ciks:
                        seen_ciks.add(other["cik"])
                        queue.append((other["cik"], {"name": other.get("name") or other["cik"],
                                                     "via": f"named in {item['name']}'s 13F notice"}))
            except (TransportError, ManagerDataError):
                item["notice"] = {"reported_by": [], "note": "The latest filing is a 13F notice (holdings reported by "
                                                             "another manager)."}
        candidates.append(item)
    candidates.sort(key=lambda c: not c["files_13f"])  # stable: relevance order within each group
    filers = [c for c in candidates if c["files_13f"]]
    if not candidates:
        return envelope("needs_input", {"query": query, "candidates": []},
                        missing=["a different spelling of the firm name, or its CIK"],
                        warnings=warnings + [f"EDGAR found no filer matching {query!r}."], sources=_sources(client))
    if not filers:
        warnings.append(f"None of the EDGAR filers matching {query!r} files Form 13F. The manager may be under the "
                        "US$100 million threshold, file under a parent or a differently named entity, or not be a "
                        "US-reporting institutional manager.")
    for c in candidates:
        if c.get("notice"):
            names = ", ".join(f"{m.get('name')} (CIK {m.get('cik')})" for m in c["notice"]["reported_by"] if m.get("name"))
            warnings.append(f"{c['name']}'s latest 13F is a notice: its holdings are reported by "
                            f"{names or 'another manager'}.")
    result = {"query": query, "search_terms": terms, "candidates": candidates,
              "note": "Only managers with at least US$100 million in 13(f) securities file 13F; a person is found "
                      "through their firm."}
    return envelope("ready" if filers else "partial", result, warnings=warnings + client.warnings,
                    sources=_sources(client, [DOCS["edgar_apis"]]))


# ------------------------------------------------------------------ holdings


def _quarter_view(quarter: Mapping[str, Any]) -> dict[str, Any]:
    book = positions_from_rows(quarter["rows"])
    return {**quarter, "book": book}


def holdings(cik: Any, period: Any = None, *, client: Edgar | None = None, tickers: Mapping[str, str] | None = None,
             include_options: bool = False, as_of: Any = None) -> dict[str, Any]:
    """The chosen (default latest) quarter's holdings, amendments applied, with the prior-quarter diff."""
    cik = normalize_cik(cik)
    wanted = normalize_period(period)
    client = _client(client)
    today = _today(as_of)
    try:
        sub, quarters, rows = _load_quarters(client, cik, 2, period=wanted)
    except UserAgentRequired as exc:
        return _needs(str(exc), SEC_UA_ENV)
    except TransportError as exc:
        return _needs(f"EDGAR could not be reached: {exc}", "EDGAR access (network)", client)
    name = sub.get("name")
    if not rows:
        return envelope("needs_input", _meta({"manager": {"cik": cik, "name": name}}),
                        missing=["a manager that files Form 13F"],
                        warnings=[f"{name or 'CIK ' + cik} has no 13F filings on EDGAR. Only managers with at least "
                                  "US$100 million in 13(f) securities file them; the holdings may be reported by a "
                                  "parent or affiliate under another CIK."], sources=_sources(client))
    if not quarters:
        return _needs("No quarter with 13F holdings was found.", "a quarter with 13F holdings", client)
    current = quarters[0]
    if current["notice"] is not None:
        reported_by = current["notice"]["reported_by"]
        older = next((q for q in quarters[1:] if not q["notice"]), None)
        names = ", ".join(f"{m.get('name')} (CIK {m.get('cik')})" for m in reported_by if m.get("name"))
        return envelope("needs_input", _meta({
            "manager": {"cik": cik, "name": name}, "period": current["period"],
            "filing": {k: current[k] for k in ("accession", "form", "filing_date", "url", "report_type")},
            "notice": current["notice"],
            "latest_holdings_period": older["period"] if older else None}),
            missing=["cik of the manager whose 13F includes these holdings" + (f": {names}" if names else "")],
            warnings=[f"{name}'s 13F for {current['period']} is a notice (13F-NT): the holdings are reported in "
                      f"{names or 'another manager’s'} 13F. Ask for that CIK, or pass period="
                      f"{older['period'] if older else 'an earlier quarter'} for this filer's last own report."],
            sources=_sources(client))
    view = _quarter_view(current)
    prev_quarter = next((q for q in quarters[1:] if not q["notice"]), None)
    prev_view = _quarter_view(prev_quarter) if prev_quarter else None
    book = view["book"]
    items = book["equity"] + book["options"] + (prev_view["book"]["equity"] if prev_view else [])
    try:
        mapping = map_cusips(client, items, tickers)
    except UserAgentRequired as exc:
        return _needs(str(exc), SEC_UA_ENV)
    _apply_mapping([book["equity"], book["options"], book["other"]] + ([prev_view["book"]["equity"]] if prev_view else []),
                   mapping)
    warnings = list(current["warnings"])
    unmapped = [p for p in book["equity"] if not p["ticker"]]
    low = [p for p in book["equity"] if p["ticker"] and p["ticker_confidence"] < MIN_CONFIDENCE]
    if unmapped:
        warnings.append(f"{len(unmapped)} holdings ({_pct(sum(p['weight'] or 0 for p in unmapped))} of long equity) "
                        "have no ticker; pass tickers {cusip: ticker} to map them.")
    if low:
        warnings.append("Tickers matched by name only, below 0.8 confidence: " +
                        ", ".join(f"{p['issuer']} -> {p['ticker']} ({p['ticker_confidence']})" for p in low) + ".")
    total_check = None
    if current.get("table_value_total") is not None:
        reported = current["table_value_total"] * (1000 if current["value_unit"] == ["thousands"] else 1)
        total_check = {"summary_page": int(reported), "parsed": book["total"],
                       "matches": abs(reported - book["total"]) <= max(1000.0, 0.001 * reported)}
        if not total_check["matches"] and not current["amendments"]:
            warnings.append("The parsed total differs from the filing's summary page total; the information table "
                            "may be incomplete.")
    result = _meta({
        "manager": {"cik": cik, "name": name or current.get("manager")},
        "period": current["period"],
        "filing": {k: current[k] for k in ("accession", "form", "filing_date", "url", "report_type")},
        "amendments": current["amendments"],
        "value_unit": "dollars" if current["value_unit"] == ["dollars"] else
                      "thousands in the filing, converted to dollars" if current["value_unit"] == ["thousands"] else
                      "mixed (original and amendment in different units), all converted to dollars",
        "currency": "USD",
        "total_reported_value": book["total"], "long_equity_value": book["equity_total"],
        "options_value": {"call": book["calls"], "put": book["puts"]}, "other_value": book["other_total"],
        "positions": book["equity"],
        "options": book["options"], "other": book["other"],
        "include_options_in_weights": False,
        "counts": {"positions": len(book["equity"]), "options": len(book["options"]), "other": len(book["other"]),
                   "unmapped": len(unmapped)},
        "unmapped": [{"cusip": p["cusip"], "issuer": p["issuer"], "weight": p["weight"]} for p in unmapped],
        "previous_period": prev_view["period"] if prev_view else None,
        "changes": diff(prev_view["book"]["equity"], book["equity"]) if prev_view else None,
        "lag": _lag(current, today),
        "summary_total_check": total_check,
    })
    if include_options:
        combined = book["equity_total"] + book["calls"] + book["puts"]
        result["include_options_in_weights"] = True
        for p in book["equity"] + book["options"]:
            p["weight_with_options"] = _r(p["value"] / combined) if combined else None
        warnings.append("Weights with options treat each option as its underlying stock value, which overstates "
                        "the money at stake in the option.")
    if prev_view is None:
        warnings.append("No earlier quarter is available, so there is no quarter-over-quarter comparison.")
    status = "partial" if unmapped or low or any("look like" in w for w in warnings) else "ready"
    return envelope(status, result, warnings=warnings + client.warnings, sources=_sources(client),
                    assumptions=["Values are the filing's quarter-end market values in US dollars.",
                                 "Long-equity weights exclude options and principal-amount (debt) rows."])


def _pct(value: float | None) -> str:
    if value is None:
        return "unknown"
    return f"{value * 100:.0f}%" if abs(value) >= 0.095 else f"{value * 100:.1f}%"


# ------------------------------------------------------------------ profile

SECTORS = (
    ((3570, 3579), "Technology"), ((3660, 3699), "Technology"), ((3670, 3679), "Technology"),
    ((7370, 7379), "Technology"), ((3820, 3829), "Technology"),
    ((2830, 2836), "Health care"), ((3840, 3851), "Health care"), ((8000, 8099), "Health care"),
    ((6321, 6324), "Health care"),
    ((6000, 6499), "Financials"), ((6700, 6797), "Financials"), ((6798, 6798), "Real estate"),
    ((6500, 6599), "Real estate"), ((4900, 4991), "Utilities"), ((4800, 4899), "Communication"),
    ((7800, 7999), "Communication"), ((2700, 2799), "Communication"),
    ((1300, 1399), "Energy"), ((2900, 2999), "Energy"),
    ((1000, 1299), "Materials"), ((1400, 1499), "Materials"), ((2800, 2829), "Materials"),
    ((2840, 2899), "Materials"), ((3300, 3399), "Materials"),
    ((2000, 2199), "Consumer staples"), ((5400, 5499), "Consumer staples"),
    ((3710, 3716), "Consumer discretionary"), ((5000, 5399), "Consumer discretionary"),
    ((5500, 5999), "Consumer discretionary"), ((7000, 7299), "Consumer discretionary"),
    ((3720, 3729), "Industrials"), ((3760, 3769), "Industrials"), ((3600, 3659), "Industrials"),
    ((3400, 3569), "Industrials"), ((3580, 3599), "Industrials"), ((4000, 4799), "Industrials"),
    ((1500, 1799), "Industrials"), ((8700, 8799), "Industrials"),
)


def sector_for_sic(sic: Any) -> str | None:
    """A broad sector from a SEC SIC code (first matching range); None when unknown."""
    try:
        code = int(str(sic))
    except (TypeError, ValueError):
        return None
    for (low, high), name in SECTORS:
        if low <= code <= high:
            return name
    if 2000 <= code <= 3999:
        return "Industrials"
    return "Other"


def _sectors(client: Edgar, tickers: Iterable[str]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Ticker -> {sector, sic, sic_description, state_of_incorporation} from issuer submissions."""
    index = _ticker_index(client)
    out: dict[str, dict[str, Any]] = {}
    missing = []
    for ticker in sorted(set(t for t in tickers if t)):
        entry = index.by_ticker.get(ticker.upper()) if index else None
        if entry is None:
            missing.append(ticker)
            continue
        try:
            sub = client.json(SUBMISSIONS_URL.format(cik=entry["cik"]), "issuer")
        except (TransportError, ManagerDataError):
            missing.append(ticker)
            continue
        out[ticker] = {"sector": sector_for_sic(sub.get("sic")), "sic": sub.get("sic"),
                       "sic_description": sub.get("sicDescription"),
                       "state_of_incorporation": sub.get("stateOfIncorporation")}
    return out, missing


_SPLITS = (2, 3, 4, 5, 8, 10, 15, 20, 25, 40, 50)


def _split_ratio(old_shares: float, new_shares: float, old_price: float | None, new_price: float | None) -> float | None:
    """A share-count change that matches a common split ratio with the price moving inversely."""
    if not old_shares or not new_shares or not old_price or not new_price:
        return None
    ratio = new_shares / old_shares
    for k in _SPLITS:
        for r in (k, 1 / k):
            if abs(ratio / r - 1) < 0.005 and 0.6 < (new_price * r / old_price) < 1.6:
                return r
    return None


def _concentration(equity: list[dict[str, Any]]) -> dict[str, Any]:
    weights = sorted((p["weight"] or 0 for p in equity), reverse=True)
    hhi = sum(w * w for w in weights)
    return {"positions": len(weights), "top5": _r(sum(weights[:5]), 4), "top10": _r(sum(weights[:10]), 4),
            "hhi": _r(hhi, 4), "effective_positions": _r(1 / hhi, 1) if hhi else None,
            "largest": _r(weights[0], 4) if weights else None}


def profile_from_history(quarters: list[Mapping[str, Any]], *, sectors: Mapping[str, Any] | None = None,
                         manager: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Interpretation from consecutive quarters (oldest first); each has period, filing_date and ``book``."""
    quarters = [q for q in quarters if not q.get("notice")]
    quarters.sort(key=lambda q: q["period"])
    sectors = sectors or {}
    series = []
    for q in quarters:
        book = q["book"]
        total = book["total"] or 0
        conc = _concentration(book["equity"])
        sector_weights: dict[str, float] = {}
        for p in book["equity"]:
            label = (sectors.get(p.get("ticker")) or {}).get("sector") or "Unknown"
            sector_weights[label] = sector_weights.get(label, 0.0) + (p["weight"] or 0)
        series.append({"period": q["period"], "filing_date": q["filing_date"], "long_equity_value": book["equity_total"],
                       "total_reported_value": total, **conc,
                       "calls_share": _r(book["calls"] / total, 4) if total else None,
                       "puts_share": _r(book["puts"] / total, 4) if total else None,
                       "sectors": {k: _r(v, 4) for k, v in sorted(sector_weights.items(), key=lambda kv: -kv[1])}})
    transitions = []
    entries: dict[str, dict[str, Any]] = {}   # cusip -> entry info for names that appear inside the window
    splits = []
    for i in range(1, len(quarters)):
        before = {p["cusip"]: p for p in quarters[i - 1]["book"]["equity"]}
        after = {p["cusip"]: p for p in quarters[i]["book"]["equity"]}
        buys = sells = new_value = add_value = 0.0
        new_names, adds, trims, exits = [], 0, 0, 0
        for cusip in set(before) | set(after):
            old, new = before.get(cusip), after.get(cusip)
            old_shares = float(old["shares"]) if old else 0.0
            new_shares = float(new["shares"]) if new else 0.0
            old_price = old["value"] / old_shares if old and old_shares else None
            new_price = new["value"] / new_shares if new and new_shares else None
            price = new_price or old_price or 0.0
            if old and new:
                ratio = _split_ratio(old_shares, new_shares, old_price, new_price)
                if ratio:
                    splits.append({"period": quarters[i]["period"], "cusip": cusip, "issuer": new["issuer"],
                                   "ratio": _r(ratio, 4)})
                    continue
            change = new_shares - old_shares
            if change > 0:
                buys += change * price
                if old:
                    adds += 1
                    add_value += change * price
                else:
                    new_value += change * price
                    new_names.append(new)
                    entries[cusip] = {"period_index": i, "weight": new["weight"], "shares": new_shares,
                                      "issuer": new["issuer"], "ticker": new.get("ticker")}
            elif change < 0:
                sells += -change * price
                if new:
                    trims += 1
                else:
                    exits += 1
        average = (quarters[i - 1]["book"]["equity_total"] + quarters[i]["book"]["equity_total"]) / 2
        transitions.append({
            "from": quarters[i - 1]["period"], "to": quarters[i]["period"],
            "buys_estimate": int(buys), "sells_estimate": int(sells),
            "turnover": _r(min(buys, sells) / average, 4) if average else None,
            "new_positions": len(new_names), "added_to": adds, "trimmed": trims, "exited": exits,
            "buying_into_existing_share": _r(add_value / (add_value + new_value), 4) if add_value + new_value else None,
            "new_position_weights": [_r(p["weight"], 4) for p in sorted(new_names, key=lambda p: -(p["weight"] or 0))],
        })
    turnovers = [t["turnover"] for t in transitions if t["turnover"] is not None]
    quarterly = statistics.fmean(turnovers) if turnovers else None
    annual = quarterly * 4 if quarterly is not None else None
    # holding periods: runs of consecutive quarters per CUSIP
    presence: dict[str, list[int]] = {}
    for i, q in enumerate(quarters):
        for p in q["book"]["equity"]:
            presence.setdefault(p["cusip"], []).append(i)
    runs, completed = [], []
    for cusip, idx in presence.items():
        start = prev = idx[0]
        for j in idx[1:] + [None]:
            if j is not None and j == prev + 1:
                prev = j
                continue
            length = prev - start + 1
            runs.append(length)
            if start > 0 and prev < len(quarters) - 1:
                completed.append(length)
            if j is not None:
                start = prev = j
    # build-up of names first bought inside the window
    build = []
    for cusip, info in entries.items():
        later = [q["book"]["equity"] for q in quarters[info["period_index"] + 1:]]
        path = [next((p for p in book if p["cusip"] == cusip), None) for book in later]
        held = []
        for p in path:
            if p is None:
                break
            held.append(p)
        if not held:
            continue
        peak = max(held, key=lambda p: p["weight"] or 0)
        build.append({"issuer": info["issuer"], "ticker": info["ticker"], "entry_weight": _r(info["weight"], 4),
                      "peak_weight": _r(peak["weight"], 4),
                      "quarters_to_peak": held.index(peak) + 1 if (peak["weight"] or 0) > (info["weight"] or 0) else 0,
                      "built_up": float(held[-1]["shares"]) > info["shares"] * 1.1})
    initial = [w for t in transitions for w in t["new_position_weights"] if w is not None]
    first, last = (series[0], series[-1]) if series else ({}, {})
    drift = None
    if len(series) >= 2:
        labels = set(first["sectors"]) | set(last["sectors"])
        drift = _r(0.5 * sum(abs((last["sectors"].get(k) or 0) - (first["sectors"].get(k) or 0)) for k in labels), 4)
    known = [s for s in series if s["sectors"] and set(s["sectors"]) != {"Unknown"}]
    result = {
        "manager": dict(manager or {}),
        "quarters": len(quarters),
        "periods": [q["period"] for q in quarters],
        "turnover": {
            "per_quarter": transitions,
            "average_quarterly": _r(quarterly, 4), "annualised": _r(annual, 4),
            "method": "min(buys, sells) / average long-equity value per quarter, with buys and sells estimated as "
                      "share changes x the quarter-end price (value / shares); average of quarters x 4.",
            "estimate": True,
            "caveat": "An estimate: a 13F shows only quarter-end snapshots, so trades within a quarter, and "
                      "round trips between filings, are invisible and real turnover is usually higher.",
            "splits_ignored": splits,
        },
        "holding_period": {
            "average_quarters": _r(statistics.fmean(runs), 2) if runs else None,
            "median_quarters": _r(float(statistics.median(runs)), 2) if runs else None,
            "completed_average_quarters": _r(statistics.fmean(completed), 2) if completed else None,
            "implied_years": _r(1 / annual, 2) if annual else None,
            "note": f"Counted inside a {len(quarters)}-quarter window, so positions held before or after it are "
                    "cut short (censored); implied years = 1 / annual turnover.",
        },
        "concentration": {
            "by_quarter": [{k: s[k] for k in ("period", "positions", "top5", "top10", "hhi", "effective_positions",
                                              "largest")} for s in series],
            "latest": {k: last.get(k) for k in ("positions", "top5", "top10", "hhi", "effective_positions", "largest")},
            "trend": {k: _r((last.get(k) or 0) - (first.get(k) or 0), 4) if len(series) >= 2 else None
                      for k in ("positions", "top10", "effective_positions")},
        },
        "conviction": {
            "new_positions": sum(t["new_positions"] for t in transitions),
            "added_to_existing": sum(t["added_to"] for t in transitions),
            "buying_into_existing_share": _r(
                statistics.fmean([t["buying_into_existing_share"] for t in transitions
                                  if t["buying_into_existing_share"] is not None]), 4)
            if any(t["buying_into_existing_share"] is not None for t in transitions) else None,
            "typical_initial_weight": _r(statistics.median(initial), 4) if initial else None,
            "average_initial_weight": _r(statistics.fmean(initial), 4) if initial else None,
            "build_up": {"names": build,
                         "share_built_up": _r(sum(b["built_up"] for b in build) / len(build), 4) if build else None,
                         "median_peak_to_entry": _r(statistics.median([b["peak_weight"] / b["entry_weight"]
                                                                       for b in build if b["entry_weight"]]), 2)
                         if any(b["entry_weight"] for b in build) else None},
        },
        "sectors": {
            "by_quarter": [{"period": s["period"], "weights": s["sectors"]} for s in series],
            "drift_first_to_last": drift,
            "method": "Issuer SIC code from EDGAR submissions mapped to broad sectors; Unknown when the ticker or "
                      "SIC code is not available.",
            "coverage": _r(1 - (last.get("sectors") or {}).get("Unknown", 0), 4) if last else None,
        },
        "options": {
            "by_quarter": [{"period": s["period"], "calls_share": s["calls_share"], "puts_share": s["puts_share"]}
                           for s in series],
            "average_calls_share": _r(statistics.fmean([s["calls_share"] or 0 for s in series]), 4) if series else None,
            "average_puts_share": _r(statistics.fmean([s["puts_share"] or 0 for s in series]), 4) if series else None,
            "note": OPTIONS_NOTE,
        },
        "style_drift": {
            "top10_change": _r((last.get("top10") or 0) - (first.get("top10") or 0), 4) if len(series) >= 2 else None,
            "positions_change": (last.get("positions") or 0) - (first.get("positions") or 0) if len(series) >= 2 else None,
            "sector_drift": drift,
        },
        "sector_data_available": bool(known),
    }
    result["character"] = character(result)
    return _meta(result)


def _turnover_word(annual: float | None) -> tuple[str, str] | None:
    if annual is None:
        return None
    if annual < 0.25:
        return "long-term", "de largo plazo"
    if annual < 0.6:
        return "patient", "paciente"
    if annual < 1.2:
        return "active", "activo"
    return "fast-trading", "de rotación muy alta"


def _concentration_word(latest: Mapping[str, Any]) -> tuple[str, str] | None:
    top10, eff = latest.get("top10"), latest.get("effective_positions")
    if top10 is None:
        return None
    if top10 >= 0.6 or (eff is not None and eff < 12):
        return "concentrated", "concentrado"
    if top10 < 0.35:
        return "diversified", "diversificado"
    return "moderately concentrated", "moderadamente concentrado"


def _a(word: str) -> str:
    return "An" if word[:1].lower() in "aeiou" else "A"


def _round_to(value: float, step: int) -> int:
    return int(step * round(value / step))


def character(profile: Mapping[str, Any]) -> dict[str, str]:
    """A one-paragraph read of the numbers, in English and Mexican Spanish."""
    turnover = profile["turnover"]["annualised"]
    latest = profile["concentration"]["latest"]
    words = _turnover_word(turnover)
    conc = _concentration_word(latest)
    if words and conc:
        head_en, head_es = f"{_a(words[0])} {words[0]}, {conc[0]} investor", f"Un inversionista {words[1]} y {conc[1]}"
    elif conc:
        head_en, head_es = f"{_a(conc[0])} {conc[0]} investor", f"Un inversionista {conc[1]}"
    else:
        head_en, head_es = "A manager", "Un administrador"
    facts_en, facts_es = [], []
    if turnover is not None and turnover < 0.05:
        facts_en.append("under 5% turnover a year")
        facts_es.append("rotación menor a 5% al año")
    elif turnover is not None:
        pct = _round_to(turnover * 100, 5)
        facts_en.append(f"about {pct}% turnover a year")
        facts_es.append(f"rotación de alrededor de {pct}% al año")
    if latest.get("positions") is not None:
        facts_en.append(f"{latest['positions']} holdings")
        facts_es.append(f"{latest['positions']} posiciones")
    if latest.get("top10") is not None:
        top = _round_to(latest["top10"] * 100, 5)
        facts_en.append(f"top 10 about {top}%")
        facts_es.append(f"las 10 mayores suman cerca de {top}%")
    en = head_en + (": " + ", ".join(facts_en) if facts_en else "") + "."
    es = head_es + (": " + ", ".join(facts_es) if facts_es else "") + "."
    hp = profile["holding_period"]
    if hp.get("median_quarters") is not None:
        en += f" A position is typically held about {hp['median_quarters']:g} quarters within the " \
              f"{profile['quarters']}-quarter window"
        es += f" Una posición suele mantenerse unos {hp['median_quarters']:g} trimestres dentro de la ventana de " \
              f"{profile['quarters']} trimestres"
        if hp.get("implied_years") and hp["implied_years"] > 30:
            en += " (turnover implies holding for decades)"
            es += " (la rotación implica mantenerlas por décadas)"
        elif hp.get("implied_years"):
            en += f" (turnover implies about {hp['implied_years']:g} years)"
            es += f" (la rotación implica unos {hp['implied_years']:g} años)"
        en += "."
        es += "."
    conv = profile["conviction"]
    if conv.get("typical_initial_weight") is not None:
        en += f" New names typically start near {_pct(conv['typical_initial_weight'])} of the book"
        es += f" Las posiciones nuevas suelen empezar cerca de {_pct(conv['typical_initial_weight'])} del portafolio"
        share = conv["build_up"].get("share_built_up")
        if share is not None:
            en += f", and {_pct(share)} of them were added to in later quarters"
            es += f", y a {_pct(share)} de ellas se les agregó en trimestres siguientes"
        en += "."
        es += "."
    sectors = (profile["sectors"]["by_quarter"] or [{}])[-1].get("weights") or {}
    top_sector = next(((k, v) for k, v in sectors.items() if k != "Unknown"), None)
    if top_sector and top_sector[1] >= 0.4:
        en += f" The book leans to {top_sector[0]} ({_pct(top_sector[1])})."
        es += f" El portafolio se inclina hacia {_SECTOR_ES.get(top_sector[0], top_sector[0])} ({_pct(top_sector[1])})."
    options = profile["options"]
    used = (options.get("average_calls_share") or 0) + (options.get("average_puts_share") or 0)
    if used >= 0.02:
        en += f" Options are a real part of it: about {_pct(used)} of reported value on average, at underlying value."
        es += (f" Las opciones pesan: cerca de {_pct(used)} del valor reportado en promedio, al valor del "
               "subyacente.")
    en += " This is read from quarter-end 13F snapshots, not the manager's full portfolio."
    es += " Esto se lee de fotos trimestrales del 13F, no del portafolio completo del administrador."
    return {"en": en, "es": es}


_SECTOR_ES = {"Technology": "tecnología", "Health care": "salud", "Financials": "finanzas", "Real estate": "bienes raíces",
              "Utilities": "servicios públicos", "Communication": "comunicaciones", "Energy": "energía",
              "Materials": "materiales", "Consumer staples": "consumo básico",
              "Consumer discretionary": "consumo discrecional", "Industrials": "industria", "Other": "otros"}


def _history(client: Edgar, cik: str, quarters: int, tickers: Mapping[str, str] | None,
             with_sectors: bool) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any], list[str]]:
    sub, loaded, rows = _load_quarters(client, cik, quarters)
    views = [_quarter_view(q) for q in loaded if not q["notice"]]
    warnings = [w for q in loaded for w in q["warnings"]]
    notices = [q["period"] for q in loaded if q["notice"]]
    if notices:
        warnings.append(f"Quarters {', '.join(notices)} are 13F notices (holdings reported by another manager) and "
                        "are left out.")
    items = [p for v in views for p in v["book"]["equity"] + v["book"]["options"]]
    mapping = map_cusips(client, items, tickers)
    for v in views:
        _apply_mapping([v["book"]["equity"], v["book"]["options"], v["book"]["other"]], mapping)
    sectors: dict[str, Any] = {}
    if with_sectors:
        sectors, missing = _sectors(client, {p.get("ticker") for v in views for p in v["book"]["equity"]
                                             if (p.get("ticker_confidence") or 0) >= MIN_CONFIDENCE})
        if missing:
            warnings.append(f"No sector for {len(missing)} tickers ({', '.join(missing[:8])}"
                            f"{'…' if len(missing) > 8 else ''}); they count as Unknown.")
    return sub, views, sectors, warnings


def profile(cik: Any, quarters: int = 8, *, client: Edgar | None = None, tickers: Mapping[str, str] | None = None,
            sectors: bool = True, as_of: Any = None) -> dict[str, Any]:
    """How a manager invests, from up to ``quarters`` consecutive 13F filings."""
    cik = normalize_cik(cik)
    if isinstance(quarters, bool) or not isinstance(quarters, int) or not 2 <= quarters <= 40:
        raise ValueError("quarters must be 2-40")
    client = _client(client)
    try:
        sub, views, sector_map, warnings = _history(client, cik, quarters, tickers, sectors)
    except UserAgentRequired as exc:
        return _needs(str(exc), SEC_UA_ENV)
    except TransportError as exc:
        return _needs(f"EDGAR could not be reached: {exc}", "EDGAR access (network)", client)
    except ManagerDataError as exc:
        return _needs(str(exc), "a manager with 13F holdings", client)
    if not views:
        return envelope("needs_input", _meta({"manager": {"cik": cik, "name": sub.get("name")}}),
                        missing=["a manager that files Form 13F holdings"],
                        warnings=warnings + [f"{sub.get('name') or cik} has no 13F holdings reports on EDGAR."],
                        sources=_sources(client))
    result = profile_from_history(views, sectors=sector_map, manager={"cik": cik, "name": sub.get("name")})
    result["lag"] = _lag(views[0], _today(as_of))
    if len(views) < 2:
        warnings.append("Only one quarter is available: turnover, holding period and drift need at least two.")
    if len(views) < quarters:
        warnings.append(f"{len(views)} of the {quarters} quarters asked for are available.")
    status = "ready" if len(views) >= 2 and result["sector_data_available"] else "partial"
    return envelope(status, result, warnings=warnings + client.warnings, sources=_sources(client),
                    assumptions=["Turnover and holding period are estimates from quarter-end snapshots."])


def compare(ciks: Iterable[Any], quarters: int = 8, *, client: Edgar | None = None,
            tickers: Mapping[str, str] | None = None, sectors: bool = True, as_of: Any = None) -> dict[str, Any]:
    """Profiles side by side."""
    ciks = [normalize_cik(c) for c in ciks]
    if not 2 <= len(ciks) <= 6 or len(set(ciks)) != len(ciks):
        raise ValueError("compare needs 2-6 different CIKs")
    client = _client(client)
    profiles, warnings, missing = [], [], []
    for cik in ciks:
        report = profile(cik, quarters, client=client, tickers=tickers, sectors=sectors, as_of=as_of)
        if report["status"] == "needs_input":
            missing += report["missing"]
            warnings += report["warnings"]
            if SEC_UA_ENV in report["missing"]:
                return report
            continue
        profiles.append(report["result"])
        warnings += [f"{report['result']['manager'].get('name') or cik}: {w}" for w in report["warnings"]]
    rows = []
    for p in profiles:
        conc = p["concentration"]["latest"]
        rows.append({"cik": p["manager"]["cik"], "name": p["manager"].get("name"), "quarters": p["quarters"],
                     "latest_period": p["periods"][-1] if p["periods"] else None,
                     "annual_turnover": p["turnover"]["annualised"],
                     "median_quarters_held": p["holding_period"]["median_quarters"],
                     "implied_holding_years": p["holding_period"]["implied_years"],
                     "positions": conc.get("positions"), "top10": conc.get("top10"),
                     "effective_positions": conc.get("effective_positions"),
                     "typical_initial_weight": p["conviction"]["typical_initial_weight"],
                     "options_share": _r((p["options"]["average_calls_share"] or 0) +
                                         (p["options"]["average_puts_share"] or 0), 4),
                     "sector_drift": p["sectors"]["drift_first_to_last"],
                     "character": p["character"]})
    if len(rows) < 2:
        return envelope("needs_input", _meta({"managers": rows}), missing=missing or ["two managers with 13F holdings"],
                        warnings=warnings, sources=_sources(client))
    result = _meta({"managers": rows, "profiles": profiles,
                    "note": "Figures come from each manager's own 13F window; compare like with like (same quarters)."})
    return envelope("partial" if missing else "ready", result, missing=missing, warnings=warnings + client.warnings,
                    sources=_sources(client))


# ------------------------------------------------------------------ mirror


def _unwrap_holdings(holdings: Any) -> dict[str, Any]:
    if isinstance(holdings, Mapping) and "status" in holdings and isinstance(holdings.get("result"), Mapping):
        holdings = holdings["result"]
    if not isinstance(holdings, Mapping) or not isinstance(holdings.get("positions"), list):
        raise ValueError("holdings must be a manager_holdings result (with positions)")
    return dict(holdings)


def target_weights(positions: list[Mapping[str, Any]], *, top_n: int | None = None, min_weight: float = 0.0,
                   cap: float | None = None, min_confidence: float = MIN_CONFIDENCE) -> dict[str, Any]:
    """Long-equity weights -> sleeve targets: drop unmapped, top-N, minimum weight, cap and redistribute pro rata."""
    dropped: dict[str, list[dict[str, Any]]] = {"unmapped": [], "below_min_weight": [], "outside_top_n": []}
    kept: dict[str, float] = {}
    names: dict[str, str] = {}
    ranked = sorted((p for p in positions if (p.get("weight") or 0) > 0), key=lambda p: -(p.get("weight") or 0))
    for p in ranked:
        ticker = p.get("ticker")
        row = {"cusip": p.get("cusip"), "issuer": p.get("issuer"), "ticker": ticker, "weight": p.get("weight")}
        if not ticker or (p.get("ticker_confidence") or 0) < min_confidence:
            dropped["unmapped"].append({**row, "confidence": p.get("ticker_confidence")})
            continue
        if p["weight"] < min_weight:
            dropped["below_min_weight"].append(row)
            continue
        if top_n is not None and len(kept) >= top_n and ticker not in kept:
            dropped["outside_top_n"].append(row)
            continue
        kept[ticker] = kept.get(ticker, 0.0) + float(p["weight"])
        names.setdefault(ticker, p.get("issuer") or ticker)
    total = sum(kept.values())
    if not total:
        return {"weights": {}, "dropped": dropped, "unallocated": 1.0, "capped": [], "names": names,
                "coverage_of_manager": 0.0}
    weights = {t: w / total for t, w in kept.items()}
    uncapped = dict(weights)
    capped: list[str] = []
    unallocated = 0.0
    if cap is not None:
        if cap <= 0 or cap > 1:
            raise ValueError("the single-name cap must be between 0 and 1")
        for _ in range(len(weights) + 1):
            over = [t for t, w in weights.items() if w > cap + 1e-12]
            if not over:
                break
            excess = sum(weights[t] - cap for t in over)
            for t in over:
                weights[t] = cap
                if t not in capped:
                    capped.append(t)
            free = {t: w for t, w in weights.items() if t not in capped}
            base = sum(free.values())
            if base <= 0:
                unallocated += excess
                break
            for t, w in free.items():
                weights[t] = w + excess * w / base
    return {"weights": {t: _r(w) for t, w in sorted(weights.items(), key=lambda kv: -kv[1])},
            "uncapped_weights": {t: _r(w) for t, w in uncapped.items()},
            "dropped": dropped, "unallocated": _r(unallocated), "capped": capped, "names": names,
            "coverage_of_manager": _r(total)}


def _residence(situation: Any) -> str | None:
    if isinstance(situation, str):
        return situation.upper()
    if not isinstance(situation, Mapping):
        return None
    profile_ = situation.get("profile") if isinstance(situation.get("profile"), Mapping) else situation
    residence = profile_.get("residence")
    if isinstance(residence, Mapping):
        return str(residence.get("country") or "").upper() or None
    if isinstance(residence, str):
        return residence.upper()
    country = profile_.get("country")
    return str(country).upper() if country else None


def _situs(cusip: str | None, security_type: str | None = None, title_class: str | None = None) -> dict[str, str]:
    depositary = re.search(r"\bAD[RS]\b|DEPOSITARY|NY REG(ISTRY)?\b", f"{security_type or ''} {title_class or ''}".upper())
    if depositary:
        return {"estate_situs": "not_us", "basis": "Depositary receipt of a non-US company: generally treated as the "
                                                   "foreign company's shares, not US-situs (the point is debated; "
                                                   "confirm)."}
    if cusip and cusip[:1].isalpha():
        return {"estate_situs": "not_us", "basis": "CINS CUSIP: a non-US issuer; shares of a foreign corporation are "
                                                   "generally not US-situs (confirm for ADRs)."}
    return {"estate_situs": "us", "basis": "US-style CUSIP: treated as a US issuer, so US-situs for estate tax "
                                           "(Canadian issuers also use these CUSIPs; confirm)."}


def _speculation(summary: dict[str, Any], ips: Any, situation: Any) -> dict[str, Any]:
    try:
        from . import guardrails  # type: ignore[attr-defined]
    except ImportError:
        guardrails = None
    check = getattr(guardrails, "speculation_check", None) if guardrails else None
    fallback = None
    if callable(check):
        try:
            return {"source": "guardrails.speculation_check", "result": check(summary, ips=ips, situation=situation)}
        except (TypeError, ValueError) as exc:
            fallback = f"guardrails.speculation_check could not be applied ({exc}); built-in assessment used."
    concentrated = (summary["top10"] or 0) >= 0.5 or summary["names"] <= 20 or (summary["largest_manager_weight"] or 0) >= 0.15
    share = summary.get("share_of_portfolio")
    verdict = "satellite" if concentrated else "core_candidate"
    explanation = ("A single manager's concentrated 13F book belongs in a small speculation/satellite sleeve, not the "
                   "core of the portfolio." if concentrated else
                   "The mirrored book is broad, but it is still one manager's lagged snapshot; treat it as a "
                   "satellite unless the policy says otherwise.")
    status = "warn" if concentrated else "pass"
    if share is not None and share > 0.2:
        status = "warn"
        explanation += f" At {_pct(share)} of the portfolio the sleeve is large for a satellite; 5-10% is typical."
    out = {"source": "managers (built-in; wealth.guardrails not present)", "verdict": verdict, "status": status,
           "explanation": explanation, "concentrated": concentrated, "share_of_portfolio": share}
    if fallback:
        out["note"] = fallback
    return out


def mirror(holdings: Any, sleeve_amount: Any, currency: str, *, ips: Mapping[str, Any] | None = None,
           situation: Any = None, constraints: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Target weights and amounts for a sleeve that mirrors a manager's long-equity 13F book.

    ``constraints``: ``top_n``, ``min_weight``, ``max_weight`` (single-name cap; default the IPS
    concentration limit, else 10%), ``min_confidence`` (0.8), ``portfolio_value`` (cap and sizing
    relative to the whole portfolio), ``prices`` ``{TICKER: USD price}``, ``usdmxn``,
    ``sic_listed`` ``{TICKER: bool}``, ``funding``, ``portfolio`` (policy_check shape), and for a trade
    list ``household`` + ``jurisdiction_context`` (+ ``cash_flows``, ``tax_inputs``,
    ``rebalance_constraints``).  Never places orders.
    """
    book = _unwrap_holdings(holdings)
    constraints = dict(constraints or {})
    known = {"top_n", "min_weight", "max_weight", "min_confidence", "portfolio_value", "prices", "usdmxn", "sic_listed",
             "funding", "portfolio", "household", "jurisdiction_context", "cash_flows", "tax_inputs",
             "rebalance_constraints"}
    unknown = sorted(set(constraints) - known)
    if unknown:
        raise ValueError(f"mirror constraints: unknown {unknown}; expected {sorted(known)}")
    try:
        amount = float(sleeve_amount)
    except (TypeError, ValueError):
        raise ValueError("sleeve_amount must be a positive number") from None
    if not math.isfinite(amount) or amount <= 0:
        raise ValueError("sleeve_amount must be a positive number")
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError("currency must be an ISO code such as USD or MXN")
    warnings: list[str] = []
    missing: list[str] = []
    assumptions: list[str] = []
    top_n = constraints.get("top_n")
    if top_n is not None and (isinstance(top_n, bool) or not isinstance(top_n, int) or top_n < 1):
        raise ValueError("top_n must be a positive whole number")
    min_weight = float(constraints.get("min_weight") or 0)
    limit = ((ips or {}).get("constraints") or {}).get("concentration", {}).get("limit") if isinstance(ips, Mapping) else None
    cap_source = "constraints.max_weight" if constraints.get("max_weight") is not None else \
        "policy.ips concentration limit" if limit is not None else "default 10% single-name limit"
    portfolio_limit = float(constraints.get("max_weight") if constraints.get("max_weight") is not None else
                            limit if limit is not None else DEFAULT_CONCENTRATION)
    portfolio_value = constraints.get("portfolio_value")
    if portfolio_value is not None:
        portfolio_value = float(portfolio_value)
        if portfolio_value < amount:
            raise ValueError("portfolio_value must include the sleeve (at least sleeve_amount)")
        cap = min(1.0, portfolio_limit * portfolio_value / amount)
        cap_basis = "whole portfolio"
    else:
        cap = portfolio_limit
        cap_basis = "sleeve (no portfolio_value given, so the limit is applied inside the sleeve)"
    targets = target_weights(book["positions"], top_n=top_n, min_weight=min_weight, cap=cap,
                             min_confidence=float(constraints.get("min_confidence", MIN_CONFIDENCE)))
    if not targets["weights"]:
        return envelope("needs_input", {"tracking_caveats": TRACKING_CAVEATS, "dropped": targets["dropped"]},
                        missing=["tickers for the manager's holdings (pass tickers {cusip: ticker})"],
                        warnings=["No holding has a usable ticker, so nothing can be mirrored."])
    if targets["dropped"]["unmapped"]:
        warnings.append(f"Left out {len(targets['dropped']['unmapped'])} holdings without a confident ticker "
                        f"({_pct(sum(r['weight'] or 0 for r in targets['dropped']['unmapped']))} of the manager's "
                        "long equity); see dropped.unmapped.")
    if targets["unallocated"]:
        warnings.append(f"The {_pct(cap)} single-name cap cannot be met by {len(targets['weights'])} names; "
                        f"{_pct(targets['unallocated'])} of the sleeve stays in cash." +
                        ("" if portfolio_value is not None else " Pass constraints.portfolio_value to apply the "
                         "policy limit to the whole portfolio instead of inside the sleeve."))
    prices = {str(k).upper(): float(v) for k, v in (constraints.get("prices") or {}).items()}
    implied = {p["ticker"]: p["value"] / float(p["shares"]) for p in book["positions"]
               if p.get("ticker") and p.get("shares")}
    resident = _residence(situation)
    usdmxn = constraints.get("usdmxn")
    sic = {str(k).upper(): v for k, v in (constraints.get("sic_listed") or {}).items()}
    rows = []
    cusips = {p.get("ticker"): p.get("cusip") for p in book["positions"]}
    kinds = {p.get("ticker"): (p.get("security_type"), p.get("class")) for p in book["positions"]}
    for ticker, weight in targets["weights"].items():
        row = {"ticker": ticker, "issuer": targets["names"].get(ticker), "cusip": cusips.get(ticker),
               "estate_situs": _situs(cusips.get(ticker), *kinds.get(ticker, (None, None)))["estate_situs"],
               "weight": weight,
               "manager_weight": next((p["weight"] for p in book["positions"] if p.get("ticker") == ticker), None),
               "amount": round(weight * amount, 2), "currency": currency, "capped": ticker in targets["capped"]}
        price = prices.get(ticker)
        row["price"] = price if price is not None else _r(implied.get(ticker), 4)
        row["price_basis"] = "supplied" if price is not None else \
            f"13F quarter-end value / shares ({book.get('period')}); stale" if implied.get(ticker) else None
        if resident == "MX":
            situs = _situs(cusips.get(ticker), *kinds.get(ticker, (None, None)))
            listed = sic.get(ticker)
            row["mexico"] = {"sic_listed": listed if isinstance(listed, bool) else "unknown", **situs}
            if row["price"] is not None and usdmxn:
                share_mxn = float(row["price"]) * float(usdmxn)
                amount_mxn = row["amount"] if currency == "MXN" else row["amount"] * float(usdmxn) if currency == "USD" else None
                row["mexico"]["whole_share_cost_mxn"] = round(share_mxn, 2)
                if amount_mxn is not None:
                    shares = math.floor(amount_mxn / share_mxn)
                    row["mexico"].update(whole_shares=shares, invested_mxn=round(shares * share_mxn, 2),
                                         left_over_mxn=round(amount_mxn - shares * share_mxn, 2),
                                         rounds_to_zero=shares == 0)
        rows.append(row)
    if resident == "MX":
        if not usdmxn:
            missing.append("constraints.usdmxn (to cost whole shares on the SIC)")
        unknown_sic = [r["ticker"] for r in rows if r["mexico"]["sic_listed"] == "unknown"]
        if unknown_sic:
            warnings.append("SIC availability is unknown for " + ", ".join(unknown_sic[:12]) +
                            ("…" if len(unknown_sic) > 12 else "") + ": a name not listed in the SIC is taxed at "
                            "progressive rates instead of 10% and cannot be bought on GBM Trading MX. Pass "
                            "constraints.sic_listed.")
        us_situs = [r["ticker"] for r in rows if r["mexico"]["estate_situs"] == "us"]
        if us_situs:
            warnings.append(f"{len(us_situs)} names are US issuers and therefore US-situs for US estate tax (US$60k "
                            "exemption for non-residents, rates up to 40%), even when bought through the SIC.")
        zero = [r["ticker"] for r in rows if r["mexico"].get("rounds_to_zero")]
        if zero:
            warnings.append("On the SIC (whole shares only) the target amount buys no share of " + ", ".join(zero) +
                            "; raise the sleeve, use top_n, or use a broker with fractional shares.")
        assumptions.append("Mexico facts: docs/mexico-investing-facts.md (verified 2026-09-21).")
    trades_now = len(rows)
    changes = book.get("changes") or {}
    mirrored = set(targets["weights"])
    quarterly = sum(1 for kind in ("new", "exited", "increased", "decreased") for r in changes.get(kind) or []
                    if r.get("ticker") in mirrored)
    weights = list(targets["weights"].values())
    summary = {"label": "13F mirror of " + str((book.get("manager") or {}).get("name") or "a manager"),
               "amount": amount, "currency": currency, "names": len(rows),
               "top10": _r(sum(sorted(weights, reverse=True)[:10]), 4),
               "largest_manager_weight": max((p.get("weight") or 0) for p in book["positions"]) if book["positions"] else None,
               "share_of_portfolio": _r(amount / portfolio_value, 4) if portfolio_value else None}
    result: dict[str, Any] = {
        "manager": book.get("manager"), "period": book.get("period"), "filing": book.get("filing"),
        "lag": book.get("lag"), "sleeve_amount": amount, "currency": currency,
        "targets": rows, "target_weights": targets["weights"], "cash_weight": targets["unallocated"],
        "cap": {"single_name": _r(cap, 4), "portfolio_limit": portfolio_limit, "basis": cap_basis, "source": cap_source,
                "capped": targets["capped"]},
        "dropped": targets["dropped"], "coverage_of_manager": targets["coverage_of_manager"],
        "expected_trades": {"initial": trades_now,
                            "per_quarter_estimate": quarterly if changes else None,
                            "note": "Initial buys to build the sleeve; per quarter, the last 13F's changes among "
                                    "mirrored names (new, exited, added to or trimmed; a proxy for what following it costs)."},
        "speculation": _speculation(summary, ips, situation),
        "tracking_caveats": TRACKING_CAVEATS, "execution_ready": False,
        "scope": "Target weights and amounts for review; never an order.",
    }
    if ips is not None:
        result["policy_check"] = _policy_checks(ips, rows, currency, constraints, situation)
        if result["policy_check"]["verdict"] == "violation":
            warnings.append("Some buys break the investment policy; see policy_check.")
    else:
        missing.append("policy.ips (accepted investment policy) to check the sleeve")
    if constraints.get("household") is not None and constraints.get("jurisdiction_context") is not None:
        result["plan"] = _handoff(rows, targets, constraints, warnings)
        if result["plan"].get("status") != "ready":
            missing += [f"plan: {m}" for m in result["plan"].get("missing") or []]
    else:
        result["plan"] = None
        assumptions.append("No trade list: pass constraints.household and constraints.jurisdiction_context "
                           "to get one from rebalance.")
    status = "partial" if missing or targets["dropped"]["unmapped"] else "ready"
    return envelope(status, result, missing=missing, warnings=warnings,
                    sources=[{"title": "SEC Form 13F FAQ", "url": DOCS["form_13f_faq"]}],
                    assumptions=assumptions + [f"Single-name cap {_pct(cap)} applied to the {cap_basis}; excess "
                                               "weight is redistributed pro rata to the names under the cap."])


def _policy_checks(ips: Mapping[str, Any], rows: list[dict[str, Any]], currency: str,
                   constraints: Mapping[str, Any], situation: Any) -> dict[str, Any]:
    from . import policy
    checks = []
    for row in rows:
        situs = row.get("estate_situs")
        proposal = {"kind": "trade", "action": "buy", "symbol": row["ticker"], "amount": row["amount"],
                    "currency": currency, "asset_class": "stock", "instrument": "stock", "tags": [],
                    "domicile": {"us": "US", "not_us": "NON-US"}.get(situs)}
        if constraints.get("funding"):
            proposal["funding"] = constraints["funding"]
        try:
            report = policy.check(ips, proposal, constraints.get("portfolio"))
        except ValueError as exc:
            return {"verdict": "warn", "rules": [], "error": str(exc)}
        checks.append({"ticker": row["ticker"], "verdict": report["verdict"],
                       "rules": [r for r in report["rules"] if r["status"] != "pass"]})
    verdict = max((c["verdict"] for c in checks), key=policy.STATUS_ORDER.index, default="pass")
    return {"verdict": verdict, "by_name": checks,
            "note": "Each buy is checked on its own against the supplied portfolio (policy.check)."}


def _handoff(rows: list[dict[str, Any]], targets: Mapping[str, Any], constraints: Mapping[str, Any],
             warnings: list[str]) -> dict[str, Any]:
    from . import rebalance
    sleeves = [{"name": r["ticker"], "weight": str(round(r["weight"], 6)), "kind": "equity", "buy": [r["ticker"]]}
               for r in rows]
    total = sum(float(s["weight"]) for s in sleeves)
    cash = round(1 - total, 6)
    if cash > 1e-6:
        sleeves.append({"name": "cash", "weight": str(cash), "kind": "cash", "buy": []})
    elif sleeves:
        sleeves[0]["weight"] = str(round(float(sleeves[0]["weight"]) + (1 - total), 6))
    jc = json.loads(json.dumps(constraints["jurisdiction_context"]))
    instruments = jc.setdefault("instruments", {})
    for row in rows:
        entry = instruments.setdefault(row["ticker"], {})
        if row.get("price") is not None and "price" not in entry and row.get("price_basis") == "supplied":
            entry.update(price=row["price"], currency="USD")
        if row.get("mexico") and isinstance(row["mexico"].get("sic_listed"), bool):
            entry.setdefault("sic_listed", row["mexico"]["sic_listed"])
    try:
        return rebalance.plan(constraints["household"], {"by": "instrument", "sleeves": sleeves},
                              jurisdiction_context=jc, cash_flows=constraints.get("cash_flows"),
                              tax_inputs=constraints.get("tax_inputs"),
                              constraints=constraints.get("rebalance_constraints"))
    except ValueError as exc:
        warnings.append(f"The trade list could not be built: {exc}")
        return envelope("needs_input", {}, missing=[str(exc)])


# ------------------------------------------------------------------ backtest


def backtest_from_history(quarters: list[Mapping[str, Any]], prices: Any, *, rules: Mapping[str, Any] | None = None,
                          benchmark: str | None = None, end: Any = None) -> dict[str, Any]:
    """Replay 'buy the 13F weights on its filing date, hold until the next filing'.

    ``quarters`` oldest first with ``filing_date`` and ``book`` (tickers mapped); ``prices`` a
    pandas DataFrame of daily closes indexed by date.
    """
    import pandas as pd

    rules = dict(rules or {})
    ordered = sorted((q for q in quarters if not q.get("notice")), key=lambda q: q["filing_date"])
    px = prices.sort_index()
    last_date = px.index.max()
    end_date = pd.Timestamp(end) if end is not None else last_date

    def price_at(symbol: str, when: Any) -> float | None:
        if symbol not in px.columns:
            return None
        series = px[symbol].loc[:when].dropna()
        return float(series.iloc[-1]) if len(series) else None

    periods, growth, bench_growth = [], 1.0, 1.0
    for i, q in enumerate(ordered):
        start = pd.Timestamp(q["filing_date"])
        stop = pd.Timestamp(ordered[i + 1]["filing_date"]) if i + 1 < len(ordered) else end_date
        if stop <= start or start > last_date:
            continue
        chosen = target_weights(q["book"]["equity"], top_n=rules.get("top_n"), min_weight=rules.get("min_weight") or 0.0,
                                cap=rules.get("cap"), min_confidence=rules.get("min_confidence", MIN_CONFIDENCE))
        weights = chosen["weights"]
        covered, ret = 0.0, 0.0
        for ticker, w in weights.items():
            p0, p1 = price_at(ticker, start), price_at(ticker, stop)
            if p0 is None or p1 is None or px[ticker].loc[:start].dropna().empty:
                continue
            covered += w
            ret += w * (p1 / p0 - 1)
        if covered <= 0:
            periods.append({"filing_date": q["filing_date"], "period": q["period"], "from": str(start.date()),
                            "to": str(stop.date()), "return": None, "price_coverage": 0.0})
            continue
        # names without prices are left out and the rest scaled up; cash (unallocated) earns nothing
        invested = 1 - (chosen["unallocated"] or 0)
        period_return = ret / covered * invested
        growth *= 1 + period_return
        row = {"filing_date": q["filing_date"], "period": q["period"], "from": str(start.date()), "to": str(stop.date()),
               "return": _r(period_return, 4), "price_coverage": _r(covered / sum(weights.values()), 4)}
        if benchmark:
            b0, b1 = price_at(benchmark, start), price_at(benchmark, stop)
            if b0 and b1:
                row["benchmark_return"] = _r(b1 / b0 - 1, 4)
                bench_growth *= b1 / b0
        periods.append(row)
    used = [p for p in periods if p["return"] is not None]
    span_days = (pd.Timestamp(used[-1]["to"]) - pd.Timestamp(used[0]["from"])).days if used else 0
    annual = (growth ** (365.25 / span_days) - 1) if used and span_days >= 180 else None
    return {
        "label": "Historical and lagged: copies each 13F on the day it was filed and holds it until the next "
                 "filing. Not the manager's return and not a forecast.",
        "historical": True, "lagged": True,
        "periods": periods, "cumulative_return": _r(growth - 1, 4) if used else None,
        "annualised_return": _r(annual, 4),
        "benchmark": benchmark, "benchmark_cumulative_return": _r(bench_growth - 1, 4) if benchmark and used else None,
        "rules": {k: rules.get(k) for k in ("top_n", "min_weight", "cap")},
        "caveats": ["Before taxes, commissions, spreads and FX.", "Prices are adjusted closes; missing names are "
                    "left out and the rest scaled up (see price_coverage)."],
    }


def backtest(cik: Any, *, quarters: int = 8, price_inputs: Mapping[str, Any] | None = None,
             rules: Mapping[str, Any] | None = None, benchmark: str | None = None, client: Edgar | None = None,
             tickers: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Load quarters and prices (market.py: supplied rows, CSV or live) and replay the lagged copy."""
    from . import market

    cik = normalize_cik(cik)
    client = _client(client)
    try:
        sub, views, _, warnings = _history(client, cik, quarters, tickers, False)
    except UserAgentRequired as exc:
        return _needs(str(exc), SEC_UA_ENV)
    except TransportError as exc:
        return _needs(f"EDGAR could not be reached: {exc}", "EDGAR access (network)", client)
    if len(views) < 1:
        return _needs("No 13F holdings to replay.", "a manager with 13F holdings", client)
    symbols = sorted({p["ticker"] for v in views for p in v["book"]["equity"] if p.get("ticker")} |
                     ({benchmark} if benchmark else set()))
    inputs = dict(price_inputs or {})
    if "prices" not in inputs and "price_csv" not in inputs and "years" not in inputs:
        first = min(_date(v["filing_date"]) for v in views)
        inputs["years"] = max(1, math.ceil((_today() - first).days / 365.25) + 1)
    loaded = market._price_frame(inputs, symbols, "USD", optional=set(symbols), align=False)
    if loaded.px is None:
        return envelope("needs_input", {}, missing=loaded.missing, warnings=warnings + loaded.warnings)
    result = backtest_from_history(views, loaded.px, rules=rules, benchmark=benchmark)
    result["manager"] = {"cik": cik, "name": sub.get("name")}
    result["price_source"] = loaded.sources
    if loaded.dropped_optional:
        warnings.append("No prices for " + ", ".join(loaded.dropped_optional[:12]) + "; left out of the replay.")
    return envelope("ready" if result["cumulative_return"] is not None else "partial", result,
                    warnings=warnings + loaded.warnings, sources=_sources(client),
                    assumptions=loaded.assumptions)


# ------------------------------------------------------------------ follow and monitor


def followed(facts: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Followed managers from ``follow.<cik>`` facts: [{cik, name?}]."""
    out = []
    for key, value in facts.items():
        if isinstance(key, str) and key.startswith("follow.") and isinstance(value, Mapping):
            try:
                cik = normalize_cik(value.get("cik") or key.split(".", 1)[1])
            except ValueError:
                continue
            if value.get("notify", True) is not False:
                out.append({"cik": cik, "name": value.get("name")})
    return out


def latest_filings(ciks: Iterable[str], *, client: Edgar | None = None) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Latest 13F per CIK ({cik: {form, filing_date, period, accession, name}}) and {cik: why unchecked}."""
    client = _client(client)
    found, unchecked = {}, {}
    for cik in ciks:
        try:
            sub, rows = filings_13f(client, normalize_cik(cik))
        except UserAgentRequired as exc:
            unchecked[cik] = str(exc)
            continue
        except (TransportError, ManagerDataError, ValueError) as exc:
            unchecked[cik] = str(exc)
            continue
        if rows:
            found[cik] = {**{k: rows[0][k] for k in ("form", "filing_date", "period", "accession")},
                          "name": sub.get("name"), "url": _filing_url(cik, rows[0]["accession"])}
        else:
            unchecked[cik] = "no 13F filings"
    return found, unchecked


def filing_check(rule: Mapping[str, Any], eligible: Mapping[str, Any], previous: Mapping[str, Any],
                 acknowledged: bool, supplied: Mapping[str, Any] | None = None,
                 client: Edgar | None = None) -> tuple[str, dict[str, Any], list, dict[str, Any]]:
    """Monitor rule ``manager_filing``: a new 13F for a followed manager is a proactive item.

    Returns ``(status, detail, identity, state)``.  The first check records what is already filed
    (no item); a later filing stays active until the rule id is acknowledged.
    """
    managers = rule.get("ciks")
    if managers is not None:
        if not isinstance(managers, list):
            raise ValueError("manager_filing ciks must be a list")
        follows = [{"cik": normalize_cik(c), "name": None} for c in managers]
    else:
        follows = followed(eligible)
    names = {f["cik"]: f["name"] for f in follows}
    if supplied is not None:
        if not isinstance(supplied, Mapping):
            raise ValueError("manager_filings must map CIKs to their latest 13F")
        latest = {normalize_cik(k): dict(v) for k, v in supplied.items() if isinstance(v, Mapping)}
        latest = {k: v for k, v in latest.items() if k in names}
        unchecked = {c: "not supplied" for c in names if c not in latest}
    else:
        latest, unchecked = latest_filings(list(names), client=client)
    seen = dict(previous.get("seen") or {})
    items = []
    for cik, filing in latest.items():
        accession = filing.get("accession")
        if not accession:
            unchecked[cik] = "no accession number"
            continue
        if cik not in seen or acknowledged:
            seen[cik] = accession
            continue
        if seen[cik] != accession:
            name = filing.get("name") or names.get(cik) or cik
            items.append({"cik": cik, "name": name, "form": filing.get("form"), "filing_date": filing.get("filing_date"),
                          "period": filing.get("period"), "accession": accession, "url": filing.get("url"),
                          "title": {"en": f"{name} filed a new 13F ({filing.get('form')}, quarter ending "
                                          f"{filing.get('period')})",
                                    "es": f"{name} presentó un nuevo 13F ({filing.get('form')}, trimestre al "
                                          f"{filing.get('period')})"},
                          "next_step": {"task": "manager_holdings", "inputs": {"cik": cik}},
                          "note": "Holdings are as of the quarter end, published with a lag of up to 45 days."})
    if not follows:
        status = "unknown"
        detail: dict[str, Any] = {"missing": "follow.<cik> facts (managers the person follows)"}
    else:
        status = "active" if items else ("unknown" if unchecked and not latest else "clear")
        detail = {"new_filings": items, "unchecked": unchecked, "following": len(follows),
                  "acknowledgement_required": bool(items), "acknowledged": acknowledged}
    identity = sorted(i["accession"] for i in items) + sorted(unchecked)
    return status, detail, identity, {"seen": seen}


# ------------------------------------------------------------------ offline example (catalog)

_EXAMPLE_CUSIPS = {
    "NVDA": ("NVIDIA CORPORATION", "COM", "67066G104", 1045810, 3674, "Semiconductors & Related Devices", "DE"),
    "MSFT": ("MICROSOFT CORP", "COM", "594918104", 789019, 7372, "Services-Prepackaged Software", "WA"),
    "AMZN": ("AMAZON COM INC", "COM", "023135106", 1018724, 5961, "Retail-Catalog & Mail-Order Houses", "DE"),
    "TSM": ("TAIWAN SEMICONDUCTOR MFG LTD", "SPONSORED ADS", "874039100", 1046179, 3674,
            "Semiconductors & Related Devices", "F5"),
    "META": ("META PLATFORMS INC", "CL A", "30303M102", 1326801, 7370, "Services-Computer Programming", "DE"),
    "V": ("VISA INC", "COM CL A", "92826C839", 1403161, 7389, "Services-Business Services", "DE"),
    "BE": ("BLOOM ENERGY CORP", "COM CL A", "093712107", 1664703, 3620, "Electrical Industrial Apparatus", "DE"),
    "BTDR": ("BITDEER TECHNOLOGIES GROUP", "CL A ORD SHS", "G11448100", 1899123, 7374,
             "Services-Computer Processing & Data Preparation", "E9"),
    "JPM": ("JPMORGAN CHASE & CO", "COM", "46625H100", 19617, 6021, "National Commercial Banks", "DE"),
    "KO": ("COCA COLA CO", "COM", "191216100", 21344, 2080, "Beverages", "DE"),
}
# Fictional quarter-end books: {period: (filing_date, {ticker: (shares, price)}, options)}
_EXAMPLE_BOOKS = {
    "0000000001": ("Example Growth Partners LP", {
        "2025-12-31": ("2026-02-13", {"NVDA": (400000, 180.0), "MSFT": (150000, 480.0), "TSM": (200000, 290.0),
                                      "AMZN": (180000, 230.0), "BE": (300000, 90.0)}, {}),
        "2026-03-31": ("2026-05-14", {"NVDA": (380000, 175.0), "MSFT": (150000, 450.0), "TSM": (260000, 300.0),
                                      "META": (60000, 640.0), "BE": (500000, 110.0), "BTDR": (900000, 15.0)},
                       {("BE", "call"): (100000, 110.0)}),
        "2026-06-30": ("2026-08-13", {"NVDA": (300000, 190.0), "TSM": (320000, 310.0), "META": (90000, 700.0),
                                      "BE": (650000, 120.0), "BTDR": (1500000, 16.0), "AMZN": (100000, 225.0)},
                       {("BE", "call"): (150000, 120.0), ("NVDA", "put"): (50000, 190.0)}),
    }),
    "0000000002": ("Example Quality Investors LLC", {
        "2025-12-31": ("2026-02-12", {"MSFT": (200000, 480.0), "V": (250000, 340.0), "JPM": (200000, 300.0),
                                      "KO": (600000, 70.0), "AMZN": (150000, 230.0)}, {}),
        "2026-03-31": ("2026-05-13", {"MSFT": (205000, 450.0), "V": (250000, 330.0), "JPM": (200000, 290.0),
                                      "KO": (600000, 72.0), "AMZN": (160000, 205.0)}, {}),
        "2026-06-30": ("2026-08-12", {"MSFT": (210000, 470.0), "V": (255000, 345.0), "JPM": (195000, 305.0),
                                      "KO": (600000, 71.0), "AMZN": (170000, 225.0)}, {}),
    }),
}


def _infotable_xml(rows: Iterable[Mapping[str, Any]]) -> str:
    from xml.sax.saxutils import escape
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable" '
             'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">']
    for r in rows:
        put_call = f"<putCall>{r['put_call'].capitalize()}</putCall>" if r.get("put_call") else ""
        parts.append(
            f"<infoTable><nameOfIssuer>{escape(r['issuer'])}</nameOfIssuer><titleOfClass>{escape(r['class'])}"
            f"</titleOfClass><cusip>{r['cusip']}</cusip><value>{int(r['value'])}</value><shrsOrPrnAmt><sshPrnamt>"
            f"{int(r['shares'])}</sshPrnamt><sshPrnamtType>{r.get('amount_type', 'SH')}</sshPrnamtType></shrsOrPrnAmt>"
            f"{put_call}<investmentDiscretion>SOLE</investmentDiscretion><votingAuthority><Sole>{int(r['shares'])}"
            f"</Sole><Shared>0</Shared><None>0</None></votingAuthority></infoTable>")
    parts.append("</informationTable>")
    return "\n".join(parts)


def _primary_xml(name: str, period: str, *, report_type: str = "13F HOLDINGS REPORT", amendment_type: str | None = None,
                 entries: int = 0, total: int = 0, other_managers: Iterable[Mapping[str, str]] = ()) -> str:
    from xml.sax.saxutils import escape
    y, m, d = period.split("-")
    amend = (f"<isAmendment>true</isAmendment><amendmentNo>1</amendmentNo><amendmentInfo><amendmentType>"
             f"{amendment_type}</amendmentType></amendmentInfo>" if amendment_type else "<isAmendment>false</isAmendment>")
    others = "".join(f"<otherManager><cik>{o['cik']}</cik><name>{escape(o['name'])}</name></otherManager>"
                     for o in other_managers)
    return (f'<?xml version="1.0" encoding="UTF-8"?><edgarSubmission xmlns="http://www.sec.gov/edgar/thirteenffiler">'
            f"<headerData><submissionType>{'13F-NT' if 'NOTICE' in report_type else '13F-HR'}</submissionType>"
            f"<filerInfo><periodOfReport>{m}-{d}-{y}</periodOfReport></filerInfo></headerData><formData><coverPage>"
            f"<reportCalendarOrQuarter>{m}-{d}-{y}</reportCalendarOrQuarter>{amend}<filingManager><name>{escape(name)}"
            f"</name></filingManager><reportType>{report_type}</reportType>"
            f"{'<otherManagersInfo>' + others + '</otherManagersInfo>' if others else ''}</coverPage>"
            f"<summaryPage><tableEntryTotal>{entries}</tableEntryTotal><tableValueTotal>{total}</tableValueTotal>"
            f"</summaryPage></formData></edgarSubmission>")


def build_snapshot(managers: Iterable[Mapping[str, Any]], *, figi: Mapping[str, str] | None = None,
                   tickers: Iterable[Mapping[str, Any]] = (), issuers: Mapping[str, Mapping[str, Any]] | None = None,
                   searches: Mapping[str, Iterable[str]] | None = None) -> dict[str, Any]:
    """Recorded EDGAR pages for :class:`Snapshot` from compact data (real URL layout and document formats).

    ``managers``: ``[{cik, name, filings: [{accession, form, filing_date, period, rows?, infotable_xml?,
    primary_xml?, amendment_type?, report_type?, other_managers?}]}]``.  ``figi``: ``{cusip: ticker}``.
    ``tickers``: company_tickers rows ``{cik_str, ticker, title}``.  ``issuers``: ``{cik: submissions fields}``.
    ``searches``: ``{query: [cik, ...]}`` answered by both search endpoints.
    """
    pages: dict[str, Any] = {}
    names = {}
    for manager in managers:
        cik = normalize_cik(manager["cik"])
        names[cik] = manager["name"]
        recent = {k: [] for k in ("accessionNumber", "filingDate", "reportDate", "form", "primaryDocument")}
        for f in sorted(manager.get("filings") or [], key=lambda f: f["filing_date"], reverse=True):
            recent["accessionNumber"].append(f["accession"])
            recent["filingDate"].append(f["filing_date"])
            recent["reportDate"].append(f["period"])
            recent["form"].append(f["form"])
            recent["primaryDocument"].append("xslForm13F_X02/primary_doc.xml")
            base = ARCHIVE_URL.format(cik=int(cik), folder=_folder(f["accession"]))
            rows = f.get("rows") or []
            table = f.get("infotable_xml") or (_infotable_xml(rows) if rows or not f["form"].startswith("13F-NT") else None)
            report_type = f.get("report_type") or ("13F NOTICE" if f["form"].startswith("13F-NT") else "13F HOLDINGS REPORT")
            raw = parse_infotable(table, value_unit="dollars")["rows"] if table and not rows else rows
            primary = f.get("primary_xml") or _primary_xml(
                manager["name"], f["period"], report_type=report_type, amendment_type=f.get("amendment_type"),
                entries=len(raw), total=sum(int(r["value"]) for r in raw), other_managers=f.get("other_managers") or ())
            items = [{"name": "primary_doc.xml"}] + ([{"name": "infotable.xml"}] if table else [])
            pages[base + "index.json"] = {"directory": {"item": items, "name": base}}
            pages[base + "primary_doc.xml"] = primary
            if table:
                pages[base + "infotable.xml"] = table
        for extra in manager.get("other_forms") or []:
            for k, v in (("accessionNumber", extra["accession"]), ("filingDate", extra["filing_date"]),
                         ("reportDate", extra.get("period", "")), ("form", extra["form"]), ("primaryDocument", "doc.htm")):
                recent[k].append(v)
        pages[SUBMISSIONS_URL.format(cik=cik)] = {"cik": cik, "name": manager["name"], "sic": manager.get("sic", ""),
                                                  "filings": {"recent": recent, "files": []}}
    for cik, fields in (issuers or {}).items():
        pages.setdefault(SUBMISSIONS_URL.format(cik=normalize_cik(cik)), {"cik": normalize_cik(cik), **fields,
                                                                         "filings": {"recent": {}, "files": []}})
    pages[COMPANY_TICKERS_URL] = {str(i): dict(row) for i, row in enumerate(tickers)}
    pages["openfigi"] = {c: [{"ticker": t, "exchCode": "US", "marketSector": "Equity", "securityType": "Common Stock",
                              "name": t}] for c, t in (figi or {}).items()}
    for query, ciks in (searches or {}).items():
        q = query.lower()
        ciks = [normalize_cik(c) for c in ciks]
        pages[ENTITY_SEARCH_URL.format(q=urllib.parse.quote(q))] = {"hits": {"hits": [
            {"_id": str(int(c)), "_source": {"entity": names.get(c, c)}} for c in ciks]}}
        pages[FULL_TEXT_URL.format(q=urllib.parse.quote('"' + q + '"'))] = {"hits": {"hits": [
            {"_source": {"ciks": [c], "display_names": [f"{names.get(c, c)}  (CIK {c})"]}} for c in ciks
            if c in names]}}
    return pages


_EXAMPLE_PAGES: dict[str, Any] | None = None


def example_pages() -> dict[str, Any]:
    """Fictional, dated managers built from real-format documents; used by catalog examples offline."""
    global _EXAMPLE_PAGES
    if _EXAMPLE_PAGES is not None:
        return _EXAMPLE_PAGES
    managers = []
    for cik, (name, books) in _EXAMPLE_BOOKS.items():
        filings = []
        for n, (period, (filed, book, options)) in enumerate(sorted(books.items())):
            rows = []
            for ticker, (shares, price) in book.items():
                issuer, klass, cusip = _EXAMPLE_CUSIPS[ticker][:3]
                rows.append({"issuer": issuer, "class": klass, "cusip": cusip, "value": shares * price, "shares": shares})
            for (ticker, kind), (shares, price) in options.items():
                issuer, klass, cusip = _EXAMPLE_CUSIPS[ticker][:3]
                rows.append({"issuer": issuer, "class": klass, "cusip": cusip, "value": shares * price, "shares": shares,
                             "put_call": kind})
            filings.append({"accession": f"{cik}-{period[2:4]}-{n + 1:06d}", "form": "13F-HR", "filing_date": filed,
                            "period": period, "rows": rows})
        managers.append({"cik": cik, "name": name, "filings": filings})
    tickers = [{"cik_str": c[3], "ticker": t, "title": c[0].title()} for t, c in _EXAMPLE_CUSIPS.items()]
    issuers = {str(c[3]): {"name": c[0].title(), "sic": str(c[4]), "sicDescription": c[5], "stateOfIncorporation": c[6]}
               for c in _EXAMPLE_CUSIPS.values()}
    figi = {c[2]: t for t, c in _EXAMPLE_CUSIPS.items() if t != "BTDR"}  # BTDR falls back to name matching
    _EXAMPLE_PAGES = build_snapshot(managers, figi=figi, tickers=tickers, issuers=issuers,
                                    searches={"example": ["0000000001", "0000000002"]})
    return _EXAMPLE_PAGES


# ------------------------------------------------------------------ service entry

TASKS = ("manager_search", "manager_holdings", "manager_mirror", "manager_profile", "manager_compare")
_KEYS = {
    "manager_search": {"name", "limit", "snapshot"},
    "manager_holdings": {"cik", "period", "include_options", "tickers", "snapshot", "as_of"},
    "manager_profile": {"cik", "quarters", "sectors", "tickers", "snapshot", "as_of"},
    "manager_compare": {"ciks", "quarters", "sectors", "tickers", "snapshot", "as_of"},
    "manager_mirror": {"holdings", "cik", "period", "sleeve_amount", "currency", "constraints", "ips", "residence",
                       "backtest", "tickers", "snapshot", "as_of"},
}


def client_for(snapshot: Any = None) -> Edgar:
    if snapshot is None:
        return Edgar()
    if snapshot == "example":
        return Edgar(transport=Snapshot(example_pages()))
    if isinstance(snapshot, Mapping):
        return Edgar(transport=Snapshot(snapshot))
    raise ValueError('snapshot must be "example" or recorded pages {url: body}')


def run(task: str, inputs: dict[str, Any], context: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Service entry for the manager_* tasks."""
    context = context if context is not None else {}
    if task not in TASKS:
        raise ValueError(f"managers tasks are {', '.join(TASKS)}")
    if not isinstance(inputs, dict):
        raise ValueError("inputs must be an object")
    unknown = sorted(set(inputs) - _KEYS[task])
    if unknown:
        raise ValueError(f"{task} inputs: unknown {unknown}; expected {sorted(_KEYS[task])}")
    client = client_for(inputs.get("snapshot"))
    tickers = inputs.get("tickers")
    if tickers is not None and not isinstance(tickers, Mapping):
        raise ValueError("tickers must map CUSIPs to tickers")
    if task == "manager_search":
        if "name" not in inputs:
            return envelope("needs_input", {}, missing=["name (a manager, firm or person)"])
        return find(inputs["name"], client=client, limit=inputs.get("limit", 8))
    if task == "manager_holdings":
        if "cik" not in inputs:
            return envelope("needs_input", {}, missing=["cik (find it with manager_search)"])
        return holdings(inputs["cik"], inputs.get("period"), client=client, tickers=tickers,
                        include_options=bool(inputs.get("include_options", False)), as_of=inputs.get("as_of"))
    if task == "manager_profile":
        if "cik" not in inputs:
            return envelope("needs_input", {}, missing=["cik (find it with manager_search)"])
        return profile(inputs["cik"], inputs.get("quarters", 8), client=client, tickers=tickers,
                       sectors=inputs.get("sectors", True) is not False, as_of=inputs.get("as_of"))
    if task == "manager_compare":
        if not isinstance(inputs.get("ciks"), list):
            return envelope("needs_input", {}, missing=["ciks: a list of 2-6 CIKs"])
        return compare(inputs["ciks"], inputs.get("quarters", 8), client=client, tickers=tickers,
                       sectors=inputs.get("sectors", True) is not False, as_of=inputs.get("as_of"))
    # manager_mirror
    need = [k for k in ("sleeve_amount", "currency") if k not in inputs]
    if "holdings" not in inputs and "cik" not in inputs:
        need.insert(0, "holdings (a manager_holdings result) or cik")
    if need:
        return envelope("needs_input", {"tracking_caveats": TRACKING_CAVEATS}, missing=need)
    book = inputs.get("holdings")
    if book is None:
        book = holdings(inputs["cik"], inputs.get("period"), client=client, tickers=tickers, as_of=inputs.get("as_of"))
        if book["status"] == "needs_input":
            book["result"]["tracking_caveats"] = TRACKING_CAVEATS
            return book
    ips = inputs.get("ips", context.get("policy.ips"))
    residence = inputs.get("residence")
    situation = {"residence": {"country": residence}} if isinstance(residence, str) else context.get("client.profile")
    report = mirror(book, inputs["sleeve_amount"], inputs["currency"], ips=ips, situation=situation,
                    constraints=inputs.get("constraints"))
    spec = inputs.get("backtest")
    if spec:
        if not isinstance(spec, Mapping):
            raise ValueError("backtest must be {quarters?, prices?|price_csv+price_source?|years?, benchmark?}")
        cik = (report["result"].get("manager") or {}).get("cik") or inputs.get("cik")
        if not cik:
            report["warnings"].append("The backtest needs the manager's CIK.")
        else:
            cap = report["result"].get("cap", {}).get("single_name")
            constraints = inputs.get("constraints") or {}
            price_inputs = {k: spec[k] for k in ("prices", "price_csv", "price_source", "years") if k in spec}
            replay = backtest(cik, quarters=int(spec.get("quarters", 8)), price_inputs=price_inputs,
                              rules={"top_n": constraints.get("top_n"), "min_weight": constraints.get("min_weight"),
                                     "cap": cap},
                              benchmark=spec.get("benchmark"), client=client, tickers=tickers)
            report["result"]["backtest"] = replay["result"] if replay["status"] != "needs_input" else None
            report["warnings"] += [f"backtest: {w}" for w in replay["warnings"]]
            if replay["status"] == "needs_input":
                report["missing"] += [f"backtest: {m}" for m in replay["missing"]]
    return report


__all__ = ["TASKS", "Edgar", "Snapshot", "UserAgentRequired", "TransportError", "ManagerDataError", "WHAT_IS_13F",
           "EXCLUDES", "TRACKING_CAVEATS", "find", "holdings", "diff", "profile", "profile_from_history", "compare",
           "mirror", "target_weights", "backtest", "backtest_from_history", "parse_infotable", "parse_primary_doc",
           "map_cusips", "positions_from_rows", "build_snapshot", "example_pages", "followed", "latest_filings",
           "filing_check", "sector_for_sic", "character", "normalize_cik", "normalize_period", "run"]
