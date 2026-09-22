"""Proactive engine: what deserves the person's attention today, and what is coming.

:func:`today` turns the canonical situation, the ledger and the raw snapshot into
at most three ranked *today* items plus a longer *upcoming* list.  :func:`weekly`
condenses the week into at most five lines of data for the model to phrase.
:func:`calendar` is the annual calendar (Mexico, United States and life events)
filtered to what is relevant for this person.

Contract (docs/scope.md sections 2 and 3):

* Every trigger is deterministic and reads only known data.  A trigger whose
  inputs are unknown does not fire; it is listed in ``unknown`` with what is
  missing.  Unknown is never read as zero.
* Taste: at most three today items, never two of the same kind, ranked money at
  risk > deadline within 14 days > opportunity > information.
* Each item carries a stable ``id`` and a ``fingerprint`` of its trigger.  A
  dismissed item stays hidden until its fingerprint changes; a snoozed one
  until its date (or until the trigger changes).  The acknowledgement state is
  plain JSON the caller stores (the service keeps it in the ``monitor``
  auxiliary namespace under ``_proactive``).

Nothing here sends messages, moves money or writes to the store.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from statistics import median
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .situation.model import D, add_months, num, ticker_of

TODAY_LIMIT = 3
UPCOMING_LIMIT = 15
UPCOMING_HORIZON_DAYS = 120
WEEKLY_LINES = 5
DEADLINE_DAYS = 14
PRIORITIES = ("risk", "deadline", "opportunity", "info")
SEVERITIES = ("act", "consider", "fyi")

# Trigger thresholds (docs/scope.md section 3).
SURPLUS_BALANCE_MULTIPLE = Decimal("1.5")     # checking > 1.5x monthly spend ...
SURPLUS_CYCLES = 2                            # ... at two consecutive month ends
SURPLUS_ADVICE_TOLERANCE = Decimal("0.05")    # a thread naming the monthly surplus within 5% is about it
CASH_DRAG_MONTHS = Decimal(12)
WINDFALL_MULTIPLE = Decimal("1.5")            # deposit >= 1.5x typical
WINDFALL_LOOKBACK_DAYS = 14
WINDFALL_HISTORY_DAYS = 180
HARVEST_MIN_LOSS_USD = Decimal(1000)
HARVEST_MIN_LOSS_SHARE = Decimal("0.05")
HARVEST_PRICE_MAX_AGE_DAYS = 45
WASH_SALE_DAYS = 30
FEE_LOOKBACK_DAYS = 35
FEE_NEW_HISTORY_DAYS = 90
FEE_INCREASE = Decimal("0.05")
CONCENTRATION_LIMIT = Decimal("0.10")
CONCENTRATION_RISK = Decimal("0.25")
SCAM_LOOKBACK_DAYS = 7
SCAM_HISTORY_DAYS = 60
SCAM_MEDIAN_MULTIPLE = Decimal(3)
SCAM_SPENDING_SHARE = Decimal("0.25")
SCAM_WORDS = ("garantizad", "guaranteed", "urgente", "urgent", "cripto", "crypto", "inversion", "investment",
              "premio", "prize", "rendimiento", "returns")
STATEMENT_OVERDUE_DAYS = 35
GUILT_FREE_MONTHS = 3
GUILT_FREE_UNSPENT = Decimal("0.20")
MEDICARE_AGE = 64
# Idle-cash yield gap: the reference rate for MXN cash when none is stored.  Banxico weekly primary auction
# (subasta de valores gubernamentales) of 2026-09-15: CETES 28 days at 6.25% (91d 6.66%, 182d 6.90%, 364d
# 7.24%), as published by Banxico and reported the same week.  Past REFERENCE_RATE_STALE_DAYS the item says so.
CETES_28D_REFERENCE = {
    "rate": "0.0625", "as_of": "2026-09-15", "checked_on": "2026-09-21", "name": "CETES 28 days",
    "source": "Banxico, subasta primaria de valores gubernamentales del 2026-09-15 (CETES 28 dias 6.25%); "
              "https://www.banxico.org.mx/mercados/resultados-subastas-valores-g.html",
}
REFERENCE_RATE_STALE_DAYS = 30
IDLE_YIELD_MIN_LOST = {"MXN": Decimal(1000), "USD": Decimal(50)}  # below this a year, not worth a nudge
STALE_KEYS_THAT_MATTER = ("client.profile", "income.", "spending.monthly", "cash.", "liability.", "investment.",
                          "goals", "reserve", "policy.ips", "planning.dca", "tax.profile", "cash_reference_rate",
                          "cash_yield")
CHECKING_TYPES = frozenset({"checking", "bank", "debit"})
TAXABLE_TYPES = frozenset({"brokerage", "taxable"})
RETIREMENT_SAVINGS_TYPES = frozenset({"ppr", "afore"})
FIBRA_TICKERS = frozenset({"FUNO11", "FMTY14", "FIBRAPL14", "DANHOS13", "TERRA13", "FIHO12", "FIBRAMQ12", "FSHOP13"})
KIND_ORDER = ("scam", "high_interest_debt", "reserve_low", "concentration", "tax_deadline", "harvest", "ppr_headroom", "windfall",
              "drift", "surplus", "follow_through", "idle_yield", "cash_drag", "dca_slipped", "fee_creep", "thread_ready", "life_calendar",
              "guilt_free", "statement_overdue", "stale_facts")

_MONTHS_ES = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")
_MONTHS_EN = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


# ------------------------------------------------------------------ small helpers


def _date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def resolve_as_of(as_of: Any = None, timezone: str | None = None, situation: Mapping[str, Any] | None = None) -> date:
    """``as_of`` if given; else today in ``timezone``, the profile timezone, or UTC."""
    day = _date(as_of)
    if as_of is not None and day is None:
        raise ValueError("as_of must be a date (YYYY-MM-DD)")
    if day is not None:
        return day
    name = timezone or ((situation or {}).get("profile") or {}).get("timezone") or "UTC"
    try:
        zone = ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"unknown timezone {name!r}; use an IANA name such as America/Mexico_City") from None
    return datetime.now(zone).date()


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _sig2(value: Decimal) -> str:
    """Two significant figures, so a trigger's fingerprint ignores day-to-day noise."""
    if value == 0:
        return "0"
    return format(Decimal(f"{float(value):.2g}"), "f")


def _money_text(amount: Any, currency: str | None, reporting: str | None) -> str:
    value = D(amount)
    if value is None:
        return "?"
    text = f"${abs(value):,.0f}"
    if value < 0:
        text = "-" + text
    if currency and currency != reporting:
        text += f" {currency}"
    return text


def _day_text(day: date | None, language: str) -> str:
    if day is None:
        return ""
    return f"{day.day} {_MONTHS_ES[day.month - 1]}" if language == "es" else f"{_MONTHS_EN[day.month - 1]} {day.day}"


def _month_end(day: date) -> date:
    return add_months(day.replace(day=1), 1) - timedelta(days=1)


def _entries(ledger: Mapping[str, Any] | None) -> list[dict]:
    if not ledger or not ledger.get("entries"):
        return []
    from .ledger.derive import active_entries
    return active_entries(ledger)[0]


def _account_types(ledger: Mapping[str, Any] | None) -> dict[str, str]:
    return {a.get("id"): (a.get("type") or "").lower() for a in (ledger or {}).get("accounts") or []}


def _own_transfer_ids(entries: list[dict]) -> set[str]:
    from .ledger.derive import match_transfers
    ids = {e["id"] for e in entries if e.get("transfer_group") or e.get("counterparty_account_id")}
    for pair in match_transfers(entries)["pairs"]:
        ids.update({pair["out"], pair["in"]})
    return ids


class _Rates:
    """Rates the situation used, plus the ledger's, for converting positions."""

    def __init__(self, situation: Mapping[str, Any], ledger: Mapping[str, Any] | None, as_of: date):
        self.rates: dict[tuple[str, str], Decimal] = {}
        for row in (ledger or {}).get("fx") or []:
            if isinstance(row, dict) and (row.get("date") or "") <= as_of.isoformat() and D(row.get("rate")):
                self.rates[(row.get("base"), row.get("quote"))] = D(row["rate"])
        for row in situation.get("fx") or []:
            pair = str(row.get("pair") or "")
            if "/" in pair and D(row.get("rate")):
                base, quote = pair.split("/", 1)
                self.rates[(base, quote)] = D(row["rate"])

    def convert(self, amount: Decimal | None, currency: str | None, to: str | None) -> Decimal | None:
        if amount is None or not currency or not to:
            return None
        if currency == to:
            return amount
        if (currency, to) in self.rates:
            return amount * self.rates[(currency, to)]
        if (to, currency) in self.rates and self.rates[(to, currency)]:
            return amount / self.rates[(to, currency)]
        return None


def _fact_ok(situation: Mapping[str, Any], key: str) -> bool:
    meta = (situation.get("meta") or {}).get(key) or {}
    return bool(meta) and not meta.get("stale") and not meta.get("inferred")


def _positions(situation: Mapping[str, Any], snapshot: Mapping[str, Any], rates: _Rates) -> list[dict]:
    """Every saved position (statements, then the legacy household) valued in the reporting currency."""
    currency = situation.get("currency")
    rows, covered = [], set()
    facts = [f for f in (snapshot or {}).get("facts") or [] if isinstance(f, dict) and f.get("status", "active") == "active"]
    for fact in facts:
        key, value = fact.get("key") or "", fact.get("value")
        if not key.startswith("account.") or not isinstance(value, dict) or not _fact_ok(situation, key):
            continue
        account = value.get("account") if isinstance(value.get("account"), dict) else {}
        account_id = account.get("id") or key.split(".", 1)[1]
        covered.add(account_id)
        for position in value.get("positions") or []:
            if isinstance(position, dict):
                rows.append(_position_row(position, account_id, account.get("type"), value.get("as_of"), key, currency, rates))
    household = next((f.get("value") for f in facts if f.get("key") == "household"), None)
    if isinstance(household, dict) and _fact_ok(situation, "household"):
        types = {a.get("id"): a.get("type") for a in household.get("accounts") or [] if isinstance(a, dict)}
        for position in household.get("positions") or []:
            if isinstance(position, dict) and position.get("account_id") not in covered:
                rows.append(_position_row(position, position.get("account_id"), types.get(position.get("account_id")),
                                          household.get("as_of"), "household", currency, rates))
    return rows


def _position_row(position: Mapping[str, Any], account_id: Any, account_type: Any, as_of: Any, key: str,
                  currency: str | None, rates: _Rates) -> dict:
    native = D(position.get("value"))
    symbol = position.get("symbol") or position.get("instrument_id")
    asset_class = position.get("asset_class") or (
        "cash" if str(position.get("instrument_id") or "").startswith("CASH:") else None)
    return {"symbol": symbol, "ticker": ticker_of(symbol), "account_id": account_id, "account_type": account_type,
            "asset_class": asset_class, "native": native, "currency": position.get("currency"),
            "quantity": D(position.get("quantity")), "as_of": as_of, "key": key,
            "domicile": position.get("issuer_domicile") or position.get("domicile"),
            "value": rates.convert(native, position.get("currency"), currency)}


# ------------------------------------------------------------------ items


def _item(kind: str, subject: str | None, *, severity: str, priority: str, title: tuple[str, str],
          why: tuple[str, str], next_step: tuple[str, str], data: Mapping[str, Any], sources: Iterable[str],
          trigger: Any, due: date | None = None, start: date | None = None) -> dict:
    """One nudge.  ``title``/``why``/``next_step`` are (en, es)."""
    assert severity in SEVERITIES and priority in PRIORITIES
    return {
        "id": f"{kind}:{subject}" if subject else kind, "kind": kind, "severity": severity, "priority": priority,
        "title": {"en": title[0], "es": title[1]}, "why": {"en": why[0], "es": why[1]},
        "due": due.isoformat() if due else None, "starts": start.isoformat() if start else None,
        "data": dict(data), "next_step": {"en": next_step[0], "es": next_step[1]},
        "sources": sorted(set(sources)), "fingerprint": _fingerprint([kind, subject, trigger]),
    }


class _Run:
    """Shared inputs for the triggers; collects items and unknowns."""

    def __init__(self, situation, ledger, snapshot, as_of: date, jurisdictions: set[str]):
        self.sit, self.ledger, self.snapshot, self.as_of = situation, ledger, snapshot or {}, as_of
        self.jurisdictions = jurisdictions
        self.currency = situation.get("currency")
        self.items: list[dict] = []
        self.unknown: list[dict] = []
        self.entries = _entries(ledger)
        self.types = _account_types(ledger)
        self.rates = _Rates(situation, ledger, as_of)
        self._positions: list[dict] | None = None
        self._own: set[str] | None = None
        self._labels: dict | None = None

    def missing(self, kind: str, *what: str) -> None:
        self.unknown.append({"kind": kind, "missing": list(what)})

    def money(self, amount: Any, currency: str | None = None) -> str:
        return _money_text(amount, currency or self.currency, self.currency)

    @property
    def positions(self) -> list[dict]:
        if self._positions is None:
            self._positions = _positions(self.sit, self.snapshot, self.rates)
        return self._positions

    @property
    def own_transfers(self) -> set[str]:
        if self._own is None:
            self._own = _own_transfer_ids(self.entries)
        return self._own

    @property
    def labels(self) -> dict:
        if self._labels is None:
            from .cashflow import categorize
            self._labels = categorize(self.ledger) if self.entries else {}
        return self._labels

    def fact_value(self, key: str) -> Any:
        for fact in self.snapshot.get("facts") or []:
            if isinstance(fact, dict) and fact.get("key") == key and fact.get("status", "active") == "active":
                return fact.get("value") if _fact_ok(self.sit, key) else None
        return None


# ------------------------------------------------------------------ triggers


