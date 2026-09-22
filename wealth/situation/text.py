"""Deterministic text from a built situation: the per-turn brief and memory sentences.

No model is involved.  ``brief`` is numbers and labels only (never advice) and
at most ``BRIEF_MAX_LINES`` lines.  ``sentences`` states what Wealth knows as
short natural sentences in Spanish or English, each with emphasis spans, the
fact key it came from, its source, age and staleness.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Any, Mapping

from .model import humanize, kind_family

BRIEF_MAX_LINES = 15
_MONTHS = {
    "es": ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
           "noviembre", "diciembre"],
    "en": ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
           "November", "December"],
}
_COUNTRY_NAMES = {"es": {"MX": "México", "US": "Estados Unidos", "CA": "Canadá", "ES": "España"},
                  "en": {"MX": "Mexico", "US": "the United States", "CA": "Canada", "ES": "Spain"}}


def _lang(language: str | None) -> str:
    return "es" if str(language or "").lower().startswith("es") else "en"


def _whole(value: Any, places: int | None = 0) -> str:
    return fmt(value, places)


def fmt(value: Any, places: int | None = None) -> str:
    """1234567.5 -> '1,234,567.50'; whole numbers without decimals."""
    if value is None:
        return "?"
    number = Decimal(str(value))
    if places is None:
        places = 0 if number == number.to_integral_value() else 2
    return f"{number:,.{places}f}"


def units(value: Any) -> str:
    """Share quantities keep their fractions: 10.5 -> '10.5', 0.12345 -> '0.1235', 1200 -> '1,200'."""
    if value is None:
        return "?"
    number = Decimal(str(value)).quantize(Decimal("0.0001"))
    text = f"{number:,.4f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def pct(rate: Any) -> str:
    text = f"{Decimal(str(rate)) * 100:.2f}".rstrip("0").rstrip(".")
    return f"{text}%"


def _month_year(iso: str | None, lang: str) -> str:
    if not iso:
        return "?"
    year, month = int(iso[:4]), int(iso[5:7])
    name = _MONTHS[lang][month - 1]
    return f"{name} de {year}" if lang == "es" else f"{name} {year}"


# ------------------------------------------------------------------ brief

_B = {
    "es": {"empty": "Situación: sin datos guardados (primera conversación).", "head": "Situación al {d} ({c})",
           "nw": "Patrimonio neto {t} = líquido {l} + ilíquido {i} − deudas {o}", "nw_none": "Patrimonio neto: desconocido",
           "nw_unknown": "Patrimonio neto sin contar {x}: {k} (esos saldos no son cero; pregúntalos); deudas {o}",
           "unconv": "sin convertir {x}", "unvalued": "sin valuar {x}",
           "flow": "Mes: ingreso {i}{net} − gasto {s} ({src}) − deudas {d} = excedente {x}", "net": " neto",
           "src_stated": "declarado", "src_ledger": "movimientos {n} meses", "essential_only": ", solo esenciales; el excedente aún cubre otros gastos", "without": " (falta el pago de {x}; antes de ese pago quedan {k}; pregúntalo)",
           "commit": "Excedente comprometido {t} ({items}); sin asignar {u}", "over": " · sobrecomprometido",
           "reserve": "Reserva {a} = {m} meses de gasto {b}; meta {t}", "unset": "sin definir",
           "r_cash": "efectivo", "inside": " (no sé si el pago de {x} ya está dentro del gasto: el excedente es {lo} si no lo está, {hi} si sí; pregúntalo)",
           "b_essential": "esencial", "b_total": "total",
           "debt": "Deuda {n}: {b} al {r}; pago {p}/mes; {when}", "paid": "liquida {d}", "missing": "falta {x}",
           "never": "no se liquida con ese pago", "interest": ", intereses {x}",
           "goal": "Meta {n}: {detail} ({st})", "per_month": "{x}/mes", "target": "{x} para {d}",
           "inv": "Inversiones: {x}", "cash": "Efectivo: {x}", "invalid": "Datos guardados que no se pudieron leer (pide el valor correcto): {x}", "pos": "Posiciones: {x}", "units": "títulos", "top": "mayor exposición {u} {w} ({s})",
           "diff": "Diferencia {inst}: dijiste {s}; estado {x} ({d})",
           "thread": "Pendiente [{k}, {d}]: {t}", "k_advice": "consejo", "k_question": "pregunta", "k_commitment": "compromiso",
           "stale": "Por reconfirmar (confirma el valor, no lo pidas de nuevo): {x}", "inferred": "Sin confirmar: {x}", "unknown": "Desconocido: {x}",
           "changes": "Cambios desde r{r}: {x}", "more": "+{n} más", "st_active": "activa", "st_paused": "pausada",
           "st_done": "cumplida", "st_dropped": "descartada", "c_goal": "metas", "c_dca": "planes periódicos",
           "m_rate": "tasa", "m_payment": "pago o plazo", "u_income": "ingreso mensual", "u_spending": "gasto mensual",
           "u_fx": "tipo de cambio {p}", "u_tax_residence": "residencia fiscal no declarada (vive en {c})",
           "u_reserve_target": "meta de reserva", "u_goal_amount": "monto de «{g}»"},
    "en": {"empty": "Situation: nothing saved yet (first conversation).", "head": "Situation on {d} ({c})",
           "nw": "Net worth {t} = liquid {l} + illiquid {i} − debts {o}", "nw_none": "Net worth: unknown",
           "nw_unknown": "Net worth excluding {x}: {k} (those balances are not zero; ask for them); debts {o}",
           "unconv": "unconverted {x}", "unvalued": "unvalued {x}",
           "flow": "Month: income {i}{net} − spending {s} ({src}) − debt payments {d} = surplus {x}", "net": " net",
           "src_stated": "stated", "src_ledger": "{n} months of transactions", "essential_only": ", essentials only; the surplus still covers other spending", "without": " (the {x} payment is missing; {k} before it; ask for it)",
           "commit": "Surplus committed {t} ({items}); unallocated {u}", "over": " · overcommitted",
           "reserve": "Reserve {a} = {m} months of {b} spending; target {t}", "unset": "not set",
           "r_cash": "cash", "inside": " (unknown whether the {x} payment is already inside spending: surplus {lo} if not, {hi} if so; ask)",
           "b_essential": "essential", "b_total": "total",
           "debt": "Debt {n}: {b} at {r}; payment {p}/month; {when}", "paid": "paid off {d}", "missing": "missing {x}",
           "never": "never at this payment", "interest": ", interest {x}",
           "goal": "Goal {n}: {detail} ({st})", "per_month": "{x}/month", "target": "{x} by {d}",
           "inv": "Investments: {x}", "cash": "Cash: {x}", "invalid": "Saved data that could not be read (ask for the correct value): {x}", "pos": "Positions: {x}", "units": "units", "top": "largest exposure {u} {w} ({s})",
           "diff": "Difference {inst}: stated {s}; statement {x} ({d})",
           "thread": "Open [{k}, {d}]: {t}", "k_advice": "advice", "k_question": "question", "k_commitment": "commitment",
           "stale": "Reconfirm (read the value back, do not ask afresh): {x}", "inferred": "Unconfirmed: {x}", "unknown": "Unknown: {x}",
           "changes": "Changed since r{r}: {x}", "more": "+{n} more", "st_active": "active", "st_paused": "paused",
           "st_done": "done", "st_dropped": "dropped", "c_goal": "goals", "c_dca": "recurring plans",
           "m_rate": "rate", "m_payment": "payment or term", "u_income": "monthly income", "u_spending": "monthly spending",
           "u_fx": "{p} rate", "u_tax_residence": "tax residence not stated (lives in {c})",
           "u_reserve_target": "reserve target", "u_goal_amount": "amount for “{g}”"},
}
_KIND = {"es": {"auto": "auto", "mortgage": "hipoteca", "card": "tarjeta", "personal": "personal",
                "student": "educativo", "other": "préstamo"},
         "en": {"auto": "car", "mortgage": "mortgage", "card": "card", "personal": "personal",
                "student": "student", "other": "loan"}}


_FREQUENCY = {"es": {"monthly": " al mes", "biweekly": " cada dos semanas", "annual": " al año", "weekly": " a la semana"},
              "en": {"monthly": " a month", "biweekly": " every two weeks", "annual": " a year", "weekly": " a week"}}
_KEY_NAMES = {"es": {"income": "ingreso", "spending": "gasto mensual", "client.profile": "perfil", "goals": "metas",
                     "reserve": "reserva", "cash": "efectivo", "investment": "inversión", "liability": "deuda",
                     "policy.ips": "política de inversión", "planning.dca": "plan de inversión periódica",
                     "tax.profile": "perfil fiscal", "household": "cuentas guardadas", "account": "cuenta"},
              "en": {"income": "income", "spending": "monthly spending", "client.profile": "profile", "goals": "goals",
                     "reserve": "reserve", "cash": "cash", "investment": "investment", "liability": "debt",
                     "policy.ips": "investment policy", "planning.dca": "recurring investment plan",
                     "tax.profile": "tax profile", "household": "saved accounts", "account": "account"}}


def _stale_name(row: Mapping[str, Any], sit: Mapping[str, Any], lang: str) -> str:
    """The person's name for a stale fact ("BBVA México", "ingreso (salary)"), never its raw key."""
    key = str(row.get("key") or "")
    names = _KEY_NAMES[lang]
    if row.get("name"):
        return str(row["name"])
    if key in names:
        return names[key]
    head, _, rest = key.partition(".")
    if head in ("income", "spending"):
        return names[head]
    if head == "liability":
        found = next((r for r in sit.get("liabilities") or [] if r.get("key") == key), None)
        return liability_name(found, lang) if found else names["liability"]
    if head in names:
        return f"{names[head]} {humanize(rest)}".strip() if rest else names[head]
    return humanize(key)


