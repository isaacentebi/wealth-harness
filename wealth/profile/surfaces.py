"""Entry point and page payloads: the profile, today, the quarterly review and connections."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from ..policy import current as current_policy, summary as policy_summary
from ..situation import build as build_situation
from .helpers import (
    _CURRENCY, _as_date, _dict_value, _facts_by_key, _history, _humanize, _memory_lang, _snapshot, _today,
)
from .classify import account_label, completeness, memory_groups
from .overviews import differences, fact_labels, goals_view, overview, situation_overview, upcoming
from .perf import performance
from .memory import memory_view
from .picture import picture_view


# ---------------------------------------------------------------- entry point

def _situation(service: Any, client_id: str, snapshot: dict, today: date) -> dict:
    if hasattr(service, "situation"):
        try:
            return service.situation(client_id, today=today)
        except TypeError:  # an older service without the today argument
            return service.situation(client_id)
    return build_situation(snapshot, None, today)


def profile_view(service: Any, client_id: str, today: Any = None, language: str | None = None) -> dict:
    """Assemble the JSON-able dashboard model: only what the page renders.

    ``language`` ("es"/"en", e.g. from ``?lang=``) builds the memory in that
    language only; without it both are included so the page can switch offline.
    """
    from ..service import usable_snapshot

    today = _today(today)
    # A fact no reader can handle (e.g. an amount of 1e308 saved before numbers were bounded) is left
    # out of every view and listed for the person to remove, instead of breaking the page.
    snapshot, invalid = usable_snapshot(_snapshot(service, client_id))
    facts = _facts_by_key(snapshot)
    profile = _dict_value(facts.get("client.profile"))
    household = _dict_value(facts.get("household"))
    sit = _situation(service, client_id, snapshot, today)
    seen_invalid = {item["key"] for item in invalid}
    invalid += [item for item in sit.get("invalid_facts") or [] if item["key"] not in seen_invalid]
    if invalid:
        excluded = {item["key"] for item in invalid}
        snapshot = {**snapshot, "facts": [f for f in snapshot["facts"] if f["key"] not in excluded]}
        facts = _facts_by_key(snapshot)
    ov = overview(snapshot, today)
    if ov.get("status") == "empty":
        ov = situation_overview(sit)
    if ov.get("status") != "empty":
        ov["differences"] = differences(sit)
        if sit.get("prices"):
            # Ledger accounts valued at provider prices: each source with its date, and what is too old.
            ov["price_sources"] = (sit.get("net_worth") or {}).get("price_sources") or []
            ov["stale_prices"] = sit.get("stale_prices") or []
    goals = goals_view(snapshot, today)
    known = completeness(snapshot, sit)
    groups = memory_groups(snapshot, today, ov, goals, known["missing"], sit,
                           language or (sit.get("profile") or {}).get("language"))
    reporting = next((c for c in (profile.get("reporting_currency"), household.get("currency"),
                                  _dict_value(facts.get("plan.resources")).get("currency"), sit.get("currency"))
                      if isinstance(c, str) and _CURRENCY.match(c)), None)
    locale = next((profile.get(k) for k in ("locale", "language") if isinstance(profile.get(k), str)), None)
    saved = sit["profile"].get("language") or ("es" if str(locale or "").lower().startswith("es") else "en")
    langs = [_memory_lang(language)] if language else ["en", "es"]
    memory: dict[str, Any] = {"language": _memory_lang(language) if language else saved}
    for lang in langs:
        memory[lang] = memory_view(sit, snapshot, lang, known["missing"])
        memory[lang]["review"] = _invalid_items(invalid, lang) + memory[lang]["review"]
    return {
        "version": 2, "today": today.isoformat(),
        "client": {"display_name": snapshot["client"].get("display_name"),
                   "revision": snapshot["client"].get("revision")},
        "reporting_currency": reporting, "locale": locale,
        "overview": ov,
        "performance": performance(snapshot, _history(service, client_id, "household"), today),
        # The page's charts: net worth split and history, the month's flow, allocation, debts, goals,
        # reserve and returns (see picture_view).
        "picture": picture_view(service, client_id, sit, snapshot, ov, today),
        "groups": groups,
        # What Wealth knows, as sentences grouped by life area (see memory_view).
        "memory": memory,
        "completeness": known,
        "upcoming": upcoming(snapshot, today, labels=fact_labels(sit, language or (sit.get("profile") or {}).get("language"))),
        # The accepted investment policy (profile, sleeves with ranges, reserve, review), or None.
        "policy": policy_summary(current_policy(snapshot, today)),
        # One collapsed Herencia / Estate line: completeness and the top gap (only once they told us something).
        "estate": _estate_line(sit, snapshot, today),
        # Saved facts left out of every number because they cannot be read; each can be removed.
        "invalid_facts": invalid,
    }


def _estate_line(sit: dict, snapshot: dict, today: date) -> dict | None:
    from ..estate_register import summary
    from ..proactive import _estate_facts
    return summary(sit, snapshot, today) if _estate_facts(snapshot) else None


def _invalid_items(invalid: list[dict], language: str) -> list[dict]:
    """Review cards for unreadable facts: named in the person's words, with a remove action."""
    from ..store import _label

    items = []
    for index, item in enumerate(invalid):
        label = _label(item["key"], None, language)
        text = (f"La cifra guardada de {label} no se puede usar (es demasiado grande o está mal escrita). "
                "Elimínala y vuelve a escribirla." if language == "es" else
                f"The saved figure for {label} can’t be used (it is too large or malformed). "
                "Remove it and enter it again.")
        items.append({"id": f"invalid-{index}", "topic": "about", "text": text, "emphasis": [], "key": item["key"],
                      "origin": {"kind": "said"}, "since": None, "age_days": None, "unconfirmed": False,
                      "stale": True, "invalid": True, "edit": None, "forget": {"field": None}, "confirm": False})
    return items


