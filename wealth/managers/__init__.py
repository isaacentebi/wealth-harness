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

from .edgar import (  # noqa: F401
    SEC_UA_ENV, OPENFIGI_KEY_ENV, CACHE_ENV, SUBMISSIONS_URL, SUBMISSIONS_PAGE_URL, ARCHIVE_URL,
    ENTITY_SEARCH_URL, FULL_TEXT_URL, COMPANY_TICKERS_URL, OPENFIGI_URL, BROWSE_URL, DOCS,
    VALUE_IN_DOLLARS_FROM, FORMS_13F, SEC_INTERVAL, FIGI_INTERVAL, FIGI_BATCH, TTL, MIN_CONFIDENCE,
    DEFAULT_CONCENTRATION, ALIASES, EXCLUDES, OPTIONS_NOTE, AMENDMENT_POLICY, WHAT_IS_13F, TRACKING_CAVEATS,
    ManagerDataError, UserAgentRequired, TransportError, Transport, http_transport, Snapshot,
    default_cache_dir, Edgar, normalize_cik, normalize_period, parse_infotable, parse_primary_doc,
    filings_13f,
)
from .positions import (  # noqa: F401
    positions_from_rows, diff, map_cusips, UA_FIX, find, holdings,
)
from .sleeve import (  # noqa: F401
    target_weights, mirror,
)
from .analytics import (  # noqa: F401
    SECTORS, sector_for_sic, SPLIT_VALUE_TOLERANCE, SplitLookup, yahoo_split_history, split_lookup,
    profile_from_history, character, profile, compare, backtest_from_history, backtest,
)
from .follow import (  # noqa: F401
    followed, latest_filings, filing_check,
)
from .example import (  # noqa: F401
    _primary_xml, build_snapshot, example_pages,
)
from .tasks import (  # noqa: F401
    TASKS, client_for, run,
)


__all__ = ["TASKS", "Edgar", "Snapshot", "UserAgentRequired", "TransportError", "ManagerDataError", "WHAT_IS_13F",
           "EXCLUDES", "TRACKING_CAVEATS", "find", "holdings", "diff", "profile", "profile_from_history", "compare",
           "mirror", "target_weights", "backtest", "backtest_from_history", "parse_infotable", "parse_primary_doc",
           "map_cusips", "positions_from_rows", "build_snapshot", "example_pages", "followed", "latest_filings",
           "filing_check", "sector_for_sic", "character", "normalize_cik", "normalize_period", "run"]
