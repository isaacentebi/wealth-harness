"""Proactive engine: each trigger fires and stays quiet, unknown is not zero, taste, dismissal, calendars."""
from __future__ import annotations

from datetime import date

import pytest

from wealth import proactive, situation
from wealth.service import WealthService

AS_OF = "2026-09-21"
DOC = {"kind": "document", "ref": "estado de cuenta"}
OWNER = [{"person_id": "p1", "share": "1"}]

MX = {"name": "Lucía", "residence": {"country": "MX"}, "tax_residence": ["MX"], "language": "es"}
US = {"name": "Ann", "residence": {"country": "US"}, "tax_residence": ["US"], "us_person": True, "language": "en"}

IPS = {"version": 1, "currency": "MXN", "allocation": {"model": "balanced", "sleeves": [
    {"id": "global_equity", "name": "Global equity", "asset": "equity", "target": 0.6, "min": 0.55, "max": 0.65},
    {"id": "mx_fixed_income", "name": "Mexican government fixed income", "asset": "fixed_income",
     "target": 0.35, "min": 0.3, "max": 0.4},
    {"id": "cash", "name": "Cash", "asset": "cash", "target": 0.05, "min": 0.0375, "max": 0.0625}]},
    "constraints": {"concentration": {"limit": 0.1}}}


# ------------------------------------------------------------------ fixtures


def snap(facts, observed="2026-09-01"):
    rows = []
    for index, fact in enumerate(facts, 1):
        key, value = fact[0], fact[1]
        extra = fact[2] if len(fact) > 2 else {}
        rows.append({"id": f"f{index}", "key": key, "value": value, "confidence": "reported", "status": "active",
                     "source": {"kind": "user", "ref": "test", "observed_on": observed}, "revision": index, **extra})
    return {"client": {"id": "c", "revision": len(rows)}, "facts": rows, "decisions": []}


def ledger(entries, accounts=None, instruments=None):
    accounts = accounts or [{"id": "chk", "institution": "BBVA", "type": "checking", "currency": "MXN", "owners": OWNER}]
    rows = []
    for index, entry in enumerate(entries, 1):
        rows.append({"confidence": "reported", "source": DOC, "currency": "MXN", "seq": index, **entry})
    return {"accounts": accounts, "instruments": instruments or [], "entries": rows, "fx": [], "assertions": [],
            "labels": [], "category_rules": []}


def e(id_, day, kind, amount, description=None, account="chk", **extra):
    row = {"id": id_, "account_id": account, "kind": kind, "date": day, "amount": str(amount)}
    if description:
        row["description"] = description
    row.update(extra)
    return row


def evaluate(facts, led=None, as_of=AS_OF, **kw):
    s = snap(facts)
    sit = situation.build(s, led, as_of)
    return proactive.evaluate(sit, led, s, as_of, **kw)


def today(facts, led=None, as_of=AS_OF, **kw):
    s = snap(facts)
    sit = situation.build(s, led, as_of)
    return proactive.today(sit, led, s, as_of, **kw)


def kinds(found):
    return {item["kind"]: item for item in found["candidates"]}


def unknown(found, kind):
    return next((u["missing"] for u in found["unknown"] if u["kind"] == kind), None)


def account_fact(account_id, positions, as_of="2026-09-15", currency="MXN", kind="brokerage"):
    return ("account." + account_id, {"proposal_id": "p-" + account_id, "as_of": as_of, "currency": currency,
                                      "account": {"id": account_id, "institution": account_id.upper(), "type": kind,
                                                  "currency": currency},
                                      "positions": [{"currency": currency, **p} for p in positions]})


# ------------------------------------------------------------------ surplus


def _checking(opening):
    rows = [e("o", "2026-06-01", "opening_balance", opening)]
    for month in ("06", "07", "08", "09"):
        rows.append(e(f"r{month}", f"2026-{month}-05", "expense", -20000, "RENTA DEPTO"))
        rows.append(e(f"s{month}", f"2026-{month}-15", "income", 25000, "PAGO DE NOMINA", subtype="salary"))
    return ledger(rows)


def test_surplus_fires_when_checking_stays_above_one_and_a_half_months_of_spending():
    item = kinds(evaluate([("client.profile", MX)], _checking(100000)))["surplus"]
    assert item["data"]["month_end_balances"] == {"2026-08-31": 115000, "2026-07-31": 110000}
    assert item["data"]["idle"] == 85000 and item["title"]["es"].startswith("Tienes $85,000")


def test_surplus_quiet_below_the_buffer_and_unknown_without_an_opening_balance():
    assert "surplus" not in kinds(evaluate([("client.profile", MX)], _checking(20000)))
    led = _checking(100000)
    led["entries"] = [x for x in led["entries"] if x["kind"] != "opening_balance"]
    found = evaluate([("client.profile", MX)], led)
    assert "surplus" not in kinds(found)
    assert unknown(found, "surplus")  # falls back to the stated flow, which is unknown


def test_surplus_from_the_stated_flow_needs_income_and_spending():
    facts = [("client.profile", MX), ("income.salary", {"amount": 50000, "currency": "MXN", "frequency": "monthly"}),
             ("spending.monthly", {"total": 30000, "currency": "MXN"})]
    assert kinds(evaluate(facts))["surplus"]["data"]["unallocated_monthly"] == 20000
    found = evaluate(facts[:2])
    assert "surplus" not in kinds(found) and "spending" in unknown(found, "surplus")


