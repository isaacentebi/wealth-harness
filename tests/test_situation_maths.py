"""Situation maths regressions: unknown is never zero, nothing is counted twice, the brief shows the fixed numbers."""
from __future__ import annotations

from decimal import Decimal

import pytest

from wealth import dca, situation
from wealth.situation.model import _liability_kind, build, payoff, same_institution

TODAY = "2026-09-21"


def _fact(key, value, *, kind="user", observed="2026-09-10", fact_id=None):
    return {"key": key, "value": value, "status": "active", "confidence": "reported", "id": fact_id or f"id-{key}",
            "source": {"kind": kind, "ref": "test", "observed_on": observed}}


def _snap(facts, **extra):
    return {"client": {"revision": 1}, "facts": [_fact(k, v) for k, v in facts.items()], **extra}


def _statement(account_id, institution, kind, currency, native, *, as_of="2026-08-31", fx=None, positions=None,
               liabilities=None):
    positions = positions if positions is not None else [
        {"symbol": f"CASH:{c}", "instrument_id": f"CASH:{c}", "value": v, "currency": c, "asset_class": "cash"}
        for c, v in native.items()]
    return {"account": {"id": account_id, "institution": institution, "type": kind, "currency": currency},
            "as_of": as_of, "positions": positions, "fx": fx or [], "liabilities": liabilities or []}


BASE = {"income.salary": {"amount": 85000, "currency": "MXN", "frequency": "monthly", "net": True},
        "spending.monthly": {"total": 45000, "currency": "MXN"}}
USD_MXN_18 = {"accounts": [], "instruments": [], "entries": [],
              "fx": [{"base": "USD", "quote": "MXN", "rate": "18", "date": "2026-09-18"}]}


# -- payoff (finding 2) -----------------------------------------------------------


def test_payoff_interest_does_not_subtract_the_final_overpayment():
    today = __import__("datetime").date(2026, 9, 21)
    # 0 %: no interest at all (it read -200).
    assert payoff(Decimal(1000), Decimal(0), Decimal(300), today) == {
        "status": "ready", "months": 4, "date": "2027-01", "interest": 0}
    # 12 %: 1% a month on 1000, 710, 417.10, 121.271 = 10 + 7.10 + 4.171 + 1.21271 = 22.48371 (it read -155.03).
    assert payoff(Decimal(1000), Decimal("0.12"), Decimal(300), today)["interest"] == 22.48


def test_brief_shows_the_fixed_interest():
    sit = build(_snap({**BASE, "liability.card": {"kind": "card", "balance": 1000, "currency": "MXN", "annual_rate": 0.12,
                                                  "payment": 300, "payment_frequency": "monthly"}}), None, TODAY)
    line = next(l for l in situation.brief(sit, "en").splitlines() if l.startswith("Debt"))
    assert line.endswith("interest 22") and "-" not in line.split("interest")[-1]


# -- unknown is never zero (findings 3, 5, 6) --------------------------------------


def test_a_debt_payment_without_a_rate_makes_the_surplus_unknown():
    # Repro sit1: a USD car payment with no USD/MXN rate used to count as 0 (surplus 40,000, complete).
    car = {"kind": "auto", "balance": 20000, "currency": "USD", "annual_rate": 0.08, "payment": 1000,
           "payment_frequency": "monthly", "in_spending": False}
    flow = build(_snap({**BASE, "liability.car": car}), None, TODAY)["cash_flow"]
    assert flow["surplus"] is None and flow["complete"] is False
    assert flow["debt_payments"] is None and flow["missing_fx"] == ["USD/MXN"]
    assert flow["debt_payments_unconverted"] == [{"amount": 1000, "currency": "USD"}]
    # With a rate: 85,000 - 45,000 - 1,000 x 18 = 22,000.
    flow = build(_snap({**BASE, "liability.car": car}), USD_MXN_18, TODAY)["cash_flow"]
    assert flow["surplus"] == 22000 and flow["complete"] is True and flow["debt_payments"] == 18000


