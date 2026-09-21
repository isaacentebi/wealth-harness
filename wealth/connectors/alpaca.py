"""Read-only Alpaca connector over the Alpaca Trading API v2.

:class:`AlpacaConnector` reads the account, open positions, open orders,
account activities and portfolio history, and turns them into the same
:class:`wealth.ingest.IngestProposal` a statement upload produces.  Nothing is
saved here; the person confirms the summary first.

Read-only by construction: every request passes
:meth:`wealth.connectors._rest.ReadOnlyClient.guard`, which allows only ``GET``
on the six read paths in ``ALLOWED_PATHS``, and the default transport refuses any other method.
Order, position-close, transfer and watchlist endpoints are never called.

Credentials
    The API key id and secret come only from ``WEALTH_ALPACA_KEY_ID`` and
    ``WEALTH_ALPACA_SECRET`` or the OS keychain (service ``wealth-alpaca``,
    accounts ``key_id`` and ``secret``).  ``WEALTH_ALPACA_PAPER=1`` (or the
    ``paper`` input) selects the paper host; paper and live keys differ.  The
    keys travel only in the ``APCA-API-KEY-ID``/``APCA-API-SECRET-KEY`` headers
    (never in a URL), are wrapped in :class:`~wealth.connectors._rest.Secret`
    and are scrubbed from every error.

Alpaca sources (read 2026-09-21)
    * Authentication headers and live/paper hosts (``https://api.alpaca.markets``,
      ``https://paper-api.alpaca.markets``; paper uses its own keys):
      https://docs.alpaca.markets/us/v1.1/docs/authentication-1
    * ``GET /v2/account`` (cash, equity = cash + long_market_value +
      short_market_value, account_number, currency):
      https://docs.alpaca.markets/reference/getaccount-1
    * ``GET /v2/positions`` (qty, avg_entry_price, cost_basis "Total cost basis in
      dollar", market_value, current_price, exchange, asset_class):
      https://docs.alpaca.markets/reference/getallopenpositions
    * ``GET /v2/assets/{symbol}`` (name, exchange, class, optional cusip; no ISIN):
      https://docs.alpaca.markets/reference/get-v2-assets-symbol_or_asset_id
    * ``GET /v2/orders`` (status open/closed/all, limit max 500):
      https://docs.alpaca.markets/reference/getallorders-1
    * ``GET /v2/account/activities`` (activity_types, after, until, direction,
      page_size max 100, page_token = the last activity id):
      https://docs.alpaca.markets/v1.1/reference/getaccountactivities-2
    * Activity objects (FILL: id, qty, price, side, symbol, transaction_time,
      order_id, type fill/partial_fill; non-trade: id, activity_type, date,
      net_amount, symbol, cusip, qty, per_share_amount) and the activity type
      list (CSD, CSW, DIV, DIVNRA, DIVFT, DIVTW, INT, INTNRA, JNLC, FEE, SSP, ...):
      https://docs.alpaca.markets/docs/account-activities
    * ``GET /v2/account/portfolio/history`` (period, timeframe, end; arrays
      timestamp/equity; left-labelled daily points):
      https://docs.alpaca.markets/reference/getaccountportfoliohistory-1
    * Rate limit: "200 requests per minute, per account", HTTP 429 when exceeded:
      https://alpaca.markets/support/usage-limit-api-calls

What Alpaca does not provide
    Per-lot cost basis (only the average entry price), ISINs, fees attached to
    fills (regulatory fees arrive as separate FEE activities) and Mexican SIC
    listing.  Positions therefore carry average cost with ``lots: "unavailable"``
    and ``sic_listed: "unknown"``; issuer domicile is set only when a CUSIP is a
    CINS number (a non-North-American issuer).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import os
import re
from typing import Any, Callable, Iterable, Mapping
from zoneinfo import ZoneInfo

from ..ingest.common import envelope, out
from ..ingest.model import _summary, build_proposal, diff_proposals, proposal_digest
from ..ingest.redact import last4, mask_account
from . import _rest
from ._rest import ConnectorError, ReadOnlyClient, Secret


NAME = "alpaca"
INSTITUTION = "Alpaca"
LIVE_URL = "https://api.alpaca.markets"
PAPER_URL = "https://paper-api.alpaca.markets"
KEYCHAIN_SERVICE = "wealth-alpaca"
KEY_ENV, SECRET_ENV, PAPER_ENV = "WEALTH_ALPACA_KEY_ID", "WEALTH_ALPACA_SECRET", "WEALTH_ALPACA_PAPER"
# The only requests this connector can make (all GET).
ALLOWED_PATHS = (
    r"/v2/account",
    r"/v2/positions",
    r"/v2/orders",
    r"/v2/account/activities",
    r"/v2/account/portfolio/history",
    r"/v2/assets/[A-Za-z0-9.\-]{1,20}",
)
RATE_LIMIT_PER_MINUTE = 200
MIN_INTERVAL = 60.0 / RATE_LIMIT_PER_MINUTE  # 0.3 s: never faster than the documented limit
PAGE_SIZE = 100
MAX_PAGES = 200
DEFAULT_DAYS = 365
TIMEOUT_SECONDS = 180.0
_EASTERN = ZoneInfo("America/New_York")
_HINTS = {401: "Check the Alpaca key id and secret (paper and live keys differ; WEALTH_ALPACA_PAPER selects paper).",
          403: "The key is not allowed to read this account."}

# Looked up at call time so tests (and hosts) can substitute them.
default_transport: _rest.Transport = _rest.https_transport({"api.alpaca.markets", "paper-api.alpaca.markets"})
default_sleep = _rest.default_sleep
default_clock = _rest.default_clock


# -- credentials ------------------------------------------------------------

class AlpacaKeys:
    """Key id and secret; never printed."""

    __slots__ = ("key_id", "secret")

    def __init__(self, key_id: Secret, secret: Secret):
        self.key_id, self.secret = key_id, secret

    @property
    def source(self) -> str:
        return self.key_id.source if self.key_id.source == self.secret.source else "mixed"

    def values(self) -> tuple[str, str]:
        return self.key_id.reveal(), self.secret.reveal()

    def __repr__(self) -> str:
        return f"AlpacaKeys(source={self.source!r}, key_id='****', secret='****')"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("AlpacaKeys cannot be serialized")


def load_keys(environ: Mapping[str, str] | None = None, runner: Callable[..., Any] | None = None,
              platform: str | None = None) -> AlpacaKeys | None:
    key = _rest.load_secret(KEY_ENV, KEYCHAIN_SERVICE, "key_id", label="Alpaca key id", environ=environ,
                            runner=runner, platform=platform)
    secret = _rest.load_secret(SECRET_ENV, KEYCHAIN_SERVICE, "secret", label="Alpaca secret", environ=environ,
                               runner=runner, platform=platform)
    return AlpacaKeys(key, secret) if key and secret else None


def paper_default(environ: Mapping[str, str] | None = None) -> bool:
    environ = os.environ if environ is None else environ
    return (environ.get(PAPER_ENV) or "").strip().lower() in {"1", "true", "yes", "on"}


# -- fetch ------------------------------------------------------------------

def client(keys: AlpacaKeys, *, paper: bool, transport: _rest.Transport | None = None,
           sleep: Callable[[float], None] | None = None, clock: Callable[[], float] | None = None,
           timeout: float = TIMEOUT_SECONDS) -> ReadOnlyClient:
    def headers() -> dict[str, str]:
        key_id, secret = keys.values()
        return {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret, "Accept": "application/json",
                "User-Agent": "wealth-harness/0.2 (read-only)"}

    return ReadOnlyClient(provider="Alpaca", base_url=PAPER_URL if paper else LIVE_URL, headers=headers,
                          allowed_paths=ALLOWED_PATHS, transport=transport or default_transport,
                          secrets=keys.values, sleep=sleep or default_sleep, clock=clock or default_clock,
                          timeout=timeout, min_interval=MIN_INTERVAL, hints=_HINTS)


def _since(value: Any, today: date) -> date:
    if value in (None, ""):
        return today - timedelta(days=DEFAULT_DAYS)
    try:
        chosen = date.fromisoformat(str(value)[:10])
    except ValueError:
        raise ValueError("since must be an ISO date (YYYY-MM-DD)") from None
    if chosen > today:
        raise ValueError("since cannot be in the future")
    return chosen


def fetch_snapshot(api: ReadOnlyClient, *, since: date, today: date) -> dict[str, Any]:
    """Every read the proposal needs, as raw Alpaca JSON."""
    account = api.get_json("/v2/account")
    positions = api.get_json("/v2/positions")
    after = api.get_json("/v2/account")  # equity again: prices move while positions are read
    orders = api.get_json("/v2/orders", {"status": "open", "limit": 500, "direction": "desc"})
    activities: list[dict[str, Any]] = []
    token = None
    for _ in range(MAX_PAGES):
        page = api.get_json("/v2/account/activities", {
            "after": (since - timedelta(days=1)).isoformat(), "direction": "asc", "page_size": PAGE_SIZE,
            "page_token": token})
        if not isinstance(page, list):
            raise ConnectorError("Alpaca returned account activities in an unexpected shape.")
        activities.extend(page)
        if len(page) < PAGE_SIZE:
            break
        token = page[-1].get("id")
        if not token:
            raise ConnectorError("Alpaca returned an activity page without ids; cannot continue paging.")
    else:
        raise ConnectorError(f"More than {MAX_PAGES * PAGE_SIZE} Alpaca activities; sync a shorter period with since.")
    days = max(1, (today - since).days + 1)
    history = api.get_json("/v2/account/portfolio/history", {"period": f"{days}D", "timeframe": "1D"})
    symbols = sorted({str(p.get("symbol") or "") for p in positions if p.get("asset_class") == "us_equity"}
                     | {str(a.get("symbol") or "") for a in activities if a.get("activity_type") == "FILL"})
    assets = {}
    for symbol in symbols:
        if re.fullmatch(r"[A-Za-z0-9.\-]{1,20}", symbol):
            try:
                assets[symbol] = api.get_json(f"/v2/assets/{symbol}")
            except ConnectorError as exc:  # a delisted or renamed symbol: keep going without its facts
                if exc.status != 404:
                    raise
    return {"account": account, "account_after": after, "positions": positions, "orders": orders,
            "activities": activities, "portfolio_history": history, "assets": assets,
            "since": since.isoformat(), "as_of": today.isoformat()}


# -- mapping ----------------------------------------------------------------

def _dec(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _day(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if "T" in text:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if moment.tzinfo is not None:
                moment = moment.astimezone(_EASTERN)
            return moment.date().isoformat()
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        return None


_US_EXCHANGES = frozenset({"NYSE", "NASDAQ", "ARCA", "AMEX", "BATS", "OTC", "NYSEARCA", "IEX"})
_LEDGER_LISTING = {"NYSE": "NYSE", "NASDAQ": "NASDAQ", "ARCA": "ARCA", "NYSEARCA": "ARCA", "OTC": "OTC"}
_FUND_WORDS = re.compile(r"(?i)\b(ETF|ETN|fund|index trust)\b")


def _facts(symbol: str, position: Mapping[str, Any] | None, asset: Mapping[str, Any] | None) -> dict[str, Any]:
    asset = asset or {}
    position = position or {}
    facts: dict[str, Any] = {"symbol": symbol, "underlying_symbol": symbol}
    klass = str(position.get("asset_class") or asset.get("class") or "").lower()
    exchange = str(asset.get("exchange") or position.get("exchange") or "").upper()
    name = " ".join(str(asset.get("name") or "").split())
    if name:
        facts["name"] = name[:120]
    if exchange:
        facts["listing_exchange"] = exchange
    if klass == "us_equity":
        facts["venue"] = "us" if exchange in _US_EXCHANGES or not exchange else "other"
        facts["asset_class"] = "fund" if _FUND_WORDS.search(name) else "equity"
        facts["asset_type"] = "etf" if facts["asset_class"] == "fund" else "stock"
    elif klass == "us_option":
        facts["asset_class"], facts["asset_type"] = "derivative", "options"
    elif klass == "crypto":
        facts["asset_type"] = "crypto"
    cusip = str(asset.get("cusip") or "").upper()
    if re.fullmatch(r"[0-9A-Z]{8}[0-9]", cusip):
        facts["cusip"] = cusip
        if cusip[0].isalpha():  # CINS: issuer outside the US and Canada
            facts["issuer_domicile"] = "other"
    return facts


def _external_id(activity: Mapping[str, Any]) -> str | None:
    raw = str(activity.get("id") or "")
    if not re.fullmatch(r"[0-9]{8,20}::[0-9a-fA-F\-]{8,40}", raw):
        return None
    head, tail = raw.split("::")
    return f"ALPACA-A{head}_{tail.replace('-', '').lower()}"


_TYPES: dict[str, tuple[str, str | None]] = {
    # activity_type: (ingest type, reason when it is listed but not posted)
    "CSD": ("deposit", None), "CSW": ("withdrawal", None), "CSR": ("withdrawal", None),
    "ACATC": ("deposit_or_withdrawal", None), "JNLC": ("transfer", None),
    "DIV": ("dividend", None), "DIVCGL": ("dividend", None), "DIVCGS": ("dividend", None),
    "DIVTXEX": ("dividend", None), "CGD": ("dividend", None),
    "DIVNRA": ("tax_withheld", None), "DIVFT": ("tax_withheld", None), "DIVTW": ("tax_withheld", None),
    "INTNRA": ("tax_withheld", None), "INTTW": ("tax_withheld", None),
    "INT": ("interest_or_fee", None),
    "FEE": ("fee", None), "CFEE": ("fee", None), "DIVFEE": ("fee", None), "PTC": ("fee", None),
    "PTR": ("other", "Alpaca pass-through rebate; record it once its nature is known"),
    "DIVROC": ("other", "return of capital lowers the cost basis; record it against the holding by hand"),
    "CIL": ("other", "cash in lieu of a fractional share is a sale; record it against the holding by hand"),
    "JNLS": ("other", "stock journal: an in-kind transfer needs its cost basis; record it by hand"),
    "ACATS": ("other", "ACATS securities transfer needs its cost basis; record it by hand"),
    "FOPT": ("other", "free-of-payment transfer needs its cost basis; record it by hand"),
    "SSP": ("corporate_action", "Alpaca stock split: the ratio is not in the activity; check the holding after it"),
    "SSO": ("corporate_action", "spin-off is not posted automatically; check the holdings after it"),
    "MA": ("corporate_action", "merger/acquisition is not posted automatically; check the holding after it"),
    "REORG": ("corporate_action", "reorganisation is not posted automatically; check the holding after it"),
    "NC": ("corporate_action", "name change: check the symbol of the holding"),
    "SC": ("corporate_action", "symbol change: check the symbol of the holding"),
    "OPASN": ("corporate_action", "option assignment is not posted automatically"),
    "OPEXP": ("corporate_action", "option expiration is not posted automatically"),
    "OPXRC": ("corporate_action", "option exercise is not posted automatically"),
}
_LABEL = {
    "CSD": "Cash deposit", "CSW": "Cash withdrawal", "CSR": "Cash receipt", "JNLC": "Cash journal",
    "ACATC": "ACATS cash transfer", "DIV": "Dividend", "DIVCGL": "Capital gain distribution (long term)",
    "DIVCGS": "Capital gain distribution (short term)", "DIVTXEX": "Tax-exempt dividend", "CGD": "Capital gain distribution",
    "DIVNRA": "US withholding on dividend (NRA)", "DIVFT": "Foreign tax withheld on dividend",
    "DIVTW": "Backup (TEFRA) withholding on dividend", "INTNRA": "US withholding on interest (NRA)",
    "INTTW": "Backup (TEFRA) withholding on interest", "INT": "Interest", "FEE": "Fee", "CFEE": "Crypto fee",
    "DIVFEE": "Dividend fee", "PTC": "Pass-through charge", "PTR": "Pass-through rebate",
}
_SIGN = {"dividend": 1, "interest": 1, "tax_withheld": -1, "fee": -1, "deposit": 1, "withdrawal": -1}
_SIGN_REASON = {
    "dividend": "negative dividend (a reversal or correction); check it against the original dividend",
    "interest": "negative interest (a reversal or correction)",
    "tax_withheld": "withholding refund or adjustment; record it once the original withholding is identified",
    "fee": "fee credit or rebate",
    "deposit": "negative cash deposit (a reversal); check it against the original deposit",
    "withdrawal": "positive cash withdrawal (a reversal); check it against the original withdrawal",
}


def _tx(account_id: str, identifier: str, kind: str, when: str, amount: Decimal | None, description: str,
        **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": identifier, "dedupe_hash": identifier, "account_id": account_id, "date": when,
        "settlement_date": None, "description": description, "amount": out(amount) if amount is not None else None,
        "currency": "USD", "type": kind, "symbol": extra.pop("symbol", None), "quantity": extra.pop("quantity", None),
        "price": extra.pop("price", None), "fees": extra.pop("fees", None), "balance": None, "page": None,
        "installment": None,
    }
    row.update({k: v for k, v in extra.items() if v is not None})
    return row


def _activities(snapshot: Mapping[str, Any], account_id: str, facts: Mapping[str, dict[str, Any]],
                warnings: list[str]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    since = snapshot["since"]
    for activity in snapshot["activities"]:
        kind_code = str(activity.get("activity_type") or "").upper()
        identifier = _external_id(activity)
        symbol = str(activity.get("symbol") or "").upper() or None
        if identifier is None:
            content = "|".join(str(activity.get(k)) for k in sorted(activity))
            identifier = "ALPACA-H" + hashlib.sha256(content.encode()).hexdigest()[:24]
            warnings.append("An Alpaca activity had no standard id; it was identified by its content, so a corrected "
                            "re-sync may not match it.")
        if kind_code == "FILL":
            when = _day(activity.get("transaction_time"))
            quantity, price = _dec(activity.get("qty")), _dec(activity.get("price"))
            side = str(activity.get("side") or "").lower()
            if not when or quantity is None or price is None or side not in {"buy", "sell", "sell_short"} or not symbol:
                warnings.append(f"An Alpaca fill in {symbol or 'an unknown symbol'} lacks a date, side, quantity or "
                                "price; skipped.")
                continue
            if when < since:
                continue
            info = facts.get(symbol) or {}
            gross = abs(quantity) * price
            kind = "buy" if side == "buy" else "sell"
            reason = None
            if side == "sell_short":
                reason = "short sale: the ledger records long holdings only"
            elif info.get("asset_class") == "derivative":
                reason = "option fill: the ledger has no contract multiplier for derivatives"
            partial = " (partial fill)" if str(activity.get("type") or "").lower() == "partial_fill" else ""
            result.append(_tx(account_id, identifier, kind, when, -gross if kind == "buy" else gross,
                              f"{kind.upper()} {out(abs(quantity))} {symbol} @ {out(price)}{partial}",
                              symbol=symbol, quantity=out(abs(quantity)), price=out(price), instrument=info or None,
                              order_ref=str(activity.get("order_id") or "")[:8] or None, not_posted_reason=reason))
            continue
        when = _day(activity.get("date"))
        amount = _dec(activity.get("net_amount"))
        if not when or amount is None:
            warnings.append(f"An Alpaca {kind_code or 'unknown'} activity has no date or amount; skipped.")
            continue
        if when < since:
            continue
        kind, reason = _TYPES.get(kind_code, ("other", f"Alpaca activity type {kind_code or '?'} is not mapped to a "
                                                        "ledger kind"))
        if kind == "deposit_or_withdrawal":
            kind = "deposit" if amount > 0 else "withdrawal"
        elif kind == "interest_or_fee":
            kind = "interest" if amount > 0 else "fee"  # negative INT is margin interest charged
        description = " ".join(str(activity.get("description") or "").split()) or _LABEL.get(kind_code, kind_code)
        if symbol and symbol not in description:
            description = f"{description} {symbol}"
        per_share = _dec(activity.get("per_share_amount"))
        if per_share is not None and activity.get("qty") not in (None, ""):
            description += f" ({activity['qty']} x {out(per_share)})"
        extra: dict[str, Any] = {"symbol": symbol, "activity_type": kind_code}
        if symbol and facts.get(symbol):
            extra["instrument"] = facts[symbol]
        if reason is None and kind in _SIGN and amount * _SIGN[kind] <= 0:
            reason = _SIGN_REASON[kind]
        elif reason is None and kind == "dividend" and not symbol:
            reason = "dividend without a symbol"
        if reason is None and kind == "transfer" and amount == 0:
            reason = "zero cash journal"
        if kind == "corporate_action":
            warnings.append(f"{symbol or 'A holding'}: {reason} (on {when}).")
            amount = None
        result.append(_tx(account_id, identifier, kind, when, amount, description, not_posted_reason=reason, **extra))
    result.sort(key=lambda t: (t["date"], t["id"]))
    return result


def _history_equity(history: Mapping[str, Any], since: str) -> tuple[str, Decimal] | None:
    stamps = history.get("timestamp") if isinstance(history, Mapping) else None
    values = history.get("equity") if isinstance(history, Mapping) else None
    if not isinstance(stamps, list) or not isinstance(values, list):
        return None
    for stamp, value in zip(stamps, values):
        amount = _dec(value)
        if amount is None or not isinstance(stamp, (int, float)):
            continue
        day = datetime.fromtimestamp(stamp, timezone.utc).astimezone(_EASTERN).date().isoformat()
        if day >= since and amount > 0:
            return day, amount
    return None


def proposal_from_alpaca(snapshot: Mapping[str, Any], *, paper: bool = False, owner_id: str = "self",
                         sic_listed: Mapping[str, bool] | Iterable[str] | None = None,
                         retrieved_at: str | None = None) -> dict[str, Any]:
    """Map a fetched Alpaca snapshot to an ingest proposal (nothing is saved)."""
    account = snapshot["account"]
    if not isinstance(account, Mapping) or not isinstance(snapshot["positions"], list):
        raise ConnectorError("Alpaca returned the account or positions in an unexpected shape.")
    as_of, since = snapshot["as_of"], snapshot["since"]
    currency = str(account.get("currency") or "USD").upper()
    if isinstance(sic_listed, Mapping):
        sic = {str(k).upper(): bool(v) for k, v in sic_listed.items()}
    else:
        sic = {str(k).upper(): True for k in (sic_listed or [])}
    warnings: list[str] = []
    reasons: list[str] = []
    assumptions = [
        "Positions, cash and equity are Alpaca's own figures at the time of the sync; activities carry Alpaca ids so "
        "a later sync adds only new lines.",
        "Alpaca reports only an average entry price per holding, not tax lots: cost basis is average cost and lots "
        "are marked unavailable. Add lots from trade confirmations if exact lot relief matters.",
        "Alpaca has no ISIN; issuer domicile is set only for CINS CUSIPs (non-US issuers). Mexican SIC listing is "
        "unknown per holding until the person or the SIC list confirms it.",
    ]
    number = str(account.get("account_number") or "")
    status = str(account.get("status") or "").upper()
    if status and status != "ACTIVE":
        reasons.append(f"The Alpaca account status is {status}.")
    positions_in = snapshot["positions"]
    assets = snapshot.get("assets") or {}
    facts: dict[str, dict[str, Any]] = {}
    rows = []
    for position in positions_in:
        symbol = str(position.get("symbol") or "").upper()
        if not symbol:
            continue
        info = _facts(symbol, position, assets.get(symbol))
        facts[symbol] = info
        quantity = _dec(position.get("qty"))
        side = str(position.get("side") or "long").lower()
        if side == "short" and quantity is not None and quantity > 0:
            quantity = -quantity
        row = {"symbol": symbol, "description": info.get("name"), "quantity": out(quantity) if quantity is not None else None,
               "market_value": position.get("market_value"), "cost_basis": position.get("cost_basis") or None,
               "currency": currency, "asset_type": info.get("asset_type"), "currency_explicit": True}
        if info.get("asset_class") != "derivative":  # option prices are per share, positions per contract
            row["price"] = position.get("current_price")
        rows.append(row)
    for symbol, asset in assets.items():
        if symbol not in facts:
            facts[symbol] = _facts(symbol, None, asset)
    cash = _dec(account.get("cash"))
    equity = _dec(account.get("equity"))
    later = _dec((snapshot.get("account_after") or {}).get("equity"))
    tolerance = Decimal("0.01") * max(1, len(rows) + 1)
    if equity is not None and later is not None and later != equity:
        drift = abs(later - equity)
        tolerance += drift
        warnings.append(f"Alpaca equity moved by {out(drift)} {currency} while holdings were read (the market is "
                        "open); reconciliation allows that difference. Sync after the close for an exact match.")
    orders = snapshot.get("orders") or []
    if orders:
        warnings.append(f"{len(orders)} open Alpaca order(s) are pending; they are not holdings until they fill.")
    statement = {
        "institution": INSTITUTION, "institution_key": "alpaca", "as_of": as_of, "currency": currency, "market": "us",
        "period_start": since,
        "accounts": [{
            "label": "Alpaca paper account" if paper else "Alpaca brokerage account",
            "number_last4": last4(number), "type": "brokerage", "currency": currency, "positions": rows,
            "cash": [{"amount": out(cash), "currency": currency, "label": "Alpaca cash"}] if cash else [],
            "reported_total": ({"amount": out(equity), "currency": currency, "label": "Alpaca equity"}
                               if equity is not None else None),
        }],
    }
    if equity is None:
        warnings.append("Alpaca returned no equity figure; holdings cannot be reconciled.")
    ref = f"alpaca:{'paper' if paper else 'live'}"
    provenance = {
        "kind": "connector", "provider": NAME, "ref": ref, "environment": "paper" if paper else "live",
        "period_start": since, "period_end": as_of, "accounts": [mask_account(number)] if number else [],
        "retrieved_at": retrieved_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if paper:
        assumptions.append("This is an Alpaca paper (simulated) account; its money is not real.")
    probe = build_proposal(statement, kind="connector", provenance=provenance, owner_id=owner_id, tolerance=tolerance)
    account_id = probe["result"]["household"]["accounts"][0]["id"]
    transactions = _activities(snapshot, account_id, facts, warnings)
    proposal = build_proposal(statement, kind="connector", provenance=provenance, owner_id=owner_id,
                              warnings=warnings, assumptions=assumptions, review_reasons=reasons,
                              transactions=transactions, tolerance=tolerance)
    result = proposal["result"]
    for position in result["household"]["positions"]:
        if str(position["instrument_id"]).startswith("CASH:"):
            continue
        info = facts.get(str(position.get("symbol") or "").upper(), {})
        for field in ("venue", "listing_exchange", "underlying_symbol", "issuer_domicile", "cusip"):
            if info.get(field):
                position[field] = info[field]
        if info.get("asset_class"):
            position["asset_class"] = info["asset_class"]
        position["cost_basis_method"] = "average_entry"
        position["lots"] = "unavailable"
        if position.get("asset_class") in {"equity", "fund"} and position.get("issuer_domicile") != "MX":
            position["sic_listed"] = sic.get(position["symbol"].upper(), "unknown")
    opening = _history_equity(snapshot.get("portfolio_history") or {}, since)
    if equity is not None:
        result["balance_assertions"].append({
            "account_id": account_id, "period_start": opening[0] if opening else since, "period_end": as_of,
            "opening": out(opening[1]) if opening else None, "closing": out(equity), "currency": currency,
            "balance_kind": "nav", "page": None, "source": "Alpaca portfolio history and account equity",
        })
    result["connector"] = {"name": NAME, "environment": "paper" if paper else "live",
                           "period": {"start": since, "end": as_of}, "activities": len(snapshot["activities"]),
                           "open_orders": len(orders)}
    result["proposal_id"] = proposal_digest(result)
    result["summary"] = _summary(result)
    return proposal


# -- ledger posting ---------------------------------------------------------

def ledger_batch(proposal: Mapping[str, Any], *, batch_id: str, ledger: Mapping[str, Any]) -> dict[str, Any]:
    """Posting batch for a confirmed Alpaca proposal.

    Built on the statement mapper (opening balances derived from the end state
    minus the period's lines, balance checks for accounts already in the
    ledger), plus: instruments keep venue/listing/CUSIP facts, and a holding
    sold out during the period gets an opening quantity so its sale has
    something to relieve.  Unposted Alpaca rows keep their reason.
    """
    from ..ingest_posting import _s, proposal_to_batch

    result = proposal["result"]
    postable = [tx for tx in result.get("transactions") or [] if not tx.get("not_posted_reason")]
    mapping = proposal_to_batch({**proposal, "result": {**result, "transactions": postable}}, batch_id=batch_id,
                                ledger=ledger)
    batch = mapping["batch"]
    mapping["not_posted"].extend(
        {"date": tx["date"], "description": tx["description"], "amount": tx["amount"], "account_id": tx["account_id"],
         "reason": tx["not_posted_reason"]} for tx in result.get("transactions") or [] if tx.get("not_posted_reason"))
    if batch is None:
        return mapping
    by_symbol: dict[str, dict[str, Any]] = {}
    for tx in result.get("transactions") or []:
        if tx.get("instrument") and tx.get("symbol"):
            by_symbol.setdefault(tx["symbol"], tx["instrument"])
    for position in result["household"]["positions"]:
        by_symbol.setdefault(str(position.get("symbol") or ""), position)
    for instrument in batch["instruments"]:
        info = by_symbol.get(instrument["symbol"], {})
        listing = _LEDGER_LISTING.get(str(info.get("listing_exchange") or ""))
        if listing:
            instrument["listing"] = listing
        for field in ("cusip", "issuer_domicile", "underlying_symbol"):
            if info.get(field) and field not in instrument:
                instrument[field] = info[field]
        if info.get("venue") in {"us", "other"} and "venue" not in instrument:
            instrument["venue"] = info["venue"]
        if info.get("name") and "name" not in instrument:
            instrument["name"] = info["name"]
        if info.get("asset_class") and "asset_class" not in instrument:
            instrument["asset_class"] = info["asset_class"]
    active = {e["account_id"] for e in ledger.get("entries", [])}
    held = {(p["account_id"], p["instrument_id"]) for p in result["household"]["positions"]}
    periods = {a["account_id"]: a for a in result.get("balance_assertions") or []}
    extra_lines, extra_checks = [], []
    for account in batch["accounts"]:
        account_id = account["id"]
        if account_id in active:
            continue
        net: dict[str, Decimal] = {}
        for line in batch["transactions"]:
            if line["account_id"] == account_id and line.get("instrument_id") and line["kind"] in {"buy", "sell"}:
                sign = 1 if line["kind"] == "buy" else -1
                net[line["instrument_id"]] = net.get(line["instrument_id"], Decimal(0)) + sign * Decimal(line["quantity"])
        start = (periods.get(account_id) or {}).get("period_start") or result["as_of"]
        for instrument, change in sorted(net.items()):
            if (account_id, instrument) in held or change >= 0:
                continue
            extra_lines.append({"kind": "opening_balance", "account_id": account_id, "date": start,
                                "instrument_id": instrument, "currency": account["currency"], "quantity": _s(-change),
                                "description": f"Opening position {instrument} (sold during the synced period)"})
            extra_checks.append({"account_id": account_id, "date": result["as_of"], "instrument_id": instrument,
                                 "quantity": "0"})
            mapping["notes"].append(f"{instrument} in {account_id}: sold out during the period; its opening cost basis "
                                    "is unknown because Alpaca reports no lots for closed holdings.")
    if extra_lines:
        batch["transactions"] = extra_lines + batch["transactions"]
        batch["balance_assertions"] = batch["balance_assertions"] + extra_checks
    return mapping


# -- connector --------------------------------------------------------------

SETUP = (
    "In the Alpaca dashboard (app.alpaca.markets) open the live or paper account and generate an API key; copy the "
    "key id and the secret (shown once). Read access is all Wealth uses.",
    f"Store them: security add-generic-password -U -s {KEYCHAIN_SERVICE} -a key_id -w  and  "
    f"security add-generic-password -U -s {KEYCHAIN_SERVICE} -a secret -w  (macOS prompts for each), or export "
    f"{KEY_ENV} and {SECRET_ENV} for one session.",
    f"For a paper account export {PAPER_ENV}=1 or pass paper=true; paper keys only work on the paper host.",
    "Sync with wealth ingest action=connector inputs={\"name\": \"alpaca\"} (optional since=YYYY-MM-DD, default the "
    "last 365 days).",
)


class AlpacaConnector:
    """Pull an Alpaca account on demand and return an ingest proposal."""

    provider = NAME
    countries = ("US", "MX", "*")

    def __init__(self, *, paper: bool | None = None, since: Any = None, keys: AlpacaKeys | None = None,
                 transport: _rest.Transport | None = None, sleep: Callable[[float], None] | None = None,
                 clock: Callable[[], float] | None = None, today: date | None = None,
                 timeout: float = TIMEOUT_SECONDS, sic_listed: Mapping[str, bool] | Iterable[str] | None = None,
                 key_loader: Callable[[], AlpacaKeys | None] = load_keys):
        if paper is not None and not isinstance(paper, bool):
            raise ValueError("paper must be true or false")
        self.paper = paper_default() if paper is None else paper
        self._today = today
        self.since = _since(since, self.today) if since not in (None, "") else None
        self._keys, self._key_loader = keys, key_loader
        self._transport, self._sleep, self._clock, self._timeout = transport, sleep, clock, timeout
        self._sic_listed = sic_listed

    @property
    def today(self) -> date:
        return self._today or datetime.now(_EASTERN).date()

    @property
    def ref(self) -> str:
        return f"alpaca:{'paper' if self.paper else 'live'}"

    def __repr__(self) -> str:
        return f"AlpacaConnector(paper={self.paper!r})"

    def proposal(self, *, owner_id: str = "self", previous: dict[str, Any] | None = None) -> dict[str, Any]:
        keys = self._keys or self._key_loader()
        if keys is None:
            return envelope("needs_input", {"setup": list(SETUP)}, missing=[{
                "key": "alpaca.keys", "reason": "missing",
                "detail": f"No Alpaca key id and secret in the keychain (service {KEYCHAIN_SERVICE}, accounts key_id and "
                          f"secret) or {KEY_ENV}/{SECRET_ENV}. The person stores them; never paste them into the chat."}])
        today = self.today
        since = self.since or _since(None, today)
        try:
            api = client(keys, paper=self.paper, transport=self._transport or default_transport,
                         sleep=self._sleep, clock=self._clock, timeout=self._timeout)
            snapshot = fetch_snapshot(api, since=since, today=today)
            proposal = proposal_from_alpaca(snapshot, paper=self.paper, owner_id=owner_id, sic_listed=self._sic_listed)
        except ConnectorError as exc:
            message = _rest.scrub(str(exc), *keys.values())
            return envelope("rejected", {"error": {"code": exc.status, "retryable": exc.retryable, "message": message}},
                            warnings=[message], sources=[self.ref])
        if previous is not None:
            proposal["result"]["changes"] = diff_proposals(previous, proposal)
        return proposal


def status(environ: Mapping[str, str] | None = None, runner: Callable[..., Any] | None = None) -> dict[str, Any]:
    keys = load_keys(environ, runner)
    return {"name": NAME, "institution": INSTITUTION, "read_only": True, "token": keys.source if keys else "missing",
            "ready": keys is not None, "environment": "paper" if paper_default(environ) else "live",
            "needs": [], "optional": ["paper", "since", "sic_listed"], "setup": list(SETUP)}


__all__ = [
    "ALLOWED_PATHS", "AlpacaConnector", "AlpacaKeys", "fetch_snapshot", "ledger_batch", "load_keys",
    "proposal_from_alpaca", "status",
]