def _native(amounts: Mapping[str, Any] | None) -> str:
    return " + ".join(f"{fmt(v)} {c}" for c, v in sorted((amounts or {}).items()))


def liability_name(row: Mapping[str, Any], lang: str) -> str:
    name = _KIND[lang].get(row.get("kind") or "other", _KIND[lang]["other"])
    if row.get("kind") in (None, "other") and row.get("name"):
        name = row["name"]
    return f"{name} ({row['lender']})" if row.get("lender") else name


def brief(sit: Mapping[str, Any], language: str | None = None) -> str:
    """At most 15 lines of numbers and labels for the per-turn prompt (whole currency units)."""
    lang = _lang(language)
    fmt = _whole
    t = _B[lang]
    nw, flow, reserve = sit["net_worth"], sit["cash_flow"], sit["reserve"]
    has_anything = any([sit["income"]["items"], sit["income"]["extras"], sit["spending"]["monthly"] is not None,
                        sit["cash"], sit["accounts"], sit["investments"], sit["liabilities"], sit["goals"],
                        sit["threads"]["open"], sit["profile"].get("residence")])
    if not has_anything:
        return t["empty"]
    cur = sit.get("currency") or "?"
    lines: list[tuple[int, str]] = [(0, t["head"].format(d=sit["as_of"], c=cur))]
    if nw["total"] is not None:
        line = t["nw"].format(t=fmt(nw["total"]), l=fmt(nw["liquid"] or 0), i=fmt(nw["illiquid"] or 0),
                              o=fmt(nw["liabilities"] or 0))
        extra = []
        if nw["unconverted"]:
            extra.append(t["unconv"].format(x=", ".join(f"{fmt(u['amount'])} {u['currency']}" for u in nw["unconverted"])))
        if nw["unvalued_accounts"]:
            extra.append(t["unvalued"].format(x=", ".join(nw["unvalued_accounts"])))
        lines.append((1, line + ("; " + "; ".join(extra) if extra else "")))
    else:
        lines.append((1, t["nw_unknown"].format(x=", ".join(nw["unknown_balances"]), k=fmt(nw.get("known_total")),
                                                o=fmt(nw["liabilities"] or 0))
                      if nw.get("unknown_balances") else t["nw_none"]))
    if flow["income"] is not None or flow["spending"] is not None:
        src = sit["spending"]["source"]
        src_text = t["src_ledger"].format(n=sit["spending"].get("ledger_months")) if src == "ledger" else t["src_stated"]
        if sit["spending"].get("monthly_basis") == "essential":
            src_text += t["essential_only"]
        debts = fmt(flow["debt_payments_known"]) if not flow["debt_payments_unknown"] else (
            f"{fmt(flow['debt_payments_known'])}+?" if flow["debt_payments_known"] else "?")
        line = t["flow"].format(i=fmt(flow["income"]), net=t["net"] if sit["income"].get("net") else "",
                                s=fmt(flow["spending"]), src=src_text, d=debts, x=fmt(flow["surplus"]))
        before = flow.get("surplus_before_unknown_debts")
        if flow["debt_payments_unknown"] and before is not None:
            names = [liability_name(r, lang) for r in sit["liabilities"] if r["id"] in flow["debt_payments_unknown"]]
            line += t["without"].format(x=", ".join(names), k=fmt(before))
        span = flow.get("surplus_range")
        if flow.get("in_spending_unknown") and span:
            names = [liability_name(r, lang) for r in sit["liabilities"] if r["id"] in flow["in_spending_unknown"]]
            line += t["inside"].format(x=", ".join(names), lo=fmt(span["low"]), hi=fmt(span["high"]))
        lines.append((2, line))
    commitments = sit["commitments"]
    if commitments["items"]:
        by_kind: dict[str, Decimal] = {}
        for item in commitments["items"]:
            by_kind[item["kind"]] = by_kind.get(item["kind"], Decimal(0)) + Decimal(str(item["monthly"]))
        items = ", ".join(f"{t['c_' + kind]} {fmt(value)}" for kind, value in sorted(by_kind.items()))
        line = t["commit"].format(t=fmt(commitments["total"]), items=items, u=fmt(commitments["unallocated"]))
        lines.append((3, line + (t["over"] if commitments["overcommitted"] else "")))
    if reserve["amount"] is not None:
        target = fmt(reserve["target_months"]) + (" m" if lang == "en" else " m") if reserve["target_months"] is not None \
            else fmt(reserve["target_amount"]) if reserve["target_amount"] is not None else t["unset"]
        parts = reserve.get("parts") or []
        amount = fmt(reserve["amount"])
        if len(parts) > 1 or any(p["kind"] == "instrument" for p in parts):
            # What it is made of: "60,000 efectivo + 180,000 CETES = 240,000".
            amount = " + ".join(f"{fmt(p['value'])} {t['r_cash'] if p['kind'] == 'cash' else p['label']}"
                                for p in parts) + f" = {amount}"
        lines.append((4, t["reserve"].format(a=amount, m=fmt(reserve["months"], 1) if reserve["months"] is not None else "?",
                                             b=t["b_" + (reserve["spending_basis"] or "total")], t=target)))
    for index, row in enumerate(sit["liabilities"]):
        if index == 2:
            lines.append((5, t["more"].format(n=len(sit["liabilities"]) - 2)))
            break
        plan = row["payoff"]
        if row["missing"]:
            when = t["missing"].format(x=", ".join(t["m_rate"] if m == "annual_rate" else t["m_payment"] for m in row["missing"]))
        elif plan["status"] == "never":
            when = t["never"]
        else:
            when = t["paid"].format(d=plan.get("date") or "?") + t["interest"].format(x=fmt(plan.get("interest")))
        lines.append((5, t["debt"].format(n=liability_name(row, lang), b=f"{fmt(row['balance'])} {row['currency']}",
                                          r=pct(row["annual_rate"]) if row["annual_rate"] is not None else "?",
                                          p=fmt(row["monthly_payment"]), when=when)))
    active = [g for g in sit["goals"] if g["status"] in ("active", "paused")]
    for index, goal in enumerate(active):
        if index == 2:
            lines.append((6, t["more"].format(n=len(active) - 2)))
            break
        parts = []
        if goal["monthly_contribution"] is not None:
            parts.append(t["per_month"].format(x=fmt(goal["monthly_contribution"])))
        if goal["target_amount"] is not None:
            parts.append(t["target"].format(x=fmt(goal["target_amount"]), d=goal["target_date"] or "?"))
        lines.append((6, t["goal"].format(n=goal["name"][:60], detail="; ".join(parts) or "?", st=t["st_" + goal["status"]])))
    investing, cash_line = [], []
    for row in sit["cash"]:
        if row["counted"] and row["value"] is not None:
            cash_line.append(f"{row.get('institution') or row.get('name') or humanize(row['id'])} {fmt(row['value'])}")
    for account in sit["accounts"]:
        if account["source"] == "ledger" or account.get("superseded_by"):
            continue
        value = fmt(account["value"]) if account["value"] is not None else _native(account["native"])
        text = f"{account['label']} {value}" + (f" ({account['as_of']})" if account.get("as_of") else "")
        # A checking or savings account is cash, never an investment.
        (cash_line if kind_family(account.get("type")) == "cash" else investing).append(text)
    if cash_line:
        lines.append((7, t["cash"].format(x="; ".join(cash_line[:4]))))
    for item in sit["investments"]:
        if item["counted"] and (item["value"] is not None or item["amount"] is not None):  # unknowns: on the net worth line
            investing.append(f"{item.get('institution') or item.get('name') or item['id']} {fmt(item['value'] if item['value'] is not None else item['amount'])}")
    holdings = sit["holdings"]
    if holdings["top"] and holdings["top"][0]["weight"] is not None:
        top = holdings["top"][0]
        symbols = [s for s in top["symbols"] if s != top["underlying"]]
        line = t["top"].format(u=_clean_symbol(top["underlying"]), w=pct(round(Decimal(str(top["weight"])), 2)), s=", ".join(symbols))
        investing.append(line.replace(" ()", ""))
    if investing:
        lines.append((7, t["inv"].format(x="; ".join(investing[:4]))))
    positions = []
    for row in holdings.get("largest") or []:
        amount = f"{units(row['quantity'])} {t['units']} " if row.get("quantity") is not None else ""
        place = f" ({row['institution']})" if row.get("institution") else ""
        positions.append(f"{row['symbol']} {amount}{fmt(row['value'])}{place}")
    if positions:
        # The saved holdings themselves, so the adviser never asks what is already known.
        lines.append((7, t["pos"].format(x="; ".join(positions))))
    for diff in sit["differences"][:1]:
        if diff.get("statement") and diff.get("stated"):
            lines.append((8, t["diff"].format(inst=diff["institution"] or "", s=f"{fmt(diff['stated']['amount'])} {diff['stated']['currency']}",
                                              x=_native(diff["statement"]), d=diff.get("as_of") or "?")))
    for index, thread in enumerate(sit["threads"]["open"]):
        if index == 3:
            lines.append((9, t["more"].format(n=len(sit["threads"]["open"]) - 3)))
            break
        lines.append((9, t["thread"].format(k=t.get("k_" + str(thread["kind"]), thread["kind"]), d=thread["created"] or "?",
                                            t=" ".join(thread["text"].split())[:160])))
    unknown = []
    stale_keys = set(sit.get("stale") or [])
    # A value waiting to be reconfirmed is not unknown: it appears on the reconfirm line instead.
    covered = {"income": any(k.startswith("income.") for k in stale_keys),
               "spending": bool(stale_keys & {"spending.monthly", "plan.resources", "client.profile"})}
    for item in sit["unknowns"]:
        if item["code"].startswith("liability") or covered.get(item["code"]):
            continue  # already on the debt line, or on the reconfirm line
        country = _COUNTRY_NAMES[lang].get(item.get("residence") or "", item.get("residence"))
        unknown.append(t["u_" + item["code"]].format(c=country, p=item.get("pair"), g=item.get("goal")))
    if unknown:
        lines.append((10, t["unknown"].format(x="; ".join(unknown[:4]))))
    if sit.get("invalid_facts"):
        lines.append((4, t["invalid"].format(x=", ".join(i["key"] for i in sit["invalid_facts"][:4]))))
    if sit["stale"]:
        stale = sit.get("stale_values") or [{"key": k} for k in sit["stale"]]
        shown = []
        for row in stale:
            if str(row["key"]).startswith("account.") and str(row["key"]).count(".") > 1:
                continue  # a statement's activity is part of its account, never a line of its own
            text = _stale_name(row, sit, lang)
            if row.get("amount") is not None:
                text += f" = {fmt(row['amount'])} {row.get('currency') or ''}".rstrip()
                text += _FREQUENCY[lang].get(row.get("frequency") or "", "")
            if row.get("observed_on"):
                text += f" ({row['observed_on']})"
            if text not in shown:
                shown.append(text)
        if shown:
            lines.append((11, t["stale"].format(x="; ".join(shown[:6]))))
    if sit["inferred"]:
        lines.append((12, t["inferred"].format(x=", ".join(sit["inferred"][:6]))))
    from ..onboarding import brief_line  # lazy: onboarding imports this package's schema
    setup = brief_line(sit)
    if setup:
        lines.append((3, setup))
    if sit.get("changes"):
        lines.append((13, t["changes"].format(r=sit.get("changes_since"), x=", ".join(c["key"] for c in sit["changes"][:6]))))
    while len(lines) > BRIEF_MAX_LINES:
        worst = max(range(len(lines)), key=lambda i: (lines[i][0], i))
        lines.pop(worst)
    return "\n".join(text for _, text in lines)