def test_dca_plans_in_their_stored_shape_are_committed():
    # Repro sit2: dca.plan_fact stores {"plans": [...]}; it read as one plan with no legs (0 committed).
    plan = {"id": "sp500", "account_id": "gbm", "currency": "MXN", "cadence": "monthly", "day_of_month": 5,
            "start_date": "2026-01-05", "legs": [{"instrument_id": "VOO", "amount": 10000}]}
    stored = dca.plan_fact(plan, TODAY)["value"]
    sit = build(_snap({**BASE, "planning.dca": stored}), None, TODAY)
    assert [(d["id"], d["value"]) for d in sit["dca"]] == [("sp500", 10000)]
    # 85,000 - 45,000 = 40,000 surplus; 10,000 committed; 30,000 left.
    assert sit["commitments"]["total"] == 10000 and sit["commitments"]["unallocated"] == 30000
    assert sit["commitments"]["complete"] is True


def test_a_goal_contribution_without_a_rate_makes_commitments_incomplete():
    goal = {"id": "college", "name": "College", "monthly_contribution": 2000, "currency": "USD",
            "target_amount": 100000, "target_date": "2036-09-01"}
    commitments = build(_snap({**BASE, "goals": [goal]}), None, TODAY)["commitments"]
    assert commitments["complete"] is False and commitments["missing_fx"] == ["USD/MXN"]
    assert commitments["unallocated"] is None and commitments["overcommitted"] is None
    assert commitments["unconverted"][0]["id"] == "college"
    # With a rate: 2,000 x 18 = 36,000 of the 40,000 surplus; 4,000 left.
    commitments = build(_snap({**BASE, "goals": [goal]}), USD_MXN_18, TODAY)["commitments"]
    assert commitments["total"] == 36000 and commitments["unallocated"] == 4000 and commitments["complete"] is True


def _spending_ledger(extra_fx=()):
    bank = {"id": "bank", "institution": "BBVA", "type": "checking", "currency": "MXN"}
    card = {"id": "amex", "institution": "Amex", "type": "credit_card", "currency": "USD"}
    entries = [{"id": "ob", "account_id": "bank", "kind": "opening_balance", "date": "2026-06-01", "amount": "100000",
                "currency": "MXN"}]
    for month in (6, 7, 8):
        for n, (account, amount, ccy, text) in enumerate((("bank", "-20000", "MXN", "RENTA"),
                                                          ("amex", "-1000", "USD", "HOTEL MARRIOTT"),
                                                          ("bank", "-5000", "MXN", "XYZ 123"))):
            entries.append({"id": f"e{month}{n}", "account_id": account, "kind": "expense", "date": f"2026-{month:02d}-10",
                            "amount": amount, "currency": ccy, "description": text})
    return {"accounts": [bank, card], "instruments": [], "fx": list(extra_fx), "entries": entries}


def test_ledger_spending_with_an_unconvertible_card_is_not_complete():
    # Repro sit3: the USD card spending had no rate and silently dropped out (complete surplus).
    snap = _snap({"income.salary": BASE["income.salary"]})
    sit = build(snap, _spending_ledger(), TODAY)
    spending, flow = sit["spending"], sit["cash_flow"]
    # June and July are the full months: 20,000 rent + 5,000 unclassified a month, the USD part unknown.
    assert spending["source"] == "ledger" and spending["ledger_months"] == 2 and spending["total"] == 25000
    assert spending["complete"] is False and spending["missing_fx"] == ["USD/MXN"]
    assert flow["complete"] is False and flow["missing_fx"] == ["USD/MXN"]
    assert any(u["code"] == "fx" and u["pair"] == "USD/MXN" for u in sit["unknowns"])
    # Unclassified spending counts as essential (the cash-flow rule): 20,000 housing + 5,000 unknown.
    assert spending["essential"] == 25000 and "essential" in spending["essential_rule"]
    # With the rates: 25,000 + 1,000 x 18 = 43,000 a month; discretionary (travel) 18,000.
    rates = [{"base": "USD", "quote": "MXN", "rate": "18", "date": f"2026-{m:02d}-10"} for m in (6, 7, 8)]
    spending = build(snap, _spending_ledger(rates), TODAY)["spending"]
    assert spending["total"] == 43000 and spending["complete"] is True and spending["discretionary"] == 18000