def _surplus(run: _Run) -> None:
    """Checking balance above 1.5x monthly spending at the last two month ends (ledger), else an
    unallocated monthly surplus in the stated picture."""
    spend = D(run.sit["spending"].get("monthly"))
    checking = [a for a, t in run.types.items() if t in CHECKING_TYPES]
    if spend and checking and run.entries:
        from .ledger.derive import holdings
        ends = []
        cursor = run.as_of.replace(day=1) - timedelta(days=1)
        for _ in range(SURPLUS_CYCLES):
            ends.append(cursor)
            cursor = cursor.replace(day=1) - timedelta(days=1)
        opened = {e["account_id"]: e["date"] for e in reversed(run.entries) if e["kind"] == "opening_balance"}
        balances, known = [], True
        for end in ends:
            if any(opened.get(a, "9999") > end.isoformat() for a in checking):
                known = False
                break
            cash = holdings(run.ledger, end.isoformat())["result"]["cash"]
            total = Decimal(0)
            for row in cash:
                if row["account_id"] in checking:
                    converted = run.rates.convert(D(row["balance"]), row["currency"], run.currency)
                    if converted is None:
                        known = False
                    else:
                        total += converted
            balances.append(total)
        if known and balances:
            buffer = SURPLUS_BALANCE_MULTIPLE * spend
            if all(b > buffer for b in balances):
                idle = balances[0] - buffer
                amount = run.money(idle)
                run.items.append(_item(
                    "surplus", None, severity="consider", priority="opportunity",
                    title=(f"{amount} idle in checking", f"Tienes {amount} sin destino en tu cuenta"),
                    why=(f"Checking has been above 1.5x your monthly spending for {SURPLUS_CYCLES} months.",
                         f"Tu cuenta lleva {SURPLUS_CYCLES} meses por encima de 1.5 veces tu gasto mensual."),
                    next_step=(f"Where should the extra {amount} in my checking go?",
                               f"¿A dónde mando los {amount} que me sobran en la cuenta?"),
                    data={"idle": num(idle), "currency": run.currency, "monthly_spending": num(spend),
                          "month_end_balances": {e.isoformat(): num(b) for e, b in zip(ends, balances)},
                          "multiple": num(SURPLUS_BALANCE_MULTIPLE), "accounts": sorted(checking)},
                    sources=["ledger", run.sit["spending"].get("key") or "ledger"],
                    trigger=[ends[0].isoformat(), _sig2(idle)]))
                return
            return
    unallocated = D(run.sit["commitments"].get("unallocated"))
    flow = run.sit["cash_flow"]
    if unallocated is None or not flow.get("complete"):
        unknown_debts = [r for r in run.sit.get("liabilities") or [] if r["id"] in (flow.get("debt_payments_unknown") or [])]
        payments = [f"{_debt_name(r, 'en')} payment" for r in unknown_debts]
        run.missing("surplus", *(["income"] if flow.get("income") is None else []),
                    *(["spending"] if flow.get("spending") is None else []), *payments)
        before = D(flow.get("surplus_before_unknown_debts"))
        asked = {i["data"].get("liability") for i in run.items if i["kind"] == "high_interest_debt"
                 and i["data"].get("payment_unknown")}
        if before is not None and before > 0 and unknown_debts and not {r["id"] for r in unknown_debts} <= asked:
            # Never silently blocked: say what is left before the payment nobody gave, and ask for it.
            amount = run.money(before)
            names_en = ", ".join(_debt_name(r, "en") for r in unknown_debts)
            names_es = ", ".join(_debt_name(r, "es") for r in unknown_debts)
            run.items.append(_item(
                "surplus", None, severity="consider", priority="opportunity",
                title=(f"{amount}/month before your {names_en} payment", f"{amount} al mes antes del pago de {names_es}"),
                why=(f"I don't know your monthly {names_en} payment, so what stays free each month is not known yet.",
                     f"No sé cuánto pagas al mes de {names_es}, así que aún no sé cuánto te queda libre."),
                next_step=(f"My monthly {names_en} payment is…", f"Lo que pago al mes de {names_es} es…"),
                data={"surplus_before_unknown_debts": num(before), "currency": run.currency,
                      "missing": [f"liability.{r['id']}.payment" for r in unknown_debts]},
                sources=[r["key"] for r in unknown_debts], trigger=["before_debts", _sig2(before)]))
        return
    if unallocated <= 0:
        return
    amount = run.money(unallocated)
    advised = _advised_surplus(run, unallocated)
    if advised is not None:
        # The adviser already said where this money goes: never call it "sin destino". Ask whether it is
        # happening instead, and say nothing once the person committed to it.
        thread, target, committed = advised
        if committed or target is None:
            return
        run.items.append(_item(
            "follow_through", thread["id"], severity="consider", priority="opportunity",
            title=(f"Sending the {amount} {target[0]}?", f"¿Ya mandas los {amount} {target[1]}?"),
            why=(f"We agreed the {amount} left each month goes {target[0]}.",
                 f"Quedamos en que los {amount} que te quedan cada mes van {target[1]}."),
            next_step=(f"Confirm I'm already doing it: {amount} a month {target[0]}.",
                       f"Confirmar que ya lo hago: mando {amount} al mes {target[1]}."),
            data={"thread_id": thread["id"], "text": thread["text"], "unallocated_monthly": num(unallocated),
                  "currency": run.currency, "related": list(thread.get("related") or [])},
            sources=[thread["key"], *[k for k in run.sit["evidence"] if k.startswith(("income.", "spending."))]],
            trigger=[thread["id"], _sig2(unallocated)]))
        return
    run.items.append(_item(
        "surplus", None, severity="consider", priority="opportunity",
        title=(f"{amount}/month has no job yet", f"{amount} al mes todavía sin destino"),
        why=("Income minus spending, debt payments and your commitments leaves this each month.",
             "Tu ingreso menos gastos, deudas y compromisos deja esto libre cada mes."),
        next_step=(f"What should I do with the {amount} left over each month?",
                   f"¿Qué hago con los {amount} que me sobran cada mes?"),
        data={"unallocated_monthly": num(unallocated), "currency": run.currency,
              "surplus_monthly": run.sit["cash_flow"]["surplus"], "committed_monthly": run.sit["commitments"]["total"]},
        sources=[k for k in run.sit["evidence"] if k.startswith(("income.", "spending.", "goals", "planning.dca", "liability."))],
        trigger=["flow", _sig2(unallocated)]))


HIGH_INTEREST_RATE = Decimal("0.20")        # a debt at 20% a year or more costs more than any safe return
HIGH_INTEREST_PAYOFF_MONTHS = 12
_DEBT_NAMES = {"en": {"card": "card", "auto": "car loan", "mortgage": "mortgage", "personal": "personal loan",
                      "student": "student loan", "other": "loan"},
               "es": {"card": "tu tarjeta", "auto": "tu crédito del coche", "mortgage": "tu hipoteca",
                      "personal": "tu préstamo personal", "student": "tu crédito educativo", "other": "tu préstamo"}}


def _debt_name(row: Mapping[str, Any], lang: str) -> str:
    kind = row.get("kind") if row.get("kind") in _DEBT_NAMES[lang] else "other"
    if kind == "other" and row.get("name"):
        return str(row["name"])
    return _DEBT_NAMES[lang][kind]


def _high_interest(run: _Run) -> None:
    """A card or loan at 20% a year or more: paying it down beats any investment.

    Says the monthly amount that clears it in a year and the interest that saves against the current
    payment; when the payment is unknown it still says the amount and asks for the payment.
    """
    from .situation.model import annuity_payment, payoff
    for row in run.sit.get("liabilities") or []:
        balance, rate = D(row.get("balance")), D(row.get("annual_rate"))
        if balance is None or balance <= 0 or rate is None or rate < HIGH_INTEREST_RATE:
            continue
        currency = row.get("currency") or run.currency
        needed = annuity_payment(balance, rate, HIGH_INTEREST_PAYOFF_MONTHS)
        fast = payoff(balance, rate, needed, run.as_of)
        payment = D(row.get("monthly_payment"))
        current = payoff(balance, rate, payment, run.as_of) if payment is not None else {"status": "unknown"}
        saved = None
        if current.get("status") == "ready" and fast.get("status") == "ready":
            saved = max(D(current["interest"]) - D(fast["interest"]), Decimal(0))
        rate_text = f"{num(rate * 100, 0)}%"
        name_en, name_es = _debt_name(row, "en"), _debt_name(row, "es")
        pronoun = "la" if row.get("kind") in ("card", "mortgage") else "lo"
        needed_text, fast_interest = run.money(needed, currency), run.money(D(fast.get("interest")), currency)
        if payment is None:
            why = (f"Clearing it in {HIGH_INTEREST_PAYOFF_MONTHS} months takes about {needed_text} a month and costs "
                   f"{fast_interest} in interest. Tell me what you pay now to see how much that saves.",
                   f"Liquidar{pronoun} en {HIGH_INTEREST_PAYOFF_MONTHS} meses pide unos {needed_text} al mes y cuesta "
                   f"{fast_interest} de intereses. Dime cuánto pagas hoy para calcular cuánto te ahorras.")
            step = (f"My monthly {name_en} payment is…", f"Lo que pago al mes de {name_es} es…")
        elif current.get("status") == "never":
            why = (f"Your {run.money(payment, currency)} payment does not cover the interest; about {needed_text} a month "
                   f"clears it in {HIGH_INTEREST_PAYOFF_MONTHS} months.",
                   f"Tu pago de {run.money(payment, currency)} no cubre los intereses; con unos {needed_text} al mes "
                   f"{pronoun} liquidas en {HIGH_INTEREST_PAYOFF_MONTHS} meses.")
            step = (f"How do I get to {needed_text} a month on my {name_en}?",
                    f"¿Cómo llego a {needed_text} al mes para {name_es}?")
        else:
            saved_text = run.money(saved, currency) if saved else None
            why = (f"About {needed_text} a month clears it in {HIGH_INTEREST_PAYOFF_MONTHS} months"
                   + (f" and saves {saved_text} in interest against your current payment." if saved_text else "."),
                   f"Con unos {needed_text} al mes {pronoun} liquidas en {HIGH_INTEREST_PAYOFF_MONTHS} meses"
                   + (f" y te ahorras {saved_text} de intereses frente a tu pago actual." if saved_text else "."))
            step = (f"How do I pay off my {name_en} faster?", f"¿Cómo liquido {name_es} más rápido?")
        before = D(run.sit["cash_flow"].get("surplus_before_unknown_debts"))
        if payment is None and before is not None and before > 0:
            left = run.money(before)
            why = (why[0] + f" Before that payment, {left} is left each month.",
                   why[1] + f" Antes de ese pago te quedan {left} al mes.")
        title_en = f"Your {name_en} charges {rate_text}: paying it off is your best investment"
        title_es = f"{name_es[:1].upper() + name_es[1:]} cobra {rate_text}: pagar{pronoun} es tu mejor inversión"
        run.items.append(_item(
            "high_interest_debt", row["id"], severity="act", priority="risk", title=(title_en, title_es),
            why=why, next_step=step,
            data={"liability": row["id"], "annual_rate": num(rate, 4), "balance": num(balance), "currency": currency,
                  "monthly_to_clear": num(needed), "months": HIGH_INTEREST_PAYOFF_MONTHS,
                  "interest_if_cleared": fast.get("interest"), "monthly_payment": num(payment),
                  "interest_at_current_payment": current.get("interest") if current.get("status") == "ready" else None,
                  "interest_saved": num(saved), "payment_unknown": payment is None},
            sources=[row["key"]], trigger=[row["id"], str(num(rate, 4)), payment is None]))
        if payment is None:
            run.missing("high_interest_debt", f"liability.{row['id']}.payment")


_AMOUNT = re.compile(r"(?<![\d.,])(\d{1,3}(?:[,. ]\d{3})+|\d+(?:[.,]\d+)?)\s*(k|mil|thousand)?(?![\w])", re.I)
_TARGETS = {"auto": ("to the car", "al auto"), "mortgage": ("to the mortgage", "a la hipoteca"),
            "card": ("to the card", "a la tarjeta"), "student": ("to the student loan", "al crédito educativo"),
            "personal": ("to the personal loan", "al préstamo personal")}


def _amounts(text: str) -> list[Decimal]:
    """Money amounts written in a sentence: 25,500 / 25 500 / 25.5 mil / 25k."""
    found = []
    for match in _AMOUNT.finditer(text or ""):
        raw, unit = match.group(1), match.group(2)
        if re.fullmatch(r"\d{1,3}(?:[,. ]\d{3})+", raw):
            value = Decimal(re.sub(r"\D", "", raw))
        else:
            value = Decimal(raw.replace(",", "."))
        found.append(value * 1000 if unit else value)
    return found


def _near(value: Decimal | None, amount: Decimal) -> bool:
    return value is not None and amount > 0 and abs(value - amount) <= SURPLUS_ADVICE_TOLERANCE * amount


_RESERVE_WORDS = re.compile(r"\breserv|fondo de emergencia|colch[oó]n|emergency fund", re.I)


def _reserve_first(run: _Run) -> bool:
    """The reserve comes before paying a debt faster: it is below its target, or open advice sends money to it."""
    reserve = run.sit.get("reserve") or {}
    months, target = D(reserve.get("months")), D(reserve.get("target_months"))
    if months is not None and target is not None and months < target:
        return True
    return any(t.get("kind") in ("advice", "commitment") and _RESERVE_WORDS.search(t.get("text") or "")
               for t in (run.sit.get("threads") or {}).get("open") or [])


