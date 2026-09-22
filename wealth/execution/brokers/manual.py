"""Brokers without an order API: Wealth prepares the ticket, the person places it, a statement confirms it.

GBM, Vest, Schwab without OAuth, Fidelity and any institution Wealth does not
trade through get a :class:`ManualBroker`.  It prices and checks a ticket from
Wealth's own data — reference prices from the market-data cache (last close,
never a live quote), cash and holdings from the ledger — and never sends
anything anywhere.  The card becomes a "place this yourself" card: exact
symbol and listing (for a Mexican broker, the SIC for foreign shares — "Compra
en el SIC" — or the BMV for Mexican ones), side, quantity, order type and limit,
the account, estimated cost, fees and the FX rate.  The person places it at
their broker and taps "Ya la puse / I placed it", which records the intended
trade as ``awaiting``; :func:`wealth.execution.tickets.refresh` then matches it
against ledger entries from the next statement or connector sync.

Fees are estimates from :data:`FEE_SCHEDULES`, labelled as such on the card;
the person's contract governs.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from .base import BrokerError, institution_profile

NAME = "manual"
_MEXICO = ZoneInfo("America/Mexico_City")
_EASTERN = ZoneInfo("America/New_York")
REFERENCE_MAX_AGE_DAYS = 5
# Estimated commission rate and the tax charged on it, per broker label.  Mexican brokerage commissions carry 16% IVA.
# These are typical published retail rates, shown as estimates; the person's contract governs.
FEE_SCHEDULES: dict[str, dict[str, Any]] = {
    "GBM": {"rate": Decimal("0.0025"), "vat": Decimal("0.16"), "basis": "0.25% + IVA (estimate)"},
    "_MX": {"rate": Decimal("0.0025"), "vat": Decimal("0.16"), "basis": "about 0.25% + IVA (typical; check your contract)"},
    "_US": {"rate": Decimal("0"), "vat": Decimal("0"), "basis": "no commission on US stocks and ETFs online (typical)"},
}
# Mexican issuers commonly bought on the BMV; a bare ticker that is not one of these, at a Mexican broker, is assumed
# to be a foreign listing bought through the SIC (the card says it was assumed).
BMV_ISSUERS = frozenset({
    "WALMEX", "AMX", "AMXB", "AMXL", "GFNORTEO", "GFNORTE", "FEMSAUBD", "FEMSA", "CEMEXCPO", "CEMEX", "BIMBOA",
    "BIMBO", "GMEXICOB", "GMEXICO", "GAPB", "ASURB", "OMAB", "KOFUBL", "AC", "ALSEA", "GRUMAB", "KIMBERA", "ORBIA",
    "PINFRA", "TLEVISACPO", "LIVEPOLC-1", "LIVEPOLC", "PE&OLES", "GCARSOA1", "BOLSAA", "Q", "VESTA", "MEGACPO",
    "NAFTRAC", "FUNO11", "FIBRAMQ12", "FIBRAPL14", "DANHOS13", "LABB", "GENTERA", "BBAJIOO", "RA", "ELEKTRA",
    "CHDRAUIB", "SORIANAB", "GCC", "ALFAA", "ALPEKA", "VOLARA", "CUERVO", "HERDEZ", "LACOMERUBC", "IVVPESO",
})
LISTINGS = {"BMV": "BMV", "BIVA": "BIVA", "SIC": "SIC", "US": "US", "NYSE": "US", "NASDAQ": "US", "ARCA": "US",
            "AMEX": "US"}
_MX_SYMBOL = re.compile(r"^[A-Z][A-Z0-9&\-]{0,13}$")

# (symbol, listing, currency) -> {price, date, currency, source} | None
Quote = Callable[[str, str, str], "dict[str, Any] | None"]
# (base, quote) -> rate | None
Fx = Callable[[str, str], "Decimal | None"]


def default_quote(db_path: Any) -> Quote:
    """Reference closes from Wealth's market-data cache (Yahoo), in the listing's currency."""
    def quote(symbol: str, listing: str, currency: str) -> dict[str, Any] | None:
        from ...prices import PriceProvider

        venue = {"SIC": "sic", "BMV": "bmv", "BIVA": "biva"}.get(listing, "us")
        try:
            found = PriceProvider(db_path).latest([{"id": symbol, "symbol": symbol, "venue": venue,
                                                    "currency": currency}], budget=5)
        except Exception:  # noqa: BLE001 - a reference price is optional; the card says it is missing
            return None
        row = (found.get("prices") or {}).get(symbol)
        if not row or row.get("price") in (None, ""):
            return None
        return {"price": str(row["price"]), "date": row.get("date"), "currency": row.get("currency") or currency,
                "source": row.get("source")}
    return quote


def default_fx(db_path: Any) -> Fx:
    def fx(base: str, quote: str) -> Decimal | None:
        from ...prices import PriceProvider

        today = date.today()
        try:
            found = PriceProvider(db_path).fx(base, quote, today - timedelta(days=10), today, budget=5)
        except Exception:  # noqa: BLE001
            return None
        rates = found.get("rates") or []
        try:
            return Decimal(str(rates[-1]["rate"])) if rates else None
        except (InvalidOperation, KeyError):
            return None
    return fx


def _dec(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() else None


def _s(value: Decimal | None) -> str | None:
    if value is None:
        return None
    text = format(value.normalize(), "f")
    return "0" if text in ("-0", "") else text


def clean_symbol(raw: Any) -> str:
    """``WALMEX.MX``, ``VOO *`` and ``gfnorte o`` become ``WALMEX``, ``VOO`` and ``GFNORTEO``."""
    text = str(raw or "").strip().upper()
    text = re.sub(r"\.MX$", "", text)
    text = text.replace("*", " ").strip()
    return re.sub(r"\s+", "", text)


def symbol_key(raw: Any) -> str:
    """A symbol for matching statements: listing marks and separators removed (``BRK.B`` ~ ``BRK B`` ~ ``BRKB``)."""
    return re.sub(r"[^A-Z0-9&]", "", clean_symbol(raw))


class ManualBroker:
    """:class:`~wealth.execution.brokers.base.OrderBroker` for a broker Wealth cannot send orders to."""

    name = NAME
    mode = "manual"
    submits = False
    posts_fills = False

    def __init__(self, *, institution: Any, account: Mapping[str, Any] | None = None,
                 ledger: Mapping[str, Any] | None = None, quote: Quote | None = None, fx: Fx | None = None,
                 clock: Callable[[], datetime] | None = None, db_path: Any = None):
        profile = institution_profile(institution, (account or {}).get("country"))
        self.label = profile["label"]
        self.market = profile["market"]
        self.currency = str((account or {}).get("currency") or profile["currency"]).upper()
        self.account = dict(account or {})
        self.ledger = ledger or {}
        self._quote = quote or default_quote(db_path)
        self._fx = fx or default_fx(db_path)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.audit = None

    def __repr__(self) -> str:
        return f"ManualBroker(label={self.label!r}, market={self.market!r})"

    # -- listing, fees and FX (manual only)
    def listing(self, symbol: str, hint: Any = None) -> tuple[str, bool]:
        """``(listing, assumed)``: SIC, BMV or BIVA at a Mexican broker, US at a US one."""
        wanted = LISTINGS.get(str(hint or "").strip().upper())
        if self.market == "US":
            return "US", False
        if wanted in ("BMV", "BIVA", "SIC"):
            return wanted, False
        if wanted == "US":
            return "SIC", False  # a US-listed security bought from Mexico goes through the SIC
        known = self._ledger_venue(symbol)
        if known:
            return known, False
        return ("BMV" if symbol in BMV_ISSUERS else "SIC"), True

    def _ledger_venue(self, symbol: str) -> str | None:
        for item in self.ledger.get("instruments") or []:
            if symbol_key(item.get("symbol") or item.get("id")) == symbol_key(symbol):
                venue = str(item.get("venue") or item.get("listing") or "").upper()
                if venue in ("SIC", "BMV", "BIVA"):
                    return venue
        return None

    def fee(self, amount: Decimal | None) -> tuple[Decimal | None, str]:
        schedule = FEE_SCHEDULES.get(self.label) or FEE_SCHEDULES["_MX" if self.market == "MX" else "_US"]
        if amount is None:
            return None, schedule["basis"]
        fee = (amount * schedule["rate"] * (1 + schedule["vat"])).quantize(Decimal("0.01"))
        return fee, schedule["basis"]

    def fx_to_usd(self) -> Decimal | None:
        """USD per unit of the ticket currency (for the card's FX line); ``None`` when the ticket is in USD."""
        if self.currency == "USD":
            return None
        rate = self._fx("USD", self.currency)
        return rate if rate and rate > 0 else None

    # -- OrderBroker reads
    def account_state(self) -> dict[str, Any]:
        cash = None
        account_id = self.account.get("id")
        if account_id and self.ledger.get("entries"):
            try:
                from ...prices import _holdings

                balances, _ = _holdings(self.ledger, self._clock().date().isoformat())
                cash = _s(balances.get((account_id, self.currency), Decimal(0)))
            except Exception:  # noqa: BLE001 - cash is then unknown and the card says so
                cash = None
        return {"active": True, "buying_power": cash, "currency": self.currency, "number": None,
                "ledger_account_id": account_id}

    def market_clock(self) -> dict[str, Any]:
        now = self._clock()
        if self.market == "MX":
            local = now.astimezone(_MEXICO)
            opens, closes = time(8, 30), time(15, 0)
        else:
            local = now.astimezone(_EASTERN)
            opens, closes = time(9, 30), time(16, 0)
        return {"is_open": local.weekday() < 5 and opens <= local.time() < closes, "next_open": None}

    def instrument(self, symbol: str) -> dict[str, Any] | None:
        listing, _ = self.listing(symbol)
        return {"symbol": symbol, "tradable": True, "fractionable": False, "exchange": listing,
                "currency": self.currency}

    def latest_trade(self, symbol: str, listing: str | None = None) -> dict[str, Any] | None:
        """A reference close (``reference: True``), never a live quote."""
        listing = listing or self.listing(symbol)[0]
        found = self._quote(symbol, listing, self.currency)
        if not found:
            return None
        price = _dec(found.get("price"))
        if price is None or price <= 0:
            return None
        if str(found.get("currency") or self.currency).upper() != self.currency:
            return None
        return {"price": _s(price), "at": found.get("date"), "reference": True, "source": found.get("source")}

    def positions(self) -> list[dict[str, Any]]:
        account_id = self.account.get("id")
        if not account_id or not self.ledger.get("entries"):
            raise BrokerError("Wealth has no holdings on record for this account.")
        from ...prices import _holdings

        _, quantities = _holdings(self.ledger, self._clock().date().isoformat())
        symbols = {i.get("id"): i.get("symbol") or i.get("id") for i in self.ledger.get("instruments") or []}
        return [{"symbol": clean_symbol(symbols.get(instrument, instrument)), "qty": _s(qty), "market_value": None}
                for (account, instrument), qty in quantities.items() if account == account_id]

    def open_orders(self) -> list[dict[str, Any]]:
        return []

    # -- nothing is ever sent
    def submit(self, order: Mapping[str, Any]) -> dict[str, Any]:
        raise BrokerError(f"Wealth cannot send orders to {self.label}; the person places them.")

    def order(self, order_id: str) -> dict[str, Any]:
        raise BrokerError(f"Wealth cannot read orders at {self.label}.")

    def order_by_client_id(self, client_order_id: str) -> dict[str, Any] | None:
        return None

    def cancel(self, order_id: str) -> None:
        raise BrokerError(f"Wealth cannot cancel orders at {self.label}; cancel it there.")

    def fills(self, after: str | None = None) -> list[dict[str, Any]]:
        return []

    def ledger_account(self) -> dict[str, Any]:
        return dict(self.account)


__all__ = ["BMV_ISSUERS", "FEE_SCHEDULES", "ManualBroker", "clean_symbol", "default_fx", "default_quote",
           "symbol_key"]
