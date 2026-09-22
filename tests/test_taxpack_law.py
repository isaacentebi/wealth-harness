"""Legal-audit regressions for the tax pack: plan limits, RMD ages, 8949 terms, Form 1116 baskets, dividends, 8938."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

from wealth import taxpack
from wealth.catalog import CATALOG
from wealth.policy import snapshot_from_facts

MX = CATALOG["tax_pack"]["example"]
US = CATALOG["tax_pack"]["variants"]["us_schwab_wash_sale"]
XB = CATALOG["tax_pack"]["variants"]["us_person_in_mexico"]


def run(inputs):
    inputs = deepcopy(inputs)
    today = inputs.get("as_of", "2026-03-01")
    return taxpack.run_task(inputs, snapshot_from_facts(inputs["facts"], today), inputs.get("ledger"), today)


def sec(report, sid):
    return report["result"]["sections"][sid]


def pend(report):
    return {p["key"] for p in report["result"]["pendientes"]}


def entry(i, account, kind, day, amount=None, currency="USD", **extra):
    row = {"id": f"t{i:03d}", "account_id": account, "kind": kind, "date": day, "currency": currency,
           "confidence": "reported", "source": {"kind": "document", "ref": "test"}, **extra}
    if amount is not None:
        row["amount"] = amount
    return row


def us_tax(inputs, year=2025):
    return next(f for f in inputs["facts"] if f["key"] == f"tax.{year}")["value"]["us"]


def profile(inputs):
    return next(f for f in inputs["facts"] if f["key"] == "client.profile")["value"]


def with_401k(amount="23000"):
    inputs = deepcopy(US)
    inputs["ledger"]["accounts"].append({"id": "fid401k", "institution": "Fidelity", "type": "401k",
                                         "currency": "USD", "country": "US",
                                         "owners": [{"person_id": "sam", "share": "1"}]})
    inputs["ledger"]["entries"].append(entry(90, "fid401k", "deposit", "2025-06-30", amount))
    return inputs


# ------------------------------------------------------------------ T1: 401(k) deferrals are not IRA contributions


def test_401k_deposits_are_not_ira_contributions_and_meet_402g():
    report = run(with_401k())
    retire = sec(report, "us_retirement")
    ira = retire["summary"]["contributions"]
    assert (ira["traditional_usd"], ira["combined_usd"], ira["excess_usd"]) == ("0.00", "6000.00", "0.00")
    plan = retire["summary"]["workplace_deferrals"]
    assert plan["plan_deposits_usd"] == "23000.00" and plan["limit_usd"] == "23500.00"
    # Deposits include employer money: the deferral comes from the W-2, and unknown is not zero.
    assert plan["deferrals_usd"] is None and plan["excess_usd"] is None
    assert "tax.2025.us.elective_deferrals_usd" in pend(report)
    assert "402(g)" in plan["rule"] and "457(e)(15)" in plan["rule"]
    assert any("Notice 2024-80" in s["title"] for s in retire["sources"])


def test_402g_catch_ups_by_age_and_year():
    inputs = with_401k()
    us_tax(inputs)["elective_deferrals_usd"] = 25000
    plan = sec(run(inputs), "us_retirement")["summary"]["workplace_deferrals"]
    assert (plan["limit_usd"], plan["excess_usd"]) == ("23500.00", "1500.00")
    profile(inputs)["birth_year"] = 1970  # 55 in 2025: 23,500 + 7,500
    plan = sec(run(inputs), "us_retirement")["summary"]["workplace_deferrals"]
    assert plan["limit_usd"] == "31000.00" and plan["excess_usd"] == "0.00" and "50+" in plan["catch_up_basis"]
    profile(inputs)["birth_year"] = 1963  # 62 in 2025: 23,500 + 11,250 (SECURE 2.0 sec. 109)
    plan = sec(run(inputs), "us_retirement")["summary"]["workplace_deferrals"]
    assert plan["limit_usd"] == "34750.00" and "60-63" in plan["catch_up_basis"]


def test_workplace_accounts_are_outside_8949_and_rmd_aggregation():
    inputs = with_401k()
    profile(inputs)["birth_year"] = 1950
    inputs["ledger"]["entries"].append(entry(91, "fid401k", "withdrawal", "2025-07-01", "-5000"))
    retire = sec(run(inputs), "us_retirement")
    # A 401(k) withdrawal is not an IRA RMD.
    assert retire["summary"]["rmd"]["taken_usd"] == "0.00"
    assert any("figured per plan" in w for w in retire["warnings"])


# ------------------------------------------------------------------ T2: RMD for people born before 1951


def ira_owner(birth_year, balance=None):
    inputs = deepcopy(US)
    profile(inputs)["birth_year"] = birth_year
    inputs["ledger"]["accounts"].append({"id": "trad", "institution": "Vanguard", "type": "ira", "currency": "USD",
                                         "country": "US", "owners": [{"person_id": "sam", "share": "1"}]})
    inputs["ledger"]["entries"].append(entry(92, "trad", "opening_balance", "2024-12-31", "400000"))
    if balance is not None:
        us_tax(inputs)["ira_prior_year_end_balance_usd"] = balance
    inputs.setdefault("parameters", {})["us_ira_catch_up"] = {"value": 1000, "source": "IRC 219(b)(5)(B)"}
    return inputs


def test_rmd_born_1950_is_age_72_and_computed():
    report = run(ira_owner(1950, 400000))
    rmd = sec(report, "us_retirement")["summary"]["rmd"]
    assert rmd["rmd_start_age"] == 72
    # Age 75 in 2025: Uniform Lifetime Table divisor 24.6.
    assert Decimal(rmd["required_usd"]) == (Decimal(400000) / Decimal("24.6")).quantize(Decimal("0.01"))
    assert Decimal(rmd["shortfall_usd"]) == Decimal(rmd["required_usd"])
    without_balance = run(ira_owner(1950))
    assert "tax.2025.us.ira_prior_year_end_balance_usd" in pend(without_balance)


def test_rmd_born_before_july_1949_is_seventy_and_a_half():
    rmd = sec(run(ira_owner(1945, 400000)), "us_retirement")["summary"]["rmd"]
    assert rmd["rmd_start_age"] == 70.5 and rmd["required_usd"] is not None
    # Born 1949: 70½ or 72 by birth month, but RMDs are due from 2021 either way.
    retire = sec(run(ira_owner(1949, 400000)), "us_retirement")
    assert retire["summary"]["rmd"]["required_usd"] is not None
    assert any("1949" in w for w in retire["warnings"])


# ------------------------------------------------------------------ T10: unknown acquisition date


def test_unknown_acquisition_date_keeps_the_gain_with_the_term_unknown():
    inputs = deepcopy(US)
    inputs["ledger"]["entries"] += [entry(75, "schwab", "transfer", "2025-02-01", instrument_id="SCHD", quantity="10",
                                          cost_basis="600"),
                                    entry(76, "schwab", "sell", "2025-09-01", "900", instrument_id="SCHD",
                                          quantity="10")]
    report = run(inputs)
    rows = sec(report, "us_8949")["table"]["rows"]
    unknown = [r for r in rows if r["date_acquired"] is None]
    assert unknown and all(r["gain_usd"] is not None and r["term"] is None for r in unknown)
    assert "VARIOUS" in unknown[0]["note"]
    keys = pend(report)
    assert not any(k.startswith("fx.USD/USD") for k in keys)
    assert not any(k.endswith(".cost_basis") for k in keys)
    assert any(k.endswith(".acquired_on") for k in keys)
    assert sec(report, "us_schedule_d")["summary"]["unknown_term_gain_usd"] is not None


# ------------------------------------------------------------------ T9: Form 1116 categories


def test_mexican_isr_is_split_into_passive_and_general_or_flagged():
    inputs = deepcopy(XB)
    us_tax(inputs)["mx_annual_isr_usd"] = 5000
    report = run(inputs)
    rows = sec(report, "us_foreign_tax")["table"]["rows"]
    stated = next(r for r in rows if "annual ISR" in r["country"])
    assert stated["category"] == "unsplit"
    assert "tax.2025.us.mx_annual_isr_passive_usd" in pend(report)
    assert any("904(d)" in w for w in sec(report, "us_foreign_tax")["warnings"])
    us_tax(inputs).update(mx_annual_isr_passive_usd=800, mx_annual_isr_general_usd=4200)
    rows = sec(run(inputs), "us_foreign_tax")["table"]["rows"]
    assert {(r["tax_usd"], r["category"]) for r in rows if "annual ISR" in r["country"]} == {
        ("800.00", "passive"), ("4200.00", "general")}


# ------------------------------------------------------------------ T6: dividends compare like with like


def test_dividend_isr_recon_excludes_us_withholding_and_shows_art140_credit():
    inputs = deepcopy(MX)
    doc = next(f for f in inputs["facts"] if f["key"] == "constancia.gbm_2025")["value"]
    doc["dividendos"] = {"domestic_gross": 1200, "isr_withheld": 120, "isr_creditable": 514.29}
    inputs["ledger"]["instruments"].append({"id": "AAPLN", "symbol": "AAPL*", "currency": "MXN",
                                            "asset_class": "equity", "venue": "sic", "issuer_domicile": "US"})
    inputs["ledger"]["entries"] += [
        entry(80, "gbm", "buy", "2025-03-03", "-40000", "MXN", instrument_id="AAPLN", quantity="10"),
        entry(81, "gbm", "dividend", "2025-08-15", "500", "MXN", instrument_id="AAPLN"),
        entry(82, "gbm", "tax_withheld", "2025-08-15", "-50", "MXN", instrument_id="AAPLN")]
    div = sec(run(inputs), "mx_dividendos")
    isr = next(r for r in div["reconciliation"] if "ISR" in r["item"])
    assert (isr["ours"], isr["document"], isr["difference"]) == ("120.00", "120.00", "0.00")
    broker = div["summary"]["brokers"][0]
    assert broker["withheld_abroad_mxn"] == "50.00"
    art140 = broker["art140"]
    assert art140["gross_up_factor"] == "1.4286" and art140["computed_credit_mxn"] == "514.30"
    assert art140["corporate_isr_credit_mxn"] == "514.29" and art140["accumulable_mxn"] == "1714.29"


# ------------------------------------------------------------------ Form 8938 abroad test


def test_8938_abroad_thresholds_need_the_residence_test():
    report = run(XB)
    form = sec(report, "us_fbar_8938")["summary"]["form_8938"]
    assert form["lives_abroad"] is True and form["abroad_thresholds_apply"] is None
    assert "tax.2025.us.foreign_residence_test" in pend(report)
    inputs = deepcopy(XB)
    us_tax(inputs)["foreign_residence_test"] = "physical_presence"
    form = sec(run(inputs), "us_fbar_8938")["summary"]["form_8938"]
    assert form["abroad_thresholds_apply"] is True and form["threshold_last_day_usd"] == "200000.00"
    us_tax(inputs)["foreign_residence_test"] = "neither"
    report = run(inputs)
    form = sec(report, "us_fbar_8938")["summary"]["form_8938"]
    assert form["abroad_thresholds_apply"] is False and form["threshold_last_day_usd"] == "50000.00"
    assert "tax.2025.us.foreign_residence_test" not in pend(report)