def _advice_target(run: _Run, thread: Mapping[str, Any]) -> tuple[str, str] | None:
    """Where the advised surplus goes; None when that is a debt prepayment the reserve comes before."""
    if _RESERVE_WORDS.search(thread.get("text") or ""):
        return ("to your emergency fund", "a tu reserva")
    related = [k for k in thread.get("related") or [] if isinstance(k, str)]
    for liability in run.sit.get("liabilities") or []:
        if liability.get("key") in related:
            if _reserve_first(run):
                return None
            if liability.get("kind") in _TARGETS:
                return _TARGETS[liability["kind"]]
            lender = liability.get("lender") or liability.get("name")
            return (f"to the {lender} loan", f"al crédito de {lender}") if lender else ("to that debt", "a esa deuda")
    for goal in run.sit.get("goals") or []:
        if goal["id"] in related or f"goals.{goal['id']}" in related:
            return (f"to {goal['name']}", f"a {goal['name']}")
    if re.search(r"invest|invert|invier", thread.get("text") or "", re.I):
        return ("to investing", "a invertir")
    return ("to that plan", "a ese plan")


def _advised_surplus(run: _Run, amount: Decimal) -> tuple[dict, tuple[str, str], bool] | None:
    """The open advice or commitment that already gives this monthly surplus a job, if any.

    A thread relates to the surplus when it points at a debt, the goals or spending, or its text names
    the amount (within 5%).  It counts as committed when it is a commitment, or when a goal or plan the
    situation already counts is tied to it (a pay_off goal for the same liability, or the same amount).
    """
    related_threads = []
    for thread in (run.sit.get("threads") or {}).get("open") or []:
        if thread.get("kind") not in ("advice", "commitment"):
            continue
        related = [k for k in thread.get("related") or [] if isinstance(k, str)]
        names_amount = any(_near(value, amount) for value in _amounts(thread.get("text") or ""))
        points = any(k.startswith(("liability.", "goals", "spending.")) for k in related)
        if names_amount or points:
            related_threads.append((not names_amount, thread))
    if not related_threads:
        return None
    thread = min(related_threads, key=lambda row: row[0])[1]  # one that names the amount first, else the newest
    related = set(thread.get("related") or [])
    stated = _amounts(thread.get("text") or "")
    committed = thread.get("kind") == "commitment"
    for goal in run.sit.get("goals") or []:
        if goal["status"] != "active" or goal["monthly_contribution"] is None or not goal.get("eligible", True):
            continue
        tied = goal.get("liability") in related or goal["id"] in related or f"goals.{goal['id']}" in related
        if tied or any(_near(D(goal["monthly_value"]), value) for value in stated):
            committed = True
    if "planning.dca" in related and run.sit.get("dca"):
        committed = True
    return thread, _advice_target(run, thread), committed


def _reserve(run: _Run) -> None:
    reserve = run.sit["reserve"]
    months, target = D(reserve.get("months")), D(reserve.get("target_months"))
    amount = D(reserve.get("amount"))
    if months is None or amount is None:
        run.missing("reserve_low", *(["reserve cash"] if amount is None else []),
                    *(["essential spending"] if months is None else []))
        return
    sources = ["reserve", *(reserve.get("source_keys") or (f"cash.{s}" for s in reserve.get("sources") or [])),
               run.sit["spending"].get("key") or "ledger"]
    if target is None:
        run.missing("reserve_low", "reserve.target_months")
    elif months < target:
        gap = D(reserve.get("gap"))
        urgent = months < 1
        gap_text = run.money(gap) if gap is not None else None
        run.items.append(_item(
            "reserve_low", None, severity="act" if urgent else "consider", priority="risk" if urgent else "opportunity",
            title=(f"Emergency fund: {num(months, 1)} of {num(target, 1)} months",
                   f"Fondo de emergencia: {num(months, 1)} de {num(target, 1)} meses"),
            why=((f"{gap_text} more reaches your target." if gap_text else "Below the target you set."),
                 (f"Te faltan {gap_text} para tu meta." if gap_text else "Está debajo de la meta que fijaste.")),
            next_step=("How do I fill my emergency fund fastest?", "¿Cómo lleno mi fondo de emergencia más rápido?"),
            data={"months": num(months, 1), "target_months": num(target, 1), "amount": num(amount),
                  "gap": num(gap), "currency": run.currency},
            # The condition (below the target, how urgently), not the exact months: a dismissed nudge stays
            # dismissed while the fund inches up or down, and comes back if the target or urgency changes.
            sources=sources, trigger=["below", str(target), urgent]))
    drag_at = max(CASH_DRAG_MONTHS, target or Decimal(0))
    if months > drag_at:
        # Excess over the person's target amount, or, with no target set, over the stated default of
        # CASH_DRAG_MONTHS of essential spending; never over an unknown.
        essential = D(run.sit["spending"].get("essential_for_reserve"))
        if target is not None:
            keep, basis = D(reserve.get("target_amount")), "target"
        else:
            keep, basis = (CASH_DRAG_MONTHS * essential if essential and essential > 0 else None), "default"
        excess = amount - keep if keep is not None else None
        if excess is None or excess <= 0:
            return
        text = run.money(excess)
        default_en = (f" You haven't set a reserve target, so this uses a default of {int(CASH_DRAG_MONTHS)} months "
                      "of essential spending." if basis == "default" else "")
        default_es = (f" No has fijado una meta de reserva; uso por defecto {int(CASH_DRAG_MONTHS)} meses de gasto "
                      "esencial." if basis == "default" else "")
        run.items.append(_item(
            "cash_drag", None, severity="consider", priority="opportunity",
            title=(f"{text} more cash than your reserve needs", f"Tienes {text} de más en efectivo"),
            why=(f"Your reserve covers {num(months, 1)} months; past {int(drag_at)} months the extra loses to inflation."
                 + default_en,
                 f"Tu reserva cubre {num(months, 1)} meses; arriba de {int(drag_at)} el excedente pierde contra la inflación."
                 + default_es),
            next_step=(f"Where could the extra {text} of cash earn more?", f"¿Dónde pongo a trabajar los {text} de más?"),
            data={"months": num(months, 1), "threshold_months": num(drag_at, 1), "excess": num(excess),
                  "currency": run.currency, "keep_basis": basis, "keep_amount": num(keep)},
            sources=sources, trigger=["drag", str(drag_at), basis]))


def _reference_rate(run: _Run, currency: str) -> tuple[dict | None, str | None]:
    """The rate idle ``currency`` cash could earn: a stored ``cash_reference_rate`` in that currency, else
    (MXN, Mexico residents) the dated CETES 28-day constant.  ``(None, why)`` when neither is known."""
    stored = run.fact_value("cash_reference_rate")
    if isinstance(stored, dict) and str(stored.get("currency") or currency).upper() == currency:
        low, high = D(stored.get("low", stored.get("rate"))), D(stored.get("high", stored.get("low", stored.get("rate"))))
        unit = str(stored.get("unit") or "decimal")
        scale = Decimal(100) if unit == "percent" else Decimal(10000) if unit == "bps" else Decimal(1)
        fact = next((f for f in run.snapshot.get("facts") or [] if isinstance(f, dict)
                     and f.get("key") == "cash_reference_rate" and f.get("status", "active") == "active"), {})
        when = _date(stored.get("as_of")) or _date((fact.get("source") or {}).get("observed_on"))
        if low is not None and high is not None and stored.get("source") and when is not None:
            # A range is read at its low end: the gap is never overstated.
            return {"rate": min(low, high) / scale, "as_of": when.isoformat(), "source": str(stored["source"]),
                    "name": str(stored.get("name") or stored["source"]), "origin": "stored cash_reference_rate",
                    "evidence": "cash_reference_rate"}, None
        return None, "cash_reference_rate needs low (or rate), unit, source and a date"
    if currency == "MXN" and _mx_resident(run):
        ref = CETES_28D_REFERENCE
        return {"rate": D(ref["rate"]), "as_of": ref["as_of"], "source": ref["source"], "name": ref["name"],
                "origin": "Wealth dated constant (checked " + ref["checked_on"] + ")", "evidence": None}, None
    return None, f"a reference rate for {currency} cash (save cash_reference_rate {{low, high, unit, source, currency}})"


def _cash_yield(run: _Run) -> Decimal | None:
    """What the idle cash earns now, from a stored ``cash_yield`` (decimal, or {value|rate, unit}); None if unsaid."""
    value = run.fact_value("cash_yield")
    if isinstance(value, dict):
        rate = D(value.get("value", value.get("rate")))
        unit = str(value.get("unit") or "decimal")
        if rate is None:
            return None
        return rate / 100 if unit == "percent" else rate / 10000 if unit == "bps" else rate
    return D(value)


def _idle_yield(run: _Run) -> None:
    """Cash above the reserve target and goal money that earns less than the reference rate (CETES 28 days in MX).

    Idle cash = liquid cash in the reference currency (stated cash and bank statements, the same pool the
    reserve reads) that is not earmarked for a goal, less the reserve target and less protected goal targets.
    Silent when the reference rate or any balance is unknown (listed under ``unknown``).
    """
    from .situation.model import kind_family
    currency = "MXN" if _mx_resident(run) else run.currency
    if not currency:
        return
    reference, why = _reference_rate(run, currency)
    if reference is None:
        run.missing("idle_yield", why)
        return
    reserve = run.sit["reserve"]
    keep = D(reserve.get("target_amount"))
    if keep is None:
        run.missing("idle_yield", "reserve.target_months (how much cash to keep)")
        return
    keep = run.rates.convert(keep, run.currency, currency)
    pool, earmarked, unknown = Decimal(0), Decimal(0), []
    for row in run.sit.get("cash") or []:
        if not row.get("counted") or not row.get("liquid"):
            continue
        if row.get("balance_unknown"):
            unknown.append(row["key"])
            continue
        if (row.get("currency") or currency) != currency:
            continue
        amount = D(row.get("amount"))
        if amount is None:
            continue
        if str(row.get("purpose") or "").startswith("goal:"):
            earmarked += amount
        else:
            pool += amount
    for account in run.sit.get("accounts") or []:
        if (account.get("source") == "ledger" or account.get("stale") or account.get("superseded_by")
                or account.get("duplicate_of") or not account.get("liquid") or kind_family(account.get("type")) != "cash"):
            continue
        native = D((account.get("native") or {}).get(currency))
        if native is not None:
            pool += native
    if unknown:
        run.missing("idle_yield", *(f"the balance of {k}" for k in unknown))
        return
    protected = Decimal(0)
    for goal in run.sit.get("goals") or []:
        if goal.get("status") == "active" and goal.get("protect_now") and D(goal.get("target_amount")) is not None:
            converted = run.rates.convert(D(goal["target_amount"]), goal.get("currency") or run.currency, currency)
            if converted is None:
                run.missing("idle_yield", f"FX to {currency} for goal {goal.get('id')}")
                return
            protected += converted
    if keep is None:
        run.missing("idle_yield", f"FX {run.currency}/{currency} for the reserve target")
        return
    # The reserve is filled by cash-like instruments first (CETES, money-market funds): only the rest of the
    # target has to sit in low-yield cash, and money already in those instruments is never called idle.
    in_instruments = sum((D(p.get("value")) or Decimal(0) for p in reserve.get("parts") or []
                          if p.get("kind") == "instrument"), Decimal(0))
    in_instruments = run.rates.convert(in_instruments, run.currency, currency) or Decimal(0)
    idle = pool - max(keep - in_instruments, Decimal(0)) - protected
    earned = _cash_yield(run)
    gap_rate = reference["rate"] - (earned or Decimal(0))
    if idle <= 0 or gap_rate <= 0:
        return
    lost = idle * gap_rate
    if lost < IDLE_YIELD_MIN_LOST.get(currency, Decimal(0)):
        return
    # The same excess cash, priced: this replaces the vaguer cash_drag nudge.
    run.items = [i for i in run.items if i["kind"] != "cash_drag"]
    as_of_rate = date.fromisoformat(reference["as_of"])
    age = (run.as_of - as_of_rate).days
    stale = age > REFERENCE_RATE_STALE_DAYS
    lost_text, idle_text = _money_text(lost, currency, run.currency), _money_text(idle, currency, run.currency)
    pct = f"{reference['rate'] * 100:.2f}%"
    bound = "at_most" if earned is None else "estimate"
    stale_en = f" The rate is from {reference['as_of']} ({age} days old); check today's rate." if stale else ""
    stale_es = f" La tasa es del {reference['as_of']} ({age} días); revisa la de hoy." if stale else ""
    earned_en = ("You haven't said what this cash earns, so this assumes nothing; it is the most it could cost."
                 if earned is None else f"It earns {earned * 100:.2f}% now.")
    earned_es = ("No sé cuánto te paga ese efectivo; supongo cero, así que es lo más que te cuesta."
                 if earned is None else f"Hoy gana {earned * 100:.2f}%.")
    rungs = _cetes_ladder(run, idle, reference, currency) if currency == "MXN" else None
    run.items.append(_item(
        "idle_yield", currency, severity="consider", priority="opportunity",
        title=(f"{'Up to ' if earned is None else ''}{lost_text}/yr lost on idle cash",
               f"{'Hasta ' if earned is None else ''}{lost_text} al año sin rendir"),
        why=(f"{idle_text} sits above your reserve and goals; {reference['name']} pays {pct}. {earned_en}{stale_en}",
             f"Tienes {idle_text} arriba de tu reserva y metas; {reference['name']} paga {pct}. {earned_es}{stale_es}"),
        next_step=((f"Should I ladder {idle_text} in CETES?", f"¿Me conviene escalonar {idle_text} en CETES?")
                   if currency == "MXN" else
                   (f"Where could {idle_text} earn {pct}?", f"¿Dónde pueden ganar {pct} mis {idle_text}?")),
        data={"idle": num(idle), "currency": currency, "lost_per_year": num(lost), "bound": bound,
              "reference_rate": num(reference["rate"], 4), "reference_name": reference["name"],
              "reference_as_of": reference["as_of"], "reference_source": reference["source"],
              "reference_origin": reference["origin"], "reference_stale": stale,
              "cash_yield": num(earned, 4) if earned is not None else None,
              "cash_yield_reason": None if earned is not None else "unknown: save cash_yield to make this exact",
              "kept": {"reserve_target": num(keep), "reserve_in_instruments": num(in_instruments),
                       "protected_goals": num(protected), "goal_earmarked_cash": num(earmarked)},
              "assumptions": ["Rates are gross annual, before the provisional ISR retention on capital that applies to "
                              "bank interest and CETES alike.",
                              "A one-year figure at today's rate; CETES 28 days reprice every week."],
              "offer": rungs},
        sources=["reserve", *(reserve.get("source_keys") or (f"cash.{s}" for s in reserve.get("sources") or [])),
                 *([reference["evidence"]] if reference["evidence"] else []),
                 *(["cash_yield"] if earned is not None else [])],
        trigger=[currency, _sig2(idle), str(reference["rate"]), reference["as_of"], earned is None]))