# ------------------------------------------------------------------ reserve and cash drag


def _reserve(cash, target=6, essential=20000):
    facts = [("client.profile", MX), ("spending.monthly", {"essential": essential, "currency": "MXN"}),
             ("cash.nu", {"amount": cash, "currency": "MXN", "purpose": "reserve"})]
    if target is not None:
        facts.append(("reserve", {"target_months": target}))
    return facts


def test_reserve_below_target_fires_and_is_money_at_risk_under_one_month():
    item = kinds(evaluate(_reserve(30000)))["reserve_low"]
    assert (item["severity"], item["priority"], item["data"]["gap"]) == ("consider", "opportunity", 90000)
    urgent = kinds(evaluate(_reserve(10000)))["reserve_low"]
    assert (urgent["severity"], urgent["priority"]) == ("act", "risk")


def test_reserve_quiet_on_target_and_unknown_without_a_target_or_spending():
    assert "reserve_low" not in kinds(evaluate(_reserve(150000)))
    found = evaluate(_reserve(30000, target=None))
    assert "reserve_low" not in kinds(found) and unknown(found, "reserve_low") == ["reserve.target_months"]
    found = evaluate([("client.profile", MX), ("cash.nu", {"amount": 30000, "currency": "MXN"}),
                      ("reserve", {"target_months": 6})])
    assert "reserve_low" not in kinds(found) and "essential spending" in unknown(found, "reserve_low")


def test_cash_drag_fires_past_twelve_months_only():
    item = kinds(evaluate(_reserve(400000)))["cash_drag"]
    assert item["data"]["excess"] == 280000
    assert "cash_drag" not in kinds(evaluate(_reserve(200000)))


# ------------------------------------------------------------------ windfall


def _payroll(extra, prior=3):
    rows = [e(f"n{i}", f"2026-{month:02d}-15", "income", 21000, "PAGO DE NOMINA", subtype="salary")
            for i, month in enumerate(range(12 - prior, 12), 1)]
    return ledger(rows + [extra])


def test_windfall_tagged_aguinaldo():
    found = evaluate([("client.profile", MX)], _payroll(e("ag", "2026-12-15", "income", 42000, "PAGO AGUINALDO")),
                     as_of="2026-12-18")
    item = kinds(found)["windfall"]
    assert item["title"]["es"] == "Llegó tu aguinaldo: $42,000" and item["id"] == "windfall:ag"
    assert item["next_step"]["es"] == "¿Qué hago con mi aguinaldo de $42,000?"
    assert item["data"]["probable"] is False


@pytest.mark.parametrize("day, label", [("2026-12-10", "aguinaldo"), ("2026-06-10", "ptu"), ("2026-04-10", "bonus")])
def test_windfall_untagged_large_deposit_is_labelled_by_month(day, label):
    month = int(day[5:7])
    rows = [e(f"n{i}", f"2026-{m:02d}-01", "income", 21000, "PAGO DE NOMINA", subtype="salary")
            for i, m in enumerate(range(month - 3, month), 1)]
    led = ledger(rows + [e("x", day, "income", 40000, "SPEI RECIBIDO")])
    item = kinds(evaluate([("client.profile", MX)], led, as_of=day))["windfall"]
    assert (item["data"]["label"], item["data"]["probable"]) == (label, True)


def test_windfall_quiet_for_ordinary_or_old_deposits_and_unknown_without_history():
    ordinary = _payroll(e("x", "2026-12-15", "income", 25000, "SPEI RECIBIDO"))
    assert "windfall" not in kinds(evaluate([("client.profile", MX)], ordinary, as_of="2026-12-18"))
    old = _payroll(e("ag", "2026-12-01", "income", 42000, "PAGO AGUINALDO"))
    assert "windfall" not in kinds(evaluate([("client.profile", MX)], old, as_of="2026-12-20"))
    thin = _payroll(e("x", "2026-12-15", "income", 60000, "SPEI RECIBIDO"), prior=2)
    found = evaluate([("client.profile", MX)], thin, as_of="2026-12-18")
    assert "windfall" not in kinds(found) and unknown(found, "windfall")


# ------------------------------------------------------------------ drift and concentration


def _portfolio(equity, fixed, cash, extra=()):
    return account_fact("gbm", [{"symbol": "CSPX", "value": equity, "asset_class": "fund"},
                                {"symbol": "CETES", "value": fixed}, {"symbol": "CASH", "value": cash,
                                                                      "asset_class": "cash"}, *extra])


def test_drift_fires_outside_the_ips_bands():
    item = kinds(evaluate([("client.profile", MX), ("policy.ips", IPS), _portfolio(800000, 200000, 0)]))["drift"]
    sleeves = {o["sleeve"]: o["direction"] for o in item["data"]["sleeves_outside"]}
    assert sleeves == {"global_equity": "over", "mx_fixed_income": "under", "cash": "under"}


def test_drift_quiet_inside_bands_and_unknown_without_policy_or_classification():
    assert "drift" not in kinds(evaluate([("client.profile", MX), ("policy.ips", IPS), _portfolio(600000, 350000, 50000)]))
    found = evaluate([("client.profile", MX), _portfolio(800000, 200000, 0)])
    assert "drift" not in kinds(found) and unknown(found, "drift") == ["policy.ips"]
    found = evaluate([("client.profile", MX), ("policy.ips", IPS),
                      _portfolio(600000, 350000, 50000, [{"symbol": "MYSTERY", "value": 1}])])
    assert "drift" not in kinds(found) and unknown(found, "drift") == ["sleeve for MYSTERY"]


