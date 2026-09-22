"""Writes (returned, not performed): fact payloads for edits, forgets and the profile form."""
from __future__ import annotations

import re
from datetime import date
from typing import Any

from ..situation.model import goal_name
from .helpers import (
    FORM_FIELDS, _CURRENCY, _FORM_PROFILE_FIELDS, _MARKET_VALUED, _as_date, _dict_value, _num, _today,
)
from .classify import _editor
from .memory import _LEGACY_LISTS, _canonical_amount_field, _legacy_item


# ---------------------------------------------------------------- writes (returned, not performed)
#
# Each write omits expires_on so the store sets the review date from
# store.REVIEW_DAYS; the web layer passes the revision the page was rendered at
# as expected_revision so an edit never overwrites a change it did not see.

def _user_fact(key: str, value: Any, ref: str, today: date, *, merge: bool = False) -> dict:
    fact = {"key": key, "value": value, "source": {"kind": "user", "ref": ref, "observed_on": today.isoformat()},
            "confidence": "confirmed"}
    if merge:
        fact["merge"] = True
    return fact


def _coerce(editor: str | None, value: Any) -> Any:
    if editor == "text":
        if not isinstance(value, str) or not value.strip() or len(value) > 2000:
            raise ValueError("enter text of 1–2,000 characters")
        return value.strip()
    if editor == "number":
        number = _num(value)
        if number is None:
            raise ValueError("enter a number")
        return int(number) if number.is_integer() else number
    if editor == "bool":
        if not isinstance(value, bool):
            raise ValueError("choose yes or no")
        return value
    if editor == "list":
        items = value.split(",") if isinstance(value, str) else value
        if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
            raise ValueError("enter a comma-separated list")
        cleaned = [i.strip() for i in items if i.strip()]
        if not cleaned or len(cleaned) > 30:
            raise ValueError("enter one to thirty items")
        return cleaned
    if editor == "amount":
        return _amount(value)
    raise ValueError("this value is structured; update it in the chat")


def _amount(value: Any) -> dict:
    if not isinstance(value, dict):
        raise ValueError("enter an amount and currency")
    amount = _num(value.get("amount"))
    currency = str(value.get("currency") or "").upper()
    if amount is None or amount < 0:
        raise ValueError("enter a non-negative amount")
    if not _CURRENCY.match(currency):
        raise ValueError("choose a three-letter currency")
    result = {"amount": int(amount) if amount.is_integer() else amount, "currency": currency}
    if value.get("period") in {"month", "year"}:
        result["period"] = value["period"]
    return result


def _goal_patch(goal: dict, value: Any) -> dict:
    if not isinstance(value, dict):
        raise ValueError("goal edits must be an object")
    updated = dict(goal)
    if "name" in value:
        updated["name"] = _coerce("text", value["name"])
    if "target_amount" in value:
        if value["target_amount"] in (None, ""):
            updated.pop("target_amount", None)  # unknown, not zero
        else:
            number = _num(value["target_amount"])
            if number is None or number < 0:
                raise ValueError("enter a non-negative target")
            updated["target_amount"] = int(number) if number.is_integer() else number
    if "monthly_contribution" in value:
        number = _num(value["monthly_contribution"])
        if number is None or number < 0:
            raise ValueError("enter a non-negative monthly amount")
        number = int(number) if number.is_integer() else number
        # Older goals kept the monthly figure as amount + frequency; keep their shape.
        if "monthly_contribution" not in goal and goal.get("amount") is not None and goal.get("frequency") == "monthly":
            updated["amount"] = number
        else:
            updated["monthly_contribution"] = number
        updated.pop("approximate", None)
    if value.get("currency"):
        currency = str(value["currency"]).upper()
        if not _CURRENCY.match(currency):
            raise ValueError("choose a three-letter currency")
        updated["currency"] = currency
    if "due" in value:
        if value["due"] in (None, ""):
            updated.pop("due", None)
        elif _as_date(value["due"]) is None:
            raise ValueError("enter a date as YYYY-MM-DD")
        else:
            updated["due"] = _as_date(value["due"]).isoformat()
    return updated