# ---------------------------------------------------------------- today, quarterly review, connections
#
# Payloads for three surfaces: the "Hoy" lines above the chat composer (and the profile rail), the quarterly
# review letter (/review) and the Conexiones section.  Each returns only what its view draws; every number is
# the engine's, and an unknown stays None (drawn as "—").

TODAY_DUE_DAYS = 14          # a due date is shown only when it is this close
SNOOZE_DAYS = 7
_VALUE_IN_TITLE = re.compile(
    r"-?(?:US)?\$\s?\d[\d,]*(?:\.\d+)?(?: ?[A-Z]{3})?"      # $312,712 or $1,000 USD
    r"|\d+(?:[.,]\d+)?\s?%"                                 # 12%
    r"|\d+(?:\.\d+)? (?:of|de) \d+(?:\.\d+)? (?:months|meses)")  # 3.7 of 6 months


def _value_spans(text: str) -> list[list[int]]:
    """Where the title's value sits, so the page sets it in weight (the first money, percent or ratio)."""
    from ..situation.text import js_span  # offsets in UTF-16 units: the page slices with JavaScript
    match = _VALUE_IN_TITLE.search(text or "")
    return [js_span(text, match.start(), match.end())] if match else []


def today_view(service: Any, client_id: str, *, action: str | None = None, item_id: str | None = None,
               timezone_name: str | None = None) -> dict:
    """At most three lines for today; ``action`` (dismiss | snooze | restore) acknowledges one item first.

    Snooze is always seven days.  Acknowledgements persist per client in the proactive state.
    """
    inputs: dict[str, Any] = {}
    if timezone_name:
        inputs["timezone"] = timezone_name
    if action is not None:
        if action not in {"dismiss", "snooze", "restore"}:
            raise ValueError("Choose dismiss, snooze or restore.")
        if not isinstance(item_id, str) or not 0 < len(item_id) <= 200:
            raise ValueError("Name the item to change.")
        inputs[action] = [{"id": item_id, "days": SNOOZE_DAYS}] if action == "snooze" else [item_id]
    report = service.run("today", inputs, client_id)
    result = report.get("result") or {}
    as_of = _as_date(result.get("as_of")) or datetime.now(timezone.utc).date()
    items = []
    for item in result.get("today") or []:
        due = _as_date(item.get("due"))
        days = (due - as_of).days if due else None
        near = days is not None and 0 <= days <= TODAY_DUE_DAYS
        title = {lang: str((item.get("title") or {}).get(lang) or "") for lang in ("en", "es")}
        items.append({"id": item["id"], "severity": item.get("severity"), "title": title,
                      "emphasis": {lang: _value_spans(text) for lang, text in title.items()},
                      "next_step": {lang: str((item.get("next_step") or {}).get(lang) or "") for lang in ("en", "es")},
                      "due": due.isoformat() if near else None, "days": days if near else None})
    return {"as_of": as_of.isoformat(), "items": items}


# ---- quarterly review

_QUARTER = re.compile(r"^(\d{4})-Q([1-4])$")
REVIEW_QUARTERS = 8


def quarter_bounds(label: str) -> tuple[date, date]:
    match = _QUARTER.match(label or "") if isinstance(label, str) else None
    if not match:
        raise ValueError("Name the quarter as YYYY-Qn, for example 2026-Q2.")
    year, quarter = int(match.group(1)), int(match.group(2))
    start = date(year, 3 * quarter - 2, 1)
    end = (date(year + 1, 1, 1) if quarter == 4 else date(year, 3 * quarter + 1, 1)) - timedelta(days=1)
    return start, end


def _quarter_of(day: date) -> str:
    return f"{day.year}-Q{(day.month - 1) // 3 + 1}"


def review_quarters(first: date | None, today: date) -> list[str]:
    """Completed quarters with ledger history, newest first."""
    labels: list[str] = []
    if first is None:
        return labels
    start = quarter_bounds(_quarter_of(today))[0]
    while len(labels) < REVIEW_QUARTERS:
        label = _quarter_of(start - timedelta(days=1))
        start, end = quarter_bounds(label)
        if end < first:
            break
        labels.append(label)
    return labels