def test_concentration_single_stock_over_ten_percent_of_net_worth():
    facts = [("client.profile", MX), account_fact("gbm", [{"symbol": "AMXL", "value": 300000, "asset_class": "stock"},
                                                          {"symbol": "CSPX", "value": 700000, "asset_class": "fund"}])]
    item = kinds(evaluate(facts))["concentration"]
    assert (item["id"], item["data"]["share"], item["priority"]) == ("concentration:AMXL", 0.3, "risk")
    facts[1][1]["positions"][0]["value"] = 120000
    item = kinds(evaluate(facts))["concentration"]
    assert (item["severity"], item["priority"]) == ("consider", "opportunity")


def test_concentration_quiet_for_funds_and_unknown_without_complete_net_worth():
    facts = [("client.profile", MX), account_fact("gbm", [{"symbol": "CSPX", "value": 1000000, "asset_class": "fund"}])]
    assert "concentration" not in kinds(evaluate(facts))
    facts.append(("cash.usd", {"amount": 5000, "currency": "USD"}))  # no USD/MXN rate: net worth incomplete
    found = evaluate(facts)
    assert "concentration" not in kinds(found) and unknown(found, "concentration") == ["complete net worth"]
    unsure = [("client.profile", MX), account_fact("gbm", [{"symbol": "ZZZ", "value": 300000},
                                                           {"symbol": "CSPX", "value": 700000, "asset_class": "fund"}])]
    found = evaluate(unsure)
    assert "concentration" not in kinds(found) and "ZZZ" in unknown(found, "concentration")[0]


# ------------------------------------------------------------------ harvesting (US)


def _lots(extra_buy=None):
    accounts = [{"id": "ib", "institution": "IBKR", "type": "brokerage", "currency": "USD", "owners": OWNER}]
    instruments = [{"id": "XYZ", "symbol": "XYZ", "currency": "USD", "listing_currency": "USD", "asset_class": "stock"}]
    rows = [e("d", "2026-01-05", "deposit", 10000, "WIRE IN", account="ib", currency="USD"),
            e("b1", "2026-01-10", "buy", -5000, account="ib", currency="USD", instrument_id="XYZ", quantity="100")]
    if extra_buy:
        rows.append(e("b2", extra_buy, "buy", -300, account="ib", currency="USD", instrument_id="XYZ", quantity="10"))
    return ledger(rows, accounts, instruments)


def _us_facts(quantity=100):
    return [("client.profile", US), account_fact("ib", [{"symbol": "XYZ", "quantity": quantity, "value": 30 * quantity,
                                                         "asset_class": "stock"}], currency="USD")]


def test_harvestable_loss_fires_for_a_us_filer():
    item = kinds(evaluate(_us_facts(), _lots()))["harvest"]
    assert item["data"]["total_loss"] == 2000 and item["due"] == "2026-12-31"
    assert item["data"]["lots"][0]["lot_id"]


def test_harvest_quiet_with_a_wash_sale_conflict_or_outside_the_us():
    found = kinds(evaluate(_us_facts(110), _lots(extra_buy="2026-09-10")))
    assert "harvest" not in found
    mx = [("client.profile", MX), *_us_facts()[1:]]
    assert "harvest" not in kinds(evaluate(mx, _lots()))
    found = evaluate([("client.profile", US)], _lots())
    assert "harvest" not in kinds(found) and unknown(found, "harvest") == ["a current price for XYZ"]


# ------------------------------------------------------------------ PPR headroom (MX)


def _ppr(deposit=10000, gross=True, as_of="2026-11-20", with_account=True):
    facts = [("client.profile", MX),
             ("income.salary", {"amount": 50000, "currency": "MXN", "frequency": "monthly", "net": not gross})]
    accounts = [{"id": "chk", "institution": "BBVA", "type": "checking", "currency": "MXN", "owners": OWNER}]
    rows = [e("o", "2026-01-01", "opening_balance", 1000)]
    if with_account:
        accounts.append({"id": "ppr", "institution": "GBM", "type": "ppr", "currency": "MXN", "owners": OWNER})
        rows.append(e("p", "2026-03-01", "deposit", deposit, "APORTACION PPR", account="ppr"))
    return evaluate(facts, ledger(rows, accounts), as_of=as_of)


def test_ppr_headroom_in_november_for_a_mexican_resident():
    item = kinds(_ppr())["ppr_headroom"]
    assert (item["data"]["cap"], item["data"]["contributed_ytd"], item["data"]["headroom"]) == (60000, 10000, 50000)
    assert item["due"] == "2026-12-31" and item["data"]["bound"] == "exact"
    assert kinds(_ppr(gross=False))["ppr_headroom"]["data"]["bound"] == "at_least"
    assert not any(i["id"].startswith("tax_deadline:mx_ppr") for i in _ppr()["candidates"])  # no duplicate


def test_ppr_quiet_outside_the_season_when_full_or_without_contribution_data():
    assert "ppr_headroom" not in kinds(_ppr(as_of="2026-10-20"))
    assert "ppr_headroom" not in kinds(_ppr(deposit=60000))
    found = _ppr(with_account=False)
    assert "ppr_headroom" not in kinds(found)
    assert unknown(found, "ppr_headroom") == ["a PPR/AFORE account in the ledger (voluntary contributions)"]


# ------------------------------------------------------------------ fees


