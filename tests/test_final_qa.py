"""Regressions from the final end-to-end QA (Mariana in Mexico City, Sam in Texas, Mariana six months later)
and the audit that followed.  Each test is built from those personas' answers and statements."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from tests.fixtures.ingest import statements as fx
from wealth import onboarding as ob
from wealth import proactive, review, situation
from wealth.profile import _value_spans
from wealth.service import WealthService, upload_dir
from wealth.situation.model import build
from wealth.store import WealthStore

TODAY = "2026-09-21"
MARIANA = [  # p1_mx: what she answered on the cards
    ("identity", {"name": "Mariana", "country": "MX", "city": "Ciudad de México"}),
    ("about", {"birth_year": 1988, "dependents": "1"}),
    ("income", {"amount": "72 mil"}),
    ("spending", {"amount": "48 mil"}),
    ("money", {"items": {"bank": {"amount": 35000}, "brokerage": {}, "afore": {"amount": 410000},
                         "cetes": {"amount": "15k"}}}),
    ("goals", {"goals": ["emergency_fund", "retirement"]}),
]
SAM = [  # p2_us
    ("identity", {"name": "Sam", "country": "US", "region": "Texas"}),
    ("income", {"amount": "8k"}),
    ("spending", {"amount": "$5,200"}),
    ("money", {"items": {"bank": {"amount": 9000}, "brokerage": {}, "retirement": {"amount": 120000}}}),
    ("debts", {"items": {"card": {"balance": 3200, "rate": 24}}}),
]


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.delenv("WEALTH_UPLOAD_DIR", raising=False)
    return WealthService(tmp_path / "w.sqlite3")


def _answer(service, client, answers, lang):
    service.create(client, client)
    for step, answer in answers:
        ob.apply(service, client, step, answer, language=lang)


def _upload(service, client, name, data):
    folder = upload_dir(client, service.db_path)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(data)
    shown = service.ingest(client, "file", {"path": name})
    return shown


def _confirm(service, client, shown):
    return service.ingest(client, "confirm", {"proposal_id": shown["result"]["proposal_id"],
                                              "acknowledge_discrepancies": shown["status"] == "needs_review"})


def said(key, value, observed="2026-09-10"):
    return {"key": key, "value": value, "status": "active", "confidence": "reported", "id": f"id-{key}",
            "source": {"kind": "user", "ref": "chat", "observed_on": observed}}


def statement(account_id, institution, kind, native, *, as_of="2026-08-31", expires=None, liabilities=None):
    positions = [{"symbol": f"CASH:{c}", "instrument_id": f"CASH:{c}", "value": v, "currency": c, "asset_class": "cash"}
                 for c, v in native.items()]
    return {"key": f"account.{account_id}", "status": "active", "confidence": "reported", "id": f"id-{account_id}",
            "source": {"kind": "document", "ref": f"{account_id}.pdf", "observed_on": as_of}, "expires_on": expires,
            "value": {"account": {"id": account_id, "institution": institution, "type": kind, "currency": "MXN"},
                      "as_of": as_of, "positions": positions, "fx": [], "liabilities": liabilities or []}}


BASE = [said("client.profile", {"reporting_currency": "MXN"}),
        said("income.salary", {"amount": 85000, "currency": "MXN", "frequency": "monthly", "net": True}),
        said("spending.monthly", {"total": 45000, "essential": 30000, "currency": "MXN"})]


def snap(*facts):
    return {"client": {"revision": 1}, "facts": [*BASE, *facts]}


# ------------------------------------------------------------------ blocker 1: the right stated account


def test_a_gbm_statement_is_never_compared_with_the_afore_and_never_prints_question_marks(service):
    _answer(service, "mariana", MARIANA, "es")
    shown = _upload(service, "mariana", "gbm.pdf", fx.gbm_multicurrency())
    compared = [i for i in shown["result"]["insights"] if i["kind"] == "stated_vs_statement"]
    # Before: "They said about MXN 410,000 at GBM" (the AFORE) and "They said about MXN ? at GBM".
    assert not any(i["key"] == "investment.afore" for i in compared)
    assert not any("?" in i["text"] for i in shown["result"]["insights"])
    bank = _upload(service, "mariana", "bbva.pdf", fx.bbva_checking())
    assert not [i for i in bank["result"]["insights"] if i["kind"] == "stated_vs_statement"]


def test_a_schwab_statement_is_not_compared_with_the_401k(service):
    _answer(service, "sam", SAM, "en")
    shown = _upload(service, "sam", "schwab.pdf", fx.us_brokerage(second_account=True))
    texts = [i["text"] for i in shown["result"]["insights"]]
    # Before: "They said about USD 120,000 at Charles Schwab" (really the 401(k)/IRA) and "USD ? at Charles Schwab".
    assert not any("120,000" in t for t in texts) and not any("?" in t for t in texts)


def test_matching_is_by_institution_or_a_name_naming_it_with_a_compatible_kind():
    gbm = {"institution": "GBM", "type": "brokerage"}
    assert situation.model.stated_matches({"name": "GBM / casa de bolsa", "kind": "brokerage"}, gbm)
    assert situation.model.stated_matches({"institution": "GBM+", "kind": "brokerage"}, gbm)
    assert not situation.model.stated_matches({"name": "AFORE", "kind": "afore"}, gbm)
    assert not situation.model.stated_matches({"institution": "GBM", "kind": "afore"}, gbm)  # never retirement -> brokerage
    assert not situation.model.stated_matches({"name": "Brokerage", "kind": "brokerage"}, gbm)  # a type is not a firm
    card = {"institution": "BBVA México", "type": "credit_card"}
    assert not situation.model.stated_matches({"institution": "BBVA"}, card, cash=True)
    assert situation.model.stated_matches({"institution": "BBVA"}, {"institution": "BBVA México", "type": "checking"}, cash=True)


# ------------------------------------------------------------------ blocker 2: a statement settles the unsized account


def test_onboarding_names_the_institution_and_the_statement_settles_it(service):
    _answer(service, "mariana", MARIANA, "es")
    facts = {f["key"]: f["value"] for f in service.inspect("mariana")["facts"]}
    assert facts["investment.brokerage"]["institution"] == "GBM"
    assert facts["investment.cetes"]["institution"] == "Cetesdirecto"
    assert facts["cash.bank"]["name"] == "Banco" and "institution" not in facts["cash.bank"]  # the generic chip is no bank
    sit = service.situation("mariana")
    assert sit["net_worth"]["total"] is None and ob.picture(sit, "es")["line"].startswith("Falta el saldo de una cuenta")
    _confirm(service, "mariana", _upload(service, "mariana", "gbm.pdf", fx.gbm_multicurrency()))
    _confirm(service, "mariana", _upload(service, "mariana", "bbva.pdf", fx.bbva_checking()))
    sit = service.situation("mariana", today=TODAY)
    # Before: "sin contar GBM / casa de bolsa" beside GBM $217,837 and no net worth.
    assert sit["net_worth"]["unknown_balances"] == [] and sit["net_worth"]["total"] == 722587.35
    brokerage = next(i for i in sit["investments"] if i["key"] == "investment.brokerage")
    assert brokerage["counted"] is False and brokerage["covered_by"]
    text = " ".join(s["text"] for s in situation.sentences(sit, "es"))
    assert "sin contar" not in text and "aún no sé" not in text


def test_a_nu_chip_names_nu():
    facts, *_ = ob.build_facts({**build(snap(said("client.profile", {"residence": {"country": "MX"}})), None, TODAY)},
                               "money", {"items": {"nu": {"amount": 40000}}}, language="es", today=TODAY)
    nu = next(f for f in facts if f["key"] == "cash.nu")
    assert nu["value"]["institution"] == "Nu" and nu["value"]["currency"] == "MXN"


# ------------------------------------------------------------------ blocker 3: the goal target


def test_a_target_goes_to_the_home_not_to_retirement(service):
    _answer(service, "sam", SAM, "en")
    result = ob.apply(service, "sam", "goals", {"goals": ["retirement", "home"], "target_amount": 60000,
                                                "target_year": 2029}, language="en")
    goals = {g["id"]: g for g in service.situation("sam")["goals"]}
    assert goals["home-down-payment"]["target_amount"] == 60000 and goals["retirement"]["target_amount"] is None
    assert result["card"]["step"] != "goals"


def test_an_ambiguous_target_is_asked_not_guessed(service):
    _answer(service, "sam", SAM, "en")
    result = ob.apply(service, "sam", "goals", {"goals": ["retirement", "grow"], "target_amount": 60000}, language="en")
    assert all(g["target_amount"] is None for g in service.situation("sam")["goals"])
    card = result["card"]
    assert card["step"] == "goals" and card["chooser"] == "target_goal"
    chooser = next(f for f in card["fields"] if f["name"] == "target_goal")
    assert [o["id"] for o in chooser["options"]] == ["retirement", "grow"]
    assert "not assigned" in result["answered"]["summary"]
    ob.apply(service, "sam", "goals", {**card["prefill"], "target_goal": "grow"}, language="en")
    goals = {g["id"]: g for g in service.situation("sam")["goals"]}
    assert goals["grow-wealth"]["target_amount"] == 60000


# ------------------------------------------------------------------ blocker 4 and audit 6: statement cash in the reserve


def test_statement_checking_counts_toward_the_reserve():
    sit = build(snap(said("cash.bank", {"amount": 35000, "currency": "MXN", "name": "Banco"}),
                     statement("bbva-6789", "BBVA México", "checking", {"MXN": 26500})), None, TODAY)
    assert sit["reserve"]["amount"] == 61500 and sit["reserve"]["sources"] == ["bank", "bbva-6789"]


def test_a_statement_keeps_the_reserve_designation_of_the_cash_it_settled():
    sit = build(snap(said("cash.bbva", {"amount": 150000, "currency": "MXN", "institution": "BBVA", "purpose": "reserve"}),
                     said("reserve", {"target_months": 6}),
                     statement("bbva-6789", "BBVA", "checking", {"MXN": 160000})), None, TODAY)
    assert sit["reserve"]["basis"] == "designated" and sit["reserve"]["amount"] == 160000


# ------------------------------------------------------------------ audit: the store and the picture


def test_a_bank_statement_never_replaces_a_stated_fund(tmp_path):
    with WealthStore(tmp_path / "s.db") as store:
        store.create_client("c", "C")
        store.remember("c", [{"key": "investment.fondos", "value": {"amount": 500000, "currency": "MXN", "kind": "fund"},
                              "source": {"kind": "user", "ref": "chat", "observed_on": "2026-09-01"}}])
        store.remember("c", [{"key": "account.bbva-6789", "value": statement("bbva-6789", "BBVA", "checking",
                                                                             {"MXN": 26500})["value"],
                              "source": {"kind": "document", "ref": "bbva.pdf", "observed_on": "2026-08-31"}}])
        assert [h["status"] for h in store.history("c", "investment.fondos")] == ["active"]
        assert build(store.snapshot("c"), None, TODAY)["net_worth"]["total"] == 526500


def test_a_credit_card_statement_leaves_stated_cash_at_the_same_bank():
    card = {"id": "cc", "name": "Tarjeta", "value": 20000, "currency": "MXN", "interest_rate": 0.45}
    sit = build(snap(said("cash.bbva", {"amount": 150000, "currency": "MXN", "institution": "BBVA"}),
                     statement("bbva-9999", "BBVA México", "credit_card", {}, liabilities=[card])), None, TODAY)
    assert sit["net_worth"]["assets"] == 150000 and sit["net_worth"]["total"] == 130000


def test_an_older_statement_becomes_history_not_current(tmp_path):
    new, old = statement("gbm-1234", "GBM", "brokerage", {"MXN": 500000}), \
        statement("gbm-1234", "GBM", "brokerage", {"MXN": 420000}, as_of="2026-05-31")
    with WealthStore(tmp_path / "s.db") as store:
        store.create_client("c", "C")
        store.remember("c", [{"key": "account.gbm-1234", "value": new["value"], "source": new["source"]}])
        receipt = store.remember("c", [{"key": "account.gbm-1234", "value": old["value"], "source": old["source"]}])
        assert any("older than the saved one" in w for w in receipt["warnings"])
        current = next(f for f in store.snapshot("c")["facts"] if f["key"] == "account.gbm-1234")
        assert current["value"]["as_of"] == "2026-08-31"
        history = store.history("c", "account.gbm-1234")
        assert [h["value"]["as_of"] for h in history] == ["2026-05-31", "2026-08-31"]


def test_a_document_that_replaces_stated_spending_says_so(tmp_path):
    with WealthStore(tmp_path / "s.db") as store:
        store.create_client("c", "C")
        store.remember("c", [{"key": "spending.monthly", "value": {"total": 45000, "currency": "MXN"},
                              "source": {"kind": "user", "ref": "chat", "observed_on": "2026-09-01"}}])
        receipt = store.remember("c", [{"key": "spending.monthly", "value": {"total": 60000, "currency": "MXN"},
                                        "source": {"kind": "document", "ref": "stmt", "observed_on": "2026-09-20"}}])
        assert any("spending.monthly was updated from a document" in w for w in receipt["warnings"])


def test_one_account_under_two_ids_counts_once_the_newest():
    older = statement("bbva-6789", "BBVA", "checking", {"MXN": 26500}, as_of="2026-07-31")
    newer = statement("bbva-bancomer-6789", "BBVA Bancomer", "checking", {"MXN": 27000})
    sit = build(snap(older, newer), None, TODAY)
    assert sit["net_worth"]["total"] == 27000
    assert next(a for a in sit["accounts"] if a["id"] == "bbva-6789")["duplicate_of"] == "account.bbva-bancomer-6789"


def test_an_unknown_debt_payment_makes_the_surplus_unknown():
    sit = build(snap(said("liability.auto", {"kind": "auto", "balance": 300000, "currency": "MXN"})), None, TODAY)
    flow = sit["cash_flow"]
    assert flow["surplus"] is None and flow["surplus_before_unknown_debts"] == 40000
    assert flow["missing"] == ["liability.auto.payment"] and sit["commitments"]["unallocated"] is None


def test_an_expired_statement_leaves_the_net_worth_incomplete():
    sit = build(snap(said("cash.nu", {"amount": 100000, "currency": "MXN", "institution": "Nu"}),
                     statement("gbm-1234", "GBM", "brokerage", {"MXN": 500000}, as_of="2026-06-30",
                               expires="2026-08-14")), None, TODAY)
    assert sit["net_worth"]["complete"] is False and sit["net_worth"]["stale_accounts"] == ["GBM"]


def test_an_invest_goal_and_its_plan_are_committed_once():
    goal = {"id": "sp500", "name": "Invertir en el S&P 500", "action": "invest", "monthly_contribution": 9075,
            "currency": "MXN"}
    plan = {"plans": [{"id": "sp500-plan", "currency": "MXN", "cadence": "monthly",
                       "legs": [{"instrument_id": "CSPXN", "amount": "9075"}]}]}
    sit = build(snap(said("goals", [goal, {"id": "casa", "name": "Casa", "monthly_contribution": 8000,
                                           "currency": "MXN"}]),
                     said("planning.dca", plan)), None, TODAY)
    assert sit["commitments"]["total"] == 17075
    assert sit["dca"][0]["same_as_goal"] == "sp500"


# ------------------------------------------------------------------ the brief and the sentences


def test_the_returning_brief_uses_names_spanish_and_a_cash_line():
    stale = statement("bbva-6789", "BBVA México", "checking", {"MXN": 26500}, expires="2026-09-30")
    activity = {**stale, "key": "account.bbva-6789.activity", "value": {"as_of": "2026-08-31", "transactions": []}}
    facts = [stale, activity,
             said("income.salary", {"amount": 72000, "currency": "MXN", "frequency": "monthly", "net": True},
                  observed="2026-09-21") | {"expires_on": "2026-12-20"},
             said("investment.brokerage", {"currency": "MXN", "kind": "brokerage", "name": "GBM", "institution": "GBM",
                                           "balance_unknown": True}) | {"expires_on": "2026-12-20"}]
    returning = build({"client": {"revision": 1}, "facts": [said("client.profile", {"residence": {"country": "MX"}}),
                                                             *facts]}, None, "2027-03-22")
    text = situation.brief(returning, "es")
    reconfirm = next(line for line in text.splitlines() if line.startswith("Por reconfirmar"))
    # Before: "account.bbva-6789; account.bbva-6789.activity" and "72,000 MXN monthly".
    assert "account." not in reconfirm and ".activity" not in text and "monthly" not in text
    assert "BBVA México" in reconfirm and "72,000 MXN al mes" in reconfirm
    assert "investment.brokerage" not in returning["stale"]  # never known: asked for, not reconfirmed
    current = build(snap(statement("bbva-6789", "BBVA México", "checking", {"MXN": 26500})), None, TODAY)
    brief = situation.brief(current, "es")
    assert "Efectivo: BBVA México 26,500" in brief and "checking" not in brief and "Inversiones" not in brief


def test_sentences_keep_the_persons_words():
    sit = build(snap(said("client.profile", {"residence": {"country": "MX", "city": "Ciudad de México"}}),
                     said("investment.afore", {"amount": 410000, "currency": "MXN", "kind": "afore", "name": "AFORE",
                                               "approximate": True}),
                     said("investment.brokerage", {"currency": "USD", "kind": "brokerage", "name": "Brokerage",
                                                   "balance_unknown": True})), None, TODAY)
    es = [s["text"] for s in situation.sentences(sit, "es")]
    en = [s["text"] for s in situation.sentences(sit, "en")]
    assert "Vives en Ciudad de México, México." in es
    assert "Tienes unos $410,000 invertidos en tu AFORE." in es
    assert "You have a brokerage account; I don't know its balance yet." in en
    us = build(snap(said("client.profile", {"residence": {"country": "US", "region": "Texas"}})), None, TODAY)
    assert "You live in Texas, United States." in [s["text"] for s in situation.sentences(us, "en")]
    facts, _, summary, _ = ob.build_facts(us, "identity", {"name": "Sam", "country": "US", "region": "Texas"},
                                          language="en", today=TODAY)
    assert summary == "Sam, in Texas, United States"


# ------------------------------------------------------------------ proactive


def test_a_card_at_42_percent_is_an_act_item_even_without_the_payment():
    sit = build(snap(said("liability.card", {"kind": "card", "balance": 18000, "annual_rate": 0.42, "currency": "MXN"})),
                None, TODAY)
    found = proactive.evaluate(sit, None, {"facts": []}, TODAY)
    item = next(i for i in found["candidates"] if i["kind"] == "high_interest_debt")
    assert item["severity"] == "act" and item["title"]["es"] == "Tu tarjeta cobra 42%: pagarla es tu mejor inversión"
    assert item["data"]["payment_unknown"] is True and item["data"]["monthly_to_clear"] > 1700
    assert "Dime cuánto pagas" in item["why"]["es"] and "Antes de ese pago te quedan $40,000" in item["why"]["es"]
    assert {"kind": "surplus", "missing": ["card payment"]} in found["unknown"]
    paying = build(snap(said("liability.card", {"kind": "card", "balance": 18000, "annual_rate": 0.42, "currency": "MXN",
                                                "payment": 1200, "payment_frequency": "monthly"})), None, TODAY)
    item = next(i for i in proactive.evaluate(paying, None, {"facts": []}, TODAY)["candidates"]
                if i["kind"] == "high_interest_debt")
    assert item["data"]["interest_saved"] > 0 and "te ahorras" in item["why"]["es"]


def test_overdue_statements_count_documents_not_sub_accounts():
    facts = [statement("gbm-4567", "GBM", "brokerage", {"MXN": 217837}),
             statement("gbm-4321", "GBM", "brokerage", {"USD": 1000}),
             statement("bbva-6789", "BBVA México", "checking", {"MXN": 26500})]
    facts.append({**facts[2], "key": "account.bbva-6789.activity", "value": {"as_of": "2026-08-31", "transactions": []}})
    sit = build(snap(*facts), None, "2027-03-22")
    item = next(i for i in proactive.evaluate(sit, None, {"facts": facts}, "2027-03-22")["candidates"]
                if i["kind"] == "statement_overdue")
    assert item["title"]["en"].endswith("(+1 more)")  # before: "(+3 more)" from sub-accounts and .activity


def test_value_spans_count_like_javascript():
    title = "Espacio en tu PPR de $48,000 antes del 31 de dic"
    [[a, b]] = _value_spans(title)
    assert title[a:b] == "$48,000"
    emoji = "🎉 Tu PPR: $48,000"
    assert _value_spans(emoji) == [[11, 18]]  # the emoji is two UTF-16 units, as the page slices it


# ------------------------------------------------------------------ onboarding progress


def test_remaining_counts_only_steps_that_are_actually_pending():
    full = build(snap(said("client.profile", {"name": "Ana", "residence": {"country": "MX"}, "birth_year": 1990,
                                              "dependents": 0}),
                      said("cash.nu", {"amount": 1000, "currency": "MXN", "institution": "Nu"}),
                      said("goals", [{"id": "casa", "name": "Casa"}])), None, TODAY)
    progress = ob.progress(full)
    assert "income" in progress["known"] and "identity" in progress["known"]
    assert set(progress["pending"]) <= {"debts", "risk"}


# ------------------------------------------------------------------ review


def _ledger(entries):
    return {"accounts": [{"id": "gbm", "type": "brokerage", "currency": "MXN", "institution": "GBM"},
                         {"id": "bbva", "type": "checking", "currency": "MXN", "institution": "BBVA"}],
            "entries": entries, "instruments": [], "fx": [], "assertions": []}


def test_the_review_says_unknown_before_the_first_statement():
    ledger = _ledger([
        {"id": "e1", "account_id": "gbm", "kind": "opening_balance", "date": "2026-08-31", "amount": "100000",
         "currency": "MXN"},
        {"id": "e2", "account_id": "bbva", "kind": "opening_balance", "date": "2026-08-01", "amount": "20000",
         "currency": "MXN"}])
    report = review.quarterly({"ledger": ledger, "currency": "MXN", "tax": {"jurisdiction": "MX"}},
                              "2026-07-01", "2026-09-30")
    sections = report["result"]["sections"]
    assert sections["net_worth"]["data"]["start"] is None  # before: "Inicio $0"
    assert any(m["key"].startswith("ledger.start") for m in sections["net_worth"]["missing"])
    assert sections["taxes"]["data"]["period"]["estimated_tax"]["total"] is None
    assert sections["fees"]["data"]["annual_cost"]["low"] is None  # before: "$0 · 0 pb" beside a floor note
    later = review.quarterly({"ledger": ledger, "currency": "MXN"}, "2026-10-01", "2026-12-31")["result"]["sections"]
    assert later["net_worth"]["data"]["contributions"]["net"] is None  # before: "Aportaste $0 netos"
    assert later["cash_flow"]["data"]["current"] is None and later["fees"]["data"]["paid_in_period"]["total"] is None


def test_prior_period_and_goal_months():
    assert review._prior_period("2026-01-15", "2026-04-14") == ("2025-10-17", "2026-01-14")
    assert review._prior_period("2026-07-01", "2026-09-30") == ("2026-04-01", "2026-06-30")
    assert review._months_left(date(2026, 9, 1), date(2026, 9, 30)) == 1
    assert review._months_left(date(2026, 9, 1), date(2026, 12, 31)) == 4
    assert review._months_left(date(2026, 9, 30), date(2026, 9, 30)) == 0


def test_an_unclassified_holding_blocks_band_judgement():
    ledger = _ledger([{"id": "o", "account_id": "gbm", "kind": "opening_balance", "date": "2026-06-01",
                       "instrument_id": "MYSTERY", "quantity": "1000", "currency": "MXN", "cost_basis": "1000000"},
                      {"id": "c", "account_id": "gbm", "kind": "opening_balance", "date": "2026-06-01",
                       "amount": "50000", "currency": "MXN"}])
    ledger["instruments"] = [{"id": "MYSTERY", "symbol": "MYSTERY", "currency": "MXN"}]
    ips = {"allocation": {"sleeves": [{"id": "cash", "asset": "cash", "target": 0.05, "min": 0.0, "max": 0.1}]}}
    prices = {"MYSTERY": [{"date": "2026-09-30", "price": "1000"}, {"date": "2026-06-30", "price": "1000"}]}
    report = review.quarterly({"ledger": ledger, "currency": "MXN", "ips": ips, "prices": prices},
                              "2026-07-01", "2026-09-30")
    alloc = report["result"]["sections"]["allocation"]["data"]
    assert alloc["unclassified"] == ["MYSTERY"] and alloc["portfolio_value"] is None
    assert all(s["outside_band"] is None for s in alloc["sleeves"])
    assert not any(c["kind"] == "drift" for c in report["result"]["sections"]["next_quarter"]["data"]["candidates"])


def test_the_review_page_hides_what_is_unknown():
    page = (Path(review.__file__).with_name("review.html")).read_text(encoding="utf-8")
    assert "['next', (s.next_quarter.items || []).length ? next(s.next_quarter) : null]" in page
    assert "feesUnknown" in page and "data.sections ? periodName(data.period) : t('title')" in page
