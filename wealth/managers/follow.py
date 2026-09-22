"""Followed managers: new-filing checks for the monitor."""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from .edgar import (
    Edgar, ManagerDataError, TransportError, UserAgentRequired, _filing_url, filings_13f, normalize_cik,
)
from .positions import _client


# ------------------------------------------------------------------ follow and monitor


def followed(facts: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Followed managers from ``follow.<cik>`` facts: [{cik, name?}]."""
    out = []
    for key, value in facts.items():
        if isinstance(key, str) and key.startswith("follow.") and isinstance(value, Mapping):
            try:
                cik = normalize_cik(value.get("cik") or key.split(".", 1)[1])
            except ValueError:
                continue
            if value.get("notify", True) is not False:
                out.append({"cik": cik, "name": value.get("name")})
    return out


def latest_filings(ciks: Iterable[str], *, client: Edgar | None = None) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Latest 13F per CIK ({cik: {form, filing_date, period, accession, name}}) and {cik: why unchecked}."""
    client = _client(client)
    found, unchecked = {}, {}
    for cik in ciks:
        try:
            sub, rows = filings_13f(client, normalize_cik(cik))
        except UserAgentRequired as exc:
            unchecked[cik] = str(exc)
            continue
        except (TransportError, ManagerDataError, ValueError) as exc:
            unchecked[cik] = str(exc)
            continue
        if rows:
            found[cik] = {**{k: rows[0][k] for k in ("form", "filing_date", "period", "accession")},
                          "name": sub.get("name"), "url": _filing_url(cik, rows[0]["accession"])}
        else:
            unchecked[cik] = "no 13F filings"
    return found, unchecked


def filing_check(rule: Mapping[str, Any], eligible: Mapping[str, Any], previous: Mapping[str, Any],
                 acknowledged: bool, supplied: Mapping[str, Any] | None = None,
                 client: Edgar | None = None) -> tuple[str, dict[str, Any], list, dict[str, Any]]:
    """Monitor rule ``manager_filing``: a new 13F for a followed manager is a proactive item.

    Returns ``(status, detail, identity, state)``.  The first check records what is already filed
    (no item); a later filing stays active until the rule id is acknowledged.
    """
    managers = rule.get("ciks")
    if managers is not None:
        if not isinstance(managers, list):
            raise ValueError("manager_filing ciks must be a list")
        follows = [{"cik": normalize_cik(c), "name": None} for c in managers]
    else:
        follows = followed(eligible)
    names = {f["cik"]: f["name"] for f in follows}
    if supplied is not None:
        if not isinstance(supplied, Mapping):
            raise ValueError("manager_filings must map CIKs to their latest 13F")
        latest = {normalize_cik(k): dict(v) for k, v in supplied.items() if isinstance(v, Mapping)}
        latest = {k: v for k, v in latest.items() if k in names}
        unchecked = {c: "not supplied" for c in names if c not in latest}
    else:
        latest, unchecked = latest_filings(list(names), client=client)
    seen = dict(previous.get("seen") or {})
    items = []
    for cik, filing in latest.items():
        accession = filing.get("accession")
        if not accession:
            unchecked[cik] = "no accession number"
            continue
        if cik not in seen or acknowledged:
            seen[cik] = accession
            continue
        if seen[cik] != accession:
            name = filing.get("name") or names.get(cik) or cik
            items.append({"cik": cik, "name": name, "form": filing.get("form"), "filing_date": filing.get("filing_date"),
                          "period": filing.get("period"), "accession": accession, "url": filing.get("url"),
                          "title": {"en": f"{name} filed a new 13F ({filing.get('form')}, quarter ending "
                                          f"{filing.get('period')})",
                                    "es": f"{name} presentó un nuevo 13F ({filing.get('form')}, trimestre al "
                                          f"{filing.get('period')})"},
                          "next_step": {"task": "manager_holdings", "inputs": {"cik": cik}},
                          "note": "Holdings are as of the quarter end, published with a lag of up to 45 days."})
    if not follows:
        status = "unknown"
        detail: dict[str, Any] = {"missing": "follow.<cik> facts (managers the person follows)"}
    else:
        status = "active" if items else ("unknown" if unchecked and not latest else "clear")
        detail = {"new_filings": items, "unchecked": unchecked, "following": len(follows),
                  "acknowledgement_required": bool(items), "acknowledged": acknowledged}
    identity = sorted(i["accession"] for i in items) + sorted(unchecked)
    return status, detail, identity, {"seen": seen}