# ------------------------------------------------------------------ sentences


class _Emph(str):
    """A value to emphasise inside a sentence."""


def js_length(text: str) -> int:
    """Length in UTF-16 code units, the unit JavaScript slices by (an emoji counts two, "é" one)."""
    return len(text.encode("utf-16-le")) // 2


def js_span(text: str, start: int, end: int) -> list[int]:
    """A Python [start, end) span of ``text`` as the page's JavaScript offsets."""
    return [js_length(text[:start]), js_length(text[:end])]


def _compose(template: str, **values: Any) -> tuple[str, list[list[int]]]:
    """Fill ``template``; spans of emphasised values are in UTF-16 units, as the page slices them."""
    out, spans, pos = [], [], 0
    for match in re.finditer(r"\{(\w+)\}", template):
        out.append(template[pos:match.start()])
        value = values[match.group(1)]
        start = sum(js_length(p) for p in out)
        out.append(str(value))
        if isinstance(value, _Emph):
            spans.append([start, start + js_length(str(value))])
        pos = match.end()
    out.append(template[pos:])
    text = "".join(out)
    text = text[:1].upper() + text[1:]  # one code point either way: the offsets are unchanged
    return text, spans


def _money_text(value: Any, currency: str | None, show_code: bool) -> str:
    """$85,000 · $1,234.50 · $217,838 (cents only below a thousand; notes, not ledgers)."""
    number = Decimal(str(value))
    places = 0 if abs(number) >= 1000 else None
    return f"${fmt(number.quantize(Decimal(1)) if places == 0 else number, places)}" + (
        f" {currency}" if show_code and currency else "")