def _cetes_ladder(run: _Run, idle: Decimal, reference: dict, currency: str) -> dict:
    """Four 28-day CETES rungs bought a week apart (weekly liquidity once rolling), as ``ladder`` task inputs.

    Maturity values use the 28-day reference yield on the CETES convention (simple, actual/360); the dated goals
    ahead are the liabilities the ladder is checked against.  Without a dated goal the ladder cannot run.
    """
    share = idle / 4
    rate = reference["rate"]
    assets = []
    for i in range(4):
        buy = run.as_of + timedelta(days=7 * i + 1)
        matures = buy + timedelta(days=28)
        assets.append({"id": f"cetes28_rung{i + 1}", "currency": currency, "buy_on": buy.isoformat(),
                       "principal": num(share),
                       "cashflows": [{"date": matures.isoformat(),
                                      "amount": num(share * (1 + rate * 28 / 360))}]})
    liabilities = []
    for goal in run.sit.get("goals") or []:
        due = _date(goal.get("target_date"))
        amount = D(goal.get("target_amount"))
        if goal.get("status") == "active" and due and due > run.as_of and amount is not None \
                and (goal.get("currency") or run.currency) == currency:
            liabilities.append({"id": str(goal.get("id")), "date": due.isoformat(), "amount": num(amount),
                                "currency": currency})
    reserve_amount = D(run.sit["reserve"].get("amount"))
    inputs = {"currency": currency, "as_of": run.as_of.isoformat(),
              "reserve": {"currency": currency, "amount": num(reserve_amount) if reserve_amount is not None else None},
              "liabilities": sorted(liabilities, key=lambda l: l["date"]),
              "assets": [{k: v for k, v in a.items() if k in ("id", "currency", "cashflows")} for a in assets]}
    missing = ([] if liabilities else ["a dated goal (target_date and target_amount) for the ladder to match"]) \
        + ([] if reserve_amount is not None else ["reserve amount"])
    return {"task": "ladder", "ready": not missing, "missing": missing, "inputs": inputs, "rungs": assets,
            "note": "Illustrative: four equal 28-day rungs bought a week apart; maturity values use the dated "
                    "28-day yield. Buying is the person's step at their platform (Cetesdirecto or a broker)."}


_WINDFALL_NAMES = {"aguinaldo": ("aguinaldo", "aguinaldo"), "ptu": ("profit share (PTU)", "PTU"),
                   "bonus": ("bonus", "bono")}


def _windfall(run: _Run) -> None:
    if not run.entries:
        run.missing("windfall", "ledger")
        return
    start = (run.as_of - timedelta(days=WINDFALL_LOOKBACK_DAYS - 1)).isoformat()
    history_start = (run.as_of - timedelta(days=WINDFALL_HISTORY_DAYS)).isoformat()
    found = []
    for entry in run.entries:
        if not (start <= entry["date"] <= run.as_of.isoformat()):
            continue
        if entry["kind"] not in {"income", "deposit"} or entry["id"] in run.own_transfers:
            continue
        amount = D(entry.get("amount"))
        if amount is None or amount <= 0:
            continue
        category = (run.labels.get(entry["id"]) or {}).get("category")
        if category == "refund":
            continue
        month = int(entry["date"][5:7])
        prior = [abs(D(e["amount"])) for e in run.entries
                 if e["account_id"] == entry["account_id"] and e["currency"] == entry["currency"]
                 and history_start <= e["date"] < entry["date"] and e["kind"] == "income"
                 and (run.labels.get(e["id"]) or {}).get("category") not in {"aguinaldo", "ptu", "bonus", "refund"}]
        typical = Decimal(str(median(prior))) if len(prior) >= 3 else None
        if category in _WINDFALL_NAMES:
            label, probable = category, False
        elif typical is not None and amount >= WINDFALL_MULTIPLE * typical:
            label = "aguinaldo" if month == 12 else "ptu" if month in (5, 6) else "bonus"
            probable = True
        else:
            if typical is None and category not in _WINDFALL_NAMES:
                run.missing("windfall", f"3+ regular deposits in {entry['account_id']} to compare {entry['id']}")
            continue
        found.append((amount, entry, label, probable, typical))
    if not found:
        return
    amount, entry, label, probable, typical = max(found, key=lambda f: (f[0], f[1]["date"]))
    text = run.money(amount, entry["currency"])
    en, es = _WINDFALL_NAMES[label]
    title = ((f"Your {en} arrived: {text}", f"Llegó tu {es}: {text}") if not probable else
             (f"Extra deposit: {text} (your {en}?)", f"Llegó un depósito extra: {text} (¿tu {es}?)"))
    run.items.append(_item(
        "windfall", entry["id"], severity="consider", priority="opportunity", title=title,
        why=((f"{num(amount / typical, 1)}x your usual deposit." if typical else "Tagged as a one-off payment on the statement."),
             (f"Es {num(amount / typical, 1)} veces tu depósito habitual." if typical else "El estado de cuenta lo marca como pago extraordinario.")),
        next_step=(f"What should I do with my {en} of {text}?", f"¿Qué hago con mi {es} de {text}?"),
        data={"amount": num(amount), "currency": entry["currency"], "date": entry["date"], "label": label,
              "probable": probable, "typical_deposit": num(typical), "account_id": entry["account_id"]},
        sources=[f"ledger:{entry['id']}"], trigger=[entry["id"]]))


def _ips(run: _Run) -> dict | None:
    from .policy import current
    ips = current(run.snapshot, run.as_of)
    if ips is None or not isinstance((ips.get("allocation") or {}).get("sleeves"), list):
        return None
    return ips


def _drift(run: _Run) -> None:
    from .policy import _sleeve_for
    ips = _ips(run)
    if ips is None:
        run.missing("drift", "policy.ips")
        return
    sleeves = ips["allocation"]["sleeves"]
    if not run.positions:
        run.missing("drift", "positions (a statement or household)")
        return
    values: dict[str, Decimal] = {}
    unassigned, unvalued = [], []
    for row in run.positions:
        if row["value"] is None:
            unvalued.append(row["symbol"])
            continue
        sleeve = _sleeve_for({"symbol": row["ticker"] or row["symbol"], "asset_class": row["asset_class"]}, sleeves)
        if sleeve is None:
            unassigned.append(row["symbol"])
        else:
            values[sleeve] = values.get(sleeve, Decimal(0)) + row["value"]
    if unassigned or unvalued:
        run.missing("drift", *(f"sleeve for {s}" for s in sorted(set(map(str, unassigned)))),
                    *(f"value/FX for {s}" for s in sorted(set(map(str, unvalued)))))
        return
    total = sum(values.values(), Decimal(0))
    if total <= 0:
        return
    outside = []
    for sleeve in sleeves:
        weight = values.get(sleeve["id"], Decimal(0)) / total
        low, high = D(sleeve.get("min")), D(sleeve.get("max"))
        if low is None or high is None:
            continue
        if weight < low - Decimal("1e-9") or weight > high + Decimal("1e-9"):
            outside.append({"sleeve": sleeve["id"], "name": sleeve.get("name") or sleeve["id"], "weight": num(weight, 4),
                            "target": sleeve.get("target"), "min": sleeve.get("min"), "max": sleeve.get("max"),
                            "direction": "over" if weight > high else "under"})
    if not outside:
        return
    worst = max(outside, key=lambda o: abs(D(o["weight"]) - D(o["target"])))
    pct = f"{D(worst['weight']) * 100:.0f}%"
    run.items.append(_item(
        "drift", None, severity="consider", priority="opportunity",
        title=(f"{worst['name']} at {pct}, outside its range", f"{worst['name']} en {pct}, fuera de su rango"),
        why=(f"Your policy keeps it between {D(worst['min']) * 100:.0f}% and {D(worst['max']) * 100:.0f}%.",
             f"Tu política lo mantiene entre {D(worst['min']) * 100:.0f}% y {D(worst['max']) * 100:.0f}%."),
        next_step=("How do I rebalance back to my policy?", "¿Cómo rebalanceo para volver a mi política?"),
        data={"sleeves_outside": outside, "portfolio_value": num(total), "currency": run.currency,
              "policy_version": ips.get("version")},
        sources=["policy.ips", *sorted({r["key"] for r in run.positions})],
        trigger=sorted((o["sleeve"], o["direction"]) for o in outside)))


def _concentration(run: _Run) -> None:
    from .policy import _diversified
    net = run.sit["net_worth"]
    total = D(net.get("total"))
    if total is None or total <= 0 or not net.get("complete"):
        run.missing("concentration", "complete net worth")
        return
    ips = _ips(run)
    limit = D(((ips or {}).get("constraints") or {}).get("concentration", {}).get("limit")) or CONCENTRATION_LIMIT
    by_ticker: dict[str, Decimal] = {}
    undecided = set()
    for row in run.positions:
        if row["value"] is None or row["asset_class"] == "cash" or not row["ticker"]:
            continue
        diversified = _diversified({"symbol": row["ticker"], "asset_class": row["asset_class"]})
        if diversified:
            continue
        if diversified is None:
            undecided.add(row["ticker"])
        by_ticker[row["ticker"]] = by_ticker.get(row["ticker"], Decimal(0)) + row["value"]
    over = []
    for ticker, value in by_ticker.items():
        share = value / total
        if share <= limit:
            continue
        if ticker in undecided:
            run.missing("concentration", f"whether {ticker} is a single stock or a fund (asset_class)")
            continue
        over.append((share, ticker, value))
    if not over:
        return
    share, ticker, value = max(over)
    pct = f"{share * 100:.0f}%"
    risk = share > CONCENTRATION_RISK
    run.items.append(_item(
        "concentration", ticker, severity="act" if risk else "consider", priority="risk" if risk else "opportunity",
        title=(f"{ticker} is {pct} of your net worth", f"{ticker} es el {pct} de tu patrimonio"),
        why=(f"One company above {limit * 100:.0f}% ties your wealth to its fate.",
             f"Una sola empresa arriba del {limit * 100:.0f}% ata tu patrimonio a su suerte."),
        next_step=(f"How do I reduce my {ticker} concentration tax-efficiently?",
                   f"¿Cómo reduzco mi concentración en {ticker} sin pagar tanto impuesto?"),
        data={"ticker": ticker, "value": num(value), "share": num(share, 4), "limit": num(limit, 4),
              "net_worth": num(total), "currency": run.currency,
              "others_over_limit": [{"ticker": t, "share": num(s, 4)} for s, t, _ in sorted(over, reverse=True)[1:]]},
        sources=sorted({r["key"] for r in run.positions if r["ticker"] == ticker}),
        trigger=sorted(t for _, t, _ in over)))


def _us_filer(run: _Run) -> bool:
    return "US" in run.jurisdictions


def _mx_resident(run: _Run) -> bool:
    return "MX" in run.jurisdictions


def _harvest(run: _Run) -> None:
    if not _us_filer(run):
        return
    if not run.entries:
        run.missing("harvest", "ledger lots")
        return
    from .ledger.derive import holdings
    held = holdings(run.ledger, run.as_of.isoformat())["result"]
    symbols = {i["id"]: ticker_of(i.get("symbol") or i["id"]) for i in (run.ledger or {}).get("instruments") or []}
    prices: dict[tuple[str, str], tuple[Decimal, str]] = {}
    for row in run.positions:
        as_of = _date(row["as_of"])
        if (row["ticker"] and row["quantity"] and row["native"] is not None and as_of
                and (run.as_of - as_of).days <= HARVEST_PRICE_MAX_AGE_DAYS):
            key = (row["ticker"], row["currency"])
            if key not in prices or prices[key][1] < row["as_of"]:
                prices[key] = (row["native"] / row["quantity"], row["as_of"])
    window = (run.as_of - timedelta(days=WASH_SALE_DAYS)).isoformat()
    recent_buys = {symbols.get(e.get("instrument_id")) for e in run.entries if e["kind"] == "buy" and e["date"] >= window}
    candidates, blocked, unpriced = [], [], set()
    for lot in held["lots"]:
        if run.types.get(lot["account_id"]) not in TAXABLE_TYPES or lot["currency"] != "USD":
            continue
        ticker = symbols.get(lot["instrument_id"]) or lot["instrument_id"]
        basis, quantity = D(lot["cost_basis"]), D(lot["quantity"])
        price = prices.get((ticker, "USD"))
        if basis is None or quantity is None or quantity <= 0:
            continue
        if price is None:
            unpriced.add(ticker)
            continue
        loss = basis - quantity * price[0]
        if loss < HARVEST_MIN_LOSS_USD or loss < HARVEST_MIN_LOSS_SHARE * basis:
            continue
        row = {"lot_id": lot["id"], "ticker": ticker, "account_id": lot["account_id"], "loss": num(loss),
               "loss_share": num(loss / basis, 4), "acquired_on": lot["acquired_on"], "price_as_of": price[1]}
        (blocked if ticker in recent_buys else candidates).append(row)
    if unpriced:
        run.missing("harvest", *(f"a current price for {t}" for t in sorted(unpriced)))
    if not candidates:
        return
    total = sum((D(c["loss"]) for c in candidates), Decimal(0))
    text = _money_text(total, "USD", run.currency)
    year_end = date(run.as_of.year, 12, 31)
    run.items.append(_item(
        "harvest", str(run.as_of.year), severity="consider", priority="opportunity",
        title=(f"{text} of harvestable losses", f"{text} en pérdidas aprovechables"),
        why=("Selling these lots offsets gains and up to $3,000 of income; no purchase in the last 30 days blocks it.",
             "Venderlos compensa ganancias y hasta US$3,000 de ingreso; ninguna compra de 30 días lo impide."),
        next_step=(f"Should I harvest the {text} of losses?", f"¿Me conviene realizar las pérdidas de {text}?"),
        data={"total_loss": num(total), "currency": "USD", "lots": candidates, "blocked_by_wash_sale": blocked,
              "thresholds": {"min_loss": num(HARVEST_MIN_LOSS_USD), "min_share": num(HARVEST_MIN_LOSS_SHARE)}},
        sources=[f"ledger:{c['lot_id']}" for c in candidates], due=year_end,
        trigger=[run.as_of.year, sorted(c["lot_id"] for c in candidates)]))