def fact_action(snapshot: dict, key: str, action: str, *, field: str | None = None,
                value: Any = None, today: Any = None) -> list[dict]:
    """Return the facts to remember for an Edit / Confirm / Delete request.

    * confirm: rewrites the current value as a user-confirmed observation dated
      today, which restarts its review period (and confirms an inferred fact).
    * edit / delete of a field: a ``merge`` patch (``null`` removes the field).
    * edit / delete of one goal: the full goals list with that goal changed.
    * delete of a whole key: a ``null`` retraction; history is kept.

    Raises ``LookupError`` for an unknown key/field, ``ValueError`` otherwise.
    """
    today = _today(today)
    if action not in {"edit", "confirm", "delete"}:
        raise ValueError("action must be edit, confirm, or delete")
    fact = next((f for f in (snapshot or {}).get("facts") or []
                 if isinstance(f, dict) and f.get("key") == key), None)
    if fact is None or fact.get("value") is None:
        raise LookupError("no current fact with that key")
    current = fact["value"]

    if action == "confirm" and key in _LEGACY_LISTS and field and ":" in field:
        # One legacy item kept as the person said it (e.g. against a statement): mark that item only.
        name, index, _ = _legacy_item(key, current, field)
        items = list(current[name])
        items[index] = {**items[index], "confirmed_on": today.isoformat()}
        return [_user_fact(key, {**current, name: items}, "profile page: confirmed still true", today)]
    if action == "confirm":
        if key in _MARKET_VALUED or key.startswith(("analysis.", "research.")):
            raise ValueError("market values need a fresh statement or analysis, not a confirmation")
        return [_user_fact(key, current, "profile page: confirmed still true", today)]
    if field is None:
        if action == "delete":
            return [_user_fact(key, None, "profile page: removed", today)]
        amount_field = _canonical_amount_field(key, current)
        if amount_field:
            amount = _amount(value)
            patch = {amount_field: amount["amount"], "currency": amount["currency"]}
            if current.get("approximate"):
                patch["approximate"] = False  # the person just gave the exact figure
            return [_user_fact(key, patch, "profile page edit", today, merge=True)]
        if key == "goals" or key in _MARKET_VALUED:
            raise ValueError("this value is structured; update it in the chat")
        return [_user_fact(key, _coerce(_editor(current), value), "profile page edit", today)]
    if key in _LEGACY_LISTS and ":" in field:
        # One item of a legacy list (plan.resources cash/debts/investments, income.schedule items):
        # lists without ids cannot be merge-patched, so the whole object is rewritten.
        name, index, amount_field = _legacy_item(key, current, field)
        items = list(current[name])
        if action == "delete":
            items.pop(index)
            return [_user_fact(key, {**current, name: items}, "profile page: item removed", today)]
        amount = _amount(value)
        item = {k: v for k, v in items[index].items() if k != "approximate"}  # the person just stated it
        items[index] = {**item, amount_field: amount["amount"], "currency": amount["currency"],
                        "confirmed_on": today.isoformat()}
        return [_user_fact(key, {**current, name: items}, "profile page edit", today)]
    if key == "goals":
        if not isinstance(current, list):
            raise ValueError("goals has an unexpected shape")
        position = next((i for i, g in enumerate(current)
                         if isinstance(g, dict) and str(g.get("id", i)) == field), None)
        if position is None:
            raise LookupError("no goal with that id")
        # Older goals had no name; the schema needs one on every rewrite, so give them their read name.
        goals = [{**g, "name": goal_name(g)} if isinstance(g, dict) and not g.get("name") else g for g in current]
        if action == "delete":
            goals.pop(position)
            return [_user_fact(key, goals, "profile page: goal removed", today)]
        goals[position] = _goal_patch(goals[position], value)
        return [_user_fact(key, goals, "profile page: goal edited", today)]
    if not isinstance(current, dict) or field not in current:
        raise LookupError("no such field on this fact")
    if action == "delete":
        return [_user_fact(key, {field: None}, "profile page: field removed", today, merge=True)]
    if isinstance(value, dict) and "amount" in value and _editor(current[field]) == "number":
        # A plain number beside the fact's currency (spending.monthly total, plan.resources essentials).
        amount = _amount(value)
        patch: dict[str, Any] = {field: amount["amount"]}
        if isinstance(current.get("currency"), str):
            patch["currency"] = amount["currency"]
        if current.get("approximate"):
            patch["approximate"] = False
        return [_user_fact(key, patch, "profile page edit", today, merge=True)]
    new_value = _coerce(_editor(current[field]), value)
    if isinstance(new_value, list):  # the store merges only id-keyed lists; replace the object
        return [_user_fact(key, {**current, field: new_value}, "profile page edit", today)]
    return [_user_fact(key, {field: new_value}, "profile page edit", today, merge=True)]


