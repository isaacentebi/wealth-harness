"""Memory: what Wealth knows, as sentences with the action each one supports."""
from __future__ import annotations

from typing import Any, Iterable

from ..situation import build as build_situation, sentences, summaries
from .helpers import _CURRENCY, _MARKET_VALUED, _as_date, _num, _today


# ---------------------------------------------------------------- memory: what Wealth knows, as sentences
#
# The page reads like notes about the person: one sentence per fact, grouped by
# life area, each with a quiet origin cue.  Every sentence carries exactly the
# action it supports (edit its amount, forget it, confirm it), resolved here so
# the page never guesses at fact shapes.

MEMORY_TOPICS = ("money_in", "money_out", "own", "owe", "goals", "invest", "about")
_REVIEW_MAX = 3
# Legacy list facts: list name -> (synthetic id prefix the model uses, amount field).
_LEGACY_LISTS = {"plan.resources": {"cash": ("cash", "amount"), "debts": ("debt", "balance"),
                                    "investments": ("investment", "amount")},
                 "income.schedule": {"items": ("item", "amount")}}
_WHOLE_KEY_FORGET_BLOCKED = {"client.profile", "plan.resources", "income.schedule", "goals", "household"}
_COUNT_FIELDS = {"dependents", "birth_year", "target_months", "reserve_months"}


def _legacy_item(key: str, current: Any, ref: str | None) -> tuple[str, int, str]:
    """(list name, index, amount field) for a ``cash:cash0``-style ref into a legacy list fact."""
    name, sep, item_id = (ref or "").partition(":")
    spec = _LEGACY_LISTS.get(key, {}).get(name)
    items = current.get(name) if isinstance(current, dict) else None
    if not sep or not spec or not isinstance(items, list):
        raise LookupError("no such item on this fact")
    for index, item in enumerate(items):
        if isinstance(item, dict) and str(item.get("id") or f"{spec[0]}{index}") == item_id:
            field = next((f for f in (spec[1], "amount", "balance") if f in item), spec[1])
            return name, index, field
    raise LookupError("no such item on this fact")


def _find_goal(goals: Any, ref: str | None) -> dict | None:
    if not isinstance(goals, list) or ref is None:
        return None
    return next((g for i, g in enumerate(goals) if isinstance(g, dict) and str(g.get("id", i)) == ref), None)


def _edit_spec(fact: dict, ref: str | None) -> dict | None:
    """What an inline edit changes: an amount (with currency) or a count, and where it lives."""
    key, value = fact["key"], fact.get("value")
    if key.startswith(("account.", "analysis.", "research.")) or key in _MARKET_VALUED:
        return None  # statement and market values change with a new statement, not by hand
    if key in _LEGACY_LISTS and ref and ":" in ref:
        try:
            name, index, field = _legacy_item(key, value, ref)
        except LookupError:
            return None
        item = value[name][index]
        return {"field": ref, "kind": "amount", "amount": _num(item.get(field)),
                "currency": item.get("currency") or value.get("currency")}
    if key == "goals":
        goal = _find_goal(value, ref)
        if goal is None:
            return None
        if goal.get("monthly_contribution") is not None or (goal.get("amount") is not None and goal.get("frequency") == "monthly"):
            wrap, amount = "monthly_contribution", goal.get("monthly_contribution", goal.get("amount"))
        elif goal.get("target_amount") is not None:
            wrap, amount = "target_amount", goal["target_amount"]
        else:
            return None
        return {"field": ref, "kind": "amount", "wrap": wrap, "amount": _num(amount), "currency": goal.get("currency")}
    if ref and isinstance(value, dict) and ref in value:
        item = value[ref]
        if isinstance(item, dict) and "amount" in item:
            return {"field": ref, "kind": "amount", "amount": _num(item["amount"]), "currency": item.get("currency")}
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            if ref in _COUNT_FIELDS:
                return {"field": ref, "kind": "number", "amount": item, "unit": "months" if "months" in ref else None}
            currency = value.get("currency")
            if isinstance(currency, str) and _CURRENCY.match(currency):
                return {"field": ref, "kind": "amount", "amount": item, "currency": currency}
        return None
    field = _canonical_amount_field(key, value) if ref is None else None
    if field:
        return {"field": None, "kind": "amount", "amount": _num(value.get(field)), "currency": value.get("currency")}
    return None