def _annual_income_mxn(run: _Run) -> tuple[Decimal | None, bool]:
    """Annual income in MXN from the stated items; (amount, is_gross)."""
    income = run.sit["income"]
    if run.currency != "MXN" or D(income.get("monthly")) is None:
        return None, False
    total = D(income["monthly"]) * 12
    for extra in income.get("extras") or []:
        amount = D(extra.get("amount"))
        if extra.get("frequency") == "annual" and amount is not None:
            converted = run.rates.convert(amount, extra.get("currency"), "MXN")
            if converted is None:
                return None, False
            total += converted
    return total, income.get("net") is False


def _ppr(run: _Run) -> None:
    if not _mx_resident(run) or run.as_of.month not in (11, 12):
        return
    from .mexico import PARAMETERS
    year = run.as_of.year
    entry = PARAMETERS["uma_annual_mxn"].get(year) or {}
    uma = D(entry.get("value")) if entry.get("status") in {"verified", "statutory"} else None
    income, gross = _annual_income_mxn(run)
    accounts = [a for a, t in run.types.items() if t in RETIREMENT_SAVINGS_TYPES]
    missing = [*(["verified UMA for " + str(year)] if uma is None else []),
               *(["annual income in MXN"] if income is None else []),
               *(["a PPR/AFORE account in the ledger (voluntary contributions)"] if not accounts else [])]
    if missing:
        run.missing("ppr_headroom", *missing)
        return
    contributed = Decimal(0)
    for entry_ in run.entries:
        if entry_["account_id"] in accounts and entry_["date"][:4] == str(year) and entry_["kind"] in {"deposit", "transfer"}:
            amount = D(entry_.get("amount"))
            if amount and amount > 0 and entry_.get("currency") == "MXN":
                contributed += amount
    cap = min(income * Decimal("0.10"), uma * 5)
    headroom = cap - contributed
    if headroom <= 0:
        return
    text = run.money(headroom)
    bound = "exact" if gross else "at_least"
    deadline = date(year, 12, 31)
    run.items.append(_item(
        "ppr_headroom", str(year), severity="consider", priority="opportunity",
        title=(f"{'At least ' if not gross else ''}{text} of PPR deduction left",
               f"Te quedan {'al menos ' if not gross else ''}{text} deducibles en PPR"),
        why=("Voluntary retirement contributions paid by Dec 31 are deductible up to 10% of income (max 5 UMA).",
             "Las aportaciones voluntarias pagadas antes del 31 dic deducen hasta 10% del ingreso (tope 5 UMA)."),
        next_step=(f"Is it worth putting {text} in my PPR before Dec 31?",
                   f"¿Me conviene poner {text} en mi PPR antes del 31 de diciembre?"),
        data={"headroom": num(headroom), "cap": num(cap), "contributed_ytd": num(contributed), "currency": "MXN",
              "annual_income": num(income), "income_basis": "gross" if gross else "take-home (the real cap is higher)",
              "bound": bound, "uma_annual": num(uma), "tax_year": year},
        sources=["ledger", *[k for k in run.sit["evidence"] if k.startswith("income.")]], due=deadline,
        trigger=[year, _sig2(headroom)]))


def _fee_creep(run: _Run) -> None:
    if not run.entries:
        run.missing("fee_creep", "ledger")
        return
    from .cashflow import merchant_key
    first_by_account: dict[str, str] = {}
    for entry in run.entries:
        first_by_account.setdefault(entry["account_id"], entry["date"])
    groups: dict[tuple, list[dict]] = {}
    for entry in run.entries:
        category = (run.labels.get(entry["id"]) or {}).get("category")
        if entry["kind"] != "fee" and category not in {"bank_fees", "subscriptions"}:
            continue
        amount = D(entry.get("amount"))
        if amount is None or amount >= 0:
            continue
        kind = "subscription" if category == "subscriptions" else "fee"
        groups.setdefault((kind, entry["account_id"], merchant_key(entry.get("description")) or "fee"), []).append(entry)
    since = (run.as_of - timedelta(days=FEE_LOOKBACK_DAYS - 1)).isoformat()
    found = []
    for (kind, account, merchant), items in groups.items():
        items.sort(key=lambda e: (e["date"], e.get("seq", 0)))
        latest = items[-1]
        if not (since <= latest["date"] <= run.as_of.isoformat()):
            continue
        now = abs(D(latest["amount"]))
        if len(items) >= 2:
            before = abs(D(items[-2]["amount"]))
            if before and now > before * (1 + FEE_INCREASE) and now - before >= 1:
                found.append({"change": "increased", "kind": kind, "merchant": merchant, "account_id": account,
                              "amount": num(now), "previous": num(before), "increase": num(now - before),
                              "currency": latest["currency"], "entry_id": latest["id"], "date": latest["date"]})
        elif kind == "fee":
            history = (date.fromisoformat(latest["date"]) - date.fromisoformat(first_by_account[account])).days
            if history >= FEE_NEW_HISTORY_DAYS:
                found.append({"change": "new", "kind": kind, "merchant": merchant, "account_id": account,
                              "amount": num(now), "previous": None, "increase": num(now),
                              "currency": latest["currency"], "entry_id": latest["id"], "date": latest["date"]})
            else:
                run.missing("fee_creep", f"{FEE_NEW_HISTORY_DAYS} days of history in {account} to call {merchant} new")
    if not found:
        return
    top = max(found, key=lambda f: (D(f["increase"]), f["date"]))
    text = _money_text(top["amount"], top["currency"], run.currency)
    name = top["merchant"].title()
    if top["change"] == "new":
        title = (f"New fee: {name} {text}", f"Comisión nueva: {name} {text}")
        why = ("It never appeared on this account before.", "Nunca había aparecido en esta cuenta.")
    else:
        prev = _money_text(top["previous"], top["currency"], run.currency)
        title = (f"{name} went up: {prev} → {text}", f"{name} subió: {prev} → {text}")
        why = ("Higher than the last charge from the same merchant.", "Más alto que el cargo anterior del mismo comercio.")
    run.items.append(_item(
        "fee_creep", None, severity="consider", priority="opportunity", title=title, why=why,
        next_step=(f"Can I avoid or lower the {name} charge?", f"¿Puedo evitar o bajar el cargo de {name}?"),
        data={"changes": sorted(found, key=lambda f: -D(f["increase"]))},
        sources=[f"ledger:{f['entry_id']}" for f in found], trigger=sorted(f["entry_id"] for f in found)))


def _scam(run: _Run) -> None:
    if not run.entries:
        run.missing("scam", "ledger")
        return
    from .cashflow import merchant_key
    from .ledger.model import fold
    first_by_account: dict[str, str] = {}
    for entry in run.entries:
        first_by_account.setdefault(entry["account_id"], entry["date"])
    since = (run.as_of - timedelta(days=SCAM_LOOKBACK_DAYS - 1)).isoformat()
    spend = D(run.sit["spending"].get("monthly"))
    found = []
    for entry in run.entries:
        if not (since <= entry["date"] <= run.as_of.isoformat()) or entry["kind"] not in {"transfer", "withdrawal", "expense"}:
            continue
        amount = D(entry.get("amount"))
        if amount is None or amount >= 0 or entry["id"] in run.own_transfers or entry.get("instrument_id"):
            continue
        payee = merchant_key(entry.get("description"))
        if not payee:
            continue
        history_days = (date.fromisoformat(entry["date"]) - date.fromisoformat(first_by_account[entry["account_id"]])).days
        if history_days < SCAM_HISTORY_DAYS:
            run.missing("scam", f"{SCAM_HISTORY_DAYS} days of history in {entry['account_id']}")
            continue
        seen = any(merchant_key(e.get("description")) == payee for e in run.entries
                   if e["date"] < entry["date"] and e["id"] != entry["id"])
        if seen:
            continue
        window = (date.fromisoformat(entry["date"]) - timedelta(days=90)).isoformat()
        prior = [abs(D(e["amount"])) for e in run.entries
                 if window <= e["date"] < entry["date"] and e["account_id"] == entry["account_id"]
                 and e["currency"] == entry["currency"] and e["kind"] in {"transfer", "withdrawal", "expense"}
                 and D(e.get("amount")) is not None and D(e["amount"]) < 0 and e["id"] not in run.own_transfers]
        if len(prior) < 5:
            run.missing("scam", f"5+ earlier payments in {entry['account_id']} to judge {entry['id']}")
            continue
        typical = Decimal(str(median(prior)))
        size = abs(amount)
        converted = run.rates.convert(size, entry["currency"], run.currency)
        if size < SCAM_MEDIAN_MULTIPLE * typical:
            continue
        if spend is not None and converted is not None and converted < SCAM_SPENDING_SHARE * spend:
            continue
        text = fold(" ".join(filter(None, [entry.get("description"), entry.get("memo")])))
        words = [w for w in SCAM_WORDS if w in text]
        found.append((size, entry, payee, typical, words))
    if not found:
        return
    size, entry, payee, typical, words = max(found, key=lambda f: (len(f[4]), f[0]))
    text = run.money(size, entry["currency"])
    shown = [w for w in (entry.get("description") or payee).split()]
    while len(shown) > 1 and shown[0].lower() in _TRANSFER_WORDS:
        shown = shown[1:]
    name = " ".join(shown[:3]).title()
    run.items.append(_item(
        "scam", entry["id"], severity="act", priority="risk",
        title=(f"Was this you? {text} to {name}", f"¿Fuiste tú? {text} a {name}"),
        why=(f"A first payment to a new payee, {num(size / typical, 1)}x your usual.",
             f"Primer pago a un destinatario nuevo, {num(size / typical, 1)} veces lo habitual."),
        next_step=(f"I don't recognise the {text} transfer to {name}; what do I do?",
                   f"No reconozco la transferencia de {text} a {name}, ¿qué hago?"),
        data={"amount": num(size), "currency": entry["currency"], "payee": payee, "date": entry["date"],
              "account_id": entry["account_id"], "typical_payment": num(typical), "keywords": words,
              "others": [f[1]["id"] for f in found if f[1]["id"] != entry["id"]]},
        sources=[f"ledger:{entry['id']}"], trigger=[entry["id"]]))


_TRANSFER_WORDS = frozenset({"spei", "transferencia", "transf", "tef", "pago", "envio", "a", "to", "wire", "ach", "zelle"})


def _stale(run: _Run) -> None:
    stale = [k for k in run.sit.get("stale") or [] if k.startswith(STALE_KEYS_THAT_MATTER) or k.startswith("account.")]
    stale = [k for k in stale if not k.startswith("account.")]  # statements have their own nudge
    if not stale:
        return
    meta = run.sit.get("meta") or {}
    rows = [{"key": k, "observed_on": (meta.get(k) or {}).get("observed_on")} for k in stale]
    policy = "policy.ips" in stale
    run.items.append(_item(
        "stale_facts", None, severity="consider" if policy else "fyi", priority="info",
        title=(f"{len(stale)} detail{'s' if len(stale) != 1 else ''} to reconfirm",
               f"{len(stale)} dato{'s' if len(stale) != 1 else ''} por confirmar"),
        why=("Past their review date, so they are left out of the numbers.",
             "Pasaron su fecha de revisión y quedaron fuera de los cálculos."),
        next_step=("Which of my details are out of date?", "¿Qué datos míos están desactualizados?"),
        data={"keys": rows}, sources=stale, trigger=sorted(stale)))


