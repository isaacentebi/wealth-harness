"""Offline example pages (catalog and tests) built from recorded fictional books."""
from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterable, Mapping

from .edgar import (
    ARCHIVE_URL, COMPANY_TICKERS_URL, ENTITY_SEARCH_URL, FULL_TEXT_URL, SUBMISSIONS_URL, _folder,
    normalize_cik, parse_infotable,
)


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