def test_a_ledger_without_bank_or_card_spending_does_not_replace_stated_spending():
    # Only brokerage-like activity over three months: the ledger says nothing about spending (not 0).
    account = {"id": "brk", "institution": "GBM", "type": "other", "currency": "MXN"}
    entries = [{"id": "ob", "account_id": "brk", "kind": "opening_balance", "date": "2026-05-01", "amount": "50000",
                "currency": "MXN"}]
    entries += [{"id": f"i{m}", "account_id": "brk", "kind": "income", "date": f"2026-0{m}-15", "amount": "100",
                 "currency": "MXN"} for m in (5, 6, 7, 8)]
    ledger = {"accounts": [account], "instruments": [], "fx": [], "entries": entries}
    sit = build(_snap(BASE), ledger, TODAY)
    assert sit["spending"]["source"] == "stated" and sit["spending"]["monthly"] == 45000
    assert sit["cash_flow"]["surplus"] == 40000
    # Without stated spending it is unknown, never a surplus equal to the income.
    sit = build(_snap({"income.salary": BASE["income.salary"]}), ledger, TODAY)
    assert sit["spending"]["monthly"] is None and sit["cash_flow"]["surplus"] is None


def test_situation_fx_uses_the_newest_rate_of_the_pair_or_its_inverse():
    # Repro fx1 through build: a 2023 USD/MXN 17 must not beat a 2026 MXN/USD 0.05.
    ledger = {"accounts": [], "instruments": [], "entries": [],
              "fx": [{"base": "USD", "quote": "MXN", "rate": "17", "date": "2023-01-02"},
                     {"base": "MXN", "quote": "USD", "rate": "0.05", "date": "2026-09-18"}]}
    sit = build(_snap({"cash.usd": {"amount": 1000, "currency": "USD"}, "client.profile": {"reporting_currency": "MXN"}}),
                ledger, TODAY)
    assert sit["net_worth"]["total"] == 20000
    assert sit["fx"] == [{"pair": "MXN/USD", "rate": 0.05, "date": "2026-09-18", "source": "ledger"}]
    # Only a stale rate: unknown, listed, never the stale value.
    stale = dict(ledger, fx=ledger["fx"][:1])
    assert build(_snap({"cash.usd": {"amount": 1000, "currency": "USD"}, "client.profile": {"reporting_currency": "MXN"}}),
                 stale, TODAY)["net_worth"]["complete"] is False


# -- never counted twice (QA coherence a, b, c, f) ------------------------------------


@pytest.mark.parametrize("a,b,same", [
    ("Schwab", "Charles Schwab & Co., Inc.", True), ("BBVA", "BBVA México", True), ("Bancomer", "BBVA", True),
    ("GBM+", "Grupo Bursátil Mexicano", True), ("Nu", "Nu México", True), ("IBKR", "Interactive Brokers", True),
    ("Banorte", "Banorte Tarjeta", True), ("Schwab", "BBVA", False), ("GBM", "Nu", False), ("Fidelity", None, False),
])
def test_institutions_match_loosely(a, b, same):
    assert same_institution(a, b) is same


SCHWAB_FX = [{"from": "USD", "to": "MXN", "rate": "18.25", "as_of": "2026-08-31"}]


@pytest.mark.parametrize("cash_name,broker_name", [("BBVA", "Schwab"), ("BBVA México", "Charles Schwab")])
def test_a_statement_supersedes_a_stated_balance_named_differently(cash_name, broker_name):
    # QA (a): "Schwab" vs "Charles Schwab" and "BBVA" vs "BBVA México" were counted twice (+74 %).
    facts = {"client.profile": {"reporting_currency": "MXN"},
             "cash.checking": {"amount": 26000, "currency": "MXN", "institution": cash_name},
             "investment.brokerage": {"amount": 38000, "currency": "USD", "institution": broker_name},
             "account.schwab-5678": _statement("schwab-5678", "Charles Schwab", "brokerage", "USD", {"USD": 38601.05},
                                               fx=SCHWAB_FX),
             "account.bbva-6789": _statement("bbva-6789", "BBVA México", "checking", "MXN", {"MXN": 26500})}
    sit = build(_snap(facts), None, TODAY)
    # 26,500 + 38,601.05 x 18.25 (704,469.16) = 730,969.16; the stated 26,000 and 38,000 USD are not added.
    assert sit["net_worth"]["total"] == 730969.16
    assert [r["counted"] for r in sit["cash"]] == [False] and [r["counted"] for r in sit["investments"]] == [False]
    assert [d["key"] for d in sit["differences"] if d.get("statement")] == ["investment.brokerage"]