def _statements(run: _Run) -> None:
    overdue = []
    covered = set()
    for fact in run.snapshot.get("facts") or []:
        key, value = (fact or {}).get("key") or "", (fact or {}).get("value")
        if not key.startswith("account.") or key.count(".") != 1 or not isinstance(value, dict) \
                or fact.get("status", "active") != "active":
            continue  # ``account.<id>.activity`` rides with its account, never a statement of its own
        account = value.get("account") if isinstance(value.get("account"), dict) else {}
        covered.add(account.get("id") or key.split(".", 1)[1])
        last = _date(value.get("as_of"))
        if last is None:
            continue
        age = (run.as_of - last).days
        if age > STATEMENT_OVERDUE_DAYS:
            overdue.append({"account": account.get("id") or key.split(".", 1)[1],
                            "institution": account.get("institution"), "last_statement": last.isoformat(),
                            "days": age, "key": key})
    names = {a.get("id"): a.get("institution") for a in (run.ledger or {}).get("accounts") or []}
    last_doc: dict[str, str] = {}
    for entry in run.entries:
        if (entry.get("source") or {}).get("kind") == "document" and entry["account_id"] not in covered:
            last_doc[entry["account_id"]] = max(last_doc.get(entry["account_id"], ""), entry["date"])
    for account, last in last_doc.items():
        age = (run.as_of - date.fromisoformat(last)).days
        if age > STATEMENT_OVERDUE_DAYS:
            overdue.append({"account": account, "institution": names.get(account), "last_statement": last,
                            "days": age, "key": "ledger"})
    if not overdue:
        return
    overdue.sort(key=lambda o: -o["days"])
    # One statement per institution and date: GBM's MXN and USD sub-accounts arrive on one document.
    documents = list(dict.fromkeys((o["institution"] or o["account"], o["last_statement"]) for o in overdue))
    first = overdue[0]
    label = first["institution"] or first["account"]
    more = len(documents) - 1
    run.items.append(_item(
        "statement_overdue", None, severity="fyi", priority="info",
        title=(f"{label} statement is {first['days']} days old" + (f" (+{more} more)" if more else ""),
               f"Tu estado de {label} tiene {first['days']} días" + (f" (+{more} más)" if more else "")),
        why=("Numbers from an old statement drift from reality.", "Con un estado viejo, los números se alejan de la realidad."),
        next_step=(f"Here is my latest {label} statement.", f"Te comparto mi último estado de cuenta de {label}."),
        data={"accounts": overdue, "threshold_days": STATEMENT_OVERDUE_DAYS},
        sources=sorted({o["key"] for o in overdue}),
        trigger=sorted((o["account"], o["last_statement"]) for o in overdue)))


def _guilt_free(run: _Run) -> None:
    stated = (run.sit["spending"].get("stated") or {})
    budget = D(stated.get("discretionary"))
    if budget is None or budget <= 0 or stated.get("currency") != run.currency:
        run.missing("guilt_free", "spending.monthly.discretionary (a fun budget)")
        return
    reserve, commitments = run.sit["reserve"], run.sit["commitments"]
    if commitments.get("unallocated") is None or D(reserve.get("gap")) is None:
        run.missing("guilt_free", *(["complete monthly flow"] if commitments.get("unallocated") is None else []),
                    *(["reserve target"] if D(reserve.get("gap")) is None else []))
        return
    if commitments.get("overcommitted") or D(reserve["gap"]) > 0 or D(commitments["unallocated"]) < 0:
        return
    if not run.entries:
        run.missing("guilt_free", "ledger")
        return
    first = run.entries[0]["date"]
    end = run.as_of.replace(day=1) - timedelta(days=1)
    start = add_months(end.replace(day=1), -(GUILT_FREE_MONTHS - 1))
    if first > start.isoformat():
        run.missing("guilt_free", f"{GUILT_FREE_MONTHS} full months in the ledger")
        return
    from .cashflow import spending_report
    report = spending_report(run.ledger, start.isoformat(), end.isoformat(), run.currency)
    if report["missing"]:
        run.missing("guilt_free", "FX for some spending")
        return
    months = report["result"]["months"]
    if any(D(m["unknown_essentiality"]) for m in months.values()):
        run.missing("guilt_free", "categories for some spending")
        return
    spent = [D(m["discretionary"]) for m in months.values()]
    if len(spent) < GUILT_FREE_MONTHS or any(s > budget * (1 - GUILT_FREE_UNSPENT) for s in spent):
        return
    unspent = sum((budget - s for s in spent), Decimal(0)) / len(spent)
    text = run.money(unspent)
    run.items.append(_item(
        "guilt_free", run.as_of.strftime("%Y-%m"), severity="fyi", priority="opportunity",
        title=(f"You can spend {text} guilt-free", f"Puedes gastar {text} sin culpa"),
        why=(f"Goals and reserve are on track and your fun budget went unspent {GUILT_FREE_MONTHS} months running.",
             f"Tus metas y tu reserva van bien y llevas {GUILT_FREE_MONTHS} meses sin usar tu presupuesto de gustos."),
        next_step=(f"Is it really fine to spend {text} on myself this month?",
                   f"¿De verdad puedo gastarme {text} en mí este mes?"),
        data={"budget": num(budget), "spent_by_month": {k: m["discretionary"] for k, m in months.items()},
              "average_unspent": num(unspent), "currency": run.currency},
        sources=["spending.monthly", "reserve", "ledger"], trigger=[run.as_of.strftime("%Y-%m")]))


def _dca(run: _Run) -> None:
    plans = run.fact_value("planning.dca")
    plans = plans if isinstance(plans, list) else [plans] if isinstance(plans, dict) else []
    plans = [p for p in plans if isinstance(p, dict) and p.get("status") not in {"stopped", "paused", "ended"}]
    if not plans:
        return
    if not run.entries:
        run.missing("dca_slipped", "ledger")
        return
    from .dca import adherence
    slipped = []
    for plan in plans:
        clean = {k: v for k, v in plan.items() if k != "status"}
        try:
            report = adherence(run.ledger, clean, run.as_of.isoformat())
        except ValueError:
            run.missing("dca_slipped", f"a valid plan for {plan.get('id')}")
            continue
        done = [i for i in report["result"]["installments"] if i["state"] not in {"pending", "unknown"}]
        if not done:
            continue
        last_due = max(i["due"] for i in done)
        latest = [i for i in done if i["due"] == last_due]
        bad = [i for i in latest if i["state"] in {"skipped", "partial"}]
        if bad:
            short = sum((D(i["planned"]) - (D(i["invested"]) or Decimal(0)) for i in bad), Decimal(0))
            slipped.append({"plan_id": report["result"]["plan_id"], "due": last_due, "states": [i["state"] for i in bad],
                            "shortfall": num(short), "currency": report["result"]["currency"],
                            "on_time_rate": report["result"]["on_time_rate"], "next_due": report["result"]["next_due"]})
    if not slipped:
        return
    top = max(slipped, key=lambda s: D(s["shortfall"]))
    text = _money_text(top["shortfall"], top["currency"], run.currency)
    run.items.append(_item(
        "dca_slipped", top["plan_id"], severity="consider", priority="opportunity",
        title=(f"Missed {text} of your {_day_text(_date(top['due']), 'en')} investment",
               f"Faltaron {text} de tu inversión del {_day_text(_date(top['due']), 'es')}"),
        why=("Your automatic plan did not buy the full amount.", "Tu plan automático no compró el monto completo."),
        next_step=(f"Should I catch up the {text} I missed?", f"¿Pongo al corriente los {text} que faltaron?"),
        data={"plans": slipped}, sources=["planning.dca", "ledger"], due=_date(top["next_due"]),
        trigger=sorted((s["plan_id"], s["due"]) for s in slipped)))


def _threads(run: _Run) -> None:
    meta = run.sit.get("meta") or {}
    ready = []
    for thread in (run.sit.get("threads") or {}).get("open") or []:
        related = [k for k in thread.get("related") or [] if isinstance(k, str)]
        if not related:
            continue
        mine = (meta.get(thread["key"]) or {}).get("revision")
        if not isinstance(mine, int):
            continue
        present = [k for k in related if _fact_ok(run.sit, k)]
        if len(present) != len(related):
            continue
        newer = [k for k in present if isinstance((meta.get(k) or {}).get("revision"), int) and meta[k]["revision"] > mine]
        if newer:
            ready.append((thread, newer))
    if not ready:
        return
    thread, newer = max(ready, key=lambda r: (r[0].get("created") or "", r[0]["id"]))
    snippet = thread["text"] if len(thread["text"]) <= 80 else thread["text"][:77] + "..."
    run.items.append(_item(
        "thread_ready", thread["id"], severity="consider", priority="opportunity",
        title=(f"I can now answer: {snippet}", f"Ya puedo responder: {snippet}"),
        why=("What we were waiting for is now saved.", "Ya tengo el dato que estábamos esperando."),
        next_step=(f"Let's pick up: {snippet}", f"Retomemos: {snippet}"),
        data={"thread_id": thread["id"], "text": thread["text"], "kind": thread.get("kind"), "arrived": newer,
              "others": [t["id"] for t, _ in ready if t["id"] != thread["id"]]},
        sources=[thread["key"], *newer],
        trigger=[thread["id"], [(k, meta[k]["revision"]) for k in sorted(newer)]]))


def _cohere(run: _Run) -> None:
    """Items that talk about the same money must not contradict each other.

    "Emergency fund: 3.7 of 6 months" next to "$312,712 idle in checking" reads
    as a contradiction: if cash is idle, why is the reserve short?  So:

    * idle checking cash that covers the reserve gap becomes ONE item that does
      the arithmetic ("$312,712 unassigned; $X of it fills your 6-month
      reserve"), whose next step is to set that amount aside;
    * idle cash that covers only part of the gap keeps both items, each saying
      how much is still missing after the idle cash;
    * idle cash already counted in the reserve is not "idle": that item goes;
    * a monthly amount with no job, next to a short reserve, says how many
      months of it fill the gap.
    """
    reserve = next((i for i in run.items if i["kind"] == "reserve_low"), None)
    surplus = next((i for i in run.items if i["kind"] == "surplus"), None)
    if reserve is None or surplus is None:
        return
    gap = D(reserve["data"].get("gap"))
    target = D(reserve["data"].get("target_months"))
    target_text = num(target, 1)
    idle = D(surplus["data"].get("idle"))
    if idle is not None:
        counted = set(surplus["data"].get("accounts") or []) & set(run.sit["reserve"].get("sources") or [])
        if counted:  # the "idle" balance is the reserve itself
            run.items.remove(surplus)
            reserve["data"]["checking_counted_in_reserve"] = sorted(counted)
            return
        if gap is None or gap <= 0:
            return
        idle_text, gap_text = run.money(idle), run.money(gap)
        if idle >= gap:
            left = idle - gap
            merged = _item(
                "reserve_low", None, severity=reserve["severity"], priority=reserve["priority"],
                title=(f"You have {idle_text} unassigned; {gap_text} of it fills your {target_text}-month reserve",
                       f"Tienes {idle_text} sin destino: con {gap_text} completas tu fondo de {target_text} meses"),
                why=(f"Your emergency fund covers {reserve['data']['months']} of {target_text} months, and checking "
                     f"has held more than you need for {SURPLUS_CYCLES} months. Moving {gap_text} closes the gap"
                     + (f" and still leaves {run.money(left)} to put to work." if left > 0 else "."),
                     f"Tu fondo cubre {reserve['data']['months']} de {target_text} meses y tu cuenta lleva "
                     f"{SURPLUS_CYCLES} meses con más de lo que necesitas. Con {gap_text} lo completas"
                     + (f" y aún te quedan {run.money(left)} para invertir." if left > 0 else ".")),
                next_step=(f"Help me set aside {gap_text} from checking as my emergency fund.",
                           f"Ayúdame a apartar {gap_text} de mi cuenta como fondo de emergencia."),
                data={**reserve["data"], "idle": num(idle), "fills_gap": num(gap), "left_after": num(left),
                      "funded_from": "idle_checking", "accounts": surplus["data"].get("accounts")},
                sources=[*reserve["sources"], *surplus["sources"]],
                trigger=["below_covered_by_idle", str(target)])
            run.items[run.items.index(reserve)] = merged
            run.items.remove(surplus)
            return
        missing = gap - idle
        missing_text = run.money(missing)
        reserve["why"] = {
            "en": f"Your idle {idle_text} in checking covers part of it; {missing_text} is still missing after that.",
            "es": f"Los {idle_text} sin destino en tu cuenta cubren una parte; aún faltan {missing_text}."}
        reserve["data"].update(idle=num(idle), still_missing=num(missing))
        surplus["why"] = {
            "en": f"Your emergency fund is short by {gap_text}; this covers part of it and {missing_text} is still missing.",
            "es": f"A tu fondo de emergencia le faltan {gap_text}; esto cubre una parte y aún faltan {missing_text}."}
        surplus["next_step"] = {"en": f"Help me move the {idle_text} to my emergency fund.",
                                "es": f"Ayúdame a pasar los {idle_text} a mi fondo de emergencia."}
        surplus["data"]["reserve_still_missing"] = num(missing)
        return
    monthly = D(surplus["data"].get("unallocated_monthly"))
    if monthly and monthly > 0 and gap is not None and gap > 0:
        months = gap / monthly
        reserve["data"]["months_to_fill_from_surplus"] = num(months, 1)
        surplus["why"] = {
            "en": surplus["why"]["en"] + f" At that pace your emergency fund is full in about {num(months, 1)} months.",
            "es": surplus["why"]["es"] + f" A ese ritmo completas tu fondo de emergencia en unos {num(months, 1)} meses."}


TRIGGERS = (_scam, _high_interest, _reserve, _idle_yield, _concentration, _harvest, _ppr, _windfall, _drift, _surplus, _dca, _fee_creep,
            _threads, _guilt_free, _statements, _stale)


# ------------------------------------------------------------------ calendar


def jurisdictions(situation: Mapping[str, Any], jurisdiction: Any = None) -> set[str]:
    """Tax jurisdictions to plan for: explicit, else stated tax residence (or residence), plus US for US persons."""
    if jurisdiction is not None:
        values = jurisdiction.replace("+", ",").split(",") if isinstance(jurisdiction, str) else jurisdiction
        if not isinstance(values, (list, tuple)) or not all(isinstance(v, str) for v in values):
            raise ValueError("jurisdiction must be MX, US, 'MX,US' or a list of them")
        codes = {v.strip().upper() for v in values if v.strip()}
        unknown = codes - {"MX", "US"}
        if unknown:
            raise ValueError(f"unsupported jurisdiction {sorted(unknown)}; use MX and/or US")
        return codes
    profile = situation.get("profile") or {}
    codes = set(profile.get("tax_residence") or ([profile["tax_residence_assumed"]] if profile.get("tax_residence_assumed") else []))
    if profile.get("us_person") is True:
        codes.add("US")
    return {c for c in codes if c in {"MX", "US"}}


