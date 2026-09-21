"""Deterministic findings about an uploaded statement, and the picture after saving it.

``statement_insights(proposal_result, before)`` compares a held proposal with
what memory already says and reads its contents: stated vs statement, single
holding concentration, overlapping exposure across listings, domicile and venue
for Mexican residents, and idle cash.  Ordered by consequence; each item has a
``kind``, English ``text`` for the model, and the figures behind it.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping

from .model import CASH_DRAG_SHARE, CONCENTRATION_SHARE, D, num, ticker_of, underlying_of
from .text import _clean_symbol, fmt


def _rates(household: Mapping[str, Any]) -> dict[tuple[str, str], Decimal]:
    rates = {}
    for row in household.get("fx") or []:
        rate = D(row.get("rate"))
        if rate and rate > 0 and row.get("from") and row.get("to"):
            rates[(row["from"], row["to"])] = rate
            rates.setdefault((row["to"], row["from"]), 1 / rate)
    return rates


def _convert(rates, amount: Decimal | None, currency: str | None, to: str | None) -> Decimal | None:
    if amount is None or not currency or not to:
        return None
    if currency == to:
        return amount
    rate = rates.get((currency, to))
    return amount * rate if rate else None


def _is_cash(position: Mapping[str, Any]) -> bool:
    return position.get("asset_class") == "cash" or str(position.get("instrument_id", "")).startswith("CASH:")


def statement_insights(result: Mapping[str, Any], before: Mapping[str, Any] | None) -> list[dict]:
    household = result.get("household") or {}
    currency = household.get("currency") or result.get("currency")
    rates = _rates(household)
    accounts = {a["id"]: a for a in household.get("accounts") or [] if isinstance(a, dict) and a.get("id")}
    positions = [p for p in household.get("positions") or [] if isinstance(p, dict)]
    out: list[dict] = []

    # 1. What the person said vs what the statement shows, per institution.
    native: dict[str, dict[str, Decimal]] = {}
    for position in positions:
        institution = (accounts.get(position.get("account_id")) or {}).get("institution")
        amount = D(position.get("value"))
        if institution and amount is not None and position.get("currency"):
            bucket = native.setdefault(institution, {})
            bucket[position["currency"]] = bucket.get(position["currency"], Decimal(0)) + amount
    if before:
        stated_rows = [*(before.get("investments") or []), *(before.get("cash") or [])]
        for institution, amounts in sorted(native.items()):
            matches = [r for r in stated_rows if (r.get("institution") or "").strip().lower() == institution.strip().lower()]
            if not matches and len(native) == 1:
                matches = [r for r in before.get("investments") or [] if not r.get("institution")]
            for row in matches:
                stated = D(row.get("amount"))
                total = Decimal(0)
                known = True
                for cur, amount in amounts.items():
                    converted = _convert(rates, amount, cur, row.get("currency"))
                    if converted is None:
                        known = False
                    else:
                        total += converted
                shown = " + ".join(f"{c} {fmt(num(v))}" for c, v in sorted(amounts.items(), key=lambda kv: kv[0] != row.get("currency")))
                text = (f"They said {'about ' if row.get('approximate') else ''}{row.get('currency')} {fmt(num(stated))} at "
                        f"{institution}; the statement shows {shown}")
                if known and stated is not None and len(amounts) > 1:
                    text += f" (≈ {row.get('currency')} {fmt(num(total))} at the statement's FX)"
                out.append({"kind": "stated_vs_statement", "institution": institution, "key": row.get("key"),
                            "stated": {"amount": num(stated), "currency": row.get("currency")},
                            "statement": {c: num(v) for c, v in sorted(amounts.items())},
                            "statement_value": num(total) if known else None,
                            "difference": num(total - stated) if known and stated is not None else None,
                            "text": text + "."})

    # 1b. Holdings that changed since they were saved: the difference is the question worth asking.
    if before:
        saved = {}
        for row in (before.get("holdings") or {}).get("saved") or []:
            saved.setdefault(row["ticker"], []).append(row)
        for position in positions:
            if _is_cash(position):
                continue
            ticker = ticker_of(position.get("symbol") or position.get("instrument_id"))
            institution = (accounts.get(position.get("account_id")) or {}).get("institution") or ""
            candidates = [r for r in saved.get(ticker, [])
                          if not r.get("institution") or not institution
                          or r["institution"].strip().lower() == institution.strip().lower()]
            quantity = D(position.get("quantity"))
            for row in candidates[:1]:
                had = D(row.get("quantity"))
                if had is None or quantity is None or had == quantity:
                    continue
                saved_on = str(row.get("as_of") or "")[:10] or None
                statement_on = str(result.get("as_of") or household.get("as_of") or "")[:10] or None
                older = bool(saved_on and statement_on and statement_on < saved_on)
                if older:
                    text = (f"{ticker}: this statement ({statement_on}) is older than what was saved ({saved_on}): "
                            f"{fmt(num(quantity))} units then, {fmt(num(had))} saved now. Do not conclude; ask whether "
                            "they bought or sold between those dates. The saved, newer figure stays current until "
                            "they answer.")
                else:
                    text = (f"{ticker}: {fmt(num(had))} units were saved" + (f" as of {saved_on}" if saved_on else "")
                            + f"; the statement ({statement_on or 'undated'}) shows {fmt(num(quantity))} "
                            f"({'more' if quantity > had else 'fewer'}). Ask whether they bought or sold since.")
                out.append({"kind": "position_change", "symbol": ticker, "institution": institution or None,
                            "saved": {"quantity": num(had), "value": row.get("value"), "as_of": saved_on},
                            "statement": {"quantity": num(quantity), "value": num(D(position.get("value"))),
                                          "currency": position.get("currency"), "as_of": statement_on},
                            "statement_is_older": older, "text": text})

    # 2. A single holding over 25% of its account.
    for account_id, account in sorted(accounts.items()):
        rows = [p for p in positions if p.get("account_id") == account_id]
        values = [(p, _convert(rates, D(p.get("value")), p.get("currency"), account.get("currency") or currency)) for p in rows]
        total = sum((v for _, v in values if v is not None), Decimal(0))
        if not total:
            continue
        for position, value in sorted(values, key=lambda pv: -(pv[1] or 0)):
            if value is None or _is_cash(position):
                continue
            share = value / total
            if share > CONCENTRATION_SHARE:
                symbol = _clean_symbol(position.get("symbol") or position.get("instrument_id"))
                out.append({"kind": "concentration", "account": account_id, "symbol": symbol,
                            "value": num(value), "currency": account.get("currency"), "share": num(share, 4),
                            "text": f"{symbol} is {num(share * 100, 0)}% of the {account.get('institution') or account_id} "
                                    f"account ({account.get('currency')} {fmt(num(value))})."})
            break

    # 3. The same underlying through several listings or accounts.
    groups: dict[str, list[dict]] = {}
    for position in positions:
        if _is_cash(position):
            continue
        underlying = underlying_of(position.get("underlying_symbol") or position.get("symbol"))
        if underlying:
            groups.setdefault(underlying, []).append(position)
    for underlying, rows in sorted(groups.items()):
        symbols = sorted({str(p.get("symbol")) for p in rows})
        if len(symbols) < 2:
            continue
        value = sum((_convert(rates, D(p.get("value")), p.get("currency"), currency) or Decimal(0) for p in rows), Decimal(0))
        out.append({"kind": "overlap", "underlying": underlying, "symbols": symbols, "value": num(value),
                    "currency": currency,
                    "listings": [{"symbol": p.get("symbol"), "domicile": p.get("issuer_domicile"), "venue": p.get("venue"),
                                  "value": num(D(p.get("value"))), "currency": p.get("currency")} for p in rows],
                    "text": f"{' and '.join(symbols)} {'both' if len(symbols) == 2 else 'all'} track the {underlying} "
                            f"({currency} {fmt(num(value))} combined)."})

    # 4. Domicile and venue for a Mexican resident.
    profile = (before or {}).get("profile") or {}
    mexican = "MX" in (profile.get("tax_residence") or []) or (profile.get("residence") or {}).get("country") == "MX"
    if mexican:
        us = [p for p in positions if p.get("issuer_domicile") == "US" and not _is_cash(p)]
        ucits = [p for p in positions if p.get("issuer_domicile") in {"IE", "LU"} and not _is_cash(p)]
        if us:
            value = sum((_convert(rates, D(p.get("value")), p.get("currency"), currency) or Decimal(0) for p in us), Decimal(0))
            usd = _convert(rates, value, currency, "USD")
            out.append({"kind": "domicile", "us_domiciled": sorted(str(p.get("symbol")) for p in us),
                        "ucits": sorted(str(p.get("symbol")) for p in ucits), "us_value": num(value), "currency": currency,
                        "us_value_usd": num(usd), "venues": sorted({str(p.get("venue")) for p in us if p.get("venue")}),
                        "text": f"US-domiciled holdings ({', '.join(sorted(str(p.get('symbol')) for p in us))}) are "
                                f"{currency} {fmt(num(value))}" + (f" (≈ USD {fmt(num(usd))})" if usd is not None else "")
                                + "; they are US-situs for estate tax even through the SIC."
                                + (f" Irish UCITS ({', '.join(sorted(str(p.get('symbol')) for p in ucits))}) are not." if ucits else "")})

    # 5. Idle cash.
    for account_id, account in sorted(accounts.items()):
        rows = [p for p in positions if p.get("account_id") == account_id]
        values = [(p, _convert(rates, D(p.get("value")), p.get("currency"), account.get("currency") or currency)) for p in rows]
        total = sum((v for _, v in values if v is not None), Decimal(0))
        idle = sum((v for p, v in values if v is not None and _is_cash(p)), Decimal(0))
        if total and idle / total > CASH_DRAG_SHARE:
            out.append({"kind": "cash_drag", "account": account_id, "cash": num(idle), "currency": account.get("currency"),
                        "share": num(idle / total, 4),
                        "text": f"{account.get('currency')} {fmt(num(idle))} ({num(idle / total * 100, 0)}% of the "
                                f"{account.get('institution') or account_id} {account.get('currency')} account) is uninvested cash."})
    return out


def picture_delta(before: Mapping[str, Any], after: Mapping[str, Any], language: str | None = None) -> dict:
    """Net worth before and after a save, what changed, and the new brief."""
    from .text import brief
    old, new = before["net_worth"], after["net_worth"]
    old_accounts = {a["id"] for a in before["accounts"]}
    changed = [{"kind": "account_added", "label": a["label"], "value": a["value"], "native": a["native"], "as_of": a["as_of"]}
               for a in after["accounts"] if a["id"] not in old_accounts and a["source"] != "ledger"]
    counted_before = {i["id"] for i in before["investments"] if i["counted"]} | {c["id"] for c in before["cash"] if c["counted"]}
    for row in [*after["investments"], *after["cash"]]:
        if not row["counted"] and row["id"] in counted_before:
            changed.append({"kind": "stated_replaced_by_statement", "institution": row.get("institution"),
                            "stated": {"amount": row["amount"], "currency": row["currency"]}})
    change = None
    if old["total"] is not None and new["total"] is not None and old["currency"] == new["currency"]:
        change = num(D(new["total"]) - D(old["total"]))
    return {"currency": new["currency"], "net_worth_before": old["total"], "net_worth_after": new["total"],
            "change": change, "liquid_after": new["liquid"], "unconverted": new["unconverted"],
            "changed": changed, "brief": brief(after, language)}


__all__ = ["statement_insights", "picture_delta"]