def _forget_spec(fact: dict, ref: str | None, sentence: dict) -> dict | None:
    key, value = fact["key"], fact.get("value")
    if sentence.get("readonly") or len(sentence.get("keys") or []) > 1:
        return None
    if key == "spending.monthly" and isinstance(value, dict):
        other = "essential" if ref == "total" else "total"
        return {"field": ref if value.get(other) is not None else None}
    if ref is not None:
        return {"field": ref}
    return None if key in _WHOLE_KEY_FORGET_BLOCKED else {"field": None}


def _origin(sentence: dict, institutions: dict[str, str], observed: dict[str, Any]) -> dict:
    key = sentence.get("key")
    if sentence.get("origin") == "transactions" or key is None:
        return {"kind": "transactions"}
    if sentence.get("unconfirmed"):
        return {"kind": "guess"}
    kind = {"user": "said", "document": "statement"}.get(sentence.get("source"), "calculated")
    if sentence.get("origin") == "statement":
        kind = "statement"
    origin = {"kind": kind}
    institution = sentence.get("institution") or institutions.get(key)
    if institution and kind == "statement":
        origin["institution"] = institution
    as_of = sentence.get("as_of") or (observed.get(key) if kind == "statement" else None)
    if as_of and kind == "statement":
        origin["as_of"] = str(as_of)[:10]
    return origin


def _insight_origin(sentence: dict, sit: dict) -> dict:
    """Where a holdings insight (largest position, an overlap) comes from: the statements that hold it.

    One institution is named with its date; holdings spread over several are credited to the
    calculation, never to whichever statement happens to be first.
    """
    holdings = sit.get("holdings") or {}
    text = sentence.get("text") or ""
    overlap = next((o for o in holdings.get("overlaps") or []
                    if o.get("symbols") and all(str(sym) in text for sym in o["symbols"])), None)
    symbols = set((overlap or ((holdings.get("top") or [{}])[0]) or {}).get("symbols") or [])
    places = {(r.get("institution"), r.get("as_of")) for r in holdings.get("saved") or []
              if r.get("symbol") in symbols and r.get("institution")}
    if len({p[0] for p in places}) != 1:
        return {"kind": "calculated"}
    institution = next(iter(places))[0]
    dates = [d for i, d in places if d]
    origin = {"kind": "statement", "institution": institution}
    if dates:
        origin["as_of"] = str(max(dates))[:10]
    return origin


def _held_back(sit: dict, snapshot: dict, language: str) -> list[dict]:
    """Sentences for facts the model keeps out of the numbers (past review, or only a guess).

    The situation never uses them, so it never words them; a relaxed copy of the
    snapshot does, and each sentence is marked stale or unconfirmed so the page
    asks about it instead of stating it.
    """
    stale, inferred = set(sit.get("stale") or []), set(sit.get("inferred") or [])
    held = stale | inferred
    if not held:
        return []
    relaxed = {**snapshot, "facts": [
        {**f, "expires_on": None, "confidence": "reported"} if f.get("key") in held else f
        for f in snapshot.get("facts") or []]}
    try:
        shadow = build_situation(relaxed, None, _as_date(sit.get("as_of")) or _today(None))
    except Exception:  # a partial legacy shape the model cannot read yet; nothing to word
        return []
    shadow["meta"] = {**shadow.get("meta", {}), **{k: v for k, v in (sit.get("meta") or {}).items() if k in held}}
    out = []
    for s in sentences(shadow, language):
        if s["key"] in held and s.get("kind") != "difference" and not s.get("readonly"):
            out.append({**s, "stale": s["key"] in stale, "unconfirmed": s["key"] in inferred})
    return out


