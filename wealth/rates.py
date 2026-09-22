"""Reference rates: the short government rate a currency's cash could earn, dated and sourced.

:func:`reference` is the one place Wealth reads a risk-free or cash reference rate.  It answers
``reference("MXN")`` or ``reference("USD")`` with ``{rate, as_of, name, source, origin, stale, ...}``.

* **United States.**  The 13-week Treasury bill (26 and 52 weeks come in the same call): the high
  investment rate (bond-equivalent yield) of the latest auction that has one, from Treasury Fiscal
  Data (``auctions_query``, no key), else TreasuryDirect's auction results.  ``as_of`` is the
  auction date.
* **Mexico.**  CETES 28 days: the primary-auction yield Banco de México publishes on its front page
  (no token needed), dated by the auction.  With a Banxico SIE token (``BANXICO_TOKEN``, else the
  keychain item ``wealth-banxico``, read only) the SIE series SF43936 (28 days, a backup), SF43939
  (91), SF43942 (182) and SF43945 (364) are read too; SIE dates them by settlement, two business
  days after the auction.  The token is only ever sent in the ``Bmx-Token`` header.
* **Cache.**  Values live in the market-data cache (``market_prices``, symbol ``rate:<series>``,
  one row per auction date, with the source and retrieval time; ``market_fetches`` logs every
  attempt).  A series is fetched at most once a day (a failed attempt is retried after 3 hours).
* **Never blocking.**  :func:`reference` reads the cache and, when a refresh is due, schedules it
  on a daemon thread (it never delays a CLI command's exit; its writes are one SQLite
  transaction, so a refresh cut short rolls back); the caller gets what is cached (or the
  built-in value) at once.  ``WEALTH_OFFLINE=1`` never touches the network.
* **Order.**  A saved ``cash_reference_rate`` fact in that currency (one naming no currency counts
  only when its source or the household settles which), else the newest fetched rate, else the
  built-in dated constant below.  Only values dated on or before the day asked about count (no
  look-ahead); with none the result is ``origin`` unavailable with no rate.  Every value says where
  it came from (``origin``: ``saved_fact`` | ``fetched`` | ``builtin`` | ``unavailable``) and is
  ``stale`` when its date is more than ``REFERENCE_RATE_STALE_DAYS`` before the day it is used for.
"""
from __future__ import annotations

import concurrent.futures as futures
import json
import re
import ssl
import threading
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .prices import OFFLINE_ENV, RATE_PREFIX, offline_mode

REFERENCE_RATE_STALE_DAYS = 30
REFRESH_EVERY = timedelta(days=1)
RETRY_AFTER = timedelta(hours=3)
TIMEOUT_SECONDS = 4.0
FISCAL_DATA_TIMEOUT = 12.0  # Fiscal Data answered in 3-11 s when measured (Sep 2026); it only runs in the background
LOOKBACK_DAYS = 45  # 52-week bills are auctioned every four weeks
TOKEN_ENV = "BANXICO_TOKEN"
KEYCHAIN_SERVICE = "wealth-banxico"
USER_AGENT = "wealth-harness reference rates (https://github.com/isaacentebi/wealth-harness)"
HOSTS = ("api.fiscaldata.treasury.gov", "www.treasurydirect.gov", "www.banxico.org.mx")
FISCAL_DATA_URL = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/od/auctions_query"
FISCAL_DATA_PAGE = "https://fiscaldata.treasury.gov/datasets/treasury-securities-auctions-data/"
TREASURYDIRECT_URL = "https://www.treasurydirect.gov/TA_WS/securities/auctioned"
TREASURYDIRECT_PAGE = "https://www.treasurydirect.gov/auctions/announcements-data-results/"
BANXICO_INDICATOR_URL = "https://www.banxico.org.mx/canales/singleCetes28.json"
BANXICO_SIE_URL = "https://www.banxico.org.mx/SieAPIRest/service/v1/series/{ids}/datos/oportuno"
BANXICO_TABLE_PAGE = ("https://www.banxico.org.mx/SieInternet/consultarDirectorioInternetAction.do?sector=22"
                      "&accion=consultarCuadro&idCuadro=CF107&locale=es")