def test_a_legacy_household_account_and_its_statement_are_counted_once():
    # QA (b): household "schwab-brokerage" and statement "schwab-5678" are the same account.
    household = {"as_of": "2026-09-01", "currency": "USD",
                 "accounts": [{"id": "schwab-brokerage", "institution": "Charles Schwab", "currency": "USD"}],
                 "positions": [{"account_id": "schwab-brokerage", "instrument_id": "VTI", "symbol": "VTI",
                                "quantity": 100, "value": 25000, "currency": "USD"}]}
    positions = [{"symbol": "VTI", "quantity": 100, "value": 25000, "currency": "USD", "asset_class": "equity"},
                 {"symbol": "AAPL", "quantity": 10.5, "value": 2101.05, "currency": "USD", "asset_class": "equity"}]
    facts = {"household": household,
             "account.schwab-5678": _statement("schwab-5678", "Charles Schwab", "brokerage", "USD",
                                               {"USD": 27101.05}, positions=positions)}
    sit = build(_snap(facts), None, TODAY)
    assert sit["net_worth"]["total"] == 27101.05
    assert [a["key"] for a in sit["accounts"]] == ["account.schwab-5678"]
    brief = situation.brief(sit, "en")
    assert brief.count("VTI 100 units") == 1
    # QA (e): fractional units are kept.
    assert "AAPL 10.5 units" in brief


def test_a_credit_card_is_a_card_and_a_stated_card_is_not_added_to_its_statement():
    # QA (c): "car" matched inside "credit_card", and the stated card was added to the statement one (4,400).
    assert _liability_kind("credit_card") == "card" and _liability_kind(None, "Tarjeta de crédito") == "card"
    assert _liability_kind("car") == "auto" and _liability_kind(None, "Scarlet loan") == "other"
    card = {"id": "liability-credit-card", "name": "Tarjeta de crédito", "value": 2200, "currency": "MXN",
            "monthly_payment": 220, "interest_rate": 0.455}
    facts = {"liability.banorte_card": {"kind": "card", "balance": 2200, "currency": "MXN", "lender": "Banorte",
                                        "annual_rate": 0.455, "payment": 1000, "payment_frequency": "monthly"},
             "cash.checking": {"amount": 50000, "currency": "MXN"},
             "account.banorte-1234": _statement("banorte-1234", "Banorte", "credit_card", "MXN", {}, positions=[],
                                                liabilities=[card])}
    sit = build(_snap(facts), None, TODAY)
    assert [(r["kind"], r["balance"], r["source"]) for r in sit["liabilities"]] == [("card", 2200, "statement")]
    assert sit["net_worth"]["liabilities"] == 2200 and sit["net_worth"]["total"] == 47800
    # A different stated balance is kept as a difference, not added.
    facts["liability.banorte_card"] = dict(facts["liability.banorte_card"], balance=3000)
    sit = build(_snap(facts), None, TODAY)
    assert sit["net_worth"]["liabilities"] == 2200
    assert [(d["kind"], d["stated"]["amount"], d["difference"]) for d in sit["differences"]] == [("liability", 3000, -800)]


def test_keep_my_figure_uses_the_stated_balance_and_supersedes_the_statement():
    # QA (f): after "keep", the stated 30,000 counts, the statement is out of net worth, and no Difference line.
    facts = {"investment.brokerage": {"amount": 30000, "currency": "USD", "institution": "Charles Schwab"},
             "account.schwab-5678": _statement("schwab-5678", "Charles Schwab", "brokerage", "USD", {"USD": 38601.05})}
    kept = [{"key": "investment.brokerage", "proposed_key": "account.schwab-5678",
             "current_fact_id": "id-investment.brokerage", "as_of": "2026-08-31"}]
    before = build(_snap(facts), None, TODAY)
    assert before["net_worth"]["total"] == 38601.05 and before["differences"]
    sit = build(_snap(facts, kept=kept), None, TODAY)
    assert sit["net_worth"]["total"] == 30000
    assert sit["accounts"][0]["superseded_by"] == "investment.brokerage"
    assert sit["investments"][0]["counted"] is True and sit["differences"] == []
    brief = situation.brief(sit, "en")
    assert "Difference" not in brief and "Charles Schwab 38,601" not in brief
    # A newer statement is new evidence: the choice no longer hides it.
    newer = dict(facts, **{"account.schwab-5678": _statement("schwab-5678", "Charles Schwab", "brokerage", "USD",
                                                             {"USD": 40000}, as_of="2026-09-15")})
    assert build(_snap(newer, kept=kept), None, TODAY)["net_worth"]["total"] == 40000
    # So does a changed stated value.
    changed = dict(kept[0], current_fact_id="an-older-fact")
    assert build(_snap(facts, kept=[changed]), None, TODAY)["net_worth"]["total"] == 38601.05