def _income_kinds(situation: Mapping[str, Any], ledger: Mapping[str, Any] | None) -> set[str]:
    kinds = {row.get("kind") for row in (*situation["income"]["items"], *situation["income"]["extras"]) if row.get("kind")}
    if ledger and ledger.get("entries"):
        from .cashflow import categorize
        for label in categorize(ledger).values():
            if label["kind"] == "income":
                kinds.add({"freelance": "business", "rental": "rent"}.get(label["category"], label["category"]))
    return kinds


def _cal(item_id: str, due: date, start: date, *, kind: str, jurisdiction: str, title: tuple[str, str],
         why: tuple[str, str], next_step: tuple[str, str], severity: str = "consider", priority: str = "opportunity",
         basis: str | None = None, verification: str = "statutory", data: Mapping[str, Any] | None = None) -> dict:
    return {"id": item_id, "kind": kind, "jurisdiction": jurisdiction, "due": due, "start": start, "title": title,
            "why": why, "next_step": next_step, "severity": severity, "priority": priority, "basis": basis,
            "verification": verification, "data": dict(data or {})}


def _mx_calendar(as_of: date, kinds: set[str], held: set[str], ppr: bool) -> list[dict]:
    from .mexico import tax_calendar
    flags = {"business_or_professional_income": True if "business" in kinds else None,
             "rental_income": True if "rent" in kinds else None, "us_person": False}
    out = []
    for year in (as_of.year - 1, as_of.year):
        report = tax_calendar({"tax_year": year, **{k: v for k, v in flags.items() if v is not None}})
        for row in report["result"]["items"]:
            due = date.fromisoformat(row["date"])
            base = row["id"].rsplit("_", 2)[0] if row["id"].startswith("mx_provisional") else row["id"]
            if base == "mx_fibra_distribution_cutoff" and not held & FIBRA_TICKERS:
                continue
            if base == "mx_art185_deposits" and not ppr:
                continue
            text = _MX_TEXT.get(base)
            if text is None:
                continue
            start = {"mx_annual_return": date(due.year, 4, 1), "mx_ppr_151v_contributions": date(due.year, 11, 1),
                     "mx_constancias_intereses": date(due.year, 2, 1)}.get(base, due - timedelta(days=DEADLINE_DAYS))
            title, why, step, severity, priority = text(year, due)
            item_id = row["id"] if base != row["id"] else f"{row['id']}_{year}"
            out.append(_cal(item_id, due, start, kind="tax_deadline", jurisdiction="MX", title=title, why=why, next_step=step,
                            severity=severity, priority=priority, basis=row["basis"], verification=row["verification"],
                            data={"tax_year": year, "source_id": row["id"]}))
    return out


_MX_TEXT = {
    "mx_annual_return": lambda y, d: (
        (f"{y} annual return due {_day_text(d, 'en')}", f"Declaración anual {y}: vence el {_day_text(d, 'es')}"),
        ("Claiming your deductions can bring a refund.", "Aplicar tus deducciones puede darte saldo a favor."),
        (f"What do I need for my {y} annual return?", f"¿Qué necesito para mi declaración anual {y}?"), "act", "opportunity"),
    "mx_ppr_151v_contributions": lambda y, d: (
        (f"Last day for {y} deductible contributions", f"Último día para aportaciones deducibles de {y}"),
        ("PPR and personal deductions count only if paid by Dec 31.", "PPR y deducciones personales cuentan solo si pagas antes del 31 dic."),
        ("What can I still deduct this year?", "¿Qué todavía puedo deducir este año?"), "consider", "opportunity"),
    "mx_constancias_intereses": lambda y, d: (
        (f"{y} interest certificates arrive", f"Llegan tus constancias de intereses {y}"),
        ("Banks and brokers issue them by mid-February; they pre-fill your return.",
         "Bancos y casas de bolsa las emiten a mediados de febrero; prellenan tu anual."),
        ("Here are my constancias de intereses.", "Te comparto mis constancias de intereses."), "fyi", "info"),
    "mx_fibra_distribution_cutoff": lambda y, d: (
        ("FIBRA distributions cut-off", "Fecha límite de distribuciones de FIBRAs"),
        ("Trustees must distribute last year's result by Mar 15.", "Los fiduciarios deben distribuir el resultado por el 15 mar."),
        ("How are my FIBRA distributions taxed?", "¿Cómo tributan mis distribuciones de FIBRAs?"), "fyi", "info"),
    "mx_art185_deposits": lambda y, d: (
        (f"Art. 185 deposits still count for {y}", f"Depósitos Art. 185 aún cuentan para {y}"),
        ("Deposits made before you file apply to the prior year.", "Lo depositado antes de presentar la anual cuenta para el año anterior."),
        ("Should I make an Art. 185 deposit before filing?", "¿Me conviene un depósito Art. 185 antes de declarar?"), "fyi", "opportunity"),
    "mx_provisional_business": lambda y, d: (
        (f"Monthly ISR payment due {_day_text(d, 'en')}", f"Pago provisional de ISR: {_day_text(d, 'es')}"),
        ("Business or professional income pays ISR by the 17th.", "La actividad empresarial o profesional paga ISR a más tardar el 17."),
        ("How much is my provisional ISR payment?", "¿Cuánto es mi pago provisional de ISR?"), "act", "info"),
    "mx_provisional_rental": lambda y, d: (
        (f"Rental ISR payment due {_day_text(d, 'en')}", f"Pago provisional por rentas: {_day_text(d, 'es')}"),
        ("Rental income pays provisional ISR by the 17th.", "Los ingresos por arrendamiento pagan ISR provisional a más tardar el 17."),
        ("How much is my rental ISR payment?", "¿Cuánto pago de ISR por mis rentas?"), "act", "info"),
}


def _us_calendar(sit, as_of: date, kinds: set[str], invests: bool) -> list[dict]:
    profile = sit.get("profile") or {}
    residence = (profile.get("residence") or {}).get("country")
    abroad = residence is not None and residence != "US"
    unwithheld = bool(kinds & {"business", "rent"})
    out = []
    for year in (as_of.year, as_of.year + 1):
        if unwithheld:
            for quarter, month, tax_year in ((4, 1, year - 1), (1, 4, year), (2, 6, year), (3, 9, year)):
                due = date(year, month, 15)
                out.append(_cal(f"us_estimate_q{quarter}_{tax_year}", due, due - timedelta(days=10), kind="tax_deadline",
                                jurisdiction="US", basis="IRC 6654", severity="act", priority="opportunity",
                                title=(f"Q{quarter} estimated tax due {_day_text(due, 'en')}",
                                       f"Pago estimado Q{quarter} (EE.UU.): {_day_text(due, 'es')}"),
                                why=("Income without withholding owes quarterly payments.",
                                     "El ingreso sin retención paga impuestos trimestrales."),
                                next_step=(f"How much should my Q{quarter} estimated payment be?",
                                           f"¿Cuánto debo pagar en el estimado Q{quarter}?"),
                                data={"quarter": quarter, "tax_year": tax_year}))
        due = date(year, 4, 15)
        out.append(_cal(f"us_return_{year - 1}", due, date(year, 3, 15), kind="tax_deadline", jurisdiction="US",
                        basis="IRC 6072(a); IRC 219(f)(3); IRC 223(d)(1)", severity="act", priority="opportunity",
                        title=(f"{year - 1} US return and last day for {year - 1} IRA/HSA",
                               f"Declaración EE.UU. {year - 1} y último día IRA/HSA {year - 1}"),
                        why=("File or extend; prior-year IRA and HSA contributions close the same day.",
                             "Presenta o extiende; las aportaciones IRA/HSA del año anterior cierran ese día."),
                        next_step=(f"Do I still have {year - 1} IRA or HSA room?", f"¿Todavía tengo espacio IRA o HSA de {year - 1}?"),
                        data={"tax_year": year - 1, "abroad_automatic_extension": date(year, 6, 15).isoformat() if abroad else None}))
        if abroad:
            due = date(year, 6, 15)
            out.append(_cal(f"us_return_abroad_{year - 1}", due, due - timedelta(days=DEADLINE_DAYS), kind="tax_deadline",
                            jurisdiction="US", basis="Treas. Reg. 1.6081-5(a)(5)", severity="consider", priority="info",
                            title=(f"{year - 1} US return due (living abroad)", f"Declaración EE.UU. {year - 1} (residentes fuera)"),
                            why=("Automatic extension for US persons abroad; interest ran from Apr 15.",
                                 "Prórroga automática para quien vive fuera; los intereses corren desde el 15 abr."),
                            next_step=("What do I file with the IRS from Mexico?", "¿Qué presento al IRS viviendo en México?"),
                            data={"tax_year": year - 1}))
            due = date(year, 10, 15)
            out.append(_cal(f"us_extended_{year - 1}", due, due - timedelta(days=DEADLINE_DAYS), kind="tax_deadline",
                            jurisdiction="US", basis="Treas. Reg. 1.6081-5; 31 CFR 1010.306(c)", severity="consider",
                            priority="info",
                            title=(f"Extended {year - 1} return and FBAR due", f"Vence la anual extendida {year - 1} y el FBAR"),
                            why=("Last day for extended returns and the FBAR for foreign accounts.",
                                 "Último día para declaraciones extendidas y el FBAR de cuentas extranjeras."),
                            next_step=("Do I need to file an FBAR?", "¿Tengo que presentar el FBAR?"), data={"tax_year": year - 1}))
        if invests:
            due = date(year, 12, 31)
            out.append(_cal(f"us_year_end_{year}", due, date(year, 11, 1), kind="tax_deadline", jurisdiction="US",
                            basis="IRC 1091, 1211, 408A(d)(3)", severity="consider", priority="opportunity",
                            title=(f"{year} harvesting and Roth conversion window", f"Ventana {year}: pérdidas y conversión Roth"),
                            why=("Loss harvesting and Roth conversions must settle by Dec 31.",
                                 "Las pérdidas fiscales y conversiones Roth deben quedar antes del 31 dic."),
                            next_step=("What should I do before year end for US taxes?",
                                       "¿Qué hago antes de fin de año para mis impuestos en EE.UU.?"),
                            data={"tax_year": year}))
        birth = profile.get("birth_year")
        if residence == "US" and isinstance(birth, int) and year - birth >= MEDICARE_AGE:
            due = date(year, 12, 7)
            out.append(_cal(f"us_medicare_oe_{year}", due, date(year, 10, 15), kind="tax_deadline", jurisdiction="US",
                            basis="42 CFR 423.38(b)", severity="consider", priority="opportunity",
                            title=(f"Medicare open enrollment ends {_day_text(due, 'en')}",
                                   f"Inscripción abierta de Medicare: hasta el {_day_text(due, 'es')}"),
                            why=("Oct 15 to Dec 7 is the window to switch plans.", "Del 15 oct al 7 dic puedes cambiar de plan."),
                            next_step=("Should I change my Medicare plan?", "¿Me conviene cambiar mi plan de Medicare?"),
                            data={"year": year}))
    return out


def _life_calendar(sit, as_of: date, kinds: set[str], ledger) -> list[dict]:
    profile = sit.get("profile") or {}
    if (profile.get("residence") or {}).get("country") != "MX":
        return []
    salaried = bool(kinds & {"salary", "aguinaldo", "ptu"})
    liabilities = {r.get("kind") for r in sit.get("liabilities") or []}
    home = "mortgage" in liabilities or any((r.get("kind") or "").lower() == "real_estate" for r in sit.get("investments") or [])
    car = "auto" in liabilities
    received: set[tuple[str, Any]] = set()
    if ledger and ledger.get("entries"):
        from .cashflow import categorize
        labels = categorize(ledger)
        received = {(e["date"][:4], (labels.get(e["id"]) or {}).get("category")) for e in _entries(ledger)}
    out = []
    for year in (as_of.year, as_of.year + 1):
        if salaried and (str(year), "aguinaldo") not in received:
            due = date(year, 12, 20)
            out.append(_cal(f"mx_aguinaldo_{year}", due, date(year, 12, 1), kind="life_calendar", jurisdiction="MX",
                            basis="LFT Art. 87", severity="fyi", priority="info",
                            title=(f"Aguinaldo due by {_day_text(due, 'en')}", f"Tu aguinaldo llega a más tardar el {_day_text(due, 'es')}"),
                            why=("Plan it before it lands: a windfall rule beats an impulse.",
                                 "Planéalo antes de que llegue: una regla gana a un impulso."),
                            next_step=("How should I split my aguinaldo this year?", "¿Cómo reparto mi aguinaldo este año?")))
        if salaried and (str(year), "ptu") not in received:
            due = date(year, 5, 30)
            out.append(_cal(f"mx_ptu_{year}", due, date(year, 5, 16), kind="life_calendar", jurisdiction="MX",
                            basis="LFT Art. 122", severity="fyi", priority="info",
                            title=(f"PTU due by {_day_text(due, 'en')}", f"El reparto de utilidades (PTU) llega a más tardar el {_day_text(due, 'es')}"),
                            why=("Companies pay by May 30; employers who are individuals by Jun 29.",
                                 "Las empresas pagan antes del 30 may; los patrones persona física antes del 29 jun."),
                            next_step=("What should I do with my PTU?", "¿Qué hago con mi PTU?"),
                            data={"individual_employer_due": date(year, 6, 29).isoformat()}))
        due = date(year, 9, 30)
        out.append(_cal(f"mx_testamento_{year}", due, date(year, 9, 1), kind="life_calendar", jurisdiction="MX",
                        basis="Colegio Nacional del Notariado, Septiembre Mes del Testamento", severity="consider",
                        priority="opportunity", verification="annual campaign",
                        title=("Will month: notary fees up to 50% off", "Mes del Testamento: notarías hasta 50% menos"),
                        why=("Dying without a will in Mexico can cost MXN 30k-100k and years.",
                             "Morir sin testamento en México cuesta de 30 a 100 mil pesos y años de trámite."),
                        next_step=("Do I need a will, and what does it cost this month?", "¿Necesito testamento y cuánto cuesta este mes?")))
        if home:
            due = date(year, 2, 28)
            out.append(_cal(f"mx_predial_{year}", due, date(year, 1, 1), kind="life_calendar", jurisdiction="MX",
                            basis="Código fiscal local (predial anual con descuento en enero-febrero)", severity="consider",
                            priority="opportunity", verification="varies by municipality",
                            title=("Pay property tax early for the discount", "Paga el predial anual con descuento"),
                            why=("Most municipalities discount the full-year payment in January-February.",
                                 "La mayoría de los municipios descuenta el pago anual en enero y febrero."),
                            next_step=("Is paying my predial for the whole year worth it?", "¿Me conviene pagar el predial de todo el año?")))
        if car:
            due = date(year, 3, 31)
            out.append(_cal(f"mx_refrendo_{year}", due, date(year, 1, 1), kind="life_calendar", jurisdiction="MX",
                            basis="Ley de hacienda estatal (tenencia/refrendo)", severity="consider", priority="info",
                            verification="varies by state",
                            title=("Tenencia/refrendo for your car", "Tenencia o refrendo de tu auto"),
                            why=("Most states collect it in the first quarter; dates vary by state.",
                                 "La mayoría de los estados lo cobra en el primer trimestre; las fechas varían."),
                            next_step=("How much is my tenencia or refrendo?", "¿Cuánto pago de tenencia o refrendo?")))
    return out