def _amount(value: Any, currency: str | None, show_code: bool) -> _Emph:
    return _Emph(_money_text(value, currency, show_code))


def _instrument(name: str, lang: str) -> str:
    """An instrument as people say it, with the article Spanish and English need for index names and bonds."""
    clean = _clean_symbol(name)
    if re.match(r"(?i)^(s&p|nasdaq|dow|udibono|bono|cetes|ipc)\b", clean):
        return ("el " if lang == "es" else "the ") + clean
    return clean


def _clean_symbol(name: str) -> str:
    """'S UDIBONO 351122' -> 'Udibono 351122': a series letter and shouting are not how people say it."""
    name = re.sub(r"^[SMB]\s+(?=(?:UDIBONO|BONO|CETES)\b)", "", str(name).strip())
    # Government series are named by maturity (YYMMDD); people say the year: "Udibono 2035".
    name = re.sub(r"\b(UDIBONO|BONO)\s+(\d{2})\d{4}\b", lambda m: f"{m.group(1)} 20{m.group(2)}", name)
    return re.sub(r"\b(UDIBONO|BONO|CETES)\b", lambda m: m.group(1).capitalize() if m.group(1) != "CETES" else "Cetes", name)


_INCOME_KIND = {
    "es": {"aguinaldo": "de aguinaldo", "ptu": "de PTU", "bonus": "de bono", "rent": "de rentas",
           "business": "de tu negocio", "pension": "de pensión"},
    "en": {"aguinaldo": "in aguinaldo", "ptu": "in profit sharing (PTU)", "bonus": "in bonuses", "rent": "in rent",
           "business": "from your business", "pension": "in pension"},
}
_DEBT_ES = {"auto": "del crédito del coche", "mortgage": "de la hipoteca", "card": "de la tarjeta de crédito",
            "personal": "del préstamo personal", "student": "del crédito educativo", "other": "del préstamo"}
_DEBT_EN = {"auto": "car loan", "mortgage": "mortgage", "card": "credit card", "personal": "personal loan",
            "student": "student loan", "other": "loan"}
_DROP = {"es": {"sell": "venderías", "hold": "esperarías sin vender", "buy_more": "comprarías más"},
         "en": {"sell": "you would sell", "hold": "you would hold", "buy_more": "you would buy more"}}
_EXPERIENCE = {"es": {"none": "Aún no tienes experiencia invirtiendo.", "some": "Tienes algo de experiencia invirtiendo.",
                      "experienced": "Tienes mucha experiencia invirtiendo."},
               "en": {"none": "You're new to investing.", "some": "You have some investing experience.",
                      "experienced": "You're an experienced investor."}}


