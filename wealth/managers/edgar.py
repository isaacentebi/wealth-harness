"""SEC EDGAR access: constants, transport, the throttled caching client, 13F XML parsing and filing reads."""
from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping


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
       "figi": 90 * 86400, "issuer": 30 * 86400, "splits": 7 * 86400}
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
    from ..service import database_path
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
    incomplete = False
    # One entry per information table applied: "thousands_as_dollars", "dollars_as_thousands" or None when the
    # implied prices look like the declared unit.
    unit_flags: list[str | None] = []

    def _unit_flag(parsed: Mapping[str, Any]) -> str | None:
        text = " ".join(parsed["warnings"])
        if "look like thousands" in text:
            return "thousands_as_dollars"
        if "look like whole dollars" in text:
            return "dollars_as_thousands"
        return None

    # The summary page's total in dollars for the quarter as amended: each filing's own summary in its own unit
    # (thousands before 2023-01-03); a RESTATEMENT's summary covers the whole quarter, a NEW HOLDINGS amendment's
    # summary only the rows it adds.  None when a filing in the chain has no summary total.
    summary_total: float | None = None

    def _summary_dollars(doc: Mapping[str, Any], unit: str) -> float | None:
        value = doc.get("table_value_total")
        return None if value is None else float(value) * (1000 if unit == "thousands" else 1)

    for filing in [base] + [f for f in ordered if f["form"].endswith("/A") and f["filing_date"] >= base["filing_date"]
                            and f is not base]:
        primary, table, folder = _documents(client, cik, filing["accession"])
        sources.append(folder)
        doc = parse_primary_doc(primary) if primary else {}
        report_type = doc.get("report_type") or ("13F NOTICE" if filing["form"].startswith("13F-NT") else None)
        if filing is base:
            cover = doc
            if filing["form"].endswith("/A") and (doc.get("amendment_type") or "").startswith("NEW"):
                # A NEW HOLDINGS amendment only adds rows to an original we do not have.
                incomplete = True
                warnings.append(f"The quarter {period} starts from a NEW HOLDINGS amendment ({filing['accession']}) "
                                "whose original 13F is not available, so its holdings are incomplete; it is left out "
                                "of turnover and new-position counts.")
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
            unit_flags = [_unit_flag(parsed)]
            summary_total = _summary_dollars(doc, parsed["value_unit"])
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
        added = _summary_dollars(doc, parsed["value_unit"])
        if kind.startswith("NEW"):
            # A NEW HOLDINGS amendment adds holdings the original left out; a line it repeats (same CUSIP,
            # option side and amount type) is already counted and is not added twice.
            def line(r: Mapping[str, Any]) -> tuple:
                return r["cusip"], r.get("put_call") or "", r.get("amount_type") or "SH"

            held = {line(r) for r in rows}
            fresh = [r for r in parsed["rows"] if line(r) not in held]
            repeated = [r for r in parsed["rows"] if line(r) in held]
            rows = rows + fresh
            unit_flags.append(_unit_flag(parsed))
            effect = f"added {len(fresh)} holdings"
            if repeated:
                effect += f"; {len(repeated)} already in the quarter were not added again"
                warnings.append(f"The NEW HOLDINGS amendment {filing['accession']} repeats "
                                f"{', '.join(sorted({r['cusip'] for r in repeated}))}, already reported for "
                                f"{period}; the repeated lines are counted once.")
                if added is not None:
                    added -= sum(r["value"] for r in repeated)
            summary_total = summary_total + added if summary_total is not None and added is not None else None
        else:
            rows = parsed["rows"]
            unit_flags = [_unit_flag(parsed)]
            summary_total = added
            effect = f"replaced the quarter with {len(parsed['rows'])} rows"
            notice = None
            incomplete = False
        amendments.append({"accession": filing["accession"], "form": filing["form"],
                           "filing_date": filing["filing_date"], "amendment_type": kind, "effect": effect})
    return {"period": period, "accession": base["accession"], "form": base["form"],
            "filing_date": base["filing_date"], "url": _filing_url(cik, base["accession"]),
            "report_type": cover.get("report_type"), "rows": rows, "value_unit": sorted(units),
            "amendments": amendments, "notice": notice, "warnings": warnings, "sources": sources,
            "incomplete": incomplete, "unit_flags": unit_flags,
            "table_value_total": cover.get("table_value_total"),
            "summary_value_dollars": summary_total,
            "table_entry_total": cover.get("table_entry_total"),
            "manager": cover.get("manager")}


def _by_period(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row["period"]:
            grouped.setdefault(row["period"], []).append(row)
    return grouped