# The last-resort values, used only while nothing fetched is cached.  Banxico weekly primary auction
# (subasta de valores gubernamentales) of 2026-09-15: CETES 28 days at 6.25% (91d 6.66%, 182d 6.90%,
# 364d 7.24%).  The 13-week Treasury bill auction of 2026-09-21 (reopening, issued 2026-09-24): high
# investment rate 4.113% (discount rate 4.015%), from Treasury Fiscal Data.
CETES_28D_REFERENCE = {
    "rate": "0.0625", "as_of": "2026-09-15", "checked_on": "2026-09-21", "name": "CETES 28 days",
    "source": "Banxico, subasta primaria de valores gubernamentales del 2026-09-15 (CETES 28 dias 6.25%); "
              + BANXICO_TABLE_PAGE,
}
TBILL_13W_REFERENCE = {
    "rate": "0.04113", "as_of": "2026-09-21", "checked_on": "2026-09-22", "name": "US Treasury bill 13 weeks",
    "source": "U.S. Treasury, 13-week bill auction of 2026-09-21 (high investment rate 4.113%); " + FISCAL_DATA_PAGE,
}

SERIES: dict[str, dict[str, str]] = {
    "us_tbill_13w": {"currency": "USD", "group": "us", "name": "US Treasury bill 13 weeks", "term": "13-Week"},
    "us_tbill_26w": {"currency": "USD", "group": "us", "name": "US Treasury bill 26 weeks", "term": "26-Week"},
    "us_tbill_52w": {"currency": "USD", "group": "us", "name": "US Treasury bill 52 weeks", "term": "52-Week"},
    "mx_cetes_28d": {"currency": "MXN", "group": "mx", "name": "CETES 28 days", "sie": "SF43936", "days": "28"},
    "mx_cetes_91d": {"currency": "MXN", "group": "mx", "name": "CETES 91 days", "sie": "SF43939", "days": "91"},
    "mx_cetes_182d": {"currency": "MXN", "group": "mx", "name": "CETES 182 days", "sie": "SF43942", "days": "182"},
    "mx_cetes_364d": {"currency": "MXN", "group": "mx", "name": "CETES 364 days", "sie": "SF43945", "days": "364"},
}
DEFAULT_SERIES = {"USD": "us_tbill_13w", "MXN": "mx_cetes_28d"}
PRIMARY = {"us": "us_tbill_13w", "mx": "mx_cetes_28d"}  # the series each group's no-token path always covers
BUILTIN = {"us_tbill_13w": TBILL_13W_REFERENCE, "mx_cetes_28d": CETES_28D_REFERENCE}
_TERMS = {meta["term"]: key for key, meta in SERIES.items() if "term" in meta}
_MONTHS = {"ENE": 1, "JAN": 1, "FEB": 2, "MAR": 3, "ABR": 4, "APR": 4, "MAY": 5, "JUN": 6, "JUL": 7, "AGO": 8,
           "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DIC": 12, "DEC": 12}

# (method, url, headers, timeout) -> (status, headers, body); the connectors' GET-only HTTPS transport.
Transport = Callable[[str, str, Mapping[str, str], float], tuple[int, Mapping[str, str], bytes]]


class RateError(Exception):
    """A provider failure or an unreadable response; its message never holds a credential."""


# ------------------------------------------------------------------ parsing