def sentences(sit: Mapping[str, Any], language: str | None = None) -> list[dict]:
    """What Wealth knows, as short natural sentences grouped by topic (about, money_in, ...)."""
    lang = _lang(language)
    es = lang == "es"
    meta = sit.get("meta") or {}
    reporting = sit.get("currency")
    out: list[dict] = []

    def add(topic: str, key: str | None, template: str, *, ref: str | None = None, extra: Mapping[str, Any] | None = None,
            **values: Any) -> None:
        # ``ref`` names the item inside the fact (a field, list item or goal id) so the page can edit
        # or forget exactly that thing; ``extra`` carries display context (institution, statement date).
        text, spans = _compose(template, **values)
        info = meta.get(key or "", {})
        out.append({"topic": topic, "text": text, "emphasis": spans, "key": key, "ref": ref,
                    "source": info.get("source"), "age_days": info.get("age_days"), "stale": bool(info.get("stale")),
                    "unconfirmed": bool(info.get("inferred")), **(extra or {})})

    def about(approximate: bool) -> str:
        return ("unos " if es else "about ") if approximate else ""

    def code(currency: str | None) -> bool:
        return currency != reporting

    profile = sit["profile"]
    key = profile.get("key")
    if profile.get("name"):
        add("about", key, "Te llamas {n}." if es else "You go by {n}.", ref="name", n=_Emph(profile["name"]))
    residence = profile.get("residence") or {}
    if residence:
        country = _COUNTRY_NAMES[lang].get(residence.get("country"), residence.get("country") or "")
        local = [p for p in (residence.get("city"), residence.get("region") if residence.get("region") != residence.get("city") else None) if p]
        if local:
            country = country.removeprefix("the ")  # "Texas, United States", not "Texas, the United States"
        place = ", ".join(p for p in (*local, country) if p)
        if place:
            add("about", key, "Vives en {p}." if es else "You live in {p}.", ref="residence", p=_Emph(place))
    if profile.get("tax_residence"):
        names = [_COUNTRY_NAMES[lang].get(c, c) for c in profile["tax_residence"]]
        joined = (" y " if es else " and ").join(names)
        add("about", key, "Eres residente fiscal en {c}." if es else "You're a tax resident of {c}.", ref="tax_residence", c=_Emph(joined))
    if profile.get("birth_year"):
        add("about", key, "Naciste en {y}." if es else "You were born in {y}.", ref="birth_year", y=_Emph(str(profile["birth_year"])))
    dependents = profile.get("dependents")
    if dependents is not None:
        if dependents == 0:
            add("about", key, "Nadie depende económicamente de ti." if es else "No one depends on you financially.", ref="dependents")
        elif dependents == 1:
            add("about", key, "Una persona depende económicamente de ti." if es else "One person depends on you financially.", ref="dependents")
        else:
            add("about", key, "{n} personas dependen económicamente de ti." if es else "{n} people depend on you financially.", ref="dependents", n=_Emph(str(dependents)))

    income = sit["income"]
    for item in income["items"]:
        amount = _amount(item["amount"], item["currency"], True)
        approx = about(item["approximate"])
        if item["frequency"] == "biweekly":
            period_es, period_en = "cada dos semanas", "every two weeks"
        else:
            period_es, period_en = "al mes", "a month"
        if item["net"] is False:
            template = f"Ganas {approx}{{a}} {period_es}, antes de impuestos." if es else f"You earn {approx}{{a}} {period_en} before tax."
        elif item["net"] is True:
            template = f"Recibes {approx}{{a}} {period_es}, netos." if es else f"You take home {approx}{{a}} {period_en}."
        else:
            template = f"Recibes {approx}{{a}} {period_es}." if es else f"You receive {approx}{{a}} {period_en}."
        add("money_in", item["key"], template, ref=_item_ref(item, "items"), extra=_since(item), a=amount)
    for item in income["extras"]:
        amount = _amount(item["amount"], item["currency"], True)
        approx = about(item["approximate"])
        what = _INCOME_KIND[lang].get(item.get("kind") or "", "")
        month = item.get("month")
        if item["frequency"] == "annual":
            when = (f" cada {_MONTHS['es'][month - 1]}" if es else f" each {_MONTHS['en'][month - 1]}") if month else (" al año" if es else " a year")
        else:
            when = ""
        template = (f"Recibes {approx}{{a}}" + (f" {what}" if what else "") + f"{when}.") if es else \
            (f"You receive {approx}{{a}}" + (f" {what}" if what else "") + f"{when}.")
        add("money_in", item["key"], template, ref=_item_ref(item, "items"), extra=_since(item), a=amount)

    spending = sit["spending"]
    if spending["source"] == "ledger" and spending["total"] is not None:
        add("money_out", None, "Según tus movimientos, gastas {a} al mes." if es else
            "Your transactions show {a} of spending a month.", extra={"origin": "transactions"},
            a=_amount(spending["total"], reporting, False))
    elif spending["stated"]:
        stated = spending["stated"]
        approx = about(spending["approximate"])
        ref_total = "total" if spending["key"] == "spending.monthly" else "monthly_spending"
        if stated.get("total") is not None:
            add("money_out", spending["key"], f"Gastas {approx}{{a}} al mes." if es else f"You spend {approx}{{a}} a month.",
                ref=ref_total, a=_amount(stated["total"], stated["currency"], code(stated["currency"])))
        if stated.get("essential") is not None:
            add("money_out", spending["key"], f"Tus gastos básicos son de {approx}{{a}} al mes." if es else
                f"Your essentials cost {approx}{{a}} a month.",
                ref="essential" if spending["key"] == "spending.monthly" else "monthly_essentials",
                a=_amount(stated["essential"], stated["currency"], code(stated["currency"])))

    for row in sit["cash"]:
        if row.get("balance_unknown") or row.get("amount") is None:
            if not row["counted"]:
                continue  # a statement settled it: the statement's sentence says what is there
            label = row.get("institution") or row.get("name") or row["id"]
            generic = _generic_bank(label) and not row.get("institution")
            where_es = "en el banco" if generic else f"en {label}"
            where_en = "in the bank" if generic else f"at {label}"
            add("own", row["key"], (f"Tienes dinero {where_es}; aún no sé cuánto." if es else
                                    f"You have money {where_en}; I don't know how much yet."), ref=_item_ref(row, "cash"))
            continue
        approx = about(row["approximate"])
        amount = _amount(row["amount"], row["currency"], code(row["currency"]))
        where = (f" en {row['institution']}" if es else f" at {row['institution']}") if row.get("institution") else ""
        purpose = ""
        if row.get("purpose") == "reserve":
            purpose = ", como fondo de emergencia" if es else ", set aside as your emergency fund"
        add("own", row["key"], (f"Tienes {approx}{{a}} en efectivo{where}{purpose}." if es else
                                f"You have {approx}{{a}} in cash{where}{purpose}."), ref=_item_ref(row, "cash"), a=amount)
    # One sentence per institution: two sub-accounts at GBM are one relationship to the person.
    by_institution: dict[str, list[Mapping[str, Any]]] = {}
    for account in sit["accounts"]:
        if account["source"] == "ledger" or not account.get("native") or account.get("superseded_by"):
            continue
        by_institution.setdefault(account.get("institution") or account["label"], []).append(account)
    for institution, group in by_institution.items():
        totals: dict[str, Decimal] = {}
        for account in group:
            for cur, value in account["native"].items():
                totals[cur] = totals.get(cur, Decimal(0)) + Decimal(str(value))
        ordered = sorted(totals.items(), key=lambda kv: (kv[0] != reporting, kv[0]))
        group.sort(key=lambda a: (a.get("currency") != reporting, a["key"]))
        parts = {f"a{i}": _amount(v, c, code(c)) for i, (c, v) in enumerate(ordered)}
        joined = (" y " if es else " and ").join(f"{{{name}}}" for name in parts)
        types = {a.get("type") for a in group}
        kind = {"brokerage": ("tu cuenta de inversión", "your brokerage account"),
                "checking": ("tu cuenta de cheques", "your checking account"),
                "savings": ("tu cuenta de ahorro", "your savings account")}.get(
            next(iter(types)) if len(types) == 1 else "", ("tus cuentas", "your accounts"))
        as_of = max((a.get("as_of") or "" for a in group), default="") or None
        add("own", group[0]["key"], (f"En {institution} tienes {joined} en {kind[0]}." if es else
                                     f"At {institution} you have {joined} in {kind[1]}."),
            extra={"institution": institution, "as_of": as_of, "keys": [a["key"] for a in group]}, **parts)
    for item in sit["investments"]:
        if not item["counted"]:
            continue
        approx = about(item["approximate"])
        if item.get("balance_unknown") or item.get("amount") is None:
            add("own", item["key"], _unknown_balance(item, es), ref=_item_ref(item, "investments"))
            continue
        where = _holding(item, lang, definite=True)
        add("own", item["key"], (f"Tienes {approx}{{a}} invertidos{where}." if es else f"You have {approx}{{a}} invested{where}."),
            ref=_item_ref(item, "investments"), a=_amount(item["amount"], item["currency"], code(item["currency"])))
    for diff in sit["differences"]:
        if not diff.get("statement") or not diff.get("stated"):
            continue
        approx = about(diff.get("stated_approximate"))
        if diff.get("statement_value") is not None and diff.get("currency"):
            shown = _amount(diff["statement_value"], diff["currency"], code(diff["currency"]))
        else:
            shown = _Emph(" + ".join(_money_text(v, c, True) for c, v in sorted(diff["statement"].items())))
        item = next((i for i in sit["investments"] if i["key"] == diff["key"] and i.get("institution") == diff["institution"]), {})
        add("own", diff["key"], (f"Dijiste {approx}{{s}} en {diff['institution']}; tu estado de cuenta dice {{x}}." if es else
                                 f"You said {approx}{{s}} at {diff['institution']}; your statement says {{x}}."),
            ref=_item_ref(item, "investments") if item else None,
            extra={"kind": "difference", "institution": diff["institution"], "as_of": diff.get("as_of")},
            s=_amount(diff["stated"]["amount"], diff["stated"]["currency"], code(diff["stated"]["currency"])), x=shown)

    for row in sit["liabilities"]:
        amount = _amount(row["balance"], row["currency"], code(row["currency"]))
        approx = about(row["approximate"])
        lender = row.get("lender")
        if es:
            what = _DEBT_ES.get(row["kind"], _DEBT_ES["other"])
            if row["kind"] == "other" and row.get("name"):
                what = f"de «{row['name']}»"
            template = f"Te quedan {approx}{{a}} {what}" + (f" con {lender}" if lender else "")
        else:
            what = _DEBT_EN.get(row["kind"], _DEBT_EN["other"])
            if row["kind"] == "other" and row.get("name"):
                what = row["name"]
            template = f"Your {what}" + (f" with {lender}" if lender else "") + f": {approx}{{a}} left"
        values: dict[str, Any] = {"a": amount}
        if row["annual_rate"] is not None:
            template += ", al {r} anual" if es else " at {r} a year"
            values["r"] = _Emph(pct(row["annual_rate"]))
        if row["monthly_payment"] is not None and row["payment_basis"] == "stated":
            template += "; pagas {p} al mes" if es else ", {p} a month"
            values["p"] = _amount(row["monthly_payment"], row["currency"], code(row["currency"]))
        extra = {"origin": "statement", "institution": lender, "as_of": row.get("as_of")} \
            if row.get("source") in ("statement", "household") else _since(row)
        add("owe", row["key"], template + ".", ref=_item_ref(row, "debts"), extra=extra, **values)

    for goal in sit["goals"]:
        if goal["status"] in ("done", "dropped"):
            continue
        cur = goal.get("currency")
        values = {}
        if goal["action"] == "invest" and goal["monthly_contribution"] is not None:
            obj = goal.get("object") or _object_from_name(goal["name"])
            if obj and not es:
                obj = re.sub(r"^(el|la|los|las)\s+", "", obj)
            values["a"] = _amount(goal["monthly_contribution"], cur, code(cur))
            if obj:
                template = f"Quieres invertir {{a}} al mes en {obj}." if es else f"You want to invest {{a}} a month in {obj}."
            else:
                template = "Quieres invertir {a} al mes." if es else "You want to invest {a} a month."
        elif goal["target_amount"] is not None:
            values["a"] = _amount(goal["target_amount"], cur, code(cur))
            values["n"] = goal["name"]
            if goal["target_date"]:
                values["d"] = _Emph(_month_year(goal["target_date"], lang))
                template = "Quieres juntar {a} para «{n}» antes de {d}." if es else "You want {a} for “{n}” by {d}."
            else:
                template = "Quieres juntar {a} para «{n}»." if es else "You want {a} for “{n}”."
        elif goal["monthly_contribution"] is not None:
            values.update(a=_amount(goal["monthly_contribution"], cur, code(cur)), n=goal["name"])
            template = "Apartas {a} al mes para «{n}»." if es else "You set aside {a} a month for “{n}”."
        else:
            values["n"] = goal["name"]
            template = "Quieres {n}." if es else "You want to {n}."
            if not re.match(r"(?i)^(ahorrar|invertir|comprar|pagar|juntar|save|invest|buy|pay)\b", goal["name"]):
                template = "Tienes una meta: «{n}»." if es else "One of your goals: “{n}”."
            else:
                values["n"] = goal["name"][:1].lower() + goal["name"][1:]
        add("goals", "goals", template, ref=str(goal["id"]), **values)
    reserve = sit["reserve"]
    if reserve["target_months"] is not None:
        months = reserve["target_months"]
        unit = ("mes" if months == 1 else "meses") if es else ("month" if months == 1 else "months")
        add("goals", "reserve" if "reserve" in meta else "plan.resources",
            "Quieres un fondo de emergencia de {m} de gastos." if es else "You want an emergency fund of {m} of expenses.",
            ref="target_months" if "reserve" in meta else "reserve_months", m=_Emph(f"{fmt(months)} {unit}"))

    risk = profile.get("risk") or {}
    if risk.get("drop_reaction") in _DROP[lang]:
        add("invest", "preference.risk", "Si tus inversiones cayeran 20%, {r}." if es else "If your investments fell 20%, {r}.",
            r=_Emph(_DROP[lang][risk["drop_reaction"]]))
    if risk.get("experience") in _EXPERIENCE[lang]:
        add("invest", "preference.risk", _EXPERIENCE[lang][risk["experience"]])
    # What the statement shows about how the money is invested (patterns, not things the person said).
    holdings = sit.get("holdings") or {}
    statement_key = next((a["key"] for a in sit["accounts"] if a["source"] == "statement" and a.get("key")), None)
    top = (holdings.get("top") or [None])[0]
    if statement_key and top and top.get("weight") is not None and len(holdings.get("top") or []) > 1:
        institution = next((a.get("institution") for a in sit["accounts"] if a["key"] == statement_key), None)
        add("invest", statement_key, "Tu posición más grande es {u}: {w} de lo que tienes invertido." if es else
            "Your largest position is {u}: {w} of what you have invested.",
            extra={"institution": institution, "readonly": True},
            u=_Emph(_instrument(top["underlying"], lang)), w=_Emph(pct(round(Decimal(str(top["weight"])), 2))))
    for overlap in (holdings.get("overlaps") or [])[:2]:
        names = list(overlap["symbols"])
        symbols = ", ".join(names[:-1]) + (" y " if es else " and ") + names[-1]
        times = {2: ("dos veces", "twice"), 3: ("tres veces", "three times"), 4: ("cuatro veces", "four times")}.get(
            len(names), (f"{len(names)} veces", f"{len(names)} times"))
        add("invest", statement_key, f"Tienes {{u}} {times[0]}: a través de {{s}}." if es else
            f"You own {{u}} {times[1]}: through {{s}}.", extra={"readonly": True},
            u=_Emph(_instrument(overlap["underlying"], lang)), s=symbols)
    return out