def _fees(last, first_day="2026-05-01"):
    rows = [e("o", first_day, "opening_balance", 5000),
            e("f1", "2026-07-10", "fee", -100, "COMISION MANEJO CUENTA"),
            e("f2", "2026-08-10", "fee", -100, "COMISION MANEJO CUENTA"),
            e("f3", "2026-09-10", "fee", -last, "COMISION MANEJO CUENTA")]
    return ledger(rows)


def test_fee_creep_increase_and_new_fee():
    item = kinds(evaluate([("client.profile", MX)], _fees(150)))["fee_creep"]
    assert item["data"]["changes"][0]["change"] == "increased" and item["title"]["es"].endswith("$100 → $150")
    led = ledger([e("o", "2026-05-01", "opening_balance", 5000), e("x", "2026-09-12", "fee", -250, "CARGO POR SERVICIO")])
    item = kinds(evaluate([("client.profile", MX)], led))["fee_creep"]
    assert item["data"]["changes"][0]["change"] == "new"


def test_fee_creep_quiet_when_unchanged_and_unknown_with_short_history():
    assert "fee_creep" not in kinds(evaluate([("client.profile", MX)], _fees(100)))
    led = ledger([e("o", "2026-08-20", "opening_balance", 5000), e("x", "2026-09-12", "fee", -250, "CARGO POR SERVICIO")])
    found = evaluate([("client.profile", MX)], led)
    assert "fee_creep" not in kinds(found) and unknown(found, "fee_creep")


# ------------------------------------------------------------------ scam


def _history(start="2026-06-01"):
    rows = [e("o", start, "opening_balance", 80000)]
    for i, day in enumerate(["06-05", "06-20", "07-05", "07-20", "08-05", "08-20", "09-05"], 1):
        rows.append(e(f"x{i}", f"2026-{day}", "expense", -800, f"SUPER TIENDA {i % 2}"))
    return rows


def test_scam_pattern_new_payee_and_large_transfer():
    rows = _history() + [e("t", "2026-09-19", "transfer", -20000, "SPEI JUAN PEREZ INVERSION GARANTIZADA")]
    found = today([("client.profile", MX)], ledger(rows))
    item = found["today"][0]
    assert (item["kind"], item["severity"], item["priority"]) == ("scam", "act", "risk")
    assert item["title"]["es"].startswith("¿Fuiste tú? $20,000")
    assert "garantizad" in item["data"]["keywords"] and "a Juan Perez Inversion" in item["title"]["es"]


def test_scam_quiet_for_known_payees_own_transfers_and_short_history():
    known = _history() + [e("k0", "2026-06-10", "transfer", -500, "SPEI JUAN PEREZ"),
                          e("t", "2026-09-19", "transfer", -20000, "SPEI JUAN PEREZ")]
    assert "scam" not in kinds(evaluate([("client.profile", MX)], ledger(known)))
    accounts = [{"id": "chk", "institution": "BBVA", "type": "checking", "currency": "MXN", "owners": OWNER},
                {"id": "sav", "institution": "Nu", "type": "savings", "currency": "MXN", "owners": OWNER}]
    own = _history() + [e("t", "2026-09-19", "transfer", -20000, "SPEI A NU"),
                        e("t2", "2026-09-19", "transfer", 20000, "SPEI DESDE BBVA", account="sav")]
    assert "scam" not in kinds(evaluate([("client.profile", MX)], ledger(own, accounts)))
    short = _history("2026-08-15")[:1] + [e("t", "2026-09-19", "transfer", -20000, "SPEI JUAN PEREZ")]
    found = evaluate([("client.profile", MX)], ledger(short))
    assert "scam" not in kinds(found) and unknown(found, "scam")


# ------------------------------------------------------------------ stale facts and statements


def test_stale_facts_that_matter():
    facts = [("client.profile", MX), ("spending.monthly", {"total": 30000, "currency": "MXN"}, {"expires_on": "2026-09-01"})]
    item = kinds(evaluate(facts))["stale_facts"]
    assert [k["key"] for k in item["data"]["keys"]] == ["spending.monthly"]
    quiet = [("client.profile", MX), ("research.AAPL", {"note": "x"}, {"expires_on": "2026-09-01"})]
    assert "stale_facts" not in kinds(evaluate(quiet))


def test_statement_overdue_after_35_days():
    item = kinds(evaluate([("client.profile", MX), account_fact("gbm", [], as_of="2026-08-01")]))["statement_overdue"]
    assert item["data"]["accounts"][0]["days"] == 51
    assert "statement_overdue" not in kinds(evaluate([("client.profile", MX), account_fact("gbm", [], as_of="2026-09-01")]))


# ------------------------------------------------------------------ guilt-free, DCA, threads


def _fun(dining):
    rows = [e("o", "2026-06-01", "opening_balance", 10000)]
    for month in ("06", "07", "08"):
        rows.append(e(f"r{month}", f"2026-{month}-03", "expense", -15000, "RENTA DEPTO"))
        rows.append(e(f"d{month}", f"2026-{month}-12", "expense", -dining, "RESTAURANTE EL CAMINO"))
    rows.append(e("r09", "2026-09-03", "expense", -15000, "RENTA DEPTO"))
    return ledger(rows)


def _fun_facts(discretionary=10000):
    spend = {"essential": 15000, "currency": "MXN"}
    if discretionary is not None:
        spend["discretionary"] = discretionary
    return [("client.profile", MX), ("income.salary", {"amount": 50000, "currency": "MXN", "frequency": "monthly"}),
            ("spending.monthly", spend), ("cash.nu", {"amount": 100000, "currency": "MXN", "purpose": "reserve"}),
            ("reserve", {"target_months": 6})]