def calendar(situation: Mapping[str, Any], as_of: Any = None, *, jurisdiction: Any = None,
             ledger: Mapping[str, Any] | None = None, snapshot: Mapping[str, Any] | None = None,
             horizon_days: int = 365) -> list[dict]:
    """Dated items relevant to this person from today through ``horizon_days``, earliest first.

    Mexico (``mexico.tax_calendar``) for MX tax residents, US dates for US filers,
    and life dates (aguinaldo, PTU, Mes del Testamento, predial, tenencia) for
    people living in Mexico.  Items that need a fact the person has not given
    (business income, a home, a car, age) are left out rather than assumed.
    """
    day = resolve_as_of(as_of, None, situation)
    codes = jurisdictions(situation, jurisdiction)
    kinds = _income_kinds(situation, ledger)
    held = {h.get("ticker") for h in (situation.get("holdings") or {}).get("saved") or []}
    types = set(_account_types(ledger).values()) | {(a.get("type") or "").lower() for a in situation.get("accounts") or []}
    ppr = bool(types & RETIREMENT_SAVINGS_TYPES)
    invests = bool(situation.get("investments")) or any(a.get("source") != "ledger" for a in situation.get("accounts") or [])
    items = []
    if "MX" in codes:
        items += _mx_calendar(day, kinds, held, ppr)
    if "US" in codes:
        items += _us_calendar(situation, day, kinds, invests)
    items += _life_calendar(situation, day, kinds, ledger)
    end = day + timedelta(days=horizon_days)
    seen, out = set(), []
    for item in sorted(items, key=lambda i: (i["due"], i["id"])):
        if day <= item["due"] <= end and item["id"] not in seen:
            seen.add(item["id"])
            out.append(item)
    return out


def _calendar_item(entry: Mapping[str, Any], as_of: date) -> dict:
    days = (entry["due"] - as_of).days
    priority = "deadline" if days <= DEADLINE_DAYS and entry["severity"] != "fyi" else entry["priority"]
    return _item(entry["kind"], entry["id"], severity=entry["severity"], priority=priority, title=entry["title"],
                 why=entry["why"], next_step=entry["next_step"],
                 data={**entry["data"], "jurisdiction": entry["jurisdiction"], "days_until": days,
                       "basis": entry["basis"], "verification": entry["verification"]},
                 sources=[f"calendar:{entry['jurisdiction']}"], trigger=[entry["due"].isoformat()],
                 due=entry["due"], start=entry["start"])


# ------------------------------------------------------------------ ranking and acknowledgement


def _rank(item: Mapping[str, Any]) -> tuple:
    kind = KIND_ORDER.index(item["kind"]) if item["kind"] in KIND_ORDER else len(KIND_ORDER)
    return (PRIORITIES.index(item["priority"]), item["due"] or "9999-12-31", SEVERITIES.index(item["severity"]), kind, item["id"])


def _hidden(item: Mapping[str, Any], state: Mapping[str, Any], as_of: date) -> dict | None:
    ack = ((state or {}).get("acknowledged") or {}).get(item["id"])
    if not isinstance(ack, dict) or ack.get("fingerprint") != item["fingerprint"]:
        return None
    if ack.get("action") == "dismiss":
        return {"id": item["id"], "kind": item["kind"], "reason": "dismissed", "on": ack.get("on")}
    until = _date(ack.get("until"))
    if ack.get("action") == "snooze" and until and as_of < until:
        return {"id": item["id"], "kind": item["kind"], "reason": "snoozed", "until": until.isoformat()}
    return None


def acknowledge(state: Mapping[str, Any] | None, candidates: Iterable[Mapping[str, Any]], as_of: date, *,
                dismiss: Iterable[str] = (), snooze: Iterable[Mapping[str, Any]] = (),
                restore: Iterable[str] = ()) -> dict:
    """Return the new acknowledgement state.

    ``dismiss``: item ids hidden until their trigger (fingerprint) changes.
    ``snooze``: ``[{id, until: YYYY-MM-DD} | {id, days}]``, hidden until that date or a trigger change.
    ``restore``: item ids to show again.  Acknowledgements whose trigger changed are dropped;
    those for items not currently triggered are kept (the same trigger may come back).
    """
    current = {c["id"]: c for c in candidates}
    acks = {k: dict(v) for k, v in ((state or {}).get("acknowledged") or {}).items()
            if isinstance(v, dict) and (k not in current or current[k]["fingerprint"] == v.get("fingerprint"))}
    for item_id in restore:
        acks.pop(item_id, None)
    for item_id in dismiss:
        if item_id not in current:
            raise ValueError(f"cannot dismiss {item_id!r}: not a current item")
        acks[item_id] = {"action": "dismiss", "fingerprint": current[item_id]["fingerprint"], "on": as_of.isoformat()}
    for entry in snooze:
        if not isinstance(entry, Mapping) or entry.get("id") not in current:
            raise ValueError("snooze entries are {id, until} or {id, days} for a current item")
        until = _date(entry.get("until"))
        if until is None:
            days = entry.get("days")
            if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 365:
                raise ValueError("snooze needs until (YYYY-MM-DD) or days (1-365)")
            until = as_of + timedelta(days=days)
        if until <= as_of:
            raise ValueError("snooze until must be after as_of")
        acks[entry["id"]] = {"action": "snooze", "fingerprint": current[entry["id"]]["fingerprint"],
                             "until": until.isoformat(), "on": as_of.isoformat()}
    return {"version": 1, "acknowledged": acks}


# ------------------------------------------------------------------ public API


def evaluate(situation: Mapping[str, Any], ledger: Mapping[str, Any] | None, snapshot: Mapping[str, Any] | None,
             as_of: Any = None, *, jurisdiction: Any = None, timezone: str | None = None) -> dict:
    """Every triggered item and every calendar item, before taste and acknowledgement."""
    day = resolve_as_of(as_of, timezone, situation)
    codes = jurisdictions(situation, jurisdiction)
    run = _Run(situation, ledger, snapshot, day, codes)
    for trigger in TRIGGERS:
        trigger(run)
    _cohere(run)  # before ranking, so no ranking can show two items that contradict each other
    entries = calendar(situation, day, jurisdiction=jurisdiction, ledger=ledger, snapshot=snapshot)
    triggered_kinds = {i["kind"] for i in run.items}
    active, future = [], []
    for entry in entries:
        if "ppr_headroom" in triggered_kinds and entry["id"].startswith("mx_ppr_151v"):
            continue  # the headroom nudge carries the same deadline with numbers
        if "harvest" in triggered_kinds and entry["id"].startswith("us_year_end"):
            continue
        (active if entry["start"] <= day else future).append(_calendar_item(entry, day))
    unknown: dict[str, list[str]] = {}
    for row in run.unknown:
        bucket = unknown.setdefault(row["kind"], [])
        bucket.extend(m for m in row["missing"] if m not in bucket)
    return {"as_of": day, "jurisdictions": sorted(codes), "candidates": run.items + active, "future": future,
            "unknown": [{"kind": k, "missing": v} for k, v in unknown.items()]}


def today(situation: Mapping[str, Any], ledger: Mapping[str, Any] | None, snapshot: Mapping[str, Any] | None,
          as_of: Any = None, *, jurisdiction: Any = None, timezone: str | None = None,
          state: Mapping[str, Any] | None = None) -> dict:
    """At most three ranked items for today, plus ``upcoming`` (overflow and dated items ahead).

    ``state`` is the acknowledgement state from :func:`acknowledge`.  The payload is
    JSON-safe; see ``docs`` in the module docstring for the item fields.
    """
    found = evaluate(situation, ledger, snapshot, as_of, jurisdiction=jurisdiction, timezone=timezone)
    day = found["as_of"]
    hidden, visible = [], []
    for item in found["candidates"]:
        why_hidden = _hidden(item, state or {}, day)
        (hidden.append(why_hidden) if why_hidden else visible.append(item))
    visible.sort(key=_rank)
    chosen, kinds, overflow = [], set(), []
    for item in visible:
        if len(chosen) < TODAY_LIMIT and item["kind"] not in kinds:
            chosen.append(item)
            kinds.add(item["kind"])
        else:
            overflow.append(item)
    horizon = (day + timedelta(days=UPCOMING_HORIZON_DAYS)).isoformat()
    ahead = []
    for item in found["future"]:
        why_hidden = _hidden(item, state or {}, day)
        if why_hidden:
            hidden.append(why_hidden)
        elif item["due"] <= horizon:
            ahead.append(item)
    upcoming = sorted(overflow + ahead, key=lambda i: (i["due"] or "9999-12-31", _rank(i)))[:UPCOMING_LIMIT]
    return {
        "as_of": day.isoformat(), "timezone": timezone or (situation.get("profile") or {}).get("timezone"),
        "language": (situation.get("profile") or {}).get("language"), "currency": situation.get("currency"),
        "jurisdictions": found["jurisdictions"], "today": chosen, "upcoming": upcoming, "hidden": hidden,
        "unknown": found["unknown"],
        "counts": {"triggered": len(found["candidates"]), "today": len(chosen), "upcoming": len(upcoming),
                   "hidden": len(hidden)},
        "rules": {"today_limit": TODAY_LIMIT, "one_per_kind": True, "order": list(PRIORITIES),
                  "deadline_days": DEADLINE_DAYS,
                  "acknowledge": "dismiss: [id] hides an item until its fingerprint changes; snooze: [{id, until|days}]; "
                                 "restore: [id]."},
        "_all": found["candidates"] + found["future"],
    }


def weekly(situation: Mapping[str, Any], ledger: Mapping[str, Any] | None, snapshot: Mapping[str, Any] | None,
           as_of: Any = None, *, jurisdiction: Any = None, timezone: str | None = None,
           state: Mapping[str, Any] | None = None) -> dict:
    """The week in at most five lines of data (the model phrases them), newest first by importance."""
    report = today(situation, ledger, snapshot, as_of, jurisdiction=jurisdiction, timezone=timezone, state=state)
    day = date.fromisoformat(report["as_of"])
    start = day - timedelta(days=6)
    currency = situation.get("currency")
    lines: list[dict] = []
    entries = [e for e in _entries(ledger) if start.isoformat() <= e["date"] <= day.isoformat()]
    if entries and currency:
        from .cashflow import income_report, spending_report
        spent = spending_report(ledger, start.isoformat(), day.isoformat(), currency)
        earned = income_report(ledger, start.isoformat(), day.isoformat(), currency)
        if not spent["missing"] and not earned["missing"]:
            income = sum((D(v) for m in earned["result"]["months"].values() for v in m.values()), Decimal(0))
            spending = D(spent["result"]["total"])
            lines.append({"kind": "cash_flow", "data": {"income": num(income), "spending": num(spending),
                                                        "net": num(income - spending), "currency": currency,
                                                        "period": [start.isoformat(), day.isoformat()]}})
        from .cashflow import merchant_key
        earlier = {merchant_key(e.get("description")) for e in _entries(ledger) if e["date"] < start.isoformat()}
        new = sorted({merchant_key(e.get("description")) for e in entries if e["kind"] == "expense"} - earlier - {""})
        if new and any(e["date"] < start.isoformat() for e in _entries(ledger)):
            lines.append({"kind": "new_merchants", "data": {"count": len(new), "merchants": new[:3]}})
    for item in report["today"]:
        if len(lines) >= WEEKLY_LINES - 1:
            break
        lines.append({"kind": "item", "data": {"id": item["id"], "item_kind": item["kind"], "severity": item["severity"],
                                               "title": item["title"], "due": item["due"], "facts": item["data"]}})
    upcoming = next((i for i in report["upcoming"] if i["due"]), None)
    if upcoming and len(lines) < WEEKLY_LINES:
        lines.append({"kind": "next_date", "data": {"id": upcoming["id"], "title": upcoming["title"], "due": upcoming["due"]}})
    return {"as_of": report["as_of"], "period": [start.isoformat(), day.isoformat()], "language": report["language"],
            "lines": lines[:WEEKLY_LINES], "unknown": report["unknown"],
            "instructions": "Phrase each line as one short sentence with its numbers; no advice beyond the items."}


def public(report: Mapping[str, Any]) -> dict:
    """The payload without internal fields."""
    return {k: v for k, v in report.items() if not k.startswith("_")}


__all__ = ["TODAY_LIMIT", "acknowledge", "calendar", "evaluate", "jurisdictions", "public", "resolve_as_of", "today",
           "weekly"]