def unknown_names(sit: Mapping[str, Any], language: str | None = None) -> list[str]:
    """The balances nobody gave, named the way the person would ("tu cuenta de inversión", "HSBC")."""
    lang = _lang(language)
    names = []
    for row in sit.get("cash") or []:
        if row.get("balance_unknown") and row.get("counted"):
            label = row.get("institution") or row.get("name") or humanize(row["id"])
            names.append(("el banco" if lang == "es" else "the bank") if _generic_bank(label) and not row.get("institution")
                         else label)
    for row in sit.get("investments") or []:
        if row.get("balance_unknown") and row.get("counted"):
            names.append(_holding(row, lang, definite=False))
    return names or list((sit.get("net_worth") or {}).get("unknown_balances") or [])


def _generic_bank(label: Any) -> bool:
    return isinstance(label, str) and re.sub(r"[^a-z/ ]", "", label.lower()).strip() in {
        "bank", "banco", "checking / savings", "cheques / ahorro", "checking", "savings"}


_PLANS = {"afore": ("AFORE", "una", "an"), "ppr": ("PPR", "un", "a"), "hsa": ("HSA", "una", "an")}


def _holding(item: Mapping[str, Any], lang: str, *, definite: bool) -> str:
    """How the person names a stated investment, in their language.

    definite=True: the place after an amount, with its preposition (" en tu AFORE", " at GBM", "" if nothing
    to say).  definite=False: the thing itself for "You have ...; I don't know its balance yet"
    ("una cuenta de inversión en GBM", "a brokerage account", "an AFORE").
    """
    es = lang == "es"
    kind = str(item.get("kind") or "").lower()
    name = item.get("name") if isinstance(item.get("name"), str) else None
    institution = item.get("institution") if isinstance(item.get("institution"), str) else None
    plan = next((p for p in _PLANS if kind == p or (name or "").strip().upper() == _PLANS[p][0]), None)
    if plan:
        label, article_es, article_en = _PLANS[plan]
        if definite:
            return f" en tu {label}" if es else f" in your {label}"
        return f"{article_es} {label}" if es else f"{article_en} {label}"
    family = kind_family(kind) or kind_family(name)
    if family == "retirement":
        if definite:
            return (f" en tu {name}" if name else " en tu cuenta de retiro") if es else \
                (f" in your {name}" if name else " in your retirement account")
        return (f"una cuenta de retiro ({name})" if name else "una cuenta de retiro") if es else \
            (f"a {name}" if name else "a retirement account")
    if definite:
        if institution:
            return f" en {institution}" if es else f" at {institution}"
        return f" en {name}" if name and not _generic_bank(name) and kind not in ("brokerage", "") else ""
    if kind == "brokerage" or family == "investment" and not name:
        base = "una cuenta de inversión" if es else "a brokerage account"
        return base + ((f" en {institution}" if es else f" at {institution}") if institution else "")
    label = name or institution
    if label:
        return label + ((f" en {institution}" if es else f" at {institution}") if institution and institution != label else "")
    return "una inversión" if es else "an investment"