def test_guilt_free_spend_when_on_track_and_fun_budget_unspent():
    item = kinds(evaluate(_fun_facts(), _fun(3000)))["guilt_free"]
    assert item["data"]["average_unspent"] == 7000 and item["title"]["es"] == "Puedes gastar $7,000 sin culpa"


def test_guilt_free_quiet_when_spent_and_unknown_without_a_budget():
    assert "guilt_free" not in kinds(evaluate(_fun_facts(), _fun(9500)))
    found = evaluate(_fun_facts(None), _fun(3000))
    assert "guilt_free" not in kinds(found) and unknown(found, "guilt_free")


def _dca(buy_september):
    accounts = [{"id": "brk", "institution": "GBM", "type": "brokerage", "currency": "MXN", "owners": OWNER}]
    instruments = [{"id": "VOO", "symbol": "VOO", "currency": "MXN", "listing_currency": "MXN", "asset_class": "fund"}]
    rows = [e("dep", "2026-05-20", "deposit", 10000, "SPEI", account="brk")]
    for month in ("06", "07", "08") + (("09",) if buy_september else ()):
        rows.append(e(f"b{month}", f"2026-{month}-01", "buy", -1000, account="brk", instrument_id="VOO", quantity="1"))
    plan = {"id": "sp500", "currency": "MXN", "cadence": "monthly", "start_date": "2026-06-01", "account_id": "brk",
            "legs": [{"instrument_id": "VOO", "amount": "1000"}]}
    return [("client.profile", MX), ("planning.dca", plan)], ledger(rows, accounts, instruments)


def test_dca_slipped_when_the_latest_installment_was_skipped():
    facts, led = _dca(buy_september=False)
    item = kinds(evaluate(facts, led))["dca_slipped"]
    assert item["data"]["plans"][0]["due"] == "2026-09-01" and item["data"]["plans"][0]["shortfall"] == 1000
    facts, led = _dca(buy_september=True)
    assert "dca_slipped" not in kinds(evaluate(facts, led))
    assert "dca_slipped" in {u["kind"] for u in evaluate(facts)["unknown"]}  # no ledger: unknown, not "skipped"


def test_thread_ready_when_the_awaited_fact_arrives_later():
    thread = ("thread.aguinaldo", {"kind": "advice", "text": "Decidir el aguinaldo cuando sepa mi sueldo neto",
                                   "status": "open", "related": ["income.salary"]})
    salary = ("income.salary", {"amount": 42000, "currency": "MXN", "frequency": "monthly"})
    item = kinds(evaluate([("client.profile", MX), thread, salary]))["thread_ready"]
    assert item["data"]["arrived"] == ["income.salary"] and item["id"] == "thread_ready:aguinaldo"
    assert "thread_ready" not in kinds(evaluate([("client.profile", MX), salary, thread]))  # already known then
    assert "thread_ready" not in kinds(evaluate([("client.profile", MX), thread]))


# ------------------------------------------------------------------ unknown is never zero


def test_an_empty_picture_fires_nothing_and_lists_what_is_missing():
    found = today([])
    assert found["today"] == [] and found["upcoming"] == [] and found["jurisdictions"] == []
    missing = {u["kind"] for u in found["unknown"]}
    assert {"surplus", "reserve_low", "concentration", "drift", "windfall"} <= missing


def test_unknown_amounts_are_not_read_as_zero():
    # A reserve with no amount: zero would look like an empty reserve and fire; unknown must not.
    found = evaluate([("client.profile", MX), ("spending.monthly", {"essential": 20000, "currency": "MXN"}),
                      ("reserve", {"target_months": 6})])
    assert "reserve_low" not in kinds(found)
    # A ledger with no fees at all is not "no change": nothing fires.
    assert "fee_creep" not in kinds(evaluate([("client.profile", MX)], ledger([e("o", "2026-01-01", "opening_balance", 1)])))


# ------------------------------------------------------------------ ranking and taste


def test_ranking_caps_at_three_one_per_kind_money_at_risk_first():
    facts = _reserve(10000) + [("income.salary", {"amount": 90000, "currency": "MXN", "frequency": "monthly"}),
                               account_fact("gbm", [{"symbol": "AMXL", "value": 50000, "asset_class": "stock"},
                                                    {"symbol": "CSPX", "value": 50000, "asset_class": "fund"}],
                                            as_of="2026-07-01")]
    rows = _history() + [e("t", "2026-09-19", "transfer", -20000, "SPEI JUAN PEREZ")]
    report = today(facts, ledger(rows))
    got = [i["kind"] for i in report["today"]]
    assert len(got) == 3 and len(set(got)) == 3
    assert got[:2] == ["scam", "life_calendar"]  # money at risk, then the will-month deadline (Sep 30)
    assert report["today"][1]["priority"] == "deadline"
    assert [i["priority"] for i in report["today"]] == sorted((i["priority"] for i in report["today"]),
                                                             key=proactive.PRIORITIES.index)
    overflow = {i["kind"] for i in report["upcoming"]}
    assert "statement_overdue" in overflow
    for item in report["today"] + report["upcoming"]:
        assert item["severity"] in proactive.SEVERITIES and item["title"]["en"] and item["title"]["es"]
        assert item["next_step"]["es"].endswith("?") or item["kind"] in {"statement_overdue", "thread_ready"}