def _dec(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _add(*values: Any) -> str | None:
    parts = [_dec(v) for v in values]
    return None if any(p is None for p in parts) else str(sum(parts, Decimal(0)))


def _money_words(value: Any) -> str | None:
    number = _dec(value)
    if number is None:
        return None
    return ("-" if number < 0 else "") + f"${abs(number):,.0f}"


def _pct_words(value: Any, places: int = 1) -> str | None:
    number = _dec(value)
    if number is None:
        return None
    text = f"{abs(number) * 100:.{places}f}"
    if "." in text:  # trailing zeros of the decimals only: 12.50 -> 12.5, 90 stays 90
        text = text.rstrip("0").rstrip(".")
    return ("-" if number < 0 else "") + text + "%"


_NOTES = {
    "price": ("No price for {x} on those dates yet, so this stays unknown.",
              "Aún no hay precio de {x} en esas fechas; por eso no se sabe."),
    "fx": ("An exchange rate for those dates is missing.", "Falta un tipo de cambio de esas fechas."),
    "ips": ("There is no accepted investment policy yet, so there are no bands.",
            "Aún no hay una política de inversión aceptada; por eso no hay rangos."),
    "benchmark": ("No benchmark series is saved for this quarter.", "No hay una serie de referencia guardada para este trimestre."),
    "goal": ("No account is earmarked for this goal yet.", "Aún no hay cuentas asignadas a esta meta."),
    "floor": ("Some costs are unknown, so this is a floor, not the full cost.",
              "Algunos costos no se conocen; esto es un mínimo, no el total."),
    "flows": ("Income and spending for this quarter are incomplete.", "Los ingresos y gastos de este trimestre están incompletos."),
    "other": ("Some inputs are missing for this section.", "Faltan algunos datos para esta sección."),
}


def _note(missing: Iterable[dict], prefer: str | None = None) -> dict | None:
    """One plain line naming what would fill the unknowns in a section."""
    rows = [m for m in missing or [] if isinstance(m, dict)]
    if not rows and prefer is None:
        return None
    kind, names = prefer, []
    for row in rows:
        key = str(row.get("key") or "")
        found = re.match(r"^(?:price |prices\.)([A-Za-z0-9._:-]+?)(?:@| on |$)", key)
        if found:
            kind = kind or "price"
            if found.group(1) not in names:
                names.append(found.group(1))
        elif kind is None:
            kind = ("fx" if key.startswith("fx") else "ips" if key.startswith("policy") else
                    "benchmark" if "benchmark" in key else "goal" if key.startswith("goal") else
                    "floor" if key.startswith(("holdings", "cash_reference", "instruments")) else "other")
    en, es = _NOTES[kind or "other"]
    x = ", ".join(names[:3]) if names else "some holdings"
    return {"en": en.format(x=x), "es": es.format(x=x if names else "algunas posiciones")}


_NEXT = {
    "reserve": (("Top up the emergency reserve", "Completar el fondo de emergencia"),
                ("How do I top up my emergency reserve this quarter? I'm {v} short.",
                 "¿Cómo completo mi fondo de emergencia este trimestre? Me faltan {v}.")),
    "drift": (("Rebalance to your policy ranges, new contributions first",
               "Volver a los rangos de tu política, primero con aportaciones nuevas"),
              ("How do I rebalance to my policy ranges with new contributions first? About {v} is out of place.",
               "¿Cómo vuelvo a los rangos de mi política usando primero aportaciones nuevas? Hay unos {v} fuera de lugar.")),
    "harvest": (("Realise losses to offset gains", "Realizar pérdidas para compensar ganancias"),
                ("Should I realise losses to offset gains this quarter?",
                 "¿Me conviene realizar pérdidas para compensar ganancias este trimestre?")),
    "ppr_headroom": (("Use the PPR deduction room you have left", "Usar el espacio de deducción del PPR que te queda"),
                     ("How much should I put in my PPR before year end?",
                      "¿Cuánto debería aportar a mi PPR antes de fin de año?")),
    "goal_pace": (("Raise the contribution to {name} or move its date", "Subir la aportación a {name} o mover su fecha"),
                  ("{name} is behind pace. Should I contribute more or move the date?",
                   "{name} va atrasada. ¿Aporto más o muevo la fecha?")),
    "fees": (("Compare lower-cost equivalents", "Comparar equivalentes de menor costo"),
             ("Which of my holdings have cheaper equivalents?", "¿Cuáles de mis inversiones tienen equivalentes más baratos?")),
    "dca": (("Catch up the {name} plan", "Ponerte al día con el plan {name}"),
            ("I'm {v} behind on my {name} plan. How do I catch up?",
             "Voy {v} atrasado en mi plan {name}. ¿Cómo me pongo al día?")),
}


# Engine and policy names arrive in English (or in the words the person used); the page shows each in its own
# language.  Pairs read both ways, so a sleeve saved in Spanish still reads in English on the en page.
_LABEL_PAIRS = (
    ("Global equity", "Renta variable global"), ("US equity", "Renta variable de EE. UU."),
    ("Mexican equity", "Renta variable mexicana"), ("International equity", "Renta variable internacional"),
    ("Emerging markets equity", "Renta variable de mercados emergentes"), ("Equity", "Renta variable"),
    ("Mexican government fixed income", "Deuda gubernamental mexicana"),
    ("Mexican fixed income", "Renta fija mexicana"), ("Global fixed income", "Renta fija global"),
    ("US fixed income", "Renta fija de EE. UU."), ("Fixed income", "Renta fija"), ("Bonds", "Bonos"),
    ("Cash (MXN)", "Efectivo (MXN)"), ("Cash (USD)", "Efectivo (USD)"), ("Cash", "Efectivo"),
    ("Real estate", "Bienes raíces"), ("Gold", "Oro"), ("Commodities", "Materias primas"),
    ("Funds", "Fondos"), ("Unclassified", "Sin clasificar"),
)
_REVIEW_LABELS = {text.casefold(): pair for pair in _LABEL_PAIRS for text in pair}
_ASSET_LABELS = {"cash": ("Cash", "Efectivo"), "equity": ("Equity", "Renta variable"),
                 "fixed_income": ("Fixed income", "Renta fija"), "fund": ("Funds", "Fondos"),
                 "real_estate": ("Real estate", "Bienes raíces"), "unknown": ("Unclassified", "Sin clasificar"),
                 "unclassified": ("Unclassified", "Sin clasificar")}
# Instruments a recurring plan buys, named by the index they track (the plan's human name when it has none).
_TRACKS = {**dict.fromkeys(("CSPX", "CSPXN", "VOO", "IVV", "SPY", "VUSA", "VUAA", "SXR8", "IVVPESO"), "S&P 500"),
           **dict.fromkeys(("CNDX", "EQQQ", "QQQ", "QQQM"), "Nasdaq-100"),
           **dict.fromkeys(("VWRA", "VWRL", "VT", "ACWI", "SSAC"), "MSCI ACWI"),
           **dict.fromkeys(("VTI", "ITOT"), "US total market"), **dict.fromkeys(("NAFTRAC",), "S&P/BMV IPC")}
# A DCA "biweekly" plan buys every 14 days: catorcenal, not quincenal (twice a month).
_CADENCE = {"weekly": ("{x} weekly", "{x} semanal"), "biweekly": ("{x} every two weeks", "{x} cada dos semanas"),
            "monthly": ("{x} monthly", "{x} mensual"), "quarterly": ("{x} quarterly", "{x} trimestral")}
# Fixture and provenance notes some inputs carry in a name, e.g. "(fictional levels)": never shown.
_ANNOTATION = re.compile(r"\s*\((?:[^)]*\b(?:fictional|ficticio|ficticia|example|ejemplo|levels|niveles|demo|test|sample)\b[^)]*)\)",
                         re.I)
_INDEX_WORDS = ((r"\b(\d+) days\b", r"\1 días"), (r"\bin (MXN|USD|EUR)\b", r"en \1"),
                (r"^Global aggregate bonds\b", "Bonos globales agregados"), (r"\bequity index\b", "índice accionario"),
                (r"\bbond index\b", "índice de bonos"), (r"^US total market\b", "Mercado total de EE. UU."))


def _both(text: str) -> dict:
    return {"en": text, "es": text}


def _label(name: Any, asset: Any = None) -> dict:
    """A sleeve or asset-class name in both languages; a name only the person uses is shown as they wrote it."""
    text = str(name or "").strip()
    pair = _REVIEW_LABELS.get(text.casefold()) or _ASSET_LABELS.get((text or str(asset or "")).casefold())
    if pair:
        return {"en": pair[0], "es": pair[1]}
    return _both(_humanize(text) if re.fullmatch(r"[a-z0-9_.]+", text) else text)


def _index_label(name: Any) -> dict | None:
    """An index name without fixture notes, with its few common words in the page language."""
    text = _ANNOTATION.sub("", str(name or "")).strip()
    if not text or re.fullmatch(r"(ips|reference_60_40)\.[\w.]+", text):
        return None
    es = text
    for pattern, repl in _INDEX_WORDS:
        es = re.sub(pattern, repl, es)
    en = text
    for pattern, repl in ((r"\b(\d+) días\b", r"\1 days"), (r"\ben (MXN|USD|EUR)\b", r"in \1")):
        en = re.sub(pattern, repl, en)
    return {"en": en, "es": es}


def _benchmark(chosen_key: str | None, sleeves: list[dict], specs: Any) -> tuple[dict | None, list[dict]]:
    """The benchmark as a label plus its parts [{weight, index}], from the policy weights and the index specs.

    With no structured spec (only the engine's free-text name) the parts are empty and the label is generic.
    """
    from .. import views as V

    if chosen_key is None:
        return None, []
    specs = specs if isinstance(specs, dict) else {}
    parts: list[dict] = []
    if chosen_key == "ips_benchmark":
        label = {"en": "Your policy benchmark", "es": "Referencia de tu política"}
        ips_specs = specs.get("ips") if isinstance(specs.get("ips"), dict) else {}
        for sleeve in sleeves:
            if not sleeve.get("target"):
                continue
            index = _index_label((ips_specs.get(sleeve.get("sleeve")) or {}).get("name"))
            if index is None:
                return label, []
            parts.append({"weight": V.ratio(sleeve["target"]), "index": index})
    else:
        label = {"en": "Global 60/40 reference", "es": "Referencia global 60/40"}
        ref = specs.get("reference_60_40") if isinstance(specs.get("reference_60_40"), dict) else {}
        for weight, leg in (("0.6", "equity"), ("0.4", "bonds")):
            index = _index_label((ref.get(leg) or {}).get("name"))
            if index is None:
                return label, []
            parts.append({"weight": V.ratio(weight), "index": index})
    return label, parts


def _plan_names(plans: Any, instruments: Iterable[dict]) -> dict[str, dict]:
    """{plan_id: {en, es}}: the plan's own name, else what it buys and how often ("S&P 500 mensual")."""
    underlying = {str(i.get("id")): str(i.get("underlying_symbol") or i.get("symbol") or i.get("id"))
                  for i in instruments or [] if isinstance(i, dict) and i.get("id")}
    rows = plans.get("plans") if isinstance(plans, dict) else plans if isinstance(plans, list) else [plans]
    names: dict[str, dict] = {}
    for plan in rows or []:
        if not isinstance(plan, dict) or not plan.get("id"):
            continue
        if str(plan.get("name") or "").strip():
            names[str(plan["id"])] = _both(str(plan["name"]).strip()[:80])
            continue
        tracked = []
        for leg in plan.get("legs") or []:
            iid = str((leg or {}).get("instrument_id") or "")
            what = _TRACKS.get(iid.upper()) or _TRACKS.get(underlying.get(iid, "").upper()) or iid
            if what and what not in tracked:
                tracked.append(what)
        if not tracked:
            continue
        what = " + ".join(tracked[:3])
        what_es = re.sub(r"^US total market$", "Mercado total de EE. UU.", what)
        en, es = _CADENCE.get(plan.get("cadence"), ("{x}", "{x}"))
        names[str(plan["id"])] = {"en": en.format(x=what), "es": es.format(x=what_es)}
    return names


def _next_item(candidate: dict, currency: str | None, plan_names: dict[str, dict] | None = None) -> dict:
    from .. import views as V

    kind = candidate.get("kind")
    data = candidate.get("data") or {}
    name = str(data.get("name") or data.get("goal") or data.get("plan_id") or "").strip()
    if kind == "goal_pace" and not name:
        found = re.search(r"to (.+?) or move", str(candidate.get("title") or ""))
        name = found.group(1) if found else ""
    if kind == "dca" and not name:
        found = re.search(r"up the (.+?) plan", str(candidate.get("title") or ""))
        name = found.group(1) if found else ""
    names = (plan_names or {}).get(name) if kind == "dca" else None
    names = names or {"en": name or "—", "es": name or "—"}
    (title_en, title_es), (ask_en, ask_es) = _NEXT.get(kind, ((str(candidate.get("title") or ""),) * 2,
                                                               ("Let's talk about: {t}", "Hablemos de: {t}")))
    words = {"v": _money_words(candidate.get("value")) or "—", "t": candidate.get("title") or ""}
    en, es = {**words, "name": names["en"]}, {**words, "name": names["es"]}
    return {"kind": kind, "title": {"en": title_en.format(**en), "es": title_es.format(**es)},
            "value": V.money(candidate.get("value"), currency),
            "prompt": {"en": ask_en.format(**en), "es": ask_es.format(**es)}}


def _quarter_label(label: str, partial: bool = False) -> dict:
    """How the page names a quarter: "T3 2026" / "Q3 2026", and "T3 2026 · en curso" / "Q3 2026 · to date"."""
    year, q = label.split("-Q")
    return {"en": f"Q{q} {year}" + (" · to date" if partial else ""),
            "es": f"T{q} {year}" + (" · en curso" if partial else "")}


def _latest_evidence(service: Any, client_id: str, ledger_dates: Iterable[date | None], today: date) -> date | None:
    """The latest ledger entry or statement date on or before today (None when there is neither)."""
    days = [d for d in ledger_dates if d]
    try:
        facts = _snapshot(service, client_id)["facts"]
    except Exception:  # noqa: BLE001 - the review still works from the ledger alone
        facts = []
    for fact in facts:
        if fact["key"].startswith("account.") and isinstance(fact.get("value"), dict):
            day = _as_date(fact["value"].get("as_of"))
            if day:
                days.append(day)
    days = [d for d in days if d <= today]
    return max(days, default=None)


def _review_summary(narrative: dict, label: str, partial: bool = False) -> dict:
    """The letter's slot until the model writes it: two plain sentences from ``narrative_inputs``."""
    nw = narrative.get("net_worth") or {}
    cf = narrative.get("cash_flow") or {}
    pf = narrative.get("performance") or {}
    year, q = label.split("-Q")
    when = ({"en": f"Q{q} {year} so far", "es": f"lo que va del T{q} {year}"} if partial else
            {"en": f"Q{q} {year}", "es": f"el T{q} {year}"})
    before = ({"en": "the same stretch of the quarter before", "es": "el mismo tramo del trimestre anterior"}
              if partial else {"en": "the quarter before", "es": "el trimestre anterior"})
    start, end = _money_words(nw.get("start")), _money_words(nw.get("end"))
    contrib, market = _money_words(nw.get("contributions")), _money_words(nw.get("market"))
    if start and end:
        first = {"en": f"Your net worth went from {start} to {end} in {when['en']}"
                       + (f": {contrib} you added and {market} from markets." if contrib and market else "."),
                 "es": f"Tu patrimonio pasó de {start} a {end} en {when['es']}"
                       + (f": {contrib} que aportaste y {market} del mercado." if contrib and market else ".")}
    elif contrib and _dec(nw.get("contributions")) != 0:
        # Without a start and end value a zero here is not a known zero, and the page never guesses why.
        first = {"en": f"You added {contrib} net in {when['en']}; the total change is not known yet.",
                 "es": f"Aportaste {contrib} netos en {when['es']}; el cambio total aún no se sabe."}
    else:
        first = {"en": f"The change in your net worth in {when['en']} is not known yet.",
                 "es": f"Aún no se sabe cuánto cambió tu patrimonio en {when['es']}."}
    rate, change = _pct_words(cf.get("savings_rate"), 0), _dec(cf.get("savings_rate_change"))
    twr, bench = _pct_words(pf.get("twr_period")), _pct_words(pf.get("ips_benchmark") or pf.get("global_60_40"))
    if rate:
        pts = None if change is None or change == 0 else f"{abs(change) * 100:.0f}"
        second_en = f"You saved {rate} of your income" + (
            f", {pts} points {'more' if change > 0 else 'less'} than {before['en']}" if pts else "")
        second_es = f"Ahorraste el {rate} de tu ingreso" + (
            f", {pts} puntos {'más' if change > 0 else 'menos'} que {before['es']}" if pts else "")
        if twr and bench:
            second_en += f"; your portfolio returned {twr} against {bench} for its benchmark."
            second_es += f"; tu portafolio rindió {twr} contra {bench} de su referencia."
        else:
            second_en += "."
            second_es += "."
        second = {"en": second_en, "es": second_es}
    elif twr and bench:
        second = {"en": f"Your portfolio returned {twr} against {bench} for its benchmark.",
                  "es": f"Tu portafolio rindió {twr} contra {bench} de su referencia."}
    else:
        second = {"en": "Your savings rate and returns are not known yet for this quarter.",
                  "es": "Tu tasa de ahorro y tu rendimiento aún no se conocen para este trimestre."}
    return {lang: [first[lang], second[lang]] for lang in ("en", "es")}


def review_view(service: Any, client_id: str, period: str | None = None, today: Any = None,
                inputs: dict | None = None) -> dict:
    """The quarterly letter for one completed quarter (default: the last one), shaped for /review.

    ``inputs`` are extra quarterly_review inputs (prices, benchmarks, fees options) a host may supply.
    """
    from .. import views as V
    from ..store import WealthStore

    today = _today(today)
    with WealthStore(service.db_path) as store:
        ledger = store.ledger(client_id)
    dates = [_as_date(e.get("date")) for e in ledger.get("entries") or []]
    first = min((d for d in dates if d), default=None)
    quarters = review_quarters(first, today)
    # The quarter in progress is offered to date once a statement or ledger entry falls inside it.
    current_label = _quarter_of(today)
    current_start, current_end = quarter_bounds(current_label)
    latest = _latest_evidence(service, client_id, dates, today)
    current = None
    if latest is not None and latest >= current_start:
        current = {"period": current_label, "start": current_start.isoformat(), "end": latest.isoformat(),
                   "quarter_end": current_end.isoformat(), "label": _quarter_label(current_label, partial=True)}
    # Open on the latest ended quarter that has evidence, not an empty one after a quiet stretch.
    with_evidence = [q for q in quarters if latest is not None and quarter_bounds(q)[0] <= latest]
    label = period or (with_evidence[0] if with_evidence else quarters[0] if quarters else
                       current_label if current else _quarter_of(current_start - timedelta(days=1)))
    start, end = quarter_bounds(label)
    partial = label == current_label
    if partial:
        if current is None:
            raise ValueError("Choose a quarter that has ended, or upload a statement from this quarter.")
        end = latest
    elif end >= today:
        raise ValueError("Choose a quarter that has ended.")
    base = {"version": 1, "period": label, "start": start.isoformat(), "end": end.isoformat(), "quarters": quarters,
            "current": current, "partial": partial, "label": _quarter_label(label, partial=partial),
            "quarter_end": quarter_bounds(label)[1].isoformat(), "today": today.isoformat()}
    if first is None or end < first or not (quarters or partial):
        return {**base, "status": "needs_input", "currency": None, "letter": None, "sections": None}
    report = service.run("quarterly_review", {**(inputs or {}), "period_start": start.isoformat(),
                                              "period_end": end.isoformat()}, client_id)
    if report.get("status") == "needs_input":
        return {**base, "status": "needs_input", "currency": None, "letter": None, "sections": None}
    result = report["result"]
    cur = result.get("currency")
    sec = result["sections"]
    money = lambda v: V.money(v, cur)  # noqa: E731
    L = V.L

    nw = sec["net_worth"]["data"]
    income_net = _add((nw.get("growth") or {}).get("investment_income"), (nw.get("growth") or {}).get("fees_and_withholding"))
    ticket_rows = [{"label": L("Start", "Inicio"), "date": V.when(nw.get("opening_date")), "value": money(nw.get("start"))},
                   {"label": L("Net contributions", "Aportaciones netas"), "value": money((nw.get("contributions") or {}).get("net"))},
                   {"label": L("Markets", "Mercado"), "value": money((nw.get("growth") or {}).get("market"))}]
    if _dec(income_net) not in (None, Decimal(0)):
        ticket_rows.append({"label": L("Dividends and interest, less fees", "Dividendos e intereses, menos comisiones"),
                            "value": money(income_net)})
    residual = (nw.get("identity") or {}).get("residual")
    if _dec(residual) not in (None, Decimal(0)):
        # What the flows and market move do not explain, mostly foreign cash moving with the exchange rate.
        ticket_rows.append({"label": L("Exchange rate and other", "Tipo de cambio y otros"), "value": money(residual)})
    net_worth = {"rows": ticket_rows, "total": {"label": L("End", "Cierre"), "date": V.when(nw.get("closing_date")),
                                               "value": money(nw.get("end"))},
                 "note": _note(sec["net_worth"]["missing"]) if nw.get("start") is None or nw.get("end") is None else None}

    pf = sec["performance"]["data"]
    total = pf.get("total") or {}
    bench = (pf.get("benchmarks") or {}).get("ips_benchmark") or {}
    ref = (pf.get("benchmarks") or {}).get("global_60_40") or {}
    chosen_key = ("ips_benchmark" if bench.get("period_return") is not None else
                  "global_60_40" if ref.get("period_return") is not None or not bench else "ips_benchmark")
    chosen = bench if chosen_key == "ips_benchmark" else ref
    market = result.get("market_data") or {}
    bench_label, bench_parts = _benchmark(chosen_key, (sec["allocation"]["data"] or {}).get("sleeves") or [],
                                          (inputs or {}).get("benchmarks") or market.get("benchmarks"))
    performance_view = {
        "portfolio": V.ratio(total.get("twr_period")), "xirr": V.ratio(total.get("xirr_annual")),
        "benchmark": {"label": bench_label, "parts": bench_parts, "value": V.ratio(chosen.get("period_return"))},
        "difference": V.ratio(chosen.get("excess_twr")),
        "note": (_note(sec["performance"]["missing"]) if total.get("twr_period") is None else
                 _note([], "benchmark") if chosen.get("period_return") is None else None)}

    al = sec["allocation"]["data"]
    priced = al.get("portfolio_value") is not None  # weights of a partly priced portfolio would mislead
    sleeves = [{"name": _label(s.get("name") or s.get("sleeve"), s.get("sleeve")), "id": s.get("sleeve"),
                "weight": V.ratio(s.get("weight") if priced else None), "min": V.ratio(s.get("min")), "max": V.ratio(s.get("max")),
                "target": V.ratio(s.get("target")), "outside": priced and bool(s.get("outside_band"))}
               for s in al.get("sleeves") or []]
    has_bands = any(s["min"]["v"] is not None for s in sleeves)
    allocation = {"as_of": al.get("as_of"), "sleeves": sleeves,
                  "note": (_note(sec["allocation"]["missing"]) if al.get("portfolio_value") is None else
                           _note([], "ips") if not has_bands else None)}

    cf = sec["cash_flow"]["data"]

    def flow(block: dict | None) -> dict | None:
        if not block:
            return None
        return {"income": money(block.get("income")), "spending": money(block.get("spending")),
                "net": money(block.get("net")), "savings_rate": V.ratio(block.get("savings_rate"))}
    cash_flow = {"current": flow(cf.get("current")), "prior": flow(cf.get("prior")),
                 "change": V.ratio((cf.get("change") or {}).get("savings_rate")),
                 "note": _note(sec["cash_flow"]["missing"], "flows") if not cf.get("current") or sec["cash_flow"]["missing"] else None}

    gl = sec["goals"]["data"]
    goals = {"items": [{"name": str(g.get("name") or g.get("id") or ""), "funded": V.ratio(g.get("funded_pct")),
                        "target": V.money(g.get("target"), g.get("currency") or cur), "by": V.when(g.get("target_date")),
                        "status": g.get("status") if g.get("status") in {"on_track", "behind", "funded", "unknown"} else "unknown"}
                       for g in gl.get("goals") or []],
             "note": _note(sec["goals"]["missing"], "goal") if sec["goals"]["missing"] else None}

    dc = sec["decisions"]["data"]
    decisions = {"items": [{"what": str(d.get("what") or "")[:200], "status": d.get("status"),
                            "on": V.when(d.get("decided_on") or d.get("proposed_on")),
                            "trades": V.count((d.get("what_happened") or {}).get("trades")),
                            "invested": money((d.get("what_happened") or {}).get("net_invested"))}
                           for d in dc.get("decisions") or []],
                 "open_before": len(dc.get("still_open_from_before") or []), "note": None}

    plan_names = _plan_names((inputs or {}).get("dca_plans") or (_facts_by_key(_snapshot(service, client_id))
                                                                     .get("planning.dca") or {}).get("value"),
                             ledger.get("instruments") or [])
    dca = {"items": [{"name": plan_names.get(str(p.get("plan_id") or "")) or _both(_humanize(str(p.get("plan_id") or ""))),
                      "on_time": V.count((p.get("counts") or {}).get("on_time")),
                      "installments": V.count(p.get("installments")), "rate": V.ratio(p.get("on_time_rate")),
                      "invested": V.money(p.get("invested"), p.get("currency") or cur),
                      "planned": V.money(p.get("planned"), p.get("currency") or cur),
                      "missed": [V.when(d) for d in (p.get("missed") or [])[:3]]}
                     for p in sec["dca"]["data"].get("plans") or []],
           "note": _note(sec["dca"]["missing"]) if sec["dca"]["missing"] else None}

    tx = sec["taxes"]["data"]
    tax_period = ((tx.get("period") or {}).get("estimated_tax") or {}).get("total")
    tax_ytd = ((tx.get("year_to_date") or {}).get("estimated_tax") or {}).get("total")
    taxes = {"jurisdiction": tx.get("jurisdiction"), "period": money(tax_period), "ytd": money(tax_ytd),
             "note": _note(sec["taxes"]["missing"]) if tax_period is None or tax_ytd is None else None}

    fe = sec["fees"]["data"]
    annual = fe.get("annual_cost") or {}
    fees = {"paid": money((fe.get("paid_in_period") or {}).get("total")),
            "annual": V.span(annual.get("low"), annual.get("high"), cur),
            "bps": {"lo": None if _dec(annual.get("bps_low")) is None else str(annual["bps_low"]),
                    "hi": None if _dec(annual.get("bps_high")) is None else str(annual["bps_high"])},
            "complete": bool(annual.get("complete")),
            "note": None if annual.get("complete") and annual.get("low") is not None else _note([], "floor")}

    nq = sec["next_quarter"]["data"]
    next_quarter = {"items": [_next_item(c, cur, plan_names) for c in (nq.get("two_decisions") or [])[:2]], "note": None}

    ticket_spec = {"id": "review-" + "0" * 10, "kind": "ticket", "title": L("Net worth", "Patrimonio"),
                   "data": {"rows": [{k: v for k, v in r.items() if k != "date"} for r in ticket_rows],
                            "total": {"label": net_worth["total"]["label"], "value": net_worth["total"]["value"]}},
                   "source": {"task": "quarterly_review", "label": L("Quarterly review", "Revisión trimestral")}}
    V.validate(ticket_spec)  # the identity ticket is a view primitive; its shape must stay drawable anywhere

    return {**base, "status": report.get("status"), "currency": cur,
            "name": (result.get("narrative_inputs") or {}).get("name"),
            "letter": {"narrative": None,
                       "summary": _review_summary(result.get("narrative_inputs") or {}, label, partial)},
            "sections": {"net_worth": net_worth, "performance": performance_view, "allocation": allocation,
                         "cash_flow": cash_flow, "goals": goals, "decisions": decisions, "dca": dca, "taxes": taxes,
                         "fees": fees, "next_quarter": next_quarter},
            # Market data the provider supplied (none when the host passed its own prices): every source with
            # its date, the benchmark proxies (named as proxies), and prices too old to be current.
            "market_data": {"sources": [{"source": p.get("source"), "date": p.get("last") or p.get("date"),
                                         "symbol": p.get("instrument_id")} for p in market.get("prices") or []],
                            "proxies": market.get("proxies") or [], "offline": market.get("offline")}
            if market else None,
            "stale_prices": market.get("stale_prices") or []}


# ---- connections

_CONNECTOR_ORDER = ("ibkr_flex", "alpaca", "cuenca")
_STALE_DAYS = 35


def _connector_setup(name: str) -> dict:
    """The exact keychain commands from docs/cli.md (the page shows them; it never takes a secret)."""
    from ..connectors import alpaca, cuenca, ibkr_flex

    if name == "ibkr_flex":
        return {"commands": [f'security add-generic-password -U -s {ibkr_flex.KEYCHAIN_SERVICE} -a "$USER" -w'],
                "env": [ibkr_flex.TOKEN_ENV], "needs": ["query_id"]}
    if name == "alpaca":
        return {"commands": [f"security add-generic-password -U -s {alpaca.KEYCHAIN_SERVICE} -a key_id -w",
                             f"security add-generic-password -U -s {alpaca.KEYCHAIN_SERVICE} -a secret -w"],
                "env": [alpaca.KEY_ENV, alpaca.SECRET_ENV], "needs": []}
    return {"commands": [f"security add-generic-password -U -s {cuenca.KEYCHAIN_SERVICE} -a api_key -w",
                         f"security add-generic-password -U -s {cuenca.KEYCHAIN_SERVICE} -a api_secret -w"],
            "env": [cuenca.KEY_ENV, cuenca.SECRET_ENV], "needs": []}


def _account_freshness(service: Any, client_id: str, today: date) -> list[dict]:
    """Every known account with the date its data runs to (statement date, else its latest ledger line)."""
    from ..store import WealthStore

    with WealthStore(service.db_path) as store:
        ledger = store.ledger(client_id)
    latest: dict[str, date] = {}
    for entry in ledger.get("entries") or []:
        day = _as_date(entry.get("date"))
        account = entry.get("account_id")
        if day and account and (account not in latest or day > latest[account]):
            latest[account] = day
    rows: dict[str, dict] = {}
    try:
        sit = service.situation(client_id, today=today) if hasattr(service, "situation") else {}
    except Exception:  # noqa: BLE001 - freshness is best effort; the ledger still answers
        sit = {}
    for account in (sit or {}).get("accounts") or []:
        if not account.get("id"):
            continue
        # Raw account types ('checking', 'roth_ira') never reach the page: the type in plain words, or the currency.
        rows[str(account["id"])] = {"id": str(account["id"]), "label": account_label(account, "en")
                                    if not account.get("institution") else str(account["institution"]) + (
                                        f" ({account['currency']})" if account.get("currency") else ""),
                                    "institution": account.get("institution"), "as_of": _as_date(account.get("as_of"))}
    for account in ledger.get("accounts") or []:
        aid = str(account.get("id") or "")
        if not aid:
            continue
        row = rows.setdefault(aid, {"id": aid, "label": str(account.get("institution") or aid),
                                    "institution": account.get("institution"), "as_of": None})
        if row["as_of"] is None or (latest.get(aid) and latest[aid] > row["as_of"]):
            row["as_of"] = latest.get(aid) or row["as_of"]
    out = []
    names: dict[str, int] = {}
    for row in rows.values():
        # The institution is the name a person uses; two accounts at one institution keep their own labels.
        if row["institution"]:
            names[str(row["institution"])] = names.get(str(row["institution"]), 0) + 1
    for row in rows.values():
        days = (today - row["as_of"]).days if row["as_of"] else None
        label = str(row["institution"]) if row["institution"] and names[str(row["institution"])] == 1 else row["label"]
        out.append({"id": row["id"], "label": label[:80], "institution": row["institution"],
                    "as_of": row["as_of"].isoformat() if row["as_of"] else None, "days": days,
                    "stale": days is None or days > _STALE_DAYS})
    out.sort(key=lambda r: (r["label"].lower(), r["id"]))
    return out


def connections_view(service: Any, client_id: str, today: Any = None) -> dict:
    """One row per read-only connector plus the statement row, with per-account freshness; never a secret."""
    import shlex

    today = _today(today)
    status = service.ingest(client_id, "connector_status", {})
    by_name = {row["name"]: row for row in (status.get("result") or {}).get("connectors") or []}
    accounts = _account_freshness(service, client_id, today)
    claimed: set[str] = set()
    rows = []
    for name in _CONNECTOR_ORDER:
        entry = by_name.get(name)
        if entry is None:
            continue
        institution = str(entry.get("institution") or name)
        key = institution.split()[0].lower()
        mine = [a for a in accounts if key in str(a.get("institution") or a["label"]).lower()]
        claimed.update(a["id"] for a in mine)
        last = entry.get("last_sync") or {}
        token = entry.get("token")
        rows.append({"name": name, "institution": institution, "configured": bool(entry.get("ready")),
                     "via": token if token in {"keychain", "env"} else None,
                     "last_sync": str(last.get("pulled_at"))[:25] if last.get("pulled_at") else None,
                     "accounts": mine, "setup": _connector_setup(name)})
    statements = [a for a in accounts if a["id"] not in claimed]
    return {"today": today.isoformat(), "connectors": rows, "statements": {"accounts": statements},
            "data": {"export": True, "forget_command": f"uv run wealth forget --client {shlex.quote(client_id)}"}}


def export_payload(service: Any, client_id: str) -> dict:
    """The full private export (the same one ``wealth client`` action export writes)."""
    return service.inspect(client_id, detail="export")