_KIND_PHRASE = {"brokerage": ("una cuenta de inversión", "a brokerage account"),
                "afore": ("una AFORE", "an AFORE"), "retirement": ("un plan de retiro", "a retirement account"),
                "fund": ("un fondo", "a fund")}


def _unknown_balance(item: Mapping[str, Any], es: bool) -> str:
    """'You have an account at GBM; I don't know its balance yet.' Never a bare type name as a noun."""
    name, institution = item.get("name"), item.get("institution")
    kind = str(item.get("kind") or "").lower()
    if institution:
        return (f"Tienes una cuenta en {institution}; aún no sé el saldo." if es else
                f"You have an account at {institution}; I don't know its balance yet.")
    if kind in _KIND_PHRASE:
        phrase = _KIND_PHRASE[kind][0 if es else 1]
        # A name that only repeats the type ("Brokerage") adds nothing; a real one ("GBM / casa de bolsa") is kept.
        if name and name.strip().lower() not in {kind, phrase.split(" ", 1)[1].lower(), "casa de bolsa", "retiro"}:
            phrase += f" ({name})"
        return (f"Tienes {phrase}; aún no sé el saldo." if es else f"You have {phrase}; I don't know its balance yet.")
    label = name or str(item["id"]).replace("_", " ")
    return (f"Tienes {label}; aún no sé el saldo." if es else f"You have {label}; I don't know its balance yet.")