def memory_view(sit: dict, snapshot: dict, language: str, missing: Iterable[str] = ()) -> dict:
    """Sentences grouped by life area, the stated-vs-statement cards and at most three check-ins."""
    facts = {f["key"]: f for f in snapshot.get("facts") or [] if f.get("value") is not None}
    meta = sit.get("meta") or {}
    institutions = {a["key"]: a.get("institution") for a in sit["accounts"] if a.get("key") and a.get("institution")}
    observed = {k: m.get("observed_on") for k, m in meta.items()}
    heads = summaries(sit, language)
    items, conflicts, answered = [], [], set()
    records = [c for c in sit.get("contradictions") or [] if c.get("status", "pending") == "pending"]
    direct = sentences(sit, language)
    # A stale statement account is worded by the situation itself; its held-back copy would repeat it.
    worded = {(s["key"], s.get("ref")) for s in direct}
    extra = [s for s in _held_back(sit, snapshot, language) if (s["key"], s.get("ref")) not in worded]
    for index, s in enumerate(direct + extra):
        fact = facts.get(s["key"] or "")
        ref = s.get("ref")
        editable = not s.get("readonly")
        item = {
            "id": f"{s['topic']}-{index}", "topic": s["topic"], "text": s["text"], "emphasis": s["emphasis"],
            "key": s["key"] if fact else None,
            "origin": _insight_origin(s, sit) if s["topic"] == "invest" and s.get("readonly")
            else _origin(s, institutions, observed),
            "since": s.get("since") or _valid_since(meta.get(s["key"] or "", {})), "age_days": s.get("age_days"),
            "unconfirmed": s["unconfirmed"], "stale": s["stale"],
            "edit": _edit_spec(fact, ref) if fact and editable else None,
            "forget": _forget_spec(fact, ref, s) if fact else None,
            "confirm": bool(fact and editable and (s["stale"] or s["unconfirmed"])
                            and not s["key"].startswith("account.") and s["key"] not in _MARKET_VALUED),
        }
        if s.get("kind") == "difference":
            record = next((c for c in records if c["key"] == item["key"]), None)
            conflict = _conflict(item, s, sit, facts, record)
            if conflict:
                conflicts.append(conflict)
                answered.add(item["key"])
            continue
        items.append(item)
    # Contradiction records the sentences did not already word (a document or pattern vs what was said).
    for record in records:
        if record["key"] not in answered:
            card = _record_card(record, language, facts)
            if card:
                conflicts.append(card)
    review = [i for i in items if i["stale"] and (i["confirm"] or i["edit"])][:_REVIEW_MAX]
    in_review = {i["id"] for i in review}
    groups = []
    for topic in MEMORY_TOPICS:
        rows = [i for i in items if i["topic"] == topic and i["id"] not in in_review]
        if rows:  # a group appears only once it has a fact
            groups.append({"id": topic, "summary": heads.get(topic), "facts": rows})
    return {"groups": groups, "conflicts": conflicts, "review": review, "missing": list(missing)}


def _valid_since(meta: dict) -> str | None:
    """'since March 2026' only when the person said when it became true (not merely when they told us)."""
    start, told = meta.get("valid_from"), meta.get("observed_on")
    if meta.get("source") != "user" or not isinstance(start, str) or not isinstance(told, str):
        return None
    return start[:10] if start[:7] < told[:7] else None


_WHERE = {"es": {"document": "tu estado de cuenta", "pattern": "tus movimientos", "connector": "tu cuenta conectada",
                 "web": "una página web", "inference": "mi lectura", "tool": "un cálculo"},
          "en": {"document": "your statement", "pattern": "your transactions", "connector": "your connected account",
                 "web": "a web page", "inference": "my reading", "tool": "a calculation"}}
_TOPIC = {"es": {"income.": "tu ingreso", "cash.": "tu efectivo", "investment.": "lo que tienes invertido",
                 "liability.": "tu deuda", "spending.": "tu gasto al mes"},
          "en": {"income.": "your income", "cash.": "your cash", "investment.": "what you have invested",
                 "liability.": "your debt", "spending.": "your monthly spending"}}


def _plain_amount(value: Any) -> tuple[Any, Any] | None:
    if isinstance(value, dict):
        for field in ("amount", "balance", "total", "value"):
            if _num(value.get(field)) is not None and isinstance(value.get("currency"), str):
                return _num(value[field]), value["currency"]
    return None