def test_deadline_within_14_days_outranks_opportunities_and_same_kind_is_not_repeated():
    facts = [("client.profile", {**MX, "tax_residence": ["MX", "US"], "us_person": True}),
             ("income.freelance", {"amount": 30000, "currency": "MXN", "frequency": "monthly", "kind": "business"}),
             ("spending.monthly", {"essential": 20000, "currency": "MXN"}),
             ("cash.nu", {"amount": 30000, "currency": "MXN", "purpose": "reserve"}), ("reserve", {"target_months": 6})]
    report = today(facts, as_of="2026-09-08")
    first = report["today"][0]
    assert first["id"] == "tax_deadline:us_estimate_q3_2026" and first["priority"] == "deadline"
    tax = [i for i in report["today"] if i["kind"] == "tax_deadline"]
    assert len(tax) == 1
    assert any(i["kind"] == "tax_deadline" for i in report["upcoming"])  # the MX provisional payment waits


# ------------------------------------------------------------------ calendars


def _cal(profile, as_of="2026-09-21", extra=(), horizon=365):
    facts = [("client.profile", profile), *extra]
    sit = situation.build(snap(facts), None, as_of)
    return {i["id"]: i for i in proactive.calendar(sit, as_of, horizon_days=horizon)}


def test_mexican_calendar_annual_return_in_april_and_ppr_by_december():
    cal = _cal(MX, extra=[("income.salary", {"amount": 1, "currency": "MXN", "frequency": "monthly", "kind": "salary"})])
    assert cal["mx_ppr_151v_contributions_2026"]["due"] == date(2026, 12, 31)
    assert cal["mx_annual_return_2026"]["due"] == date(2027, 4, 30)
    assert "mx_aguinaldo_2026" in cal and "mx_ptu_2027" in cal and "mx_testamento_2026" in cal
    assert not any(k.startswith("us_") for k in cal)
    assert not any(k.startswith(("mx_provisional", "mx_predial", "mx_refrendo")) for k in cal)  # not relevant
    report = today([("client.profile", MX)], as_of="2027-04-06")
    assert report["today"][0]["id"] == "tax_deadline:mx_annual_return_2026"
    assert report["today"][0]["title"]["es"] == "Declaración anual 2026: vence el 30 abr"


def test_life_calendar_only_what_applies():
    cal = _cal(MX, extra=[("liability.casa", {"kind": "mortgage", "balance": 1000000, "currency": "MXN"}),
                          ("liability.coche", {"kind": "auto", "balance": 100000, "currency": "MXN"})])
    assert {"mx_predial_2027", "mx_refrendo_2027"} <= set(cal)
    assert "mx_aguinaldo_2026" not in cal  # no salary on file
    assert not any(k.startswith("mx_") and "testamento" not in k and "predial" not in k and "refrendo" not in k
                   and not k.startswith(("mx_ppr", "mx_annual", "mx_constancias")) for k in cal)


def test_us_calendar_estimates_filing_year_end_and_medicare():
    business = ("income.consulting", {"amount": 1, "currency": "USD", "frequency": "monthly", "kind": "business"})
    cal = _cal({**US, "birth_year": 1960}, extra=[business, ("investment.ira", {"amount": 1, "currency": "USD"})])
    assert {"us_estimate_q4_2026", "us_estimate_q1_2027", "us_return_2026", "us_year_end_2026",
            "us_medicare_oe_2026"} <= set(cal)
    assert cal["us_estimate_q4_2026"]["due"] == date(2027, 1, 15)
    assert not any(k.startswith("mx_") for k in cal)
    quiet = _cal({**US, "birth_year": 1990})
    assert "us_medicare_oe_2026" not in quiet and not any(k.startswith("us_estimate") for k in quiet)
    assert "us_medicare_oe_2026" not in _cal(US)  # unknown age is not "old enough"


def test_us_person_in_mexico_gets_both_calendars_and_the_abroad_dates():
    cal = _cal({**MX, "us_person": True})
    assert "mx_annual_return_2026" in cal and "us_return_2026" in cal and "us_extended_2025" in cal
    assert "us_return_abroad_2026" in cal
    assert proactive.jurisdictions(situation.build(snap([("client.profile", MX)]), None, AS_OF), "MX,US") == {"MX", "US"}
    with pytest.raises(ValueError):
        proactive.jurisdictions({}, "FR")


# ------------------------------------------------------------------ weekly


def test_weekly_is_at_most_five_lines_of_data():
    rows = _history() + [e("t", "2026-09-19", "transfer", -20000, "SPEI JUAN PEREZ"),
                         e("s", "2026-09-15", "income", 30000, "PAGO DE NOMINA", subtype="salary")]
    s = snap(_reserve(10000))
    led = ledger(rows)
    sit = situation.build(s, led, AS_OF)
    report = proactive.weekly(sit, led, s, AS_OF)
    assert 1 <= len(report["lines"]) <= 5 and report["period"] == ["2026-09-15", "2026-09-21"]
    flow = next(line for line in report["lines"] if line["kind"] == "cash_flow")["data"]
    assert flow["income"] == 30000
    assert any(line["kind"] == "item" and line["data"]["item_kind"] == "scam" for line in report["lines"])


# ------------------------------------------------------------------ dismissal through the service