def form_facts(snapshot: dict, form: dict, today: Any = None) -> list[dict]:
    """Turn "Complete your profile" answers into canonical facts.

    Blank answers are skipped (unknown stays unknown, never zero).  Profile
    fields are written as a ``merge`` patch into ``client.profile``; a new goal
    is appended to ``goals`` without dropping existing goals.  Returns an empty
    list when nothing was supplied.
    """
    today = _today(today)
    if not isinstance(form, dict):
        raise ValueError("form must be an object")
    unknown = set(form) - set(FORM_FIELDS)
    if unknown:
        raise ValueError(f"unknown form fields: {sorted(unknown)}")
    facts = {f["key"]: f for f in (snapshot or {}).get("facts") or [] if isinstance(f, dict) and "key" in f}
    patch: dict[str, Any] = {}
    for field, target in _FORM_PROFILE_FIELDS.items():
        raw = form.get(field)
        if raw in (None, "") or (isinstance(raw, dict) and raw.get("amount") in (None, "")):
            continue
        patch[target] = _amount(raw)
    if form.get("dependents") not in (None, ""):
        number = _num(form["dependents"])
        if number is None or number < 0 or not number.is_integer() or number > 50:
            raise ValueError("dependents must be a whole number")
        patch["dependents"] = int(number)
    for field in ("tax_residence", "currencies"):
        if form.get(field) not in (None, "", []):
            items = _coerce("list", form[field])
            if field == "currencies":
                items = [i.upper() for i in items]
                if not all(_CURRENCY.match(i) for i in items):
                    raise ValueError("currencies must be three-letter codes")
            patch[field] = items
    out = []
    ref = "profile page form"
    if patch:
        current = _dict_value(facts.get("client.profile"))
        if any(isinstance(v, list) and isinstance(current.get(k), list) for k, v in patch.items()):
            # String lists cannot be merge-patched; replace the whole object (needs expected_revision).
            out.append(_user_fact("client.profile", {**current, **patch}, ref, today))
        else:
            out.append(_user_fact("client.profile", patch, ref, today, merge=True))
    if form.get("risk") not in (None, ""):
        out.append(_user_fact("preference.risk", _coerce("text", form["risk"]), ref, today, merge=True))
    new_goal = form.get("goals")
    if isinstance(new_goal, dict) and str(new_goal.get("name") or "").strip():
        goals_fact = facts.get("goals")
        goals = list(goals_fact["value"]) if goals_fact and isinstance(goals_fact.get("value"), list) else []
        base_id = re.sub(r"[^a-z0-9]+", "-", str(new_goal["name"]).lower()).strip("-")[:40] or "goal"
        existing = {str(g.get("id")) for g in goals if isinstance(g, dict)}
        goal_id, n = base_id, 2
        while goal_id in existing:
            goal_id, n = f"{base_id}-{n}", n + 1
        goal = _goal_patch({"id": goal_id}, {k: v for k, v in new_goal.items()
                                             if k in {"name", "target_amount", "currency", "due"}})
        if all(isinstance(g, dict) and "id" in g for g in goals):
            out.append(_user_fact("goals", [goal], ref, today, merge=True))  # merged by id
        else:
            out.append(_user_fact("goals", [*goals, goal], ref, today))
    elif new_goal not in (None, "", {}) and not isinstance(new_goal, dict):
        raise ValueError("goals must be an object with a name")
    return out