def _amount_words(amount: float, currency: str, reporting: str | None) -> str:
    """"$150,000" in the reporting currency, "$8,000 USD" otherwise (the contradiction card's two figures)."""
    whole = round(amount) if abs(amount) >= 1000 else amount
    text = f"${whole:,.0f}" if float(whole).is_integer() else f"${whole:,.2f}"
    return text + (f" {currency}" if currency != reporting else "")


def _record_card(record: dict, language: str, facts: dict) -> dict | None:
    """A stored contradiction (document, pattern, web) worded as one sentence, amounts only."""
    mine, theirs = _plain_amount(record.get("current_value")), _plain_amount(record.get("proposed_value"))
    topic = next((v for k, v in _TOPIC[language].items() if record["key"].startswith(k)), None)
    if not mine or not theirs or not topic or record["key"] not in facts:
        return None
    es = language == "es"
    where = _WHERE[language].get(((record.get("sources") or {}).get("proposed") or {}).get("kind"),
                                 "otra fuente" if es else "another source")
    a, b = _amount_words(*mine, mine[1]), _amount_words(*theirs, mine[1])
    head = f"Me dijiste que {topic} es de " if es else f"You told me {topic} is "
    mid = f"; {where} dice "
    if not es:
        mid = f"; {where} says "
    text = head + a + mid + b + "."
    text = text[:1].upper() + text[1:]
    spans = [[len(head), len(head) + len(a)], [len(head) + len(a) + len(mid), len(head) + len(a) + len(mid) + len(b)]]
    return {"id": f"contradiction-{record['id']}", "text": text, "emphasis": spans, "key": record["key"],
            "contradiction_id": record["id"], "institution": None,
            "as_of": (record.get("valid_from") or {}).get("proposed"),
            "use_statement": None, "edit": _edit_spec(facts[record["key"]], None)}


def _unanswered(fact: dict | None, ref: str | None, as_of: str | None) -> bool:
    """A legacy stated item not yet kept, replaced or changed since the statement's date."""
    if fact is None or not ref or ":" not in ref:
        return fact is not None
    try:
        name, index, _ = _legacy_item(fact["key"], fact.get("value"), ref)
    except LookupError:
        return False
    answered = fact["value"][name][index].get("confirmed_on")
    return not (isinstance(answered, str) and answered >= str(as_of or ""))


def _conflict(item: dict, sentence: dict, sit: dict, facts: dict, record: dict | None = None) -> dict | None:
    """A stated figure a newer statement disagrees with; gone once the person has answered it."""
    diff = next((d for d in sit["differences"] if d.get("statement") and d["key"] == item["key"]
                 and d.get("institution") == sentence.get("institution")), None)
    if diff is None or item["key"] is None:
        return None
    if record is None and item["key"].startswith(("investment.", "cash.")):
        return None  # the store asks about canonical balances itself; no record means nothing to ask
    if record is None and not _unanswered(facts.get(item["key"]), sentence.get("ref"), diff.get("as_of")):
        return None  # the person already kept, replaced or changed this figure
    edit = item["edit"]
    use = None
    if edit and diff.get("statement_value") is not None and diff.get("currency"):
        use = {"field": edit["field"], "amount": round(float(diff["statement_value"])), "currency": diff["currency"],
               "wrap": edit.get("wrap")}
    return {"id": item["id"], "text": item["text"], "emphasis": item["emphasis"], "key": item["key"],
            "contradiction_id": record["id"] if record else None,
            "institution": diff.get("institution"), "as_of": diff.get("as_of"),
            "use_statement": use, "edit": edit}


def _canonical_amount_field(key: str, current: Any) -> str | None:
    """The amount field a row edit changes on a canonical fact, or None."""
    if not isinstance(current, dict) or "proposal_id" in current:
        return None
    if key.startswith(("income.", "cash.", "investment.")) and key != "income.schedule" and "amount" in current:
        return "amount"
    if key.startswith("liability.") and "balance" in current:
        return "balance"
    if key == "spending.monthly":
        return "total" if current.get("total") is not None else "essential"
    return None