@pytest.fixture
def client(tmp_path):
    service = WealthService(tmp_path / "w.sqlite3")
    service.create("ana", "Ana")
    service.remember("ana", [
        {"key": "client.profile", "value": MX, "source": {"kind": "user", "ref": "chat", "observed_on": "2026-09-01"}},
        {"key": "spending.monthly", "value": {"essential": 20000, "currency": "MXN"},
         "source": {"kind": "user", "ref": "chat", "observed_on": "2026-09-01"}},
        {"key": "cash.nu", "value": {"amount": 30000, "currency": "MXN", "purpose": "reserve"},
         "source": {"kind": "user", "ref": "chat", "observed_on": "2026-09-01"}},
        {"key": "reserve", "value": {"target_months": 6}, "source": {"kind": "user", "ref": "chat", "observed_on": "2026-09-01"}},
    ])
    return service


def _ids(report, part="today"):
    return [i["id"] for i in report["result"][part]]


def test_dismiss_persists_until_the_trigger_changes(client):
    first = client.run("today", {"as_of": AS_OF}, client_id="ana")
    assert "reserve_low" in _ids(first)
    after = client.run("today", {"as_of": AS_OF, "dismiss": ["reserve_low"]}, client_id="ana")
    assert "reserve_low" not in _ids(after) + _ids(after, "upcoming")
    assert after["result"]["hidden"] == [{"id": "reserve_low", "kind": "reserve_low", "reason": "dismissed", "on": AS_OF}]
    again = client.run("today", {"as_of": "2026-09-25"}, client_id="ana")
    assert "reserve_low" not in _ids(again)
    # A monitor run shares the namespace and must not wipe the dismissal.
    client.run("monitor", {"rules": [{"id": "fresh", "kind": "expiry", "keys": ["goals"]}], "timezone": "UTC"},
               client_id="ana")
    assert "reserve_low" not in _ids(client.run("today", {"as_of": AS_OF}, client_id="ana"))
    # The trigger changes (the reserve drops below one month): the item comes back.
    client.remember("ana", [{"key": "cash.nu", "merge": True, "value": {"amount": 10000},
                             "source": {"kind": "user", "ref": "chat", "observed_on": "2026-09-20"}}])
    back = client.run("today", {"as_of": AS_OF}, client_id="ana")
    assert "reserve_low" in _ids(back) and back["result"]["today"][0]["severity"] == "act"


def test_snooze_and_restore(client):
    client.run("today", {"as_of": AS_OF, "snooze": [{"id": "reserve_low", "until": "2026-10-01"}]}, client_id="ana")
    assert "reserve_low" not in _ids(client.run("today", {"as_of": "2026-09-30"}, client_id="ana"))
    assert "reserve_low" in _ids(client.run("today", {"as_of": "2026-10-01"}, client_id="ana"))
    client.run("today", {"as_of": AS_OF, "dismiss": ["reserve_low"]}, client_id="ana")
    restored = client.run("today", {"as_of": AS_OF, "restore": ["reserve_low"]}, client_id="ana")
    assert "reserve_low" in _ids(restored)


def test_acknowledgement_errors(client):
    with pytest.raises(ValueError, match="not a current item"):
        client.run("today", {"as_of": AS_OF, "dismiss": ["nope"]}, client_id="ana")
    with pytest.raises(ValueError, match="client_id"):
        client.run("today", {"as_of": AS_OF, "dismiss": ["reserve_low"], "facts": []})
    with pytest.raises(ValueError, match="unknown"):
        client.run("weekly", {"as_of": AS_OF, "dismiss": ["reserve_low"]}, client_id="ana")


# ------------------------------------------------------------------ QA round 2


def _reserve_facts(target=6):
    return [("client.profile", MX), ("spending.monthly", {"essential": 20000, "total": 20000, "currency": "MXN"}),
            ("cash.nu", {"amount": 30000, "currency": "MXN", "purpose": "reserve"}),
            ("reserve", {"target_months": target})]


def test_idle_cash_that_covers_the_reserve_gap_is_one_item_that_does_the_arithmetic():
    # Reserve 1.5 of 6 months (gap 90,000) while checking holds 285,000 more than it needs: never two items
    # that contradict each other ("reserve short" next to "cash with no job").
    found = kinds(evaluate(_reserve_facts(), _checking(300000)))
    assert "surplus" not in found
    item = found["reserve_low"]
    assert item["title"]["es"] == "Tienes $285,000 sin destino: con $90,000 completas tu fondo de 6 meses"
    assert item["title"]["en"] == "You have $285,000 unassigned; $90,000 of it fills your 6-month reserve"
    assert "$90,000" in item["next_step"]["es"] and "fondo de emergencia" in item["next_step"]["es"]
    assert item["data"]["fills_gap"] == 90000 and item["data"]["left_after"] == 195000
    shown = today(_reserve_facts(), _checking(300000))["today"]
    assert [i["kind"] for i in shown].count("reserve_low") == 1 and "surplus" not in [i["kind"] for i in shown]


def test_idle_cash_short_of_the_gap_keeps_both_items_and_says_what_is_still_missing():
    found = kinds(evaluate(_reserve_facts(), _checking(100000)))  # idle 85,000 against a 90,000 gap
    assert {"reserve_low", "surplus"} <= set(found)
    assert "$5,000" in found["reserve_low"]["why"]["es"] and "$5,000" in found["surplus"]["why"]["es"]
    assert found["reserve_low"]["data"]["still_missing"] == 5000
    assert "fondo de emergencia" in found["surplus"]["next_step"]["es"]