def _dec(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _percent(value: Any) -> Decimal | None:
    """A published percentage ("4.113000") as a decimal rate; None for "null", "N/E" or out of range."""
    number = _dec(value)
    return number / 100 if number is not None and 0 < number < 100 else None


def _plausible(day: date | None, today: date) -> bool:
    return day is not None and date(2000, 1, 1) <= day <= today + timedelta(days=7)


def _obs(series: str, rate: Decimal, day: date, source: str) -> dict:
    return {"series": series, "rate": rate, "as_of": day.isoformat(), "source": source}


def _bills(rows: Iterable[Any], today: date, keys: tuple[str, str, str, str, str],
           describe: Callable[[str, date, Decimal], str]) -> dict[str, dict]:
    """The latest auction with a published high investment rate for each tracked bill term."""
    term_key, day_key, rate_key, cmb_key, type_key = keys
    found: dict[str, dict] = {}
    dated = [(_as_date(r.get(day_key)), r) for r in rows if isinstance(r, Mapping)]
    for day, row in sorted(((d, r) for d, r in dated if d is not None), key=lambda x: x[0], reverse=True):
        series = _TERMS.get(str(row.get(term_key) or ""))
        if (series is None or series in found or str(row.get(type_key) or "Bill") != "Bill"
                or str(row.get(cmb_key) or "No") == "Yes" or not _plausible(day, today)):
            continue
        rate = _percent(row.get(rate_key))
        if rate is not None:
            found[series] = _obs(series, rate, day, describe(SERIES[series]["term"], day, rate))
    return found


def parse_fiscal_data(payload: Any, today: date) -> dict[str, dict]:
    """Treasury Fiscal Data ``auctions_query`` rows -> ``{series: {rate, as_of, source}}``."""
    rows = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(rows, list):
        raise RateError("Treasury Fiscal Data: the response has no data rows")
    return _bills(rows, today, ("security_term", "auction_date", "high_investment_rate", "cash_management_bill_cmb",
                                "security_type"),
                  lambda term, day, rate: f"U.S. Treasury Fiscal Data, Treasury Securities Auctions Data: {term} bill "
                                          f"auction of {day}, high investment rate {_text(rate * 100)}% "
                                          f"(bond-equivalent yield); {FISCAL_DATA_PAGE}")


def parse_treasurydirect(payload: Any, today: date) -> dict[str, dict]:
    """TreasuryDirect ``securities/auctioned`` rows -> ``{series: {rate, as_of, source}}``."""
    if not isinstance(payload, list):
        raise RateError("TreasuryDirect: the response is not a list of auctions")
    return _bills(payload, today, ("securityTerm", "auctionDate", "highInvestmentRate", "cashManagementBillCMB",
                                   "securityType"),
                  lambda term, day, rate: f"U.S. Treasury, TreasuryDirect auction results: {term} bill auction of "
                                          f"{day}, high investment rate {_text(rate * 100)}% (bond-equivalent yield); "
                                          f"{TREASURYDIRECT_PAGE}")


def parse_banxico_indicator(payload: Any, today: date) -> dict[str, dict]:
    """Banxico's front-page CETES 28 indicator ``{"valor": "6.15", "fecha": "22 - SEP - 2026"}``."""
    if not isinstance(payload, Mapping):
        raise RateError("Banxico: the CETES 28 indicator is not an object")
    rate = _percent(payload.get("valor"))
    match = re.fullmatch(r"\s*(\d{1,2})\s*-\s*([A-Za-z]{3})\s*-\s*(\d{4})\s*", str(payload.get("fecha") or ""))
    day = None
    if match and match.group(2).upper() in _MONTHS:
        try:
            day = date(int(match.group(3)), _MONTHS[match.group(2).upper()], int(match.group(1)))
        except ValueError:
            day = None
    if rate is None or not _plausible(day, today):
        raise RateError(f"Banxico: unreadable CETES 28 indicator {dict(payload)!r:.120}")
    source = (f"Banco de México, CETES 28 días: tasa de rendimiento de la subasta primaria del {day} "
              f"({_text(rate * 100)}%), indicador publicado en banxico.org.mx; {BANXICO_TABLE_PAGE}")
    return {"mx_cetes_28d": _obs("mx_cetes_28d", rate, day, source)}


def parse_sie(payload: Any, today: date) -> dict[str, dict]:
    """Banxico SIE ``datos/oportuno`` -> the latest published observation per CETES series."""
    series = ((payload or {}).get("bmx") or {}).get("series") if isinstance(payload, Mapping) else None
    if not isinstance(series, list):
        raise RateError("Banxico SIE: the response has no series")
    by_id = {meta["sie"]: key for key, meta in SERIES.items() if "sie" in meta}
    found: dict[str, dict] = {}
    for entry in series:
        key = by_id.get(str((entry or {}).get("idSerie") or ""))
        if key is None:
            continue
        best: tuple[date, Decimal] | None = None
        for point in entry.get("datos") or []:
            try:
                day = datetime.strptime(str(point.get("fecha")), "%d/%m/%Y").date()
            except (ValueError, AttributeError):
                continue
            rate = _percent(point.get("dato"))
            if rate is not None and _plausible(day, today) and (best is None or day > best[0]):
                best = (day, rate)
        if best is not None:
            meta = SERIES[key]
            found[key] = _obs(key, best[1], best[0], (
                f"Banco de México SIE, serie {meta['sie']} (Cetes a {meta['days']} días, tasa de rendimiento de la "
                f"subasta semanal) con fecha de liquidación {best[0]}: {_text(best[1] * 100)}%; {BANXICO_TABLE_PAGE}"))
    return found


# ------------------------------------------------------------------ network


def _tls() -> ssl.SSLContext:
    """The system trust store plus certifi's (the macOS OpenSSL bundle lacks the root Treasury's chain ends in)."""
    context = ssl.create_default_context()
    try:
        import certifi
        context.load_verify_locations(certifi.where())
    except (ImportError, OSError, ssl.SSLError):
        pass
    return context


def default_transport() -> Transport:
    from .connectors._rest import https_transport
    return https_transport(HOSTS, _tls())


def banxico_token(environ: Mapping[str, str] | None = None, runner: Callable[..., Any] | None = None,
                  platform: str | None = None):
    """The SIE token from ``BANXICO_TOKEN``, else the keychain item ``wealth-banxico`` (read only), else None."""
    from .connectors._rest import load_secret
    try:
        return load_secret(TOKEN_ENV, KEYCHAIN_SERVICE, None, label="Banxico SIE token", environ=environ,
                           runner=runner, platform=platform)
    except ValueError:
        return None


def _get_json(transport: Transport, url: str, timeout: float, headers: Mapping[str, str] | None = None,
              secrets: Iterable[str] = ()) -> Any:
    from .connectors._rest import scrub
    host = urllib.parse.urlsplit(url).hostname
    try:
        status, _, body = transport("GET", url, {"Accept": "application/json", "User-Agent": USER_AGENT,
                                                 **(headers or {})}, timeout)
    except Exception as exc:  # noqa: BLE001 - any transport failure is a failed fetch, reported without secrets
        raise RateError(scrub(f"{host}: {exc}", *secrets)) from None
    if status != 200:
        raise RateError(f"{host} answered HTTP {status}")
    try:
        return json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        raise RateError(f"{host} did not return JSON") from None


def _attempt(errors: list[str], parse: Callable[[], dict[str, dict]]) -> dict[str, dict]:
    try:
        return parse()
    except RateError as exc:
        errors.append(str(exc))
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        errors.append(f"unreadable response ({type(exc).__name__})")
    return {}


def fetch_us(transport: Transport, today: date) -> tuple[dict[str, dict], list[str]]:
    """13/26/52-week bills from Fiscal Data, with TreasuryDirect filling any term it missed."""
    errors: list[str] = []
    start = (today - timedelta(days=LOOKBACK_DAYS)).isoformat()
    query = urllib.parse.urlencode({
        "fields": "security_type,security_term,auction_date,issue_date,high_investment_rate,cash_management_bill_cmb",
        "filter": f"security_type:eq:Bill,security_term:in:(13-Week,26-Week,52-Week),auction_date:gte:{start}",
        "sort": "-auction_date", "page[size]": "100"}, safe=":,()")
    found = _attempt(errors, lambda: parse_fiscal_data(
        _get_json(transport, f"{FISCAL_DATA_URL}?{query}", FISCAL_DATA_TIMEOUT), today))
    if {k for k, m in SERIES.items() if m["group"] == "us"} - set(found):
        query = urllib.parse.urlencode({"format": "json", "type": "Bill", "days": LOOKBACK_DAYS})
        for key, obs in _attempt(errors, lambda: parse_treasurydirect(
                _get_json(transport, f"{TREASURYDIRECT_URL}?{query}", TIMEOUT_SECONDS), today)).items():
            found.setdefault(key, obs)
    return found, errors


def fetch_mx(transport: Transport, today: date, token=None) -> tuple[dict[str, dict], list[str]]:
    """CETES 28 from Banxico's public indicator; with a SIE token, every CETES tenor from SIE too."""
    errors: list[str] = []
    found = _attempt(errors, lambda: parse_banxico_indicator(
        _get_json(transport, BANXICO_INDICATOR_URL, TIMEOUT_SECONDS), today))
    if token is not None:
        secret = token.reveal()
        url = BANXICO_SIE_URL.format(ids=",".join(m["sie"] for m in SERIES.values() if "sie" in m))
        for key, obs in _attempt(errors, lambda: parse_sie(
                _get_json(transport, url, TIMEOUT_SECONDS, {"Bmx-Token": secret}, (secret,)), today)).items():
            found.setdefault(key, obs)
    return found, errors


# ------------------------------------------------------------------ cache


def _store(db_path):
    from .store import WealthStore
    return WealthStore(Path(db_path))


def _symbol(series: str) -> str:
    return RATE_PREFIX + series


def _last_row(store, series: str, on: date) -> dict | None:
    """The newest cached value dated on or before ``on`` (never one published after it)."""
    rows = store.market_rows(_symbol(series), "close", None, on.isoformat())
    return rows[-1] if rows else None


def _due(store, series: str, now: datetime) -> bool:
    """True unless a successful fetch is under a day old or any attempt is under three hours old."""
    for fetch in store.market_fetches(_symbol(series), "close", limit=20):
        try:
            age = now - datetime.fromisoformat(fetch["retrieved_at"])
        except (TypeError, ValueError):
            continue
        if age < RETRY_AFTER or (fetch["status"] == "ok" and age < REFRESH_EVERY):
            return False
    return True


def refresh_group(db_path, group: str, *, transport: Transport | None = None, now: datetime | None = None,
                  token_loader: Callable[[], Any] | None = None) -> dict:
    """Fetch one group (``us`` or ``mx``) now and write every series it covers to the cache (blocking)."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    transport = transport or default_transport()
    wanted = [k for k, m in SERIES.items() if m["group"] == group]
    token = None
    if group == "us":
        found, errors = fetch_us(transport, now.date())
    elif group == "mx":
        token = (token_loader or banxico_token)()
        found, errors = fetch_mx(transport, now.date(), token)
    else:
        raise ValueError("rate groups are us and mx")
    retrieved = now.isoformat(timespec="seconds")
    detail = "; ".join(errors)[:300] or "not in the provider's response"
    no_token = group == "mx" and token is None
    # One transaction for every series: a write cut short (a daemon thread at exit) rolls back whole.
    with _store(db_path) as store, store.atomic():
        for series in wanted:
            obs, log = found.get(series), {"symbol": _symbol(series), "kind": "close", "retrieved_at": retrieved}
            if obs is None:
                reason = "needs a Banxico SIE token" if no_token and series != PRIMARY[group] else detail
                store.put_market([], {**log, "start": now.date().isoformat(), "end": now.date().isoformat(),
                                      "status": "failed", "detail": reason})
                continue
            store.put_market([{"symbol": _symbol(series), "kind": "close", "date": obs["as_of"],
                               "value": _text(obs["rate"]), "currency": SERIES[series]["currency"],
                               "source": obs["source"], "retrieved_at": retrieved}],
                             {**log, "start": obs["as_of"], "end": obs["as_of"], "status": "ok"})
    return {"group": group, "fetched": {k: {"rate": _text(v["rate"]), "as_of": v["as_of"]} for k, v in found.items()
                                        if k in wanted},
            "failed": [k for k in wanted if k not in found and not (no_token and k != PRIMARY[group])],
            **({"needs_token": [k for k in wanted if k not in found and k != PRIMARY[group]]} if no_token else {}),
            "errors": errors}


_PENDING: dict[tuple[str, str], futures.Future] = {}
_LOCK = threading.Lock()


def _run(job: futures.Future, db_path, group: str, transport: Transport | None) -> None:
    if not job.set_running_or_notify_cancel():
        return
    try:
        job.set_result(refresh_group(db_path, group, transport=transport))
    except BaseException as exc:  # noqa: BLE001 - kept on the future; a failed refresh is logged in the cache
        job.set_exception(exc)


def _schedule(db_path, group: str, transport: Transport | None = None) -> futures.Future:
    """One background refresh per database and group at a time, on a daemon thread.

    A daemon thread never holds up interpreter exit (a one-shot CLI command ends at once); if the process
    ends mid-write, the refresh's single SQLite transaction rolls back and the cache is as it was.
    """
    key = (str(db_path), group)
    with _LOCK:
        job = _PENDING.get(key)
        if job is None or job.done():
            job = _PENDING[key] = futures.Future()
            threading.Thread(target=_run, args=(job, db_path, group, transport), name=f"wealth-rates-{group}",
                             daemon=True).start()
        return job


def _refreshing(db_path, group: str) -> bool:
    with _LOCK:
        job = _PENDING.get((str(db_path), group))
        return job is not None and not job.done()


def wait(timeout: float | None = None) -> None:
    """Wait for background refreshes (tests and ``rates refresh``)."""
    with _LOCK:
        jobs = list(_PENDING.values())
    futures.wait(jobs, timeout=timeout)


# ------------------------------------------------------------------ the reference rate


def _result(currency: str, series: str | None, name: str, rate: Decimal, as_of: str, source: str, origin: str,
            on: date, **extra: Any) -> dict:
    age = (on - date.fromisoformat(as_of)).days
    stale = age > REFERENCE_RATE_STALE_DAYS
    offline = extra.pop("offline", False)
    notes = []
    if origin == "builtin":
        notes.append(f"Built-in dated value (the {as_of} auction, checked {extra.get('checked_on')}): no fetched "
                     "rate is cached"
                     + (" yet; a refresh is running." if extra.get("refreshing") else
                        f" and {OFFLINE_ENV} is set." if offline else "."))
    if stale:
        notes.append(f"The rate is from {as_of} ({age} days old); check today's rate.")
    return {"currency": currency, "series": series, "name": name, "rate": _text(rate), "percent": _text(rate * 100),
            "as_of": as_of, "source": source, "origin": origin, "age_days": age, "stale": stale,
            "note": " ".join(notes) or None, "fetched_at": None, "refreshing": False, "fact_id": None, **extra}


_MX_WORDS = re.compile(r"\b(cetes|banxico|mxn|pesos?)\b", re.I)
_US_WORDS = re.compile(r"\b(t-?bills?|treasury|treasuries|usd|dollars?|d[oó]lares)\b", re.I)


def fact_currency(value: Mapping[str, Any], household: str | None = None) -> str | None:
    """The currency a saved ``cash_reference_rate`` is in: its own ``currency``, else what its source or name
    names unambiguously (CETES/Banxico: MXN; T-bill/Treasury: USD), else the household's single currency.
    None when that is ambiguous: the fact then applies to no currency."""
    if value.get("currency"):
        return str(value["currency"]).upper()
    text = f"{value.get('source') or ''} {value.get('name') or ''}"
    mx, us = bool(_MX_WORDS.search(text)), bool(_US_WORDS.search(text))
    if mx != us:
        return "MXN" if mx else "USD"
    return None if mx and us else (str(household).upper() if household else None)


_UNITS = {"decimal": Decimal(1), "percent": Decimal(100), "%": Decimal(100), "pct": Decimal(100),
          "bps": Decimal(10000), "bp": Decimal(10000)}


def _unit_scale(unit: Any, low: Decimal | None, high: Decimal | None) -> Decimal | None:
    """What a saved rate is divided by to be a decimal; None when the unit is unknown or the number ambiguous.

    Without a unit, a rate under 0.1 is a decimal (6.15% saved as 0.0615) and one from 1 to 30 a percent
    (6.15); anything between could be either (0.2 is 0.2% or 20%) and is not guessed."""
    if unit is not None and str(unit).strip():
        return _UNITS.get(str(unit).strip().lower())
    values = [v for v in (low, high) if v is not None]
    if values and all(v < Decimal("0.1") for v in values):
        return Decimal(1)
    if values and all(Decimal(1) <= v <= Decimal(30) for v in values):
        return Decimal(100)
    return None


def from_fact(fact: Mapping[str, Any] | None, currency: str, on: date | None = None, *,
              household: str | None = None) -> tuple[dict | None, str | None]:
    """A saved ``cash_reference_rate`` fact in ``currency`` as a reference (a range is read at its low end).

    ``household`` is the person's single currency when that is unambiguous (it dates a fact that names none).
    ``(None, None)`` when the fact is for another currency; ``(None, why)`` when it cannot be used.
    """
    value = fact.get("value") if isinstance(fact, Mapping) else None
    if not isinstance(value, Mapping):
        return None, None
    owner = fact_currency(value, household)
    if owner is None:
        return None, ("the saved cash_reference_rate names no currency and its source does not say which; it was "
                      "not used (save its currency, MXN or USD)")
    if owner != currency:
        return None, None
    day = on or datetime.now(timezone.utc).date()
    low = _dec(value.get("low", value.get("rate")))
    high = _dec(value.get("high", value.get("low", value.get("rate"))))
    scale = _unit_scale(value.get("unit"), low, high)
    if scale is None:
        return None, (f"the saved cash_reference_rate has unit {value.get('unit')!r} and {low} could be a decimal or "
                      "a percent; it was not used (save unit decimal, percent or bps)")
    meta = fact.get("source") if isinstance(fact.get("source"), Mapping) else {}
    when = (_as_date(value.get("as_of")) or _as_date(meta.get("observed_on")) or _as_date(fact.get("valid_from"))
            or _as_date(fact.get("recorded_at")))
    if low is None or high is None or low < 0 or high < 0 or not value.get("source") or when is None:
        return None, "the saved cash_reference_rate needs low (or rate), unit, source and a date; it was not used"
    if when > day:
        return None, f"the saved cash_reference_rate is dated {when}, after {day}; it was not used"
    low, high = min(low, high) / scale, max(low, high) / scale
    if high >= 1:
        return None, f"the saved cash_reference_rate reads as {_text(high * 100)}% a year; it was not used (check its unit)"
    source = str(value["source"])
    return _result(currency, None, str(value.get("name") or source), low, when.isoformat(), source, "saved_fact",
                   day, fact_id=fact.get("id"), low=_text(low), high=_text(high)), None


def household_currency(jurisdictions: Iterable[str]) -> str | None:
    """MXN for a Mexico-only household, USD for a US-only one, else None (both, or neither)."""
    codes = {str(c).upper() for c in jurisdictions}
    return {"MX": "MXN", "US": "USD"}.get(next(iter(codes))) if len(codes) == 1 else None


def available(ref: Mapping[str, Any] | None) -> bool:
    """True when ``ref`` carries a usable rate (not None, not ``origin`` unavailable)."""
    return bool(ref) and ref.get("rate") is not None


def reference(currency: str | None, *, fact: Mapping[str, Any] | None = None, db_path=None, on: Any = None,
              series: str | None = None, refresh: bool = True, offline: bool | None = None,
              transport: Transport | None = None, household: str | None = None) -> dict | None:
    """The reference rate for ``currency`` (MXN: CETES 28 days; USD: the 13-week T-bill) on ``on``.

    ``fact`` is the saved ``cash_reference_rate`` fact record (it wins when it is for this currency and
    complete; ``household`` settles a fact that names no currency).  ``db_path`` is the Wealth database
    holding the cache; without it only the built-in value is known.  Only values dated on or before ``on``
    are used (no look-ahead): with none, the result has ``origin`` unavailable and ``rate`` None.  A due
    refresh runs in the background unless ``refresh`` is false or offline.  None for a currency with no
    series (only a saved fact could price it).
    """
    ccy = str(currency or "").upper()
    day = _as_date(on) or datetime.now(timezone.utc).date()
    ignored = None
    if fact is not None:
        saved, ignored = from_fact(fact, ccy, day, household=household)
        if saved is not None:
            return saved
    series = series or DEFAULT_SERIES.get(ccy)
    if series not in SERIES or SERIES[series]["currency"] != ccy:
        return None
    offline = offline_mode() if offline is None else bool(offline)
    group = SERIES[series]["group"]
    row, due, refreshing = None, False, False
    if db_path is not None:
        try:
            with _store(db_path) as store:
                row = _last_row(store, series, day)
                due = refresh and not offline and _due(store, series, datetime.now(timezone.utc))
        except Exception:  # noqa: BLE001 - the cache is optional; a turn never fails over it
            row, due = None, False
        try:
            if due:
                _schedule(db_path, group, transport)
            refreshing = _refreshing(db_path, group)
        except RuntimeError:  # the interpreter is shutting down: no new background work
            refreshing = False
    builtin = BUILTIN.get(series)
    if builtin is not None and builtin["as_of"] > day.isoformat():
        builtin = None  # published after the day asked about
    extra: dict[str, Any] = {"refreshing": refreshing}
    if ignored:
        extra["ignored"] = ignored
    if row is not None and (builtin is None or row["date"] >= builtin["as_of"]):
        return _result(ccy, series, SERIES[series]["name"], Decimal(row["value"]), row["date"], row["source"],
                       "fetched", day, fetched_at=row["retrieved_at"], **extra)
    if builtin is not None:
        return _result(ccy, series, builtin["name"], Decimal(builtin["rate"]), builtin["as_of"], builtin["source"],
                       "builtin", day, checked_on=builtin["checked_on"], offline=offline, **extra)
    return {"currency": ccy, "series": series, "name": SERIES[series]["name"], "rate": None, "percent": None,
            "as_of": None, "source": None, "origin": "unavailable", "age_days": None, "stale": None,
            "note": (f"No published {SERIES[series]['name']} rate on or before {day} is known"
                     + ("; a refresh is running." if refreshing else
                        "; it needs a Banxico SIE token (BANXICO_TOKEN or keychain item wealth-banxico)."
                        if group == "mx" and series != PRIMARY[group] else
                        "; give the rate for that date." if db_path is None or day < datetime.now(timezone.utc).date()
                        else "; it has not been fetched yet.")),
            "fetched_at": None, "fact_id": None, **extra}


def origin_text(ref: Mapping[str, Any]) -> str:
    """How a reference was obtained, in words (for item data and reports)."""
    if ref["origin"] == "saved_fact":
        return "stored cash_reference_rate"
    if ref["origin"] == "fetched":
        return f"fetched {str(ref.get('fetched_at') or '')[:10]}".strip()
    if ref["origin"] == "builtin":
        return f"Wealth dated constant (checked {ref.get('checked_on')})"
    return "unavailable"


def status(db_path, on: Any = None, *, refresh: bool = True) -> dict:
    """Every series: its cached (or built-in) value, age and source, and the refresh state."""
    day = _as_date(on) or datetime.now(timezone.utc).date()
    offline = offline_mode()
    rows = [reference(meta["currency"], db_path=db_path, on=day, series=key, refresh=refresh)
            for key, meta in SERIES.items()]
    return {"as_of": day.isoformat(), "offline": offline, "stale_after_days": REFERENCE_RATE_STALE_DAYS,
            "defaults": dict(DEFAULT_SERIES), "rates": rows}


def refresh(db_path, currency: str | None = None, *, transport: Transport | None = None) -> dict:
    """Fetch now (blocking) for ``currency`` or both; offline fetches nothing."""
    if offline_mode():
        return {"refreshed": [], "offline": True,
                "detail": f"{OFFLINE_ENV}=1: nothing was fetched; cached or built-in values are served as they are."}
    ccy = str(currency or "").upper() or None
    if ccy is not None and ccy not in DEFAULT_SERIES:
        raise ValueError("currency must be MXN or USD")
    groups = [SERIES[DEFAULT_SERIES[ccy]]["group"]] if ccy else ["us", "mx"]
    return {"refreshed": [refresh_group(db_path, g, transport=transport) for g in groups], "offline": False}


__all__ = ["BUILTIN", "CETES_28D_REFERENCE", "DEFAULT_SERIES", "REFERENCE_RATE_STALE_DAYS", "RateError", "SERIES",
           "TBILL_13W_REFERENCE", "available", "banxico_token", "fact_currency", "fetch_mx", "fetch_us", "from_fact",
           "household_currency", "origin_text", "parse_banxico_indicator", "parse_fiscal_data", "parse_sie", "parse_treasurydirect",
           "reference", "refresh", "refresh_group", "status", "wait"]
