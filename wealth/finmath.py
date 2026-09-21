"""Shared financial maths: one implementation of each rule used across Wealth.

* :class:`FxTable` - dated FX lookups (newest of the direct pair and its
  inverse within a maximum age; unknown is ``None``, never 1 or 0).
* :func:`xirr` / :func:`xirr_solve` - money-weighted annual return with a
  robust bracket, bisection, and multiple-root detection.
* :func:`annualize` - compound a period return to a year (never under a year).
* Month arithmetic: :func:`add_months`, :func:`months_between`,
  :func:`month_ends`, :func:`month_keys`.
* :data:`PER_MONTH` / :func:`per_month` - frequency-to-monthly factors.
* :func:`holding_character` - US long/short term (the tax engine's rule).
* :func:`essential_spending` / :data:`ESSENTIAL_RULE` - unclassified spending
  counts as essential.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

# ------------------------------------------------------------------ dates


def as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and len(value) >= 10:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def add_months(day: date, months: int, day_of_month: int | None = None) -> date:
    """``day`` moved by ``months`` calendar months, clamped to the month's last day.

    ``day_of_month`` pins the target day (clamped) instead of keeping ``day.day``.
    """
    year, month = divmod(day.year * 12 + day.month - 1 + months, 12)
    month += 1
    wanted = day.day if day_of_month is None else day_of_month
    return date(year, month, min(wanted, monthrange(year, month)[1]))


def months_between(start: date, end: date) -> int:
    """Whole calendar months from ``start``'s month to ``end``'s month (days ignored)."""
    return (end.year - start.year) * 12 + end.month - start.month


def month_end(day: date) -> date:
    return date(day.year, day.month, monthrange(day.year, day.month)[1])


def month_ends(start: str | date, end: str | date) -> list[str]:
    """Calendar month ends strictly after ``start`` and strictly before ``end`` (ISO dates)."""
    first, last = as_date(start), as_date(end)
    result, current = [], month_end(first)
    while current < last:
        if current > first:
            result.append(current.isoformat())
        current = month_end(add_months(current.replace(day=1), 1))
    return result


def month_keys(start: str | date, end: str | date) -> list[str]:
    """``YYYY-MM`` for every calendar month touched by ``start``..``end`` inclusive."""
    current, last = as_date(start).replace(day=1), as_date(end)
    keys = []
    while current <= last:
        keys.append(current.strftime("%Y-%m"))
        current = add_months(current, 1)
    return keys


# ------------------------------------------------------------------ frequencies

# Payments per year / 12, kept as a ratio so 300 a quarter is exactly 100 a month.
_PER_YEAR = {"weekly": 52, "biweekly": 26, "semimonthly": 24, "monthly": 12, "quarterly": 4, "semiannual": 2, "annual": 1}
PER_MONTH: dict[str, Decimal] = {name: Decimal(n) / Decimal(12) for name, n in _PER_YEAR.items()}


def per_month(amount: Decimal | None, frequency: str | None) -> Decimal | None:
    """Monthly equivalent of ``amount`` paid at ``frequency``; unknown frequency is ``None``."""
    times = _PER_YEAR.get(frequency) if isinstance(frequency, str) else None
    return None if amount is None or times is None else amount * times / 12


# ------------------------------------------------------------------ holding period


def holding_character(acquired: str | date | None, sold: str | date) -> str:
    """``long`` / ``short`` under the US one-year rule (tax engine), ``unknown`` without a date."""
    from .tax import _holding_character  # the tax engine owns the rule; imported lazily (no cycle)

    start, end = as_date(acquired), as_date(sold)
    if start is None or end is None:
        return "unknown"
    return "long" if _holding_character(start, end) == "long_term" else "short"


# ------------------------------------------------------------------ essential spending

ESSENTIAL_RULE = ("Spending whose essentiality is unknown (uncategorized, cash withdrawals, 'other') counts as "
                  "essential, so uncertainty lowers, never raises, the amount called free or investable.")


def essential_spending(essential: Decimal, unknown: Decimal) -> Decimal:
    """Essential spending under :data:`ESSENTIAL_RULE`."""
    return essential + unknown


# ------------------------------------------------------------------ annualisation


def annualize(total: Any, days: int, *, min_days: int = 365) -> Any:
    """Compound a period return over ``days`` to an annual rate.

    ``None`` when unknown, when the period is under ``min_days`` (short periods
    are not annualized), or when the loss is total.  A ``Decimal`` in gives a
    ``Decimal`` out; a float gives a float.
    """
    if total is None or days < min_days or days <= 0 or total <= -1:
        return None
    value = (1 + float(total)) ** (365.0 / days) - 1
    return Decimal(str(value)) if isinstance(total, Decimal) else value


# ------------------------------------------------------------------ XIRR

XIRR_LOW = -0.9999
XIRR_HIGH = 10.0
XIRR_CEILING = 1e6


@dataclass
class XirrResult:
    rate: float | None
    roots: list[float] = field(default_factory=list)
    warning: str | None = None


def _grid() -> list[float]:
    points = {-1 + 10 ** -k for k in range(1, 10)}          # toward -100 %
    points |= {x / 100 for x in range(-90, 101)}            # -90 % .. 100 % by 1 %
    points |= {1 + x / 10 for x in range(1, 91)}            # 100 % .. 1,000 % by 10 %
    points.add(XIRR_LOW)
    return sorted(points)


_GRID = _grid()


def xirr_solve(cashflows: Iterable[tuple[Any, Any]]) -> XirrResult:
    """Every annual rate (in (-100 %, 1e6 %)) at which the flows' NPV is zero.

    Investor view: contributions negative, withdrawals and the ending value
    positive.  Brackets sign changes on a fixed grid from -99.99 % to 1,000 %,
    expanding upward to 1e6 %, then bisects each bracket to machine precision.
    ``rate`` is ``None`` only when no sign change exists; with several roots
    the one nearest 0 is returned and ``warning`` lists them all.
    """
    flows = []
    for when, amount in cashflows:
        day = as_date(when)
        value = float(amount)
        if day is None or not math.isfinite(value):
            raise ValueError("cash flows need ISO dates and finite amounts")
        if value:
            flows.append((day, value))
    if not flows or all(a > 0 for _, a in flows) or all(a < 0 for _, a in flows):
        return XirrResult(None, [], None)
    origin = min(d for d, _ in flows)
    points = [((d - origin).days / 365.0, a) for d, a in flows]
    if all(t == 0 for t, _ in points):
        return XirrResult(None, [], None)  # all on one day: no time, no rate

    def npv(rate: float) -> float:
        total = 0.0
        for t, a in points:
            try:
                total += a / (1.0 + rate) ** t
            except (OverflowError, ZeroDivisionError):
                return math.copysign(math.inf, a)
        return total

    grid = list(_GRID)
    upper = XIRR_HIGH
    values = [npv(r) for r in grid]
    brackets: list[tuple[float, float, float]] = []
    exact: list[float] = []

    def scan(start: int) -> None:
        for i in range(start, len(grid) - 1):
            f0, f1 = values[i], values[i + 1]
            if f0 == 0:
                exact.append(grid[i])
            elif f1 != 0 and (f0 < 0) != (f1 < 0):
                brackets.append((grid[i], grid[i + 1], f0))
        if values[-1] == 0:
            exact.append(grid[-1])

    scan(0)
    while upper < XIRR_CEILING:  # expand to the ceiling: a second root can sit far above the first
        upper *= 2
        grid.append(upper)
        values.append(npv(upper))
        scan(len(grid) - 2)
    roots = set(exact)
    for low, high, f_low in brackets:
        for _ in range(300):
            mid = (low + high) / 2
            if mid in (low, high):
                break
            f_mid = npv(mid)
            if f_mid == 0:
                low = high = mid
                break
            if (f_mid < 0) == (f_low < 0):
                low, f_low = mid, f_mid
            else:
                high = mid
        mid = (low + high) / 2
        roots.add(min((low, mid, high), key=lambda r: (abs(npv(r)), abs(r - mid))))
    ordered = sorted(roots)
    if not ordered:
        return XirrResult(None, [], None)
    rate = min(ordered, key=lambda r: (abs(r), r))
    warning = None
    if len(ordered) > 1:
        warning = ("Money-weighted return is not unique: these flows have rates of "
                   + ", ".join(f"{r:.2%}" for r in ordered) + f"; {rate:.2%} (nearest zero) is shown.")
    return XirrResult(rate, ordered, warning)


def xirr(cashflows: Iterable[tuple[Any, Any]]) -> float | None:
    """Annual money-weighted return (see :func:`xirr_solve`); ``None`` without a sign change."""
    return xirr_solve(cashflows).rate


# ------------------------------------------------------------------ FX

DEFAULT_FX_MAX_AGE_DAYS = 7


@dataclass(frozen=True)
class FxQuote:
    rate: Decimal           # base -> quote as asked
    date: str               # date of the stored rate
    pair: tuple[str, str]   # the stored pair (may be the inverse of what was asked)
    source: str | None


class FxTable:
    """Dated FX lookups: the newest stored rate on or before a date within ``max_age_days``.

    The direct pair and its inverse are both candidates; the newer one wins
    (the direct pair on a tie).  No cross rate is inferred.  A missing or
    too-old rate returns ``None`` (unknown), never 1 and never 0.
    """

    def __init__(self, rows: Iterable[Mapping[str, Any]] = (), max_age_days: int | None = DEFAULT_FX_MAX_AGE_DAYS):
        self.max_age_days = max_age_days
        self._raw: dict[tuple[str, str], dict[str, tuple[Decimal, str | None]]] = {}
        self._series: dict[tuple[str, str], tuple[list[str], list[tuple[Decimal, str | None]]]] | None = None
        for row in rows:
            self.add(row.get("base", row.get("from")), row.get("quote", row.get("to")), row.get("rate"),
                     row.get("date", row.get("as_of")), row.get("source"))

    def add(self, base: Any, quote: Any, rate: Any, on: Any, source: str | None = None) -> bool:
        """Store one rate; invalid rows (no currencies, no date, non-positive rate) are ignored."""
        day = as_date(on)
        if not (isinstance(base, str) and isinstance(quote, str)) or day is None or rate is None \
                or isinstance(rate, bool):
            return False
        try:
            value = Decimal(str(rate))
        except ArithmeticError:
            return False
        if not value.is_finite() or value <= 0:
            return False
        self._raw.setdefault((base, quote), {})[day.isoformat()] = (value, source)
        self._series = None
        return True

    def _index(self) -> dict[tuple[str, str], tuple[list[str], list[tuple[Decimal, str | None]]]]:
        if self._series is None:
            self._series = {}
            for pair, values in self._raw.items():
                days = sorted(values)
                self._series[pair] = (days, [values[d] for d in days])
        return self._series

    def _lookup(self, pair: tuple[str, str], on: str) -> tuple[Decimal, str, str | None] | None:
        series = self._index().get(pair)
        if not series:
            return None
        days, values = series
        index = bisect_right(days, on) - 1
        if index < 0:
            return None
        found = days[index]
        if self.max_age_days is not None and (date.fromisoformat(on) - date.fromisoformat(found)).days > self.max_age_days:
            return None
        return values[index][0], found, values[index][1]

    def quote(self, base: str, quote: str, on: Any) -> FxQuote | None:
        if base == quote:
            day = as_date(on)
            return FxQuote(Decimal(1), day.isoformat() if day else "", (base, quote), None)
        day = as_date(on)
        if day is None:
            return None
        on_text = day.isoformat()
        direct = self._lookup((base, quote), on_text)
        inverse = self._lookup((quote, base), on_text)
        if direct and (not inverse or direct[1] >= inverse[1]):
            return FxQuote(direct[0], direct[1], (base, quote), direct[2])
        if inverse:
            return FxQuote(Decimal(1) / inverse[0], inverse[1], (quote, base), inverse[2])
        return None

    def rate(self, base: str, quote: str, on: Any) -> Decimal | None:
        found = self.quote(base, quote, on)
        return None if found is None else found.rate

    def convert(self, amount: Decimal | None, base: str | None, quote: str | None, on: Any) -> Decimal | None:
        if amount is None or base is None or quote is None:
            return None
        rate = self.rate(base, quote, on)
        return None if rate is None else amount * rate

    def latest(self, on: str) -> list[dict[str, Any]]:
        """Every stored pair's rate usable on ``on``, as ``{from, to, rate, as_of}``."""
        from .ledger.model import out

        rows = []
        for base, quote in sorted(self._index()):
            found = self._lookup((base, quote), on)
            if found:
                rows.append({"from": base, "to": quote, "rate": out(found[0]), "as_of": found[1]})
        return rows


def pairs(items: Sequence[Mapping[str, Any]], to: str | None) -> list[str]:
    """``CUR/TO`` names for money rows that could not be converted."""
    return sorted({f"{i['currency']}/{to}" for i in items if i and i.get("currency")}) if to else []


__all__ = ["DEFAULT_FX_MAX_AGE_DAYS", "ESSENTIAL_RULE", "FxQuote", "FxTable", "PER_MONTH", "XirrResult",
           "add_months", "annualize", "as_date", "essential_spending", "holding_character", "month_end",
           "month_ends", "month_keys", "months_between", "pairs", "per_month", "xirr", "xirr_solve"]