def test_dismissed_reserve_nudge_stays_dismissed_while_the_fund_moves_within_its_condition(tmp_path):
    source = {"kind": "user", "ref": "chat", "observed_on": "2026-09-01"}
    service = WealthService(tmp_path / "w.sqlite3")
    service.create("ana", "Ana")
    service.remember("ana", [{"key": k, "value": v, "source": source} for k, v in _reserve_facts()])
    service.run("today", {"as_of": AS_OF, "dismiss": ["reserve_low"]}, client_id="ana")
    service.remember("ana", [{"key": "cash.nu", "merge": True, "value": {"amount": 41000},
                              "source": {**source, "observed_on": "2026-09-20"}}])  # 1.5 -> 2.05 months
    shown = service.run("today", {"as_of": AS_OF}, client_id="ana")["result"]
    assert "reserve_low" not in [i["id"] for i in shown["today"] + shown["upcoming"]]


def test_cash_drag_without_a_target_states_its_default():
    facts = [("client.profile", MX), ("spending.monthly", {"essential": 20000, "currency": "MXN"}),
             ("reserve", {"target_months": None}), ("cash.nu", {"amount": 300000, "currency": "MXN",
                                                                  "purpose": "reserve"})]
    item = kinds(evaluate(facts))["cash_drag"]
    assert item["data"]["keep_basis"] == "default" and "12" in item["why"]["en"] and "default" in item["why"]["en"]
    no_spending = [f for f in facts if f[0] != "spending.monthly"]
    assert "cash_drag" not in kinds(evaluate(no_spending))  # never measured against an unknown


# ------------------------------------------------------------------ nudges follow the adviser's open advice


_FLOW = [("client.profile", MX), ("income.salary", {"amount": 80500, "currency": "MXN", "frequency": "monthly"}),
         ("spending.monthly", {"total": 45000, "currency": "MXN"}),
         ("liability.auto", {"kind": "auto", "balance": 180000, "currency": "MXN", "annual_rate": 0.14,
                             "payment": 10000, "payment_frequency": "monthly", "in_spending": False})]
_CAR_ADVICE = ("thread.auto-primero", {"kind": "advice", "status": "open", "related": ["liability.auto"],
                                       "text": "Manda los $25,500 restantes al mes al crédito del auto hasta liquidarlo"})


def test_open_advice_about_the_surplus_turns_sin_destino_into_a_follow_through():
    plain = kinds(evaluate(_FLOW))
    assert plain["surplus"]["title"]["es"] == "$25,500 al mes todavía sin destino"
    found = kinds(evaluate([*_FLOW, _CAR_ADVICE]))
    assert "surplus" not in found
    item = found["follow_through"]
    assert item["title"] == {"en": "Sending the $25,500 to the car?", "es": "¿Ya mandas los $25,500 al auto?"}
    assert item["next_step"]["es"].startswith("Confirmar que ya lo hago")
    assert item["data"]["thread_id"] == "auto-primero" and item["id"] == "follow_through:auto-primero"
    shown = today([*_FLOW, _CAR_ADVICE])["today"]
    assert not any("sin destino" in i["title"]["es"] for i in shown)


def test_a_thread_naming_the_amount_relates_even_without_related_keys():
    advice = ("thread.invertir", {"kind": "advice", "status": "open",
                                  "text": "Invierte unos 25 mil al mes en un fondo indexado"})  # within 5%
    found = kinds(evaluate([*_FLOW, advice]))
    assert "surplus" not in found and found["follow_through"]["title"]["es"] == "¿Ya mandas los $25,500 a invertir?"
    far = ("thread.otro", {"kind": "advice", "status": "open", "text": "Aparta 5,000 para el seguro"})
    assert "surplus" in kinds(evaluate([*_FLOW, far]))  # unrelated advice leaves the nudge alone


def test_nothing_shows_once_the_person_committed():
    commitment = ("thread.auto-primero", {**_CAR_ADVICE[1], "kind": "commitment"})
    found = kinds(evaluate([*_FLOW, commitment]))
    assert "surplus" not in found and "follow_through" not in found
    # A counted pay_off goal for the same debt settles it too, even for part of the amount.
    goal = ("goals", [{"id": "liquidar-auto", "name": "Liquidar el auto", "action": "pay_off",
                       "liability": "liability.auto", "monthly_contribution": 20000, "currency": "MXN"}])
    found = kinds(evaluate([*_FLOW, _CAR_ADVICE, goal]))
    assert "surplus" not in found and "follow_through" not in found


def test_a_pay_off_goal_is_a_commitment_the_situation_counts():
    goal = ("goals", [{"id": "liquidar-auto", "name": "Liquidar el auto", "action": "pay_off",
                       "liability": "liability.auto", "monthly_contribution": 25500, "currency": "MXN"}])
    sit = situation.build(snap([*_FLOW, goal]), None, AS_OF)
    assert sit["commitments"]["total"] == 25500 and sit["commitments"]["unallocated"] == 0
    assert sit["goals"][0]["liability"] == "liability.auto" and sit["goals"][0]["action"] == "pay_off"
    found = kinds(evaluate([*_FLOW, _CAR_ADVICE, goal]))
    assert "surplus" not in found and "follow_through" not in found


def test_a_goal_liability_must_name_a_debt():
    from wealth.situation.schema import SchemaError, validate
    with pytest.raises(SchemaError, match="liability"):
        validate("goals", [{"id": "x", "name": "X", "liability": "cash.bbva"}])