def _item_ref(item: Mapping[str, Any], list_name: str) -> str | None:
    """Where one item lives inside its fact: a legacy list entry, a profile field, or the whole key."""
    key = item.get("key") or ""
    if key in ("plan.resources", "income.schedule"):
        return f"{list_name}:{item['id']}"
    if key == "client.profile":
        return str(item.get("id"))
    return None


def _since(item: Mapping[str, Any]) -> dict:
    for name in ("since", "valid_from", "start_date", "started_on"):
        value = item.get(name)
        if isinstance(value, str) and re.match(r"^\d{4}-\d{2}", value):
            return {"since": value[:10]}
    return {}


_PAYMENT_ES = {"auto": "del coche", "mortgage": "de la hipoteca", "card": "de la tarjeta", "personal": "del préstamo",
               "student": "del crédito educativo", "other": "del préstamo"}
_PAYMENT_EN = {"auto": "car", "mortgage": "mortgage", "card": "card", "personal": "loan", "student": "student loan",
               "other": "loan"}


def summaries(sit: Mapping[str, Any], language: str | None = None) -> dict[str, dict]:
    """One human line that opens each memory group, or nothing when it would only repeat a fact."""
    lang = _lang(language)
    es = lang == "es"
    cur = sit.get("currency")
    flow, out = sit["cash_flow"], {}

    def put(topic: str, template: str, **values: Any) -> None:
        text, spans = _compose(template, **values)
        out[topic] = {"text": text, "emphasis": spans}

    def money(value: Any) -> _Emph:
        return _amount(abs(Decimal(str(value))), cur, False)

    approx = sit["income"].get("approximate") or sit["spending"].get("approximate")
    unknown_debts = [r for r in sit["liabilities"] if r["id"] in (flow.get("debt_payments_unknown") or [])]
    about = ("unos " if es else "about ") if approx or unknown_debts else ""
    surplus = flow.get("surplus")
    if surplus is None and unknown_debts:
        surplus = flow.get("surplus_before_unknown_debts")  # said as "before your car payment", never as the surplus
    if surplus is not None and Decimal(str(surplus)) > 0:
        tail = ""
        if len(unknown_debts) == 1:
            kind = unknown_debts[0]["kind"]
            tail = f", sin contar el pago {_PAYMENT_ES.get(kind, _PAYMENT_ES['other'])}" if es else \
                f", before your {_PAYMENT_EN.get(kind, _PAYMENT_EN['other'])} payment"
        elif unknown_debts:
            tail = ", sin contar los pagos de tus deudas" if es else ", before your debt payments"
        put("money_in", (f"Te sobran {about}{{x}} al mes{tail}." if es else f"{about.capitalize()}{{x}} a month is left over{tail}."),
            x=money(surplus))
    elif surplus is not None and Decimal(str(surplus)) < 0:
        put("money_in", "Cada mes sale {x} más de lo que entra." if es else "Each month {x} more goes out than comes in.",
            x=money(surplus))
    income, spending = flow.get("income"), flow.get("spending")
    if income and spending is not None and Decimal(str(income)) > 0:
        share = (Decimal(str(spending)) / Decimal(str(income)) * 100).quantize(Decimal(1))
        put("money_out", "Gastas el {p} de lo que entra." if es else "You spend {p} of what comes in.", p=_Emph(f"{share}%"))
    nw = sit["net_worth"]
    owned = [r for r in sit["cash"] if r["counted"]] + [r for r in sit["investments"] if r["counted"]]
    institutions = {a.get("institution") or a["label"] for a in sit["accounts"] if a["source"] != "ledger" and a.get("native")}
    if nw.get("assets") is not None and not nw.get("unconverted") and len(owned) + len(institutions) >= 2:
        missing = ", ".join(unknown_names(sit, lang))
        if missing:
            put("own", f"En total tienes {{x}}, sin contar {missing}." if es else
                f"In all, you have {{x}}, not counting {missing}.", x=money(nw["assets"]))
        else:
            put("own", "En total tienes {x}." if es else "In all, you have {x}.", x=money(nw["assets"]))
    debts = sit["liabilities"]
    if len(debts) >= 2 and nw.get("liabilities") is not None:
        put("owe", "Debes {x} en total." if es else "You owe {x} in all.", x=money(nw["liabilities"]))
    elif len(debts) == 1 and debts[0]["missing"]:
        kind = debts[0]["kind"]
        put("owe", f"Aún no sé cuánto pagas {_PAYMENT_ES.get(kind, _PAYMENT_ES['other'])} al mes." if es else
            f"I don’t know your monthly {_PAYMENT_EN.get(kind, _PAYMENT_EN['other'])} payment yet.")
    elif len(debts) == 1 and debts[0]["payoff"].get("status") == "ready" and debts[0]["payoff"].get("date"):
        put("owe", "Terminas de pagar en {d}." if es else "It's paid off by {d}.",
            d=_Emph(_month_year(debts[0]["payoff"]["date"], lang)))
    commitments = sit["commitments"]
    if commitments["items"] and commitments.get("total"):
        if commitments.get("overcommitted"):
            put("goals", "Tus metas piden {t} al mes, más de lo que te sobra." if es else
                "Your goals ask for {t} a month, more than you have left over.", t=money(commitments["total"]))
        elif commitments.get("unallocated") is not None:
            put("goals", "Apartas {t} al mes para tus metas; te quedan {u} libres." if es else
                "You set aside {t} a month for your goals; {u} stays free.",
                t=money(commitments["total"]), u=money(commitments["unallocated"]))
        else:  # what stays free is unknown (a debt payment is missing): say only what is set aside
            put("goals", "Apartas {t} al mes para tus metas." if es else "You set aside {t} a month for your goals.",
                t=money(commitments["total"]))
    return out


def _object_from_name(name: str) -> str | None:
    match = re.search(r"\b(?:en|in)\s+(el\s+|la\s+|los\s+|the\s+)?([A-Z][\w&./ -]{1,40})$", name.strip().rstrip("."))
    if not match:
        return None
    article = (match.group(1) or "").strip()
    return f"{article} {match.group(2)}".strip() if article else match.group(2)


def _day(iso: str | None, lang: str) -> str:
    if not iso:
        return "?"
    day = date.fromisoformat(iso[:10])
    month = _MONTHS[lang][day.month - 1]
    return f"{day.day} de {month} de {day.year}" if lang == "es" else f"{month} {day.day}, {day.year}"


__all__ = ["brief", "sentences", "summaries", "fmt", "pct", "BRIEF_MAX_LINES", "liability_name"]
