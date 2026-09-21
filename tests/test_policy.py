"""Investment Policy Statement: draft rules, checks, and the decision lifecycle through the service."""
from __future__ import annotations

import copy
from datetime import datetime, timezone

import pytest

from wealth import policy
from wealth.profile import profile_view
from wealth.situation import build
from wealth.situation.schema import SchemaError, validate
from wealth.service import WealthService
from wealth.store import IneligibleEvidenceError, ValidationError

AS_OF = "2026-09-21"
TODAY = datetime.now(timezone.utc).date().isoformat()

MX = {
    "client.profile": {"name": "Ana", "residence": {"country": "MX"}, "tax_residence": ["MX"], "birth_year": 1990,
                       "us_person": False},
    "income.salary": {"amount": 60000, "currency": "MXN", "frequency": "monthly", "net": True, "kind": "salary"},
    "spending.monthly": {"essential": 25000, "total": 35000, "currency": "MXN"},
    "cash.nu": {"amount": 180000, "currency": "MXN", "purpose": "reserve"},
    "cash.enganche": {"amount": 100000, "currency": "MXN", "purpose": "goal:casa"},
    "liability.auto": {"kind": "auto", "balance": 120000, "currency": "MXN", "annual_rate": 0.12, "payment": 5000,
                       "payment_frequency": "monthly"},
    "goals": [{"id": "casa", "name": "Enganche casa", "target_amount": 400000, "currency": "MXN",
               "target_date": "2028-06-01", "monthly_contribution": 8000},
              {"id": "retiro", "name": "Retiro", "target_amount": 8000000, "currency": "MXN",
               "target_date": "2055-01-01", "monthly_contribution": 6000}],
    "preference.risk": {"drop_reaction": "hold", "experience": "some"},
    "constraint.esg": {"exclude": ["tobacco"]},
}
US = {
    "client.profile": {"name": "Sam", "residence": {"country": "US"}, "birth_year": 1985},
    "income.salary": {"amount": 9000, "currency": "USD", "frequency": "monthly", "net": True, "kind": "salary"},
    "spending.monthly": {"essential": 4000, "total": 5500, "currency": "USD"},
    "cash.hysa": {"amount": 30000, "currency": "USD", "purpose": "reserve"},
    "goals": [{"id": "college", "name": "College", "target_amount": 150000, "currency": "USD",
               "target_date": "2040-09-01", "monthly_contribution": 500}],
    "preference.risk": {"drop_reaction": "buy_more", "experience": "experienced"},
    "onboarding": {"steps": {"debts": "done"}, "started_at": "2026-09-01T00:00:00Z"},
}


def _snapshot(facts: dict, *, inferred: tuple = ()) -> dict:
    return {"client": {"id": "c", "revision": 1}, "facts": [
        {"id": "id-" + key, "key": key, "value": value, "status": "active",
         "confidence": "inferred" if key in inferred else "reported",
         "source": {"kind": "user", "ref": "chat", "observed_on": AS_OF}, "expires_on": "2027-09-01"}
        for key, value in facts.items()]}


def _draft(facts: dict, **kwargs) -> dict:
    snapshot = _snapshot(facts, inferred=kwargs.pop("inferred", ()))
    sit = build(snapshot, None, AS_OF)
    return policy.draft(sit, policy.preferences_from_snapshot(snapshot, AS_OF), **kwargs)


def _with(base: dict, **changes) -> dict:
    facts = copy.deepcopy(base)
    for key, value in changes.items():
        key = key.replace("__", ".")
        if value is None:
            facts.pop(key, None)
        else:
            facts[key] = value
    return facts


# ------------------------------------------------------------------ required return


def test_required_return_reaches_the_target_exactly():
    solved = policy.required_return(target=100000, funded=50000, monthly=500, months=60)
    rate = solved["rate"]
    assert solved["status"] == "ready" and 0 < rate < 0.2
    assert policy.future_value(50000, 500, 60, rate) == pytest.approx(100000, rel=1e-5)
    # Lump sum only: (1+r)^5 = 2 -> r = 2^(1/5) - 1.
    assert policy.required_return(200, 100, 0, 60)["rate"] == pytest.approx(2 ** 0.2 - 1, abs=1e-6)
    # Contributions alone at 0% reach 100 * 12 = 1200: the required rate is 0.
    assert policy.required_return(1200, 0, 100, 12)["rate"] == pytest.approx(0, abs=1e-6)


def test_required_return_edges():
    assert policy.required_return(1000, 0, 0, 12)["status"] == "unfunded"
    assert policy.required_return(10 ** 9, 10, 1, 12)["status"] == "unrealistic"
    assert policy.required_return(1000, 2000, 0, 0)["status"] == "reached"
    assert policy.required_return(1000, 100, 0, 0)["status"] == "due"
    # Already over-funded: no return is needed; never a negative "required" rate.
    over = policy.required_return(1000, 2000, 0, 24)
    assert over["status"] == "met" and over["rate"] == 0.0


def test_goal_objectives_use_earmarked_cash_and_contributions():
    ips = _draft(MX)["result"]["ips"]
    casa, retiro = ips["objectives"]
    assert casa["funded"] == 100000 and casa["months_left"] == 21
    assert policy.future_value(100000, 8000, 21, casa["required_return"]) == pytest.approx(400000, rel=1e-5)
    assert retiro["funded"] == 0 and retiro["required_return"] == pytest.approx(0.08504, abs=1e-4)
    # The near goal does not set the portfolio's requirement; the long one does.
    assert ips["return_requirement"]["goal_id"] == "retiro"
    assert "Required return" in casa["rationale"]


# ------------------------------------------------------------------ ability versus willingness


def test_profile_is_the_stricter_of_ability_and_willingness():
    ips = _draft(MX)["result"]["ips"]
    assert ips["risk"]["ability"]["level"] == "high"
    assert ips["risk"]["willingness"]["level"] == "medium"
    assert ips["risk"]["profile"] == "balanced"
    assert {f["level"] for f in ips["risk"]["ability"]["factors"].values()} == {"high"}
    # A seller in a fall is low willingness whatever the ability.
    sell = _draft(_with(MX, preference__risk={"drop_reaction": "sell"}))["result"]["ips"]
    assert sell["risk"]["profile"] == "conservative"
    # A thin reserve lowers ability below a bold willingness.
    thin = _with(US, cash__hysa={"amount": 8000, "currency": "USD", "purpose": "reserve"})
    ips = _draft(thin)["result"]["ips"]
    assert ips["risk"]["ability"]["factors"]["reserve"]["level"] == "low"
    assert ips["risk"]["willingness"]["level"] == "high" and ips["risk"]["profile"] == "conservative"


def test_no_experience_caps_willingness_and_high_interest_debt_lowers_ability():
    novice = _draft(_with(US, preference__risk={"drop_reaction": "buy_more", "experience": "none"}))["result"]["ips"]
    assert novice["risk"]["willingness"]["level"] == "medium" and novice["risk"]["profile"] == "balanced"
    card = _with(US, liability__card={"kind": "card", "balance": 5000, "currency": "USD", "annual_rate": 0.29,
                                      "payment": 300, "payment_frequency": "monthly"})
    ips = _draft(card)["result"]["ips"]
    assert ips["risk"]["ability"]["factors"]["debt_load"]["level"] == "low"
    assert ips["risk"]["ability"]["factors"]["debt_load"]["high_interest"] == ["card"]
    assert ips["risk"]["profile"] == "conservative"


def test_debt_load_and_income_stability_ratios():
    ips = _draft(MX)["result"]["ips"]
    factors = ips["risk"]["ability"]["factors"]
    assert factors["debt_load"]["value"] == pytest.approx(5000 / 60000, abs=1e-4)
    assert factors["income_stability"]["value"] == 1.0
    business = _with(MX, income__shop={"amount": 60000, "currency": "MXN", "frequency": "monthly", "kind": "business"})
    factors = _draft(business)["result"]["ips"]["risk"]["ability"]["factors"]
    assert factors["income_stability"]["value"] == 0.5 and factors["income_stability"]["level"] == "medium"


# ------------------------------------------------------------------ buckets and liquidity


def test_goals_are_bucketed_by_horizon_and_near_goals_stay_liquid():
    goals = [
        {"id": "trip", "name": "Trip", "target_amount": 30000, "currency": "USD", "target_date": "2027-06-01",
         "monthly_contribution": 1000},
        {"id": "car", "name": "Car", "target_amount": 40000, "currency": "USD", "target_date": "2030-09-01",
         "monthly_contribution": 500},
        {"id": "house", "name": "House", "target_amount": 120000, "currency": "USD", "target_date": "2034-01-01",
         "monthly_contribution": 800},
        {"id": "retire", "name": "Retire", "target_amount": 2000000, "currency": "USD", "target_date": "2050-01-01",
         "monthly_contribution": 1500},
        {"id": "sp500", "name": "Invest monthly", "action": "invest", "monthly_contribution": 300, "currency": "USD"},
    ]
    ips = _draft(_with(US, goals=goals))["result"]["ips"]
    buckets = {b["id"]: b for b in ips["buckets"]}
    assert buckets["liquidity"]["goals"] == ["trip"] and buckets["liquidity"]["max_equity"] == 0
    assert buckets["short"]["goals"] == ["car"] and buckets["short"]["max_profile"] == "conservative"
    assert buckets["medium"]["goals"] == ["house"] and buckets["medium"]["max_profile"] == "balanced"
    assert buckets["long"]["goals"] == ["retire", "sp500"] and buckets["long"]["max_profile"] == "growth"
    assert [g["goal_id"] for g in ips["liquidity"]["near_goals"]] == ["trip"]
    assert ips["liquidity"]["reserve"]["target_amount"] == 24000  # 6 x 4,000 essential (default)
    assert ips["liquidity"]["reserve"]["held"] == 30000


def test_near_goal_shortfall_is_stated_in_numbers():
    report = _draft(MX)
    casa = report["result"]["ips"]["objectives"][0]
    assert casa["bucket"] == "liquidity"
    assert casa["at_zero_return"] == 100000 + 8000 * 21
    assert casa["monthly_needed_at_zero"] == pytest.approx(300000 / 21, abs=0.01)
    assert any("Enganche casa" in w and "268,000" in w for w in report["warnings"])


def test_stated_reserve_target_wins_over_the_default():
    ips = _draft(_with(US, reserve={"target_months": 9}))["result"]["ips"]
    assert ips["liquidity"]["reserve"]["target_amount"] == 36000
    assert ips["evidence"]["reserve"] == "id-reserve"


# ------------------------------------------------------------------ Mexico versus US


def test_mexican_draft_uses_cetes_and_irish_ucits():
    report = _draft(MX)
    ips = report["result"]["ips"]
    assert report["status"] == "ready" and report["missing"] == []
    sleeves = {s["id"]: s for s in ips["allocation"]["sleeves"]}
    assert set(sleeves) == {"global_equity", "mx_fixed_income", "cash"}
    assert any("CETES" in v for v in sleeves["mx_fixed_income"]["vehicles"])
    assert any("UDIBONOS" in v for v in sleeves["mx_fixed_income"]["vehicles"])
    assert all("UCITS" in v for v in sleeves["global_equity"]["vehicles"])
    assert (sleeves["global_equity"]["target"], sleeves["global_equity"]["min"], sleeves["global_equity"]["max"]) == (0.6, 0.55, 0.65)
    assert (sleeves["cash"]["min"], sleeves["cash"]["max"]) == (0.0375, 0.0625)  # 25% relative band
    assert ips["constraints"]["estate_situs"]["prefer"] == "non_us_domiciled"
    assert ips["currency"] == "MXN"
    assert [t["rule"] for t in ips["constraints"]["tax"]] == ["SIC listing decides the 10% rate"]
    assert ips["constraints"]["exclusions"]["tags"] == ["tobacco"]
    assert ips["constraints"]["leverage"]["allowed"] is False and ips["constraints"]["concentration"]["limit"] == 0.10
    assert "Irish UCITS" in ips["allocation"]["rationale"]


def test_us_draft_uses_us_funds_and_avoids_pfics():
    ips = _draft(US)["result"]["ips"]
    sleeves = {s["id"]: s for s in ips["allocation"]["sleeves"]}
    assert set(sleeves) == {"global_equity", "us_fixed_income", "cash"}
    assert ips["risk"]["profile"] == "growth" and sleeves["global_equity"]["target"] == 0.8
    assert ips["constraints"]["estate_situs"]["prefer"] is None
    assert [t["rule"] for t in ips["constraints"]["tax"]] == ["PFIC"]
    assert ips["currency"] == "USD"


def test_us_person_in_mexico_gets_no_ucits_preference():
    facts = _with(MX, client__profile={**MX["client.profile"], "us_person": True})
    ips = _draft(facts)["result"]["ips"]
    assert ips["constraints"]["estate_situs"]["prefer"] is None
    assert {t["rule"] for t in ips["constraints"]["tax"]} == {"SIC listing decides the 10% rate", "PFIC"}


# ------------------------------------------------------------------ missing inputs


def test_unknown_inputs_are_missing_never_guessed():
    facts = _with(MX, preference__risk=None, income__salary=None)
    report = _draft(facts)
    assert report["status"] == "needs_input"
    assert report["result"]["ips"]["allocation"] is None
    assert "preference.risk.drop_reaction" in report["missing"]
    assert "income.<id>" in report["missing"]
    assert report["result"]["ips"]["risk"]["profile"] is None
    no_contribution = _with(MX, goals=[{"id": "retiro", "name": "Retiro", "target_amount": 8000000,
                                        "currency": "MXN", "target_date": "2055-01-01"}])
    report = _draft(no_contribution)
    assert "goals.retiro.monthly_contribution" in report["missing"]
    assert report["result"]["ips"]["objectives"][0]["required_return"] is None


def test_one_low_factor_decides_even_with_unknowns():
    facts = _with(MX, preference__risk={"drop_reaction": "sell"}, income__salary=None)
    report = _draft(facts)
    assert report["status"] == "partial" and report["result"]["ips"]["risk"]["profile"] == "conservative"
    assert "income.<id>" in report["missing"]


def test_inferred_preferences_are_not_used():
    report = _draft(MX, inferred=("preference.risk",))
    assert report["status"] == "needs_input" and "preference.risk.drop_reaction" in report["missing"]


def test_amendments_may_only_reduce_risk():
    ips = _draft(US, overrides={"profile": "balanced", "concentration_limit": 0.05,
                                "review_cadence": "semiannual"})["result"]["ips"]
    assert ips["risk"]["profile"] == "balanced" and ips["risk"]["derived_profile"] == "growth"
    assert ips["constraints"]["concentration"]["limit"] == 0.05
    assert ips["review"]["cadence"] == "semiannual" and ips["review"]["next_review"] == "2027-03-21"
    with pytest.raises(ValueError, match="only be stricter"):
        _draft(MX, overrides={"profile": "growth"})
    with pytest.raises(ValueError, match="unknown"):
        _draft(MX, overrides={"equity": 0.9})


# ------------------------------------------------------------------ checks


def _mx_ips() -> dict:
    return _draft(MX)["result"]["ips"]


MX_PORTFOLIO = {"currency": "MXN", "positions": [
    {"symbol": "CSPX", "value": 300000}, {"symbol": "CETES", "value": 175000},
    {"symbol": "CASH", "value": 25000, "asset_class": "cash"}]}


def _status(result: dict, rule: str) -> list[str]:
    return [r["status"] for r in result["rules"] if r["rule"] == rule]


def test_check_allocation_bands():
    ips = _mx_ips()
    inside = policy.check(ips, {"kind": "allocation", "target": {"global_equity": 0.62, "mx_fixed_income": 0.33,
                                                                 "cash": 0.05}})
    assert inside["verdict"] == "pass"
    outside = policy.check(ips, {"kind": "allocation", "target": {"global_equity": 0.8, "mx_fixed_income": 0.15,
                                                                  "cash": 0.05}})
    assert outside["verdict"] == "violation"
    assert sorted(_status(outside, "allocation_band")) == ["pass", "violation", "violation"]
    unknown = policy.check(ips, {"kind": "allocation", "target": {"crypto": 0.1, "global_equity": 0.6,
                                                                  "mx_fixed_income": 0.3}})
    assert unknown["verdict"] == "violation" and "crypto is not a sleeve" in unknown["rules"][0]["explanation"]


def test_check_trade_bands_before_and_after():
    ips = _mx_ips()
    small = policy.check(ips, {"kind": "trade", "action": "buy", "symbol": "CSPX", "amount": 10000,
                               "funding": "surplus"}, MX_PORTFOLIO)
    assert _status(small, "allocation_band") == ["pass"]
    big = policy.check(ips, {"kind": "trade", "action": "buy", "symbol": "CSPX", "amount": 100000,
                             "funding": "surplus"}, MX_PORTFOLIO)
    assert "violation" in _status(big, "allocation_band")
    no_portfolio = policy.check(ips, {"kind": "trade", "action": "buy", "symbol": "CSPX", "amount": 1000,
                                      "funding": "surplus"})
    assert _status(no_portfolio, "allocation_band") == ["warn"]
    assert "Not checked" in no_portfolio["rules"][0]["explanation"]


def test_check_concentration_limit():
    ips = _mx_ips()
    stock = policy.check(ips, {"kind": "trade", "action": "buy", "symbol": "AMXL", "amount": 80000,
                               "asset_class": "stock", "domicile": "MX", "funding": "surplus", "tags": []},
                         MX_PORTFOLIO)
    assert _status(stock, "concentration") == ["violation"]
    assert "above the 10% single-holding limit" in next(r for r in stock["rules"] if r["rule"] == "concentration")["explanation"]
    near = policy.check(ips, {"kind": "trade", "action": "buy", "symbol": "AMXL", "amount": 50000,
                              "asset_class": "stock", "domicile": "MX", "funding": "surplus"}, MX_PORTFOLIO)
    assert _status(near, "concentration") == ["warn"]  # 9.1%: close to the limit
    fund = policy.check(ips, {"kind": "trade", "action": "buy", "symbol": "CSPX", "amount": 80000,
                              "funding": "surplus"}, MX_PORTFOLIO)
    assert _status(fund, "concentration") == ["pass"]
    tighter = copy.deepcopy(ips)
    tighter["constraints"]["concentration"]["limit"] = 0.05
    assert _status(policy.check(tighter, {"kind": "trade", "action": "buy", "symbol": "AMXL", "amount": 30000,
                                          "asset_class": "stock", "domicile": "MX", "funding": "surplus"},
                                MX_PORTFOLIO), "concentration") == ["violation"]


def test_check_reserve_and_goal_buckets_are_protected():
    ips = _mx_ips()
    reserve = policy.check(ips, {"kind": "trade", "action": "buy", "symbol": "CSPX", "amount": 20000,
                                 "funding": "reserve"})
    assert _status(reserve, "liquidity_reserve") == ["violation"]
    by_account = policy.check(ips, {"kind": "trade", "action": "buy", "symbol": "CSPX", "amount": 20000,
                                    "funding": "cash:nu"})
    assert _status(by_account, "liquidity_reserve") == ["violation"]
    goal = policy.check(ips, {"kind": "trade", "action": "buy", "symbol": "CSPX", "amount": 20000,
                              "funding": "goal:casa"})
    assert _status(goal, "goal_bucket") == ["violation"]
    cash_like = policy.check(ips, {"kind": "trade", "action": "buy", "symbol": "CETES", "amount": 20000,
                                   "funding": "goal:casa"})
    assert _status(cash_like, "goal_bucket") == ["pass"]  # CETES are cash-like: allowed for near goals
    # A reserve below target warns when the source is not named.
    low = build(_snapshot(_with(MX, cash__nu={"amount": 100000, "currency": "MXN", "purpose": "reserve"})), None, AS_OF)
    unfunded = policy.check(ips, {"kind": "trade", "action": "buy", "symbol": "CSPX", "amount": 20000}, None, low)
    rule = next(r for r in unfunded["rules"] if r["rule"] == "liquidity_reserve")
    assert rule["status"] == "warn" and "50,000 MXN below its target" in rule["explanation"]


def test_check_constraints_leverage_and_exclusions():
    ips = _mx_ips()
    margin = policy.check(ips, {"kind": "trade", "action": "buy", "symbol": "CSPX", "amount": 1000,
                                "funding": "surplus", "instrument": "margin"})
    assert _status(margin, "leverage") == ["violation"] and margin["verdict"] == "violation"
    tobacco = policy.check(ips, {"kind": "trade", "action": "buy", "symbol": "PM", "amount": 1000, "domicile": "US",
                                 "asset_class": "stock", "funding": "surplus", "tags": ["Tobacco"]})
    assert _status(tobacco, "exclusions") == ["violation"]
    untagged = policy.check(ips, {"kind": "trade", "action": "buy", "symbol": "CSPX", "amount": 1000,
                                  "funding": "surplus"})
    assert _status(untagged, "exclusions") == ["warn"]
    allowed = copy.deepcopy(ips)
    allowed["constraints"]["leverage"]["allowed"] = True
    assert _status(policy.check(allowed, {"kind": "trade", "action": "buy", "symbol": "CSPX", "amount": 1000,
                                          "funding": "surplus", "leverage": True}), "leverage") == ["pass"]


def test_check_estate_situs_preference_for_mexican_residents():
    ips = _mx_ips()
    voo = policy.check(ips, {"kind": "trade", "action": "buy", "symbol": "VOO", "amount": 1000, "funding": "surplus",
                             "tags": []})
    rule = next(r for r in voo["rules"] if r["rule"] == "estate_situs")
    assert rule["status"] == "warn" and rule["alternative"] == "CSPX"
    cspx = policy.check(ips, {"kind": "trade", "action": "buy", "symbol": "CSPX", "amount": 1000, "funding": "surplus",
                              "tags": []})
    assert _status(cspx, "estate_situs") == ["pass"]
    us = _draft(US)["result"]["ips"]
    assert _status(policy.check(us, {"kind": "trade", "action": "buy", "symbol": "VOO", "amount": 1000,
                                     "funding": "surplus"}), "estate_situs") == []


def test_check_rejects_malformed_proposals():
    ips = _mx_ips()
    with pytest.raises(ValueError, match="action"):
        policy.check(ips, {"kind": "trade", "action": "hold", "symbol": "X", "amount": 1})
    with pytest.raises(ValueError, match="amount"):
        policy.check(ips, {"kind": "trade", "action": "buy", "symbol": "X", "amount": -1})
    with pytest.raises(ValueError, match="allocation.sleeves"):
        policy.check({}, {"kind": "allocation", "target": {"a": 1}})


# ------------------------------------------------------------------ schema


def test_policy_ips_schema():
    ips = {**_mx_ips(), "decision_id": "d1", "accepted_on": AS_OF}
    assert validate("policy.ips", ips) == []
    broken = copy.deepcopy(ips)
    broken["allocation"]["sleeves"][0]["target"] = 0.9
    with pytest.raises(SchemaError, match="sum to 1|min <= target"):
        validate("policy.ips", broken)
    with pytest.raises(SchemaError, match="decision_id"):
        validate("policy.ips", {k: v for k, v in ips.items() if k != "decision_id"})
    bad_cadence = copy.deepcopy(ips)
    bad_cadence["review"]["cadence"] = "weekly"
    with pytest.raises(SchemaError, match="cadence"):
        validate("policy.ips", bad_cadence)


# ------------------------------------------------------------------ the decision lifecycle


def _client(tmp_path, facts: dict) -> WealthService:
    service = WealthService(tmp_path / "wealth.sqlite3")
    service.create("ana", "Ana")
    service.remember("ana", [{"key": key, "value": value,
                              "source": {"kind": "user", "ref": "conversation", "observed_on": TODAY}}
                             for key, value in facts.items()])
    return service


def test_draft_propose_accept_check_and_amend(tmp_path):
    service = _client(tmp_path, MX)
    no_policy = service.run("policy_check", {"proposal": {"kind": "trade", "action": "buy", "symbol": "CSPX",
                                                          "amount": 1000}}, client_id="ana")
    assert no_policy["status"] == "needs_input" and no_policy["missing"] == ["policy.ips"]

    drafted = service.run("policy_draft", {"propose": True}, client_id="ana")
    decision = drafted["decision"]
    assert decision["status"] == "proposed" and decision["title"].startswith("Investment policy: balanced")
    ips = drafted["result"]["ips"]
    assert sorted(decision["evidence_ids"]) == sorted(set(ips["evidence"].values()))
    assert set(drafted["evidence_ids"]) == set(decision["evidence_ids"])
    assert {a["model"] for a in decision["alternatives"]} == {"conservative", "growth"}
    assert service.inspect("ana", key="policy.ips").get("absent_keys") == ["policy.ips"]

    accepted = service.decision("accept", "ana", {"decision_id": decision["id"]})
    assert accepted["status"] == "accepted" and accepted["policy"]["version"] == 1
    stored = service.inspect("ana", key="policy.ips")["facts"][0]
    assert stored["value"]["decision_id"] == decision["id"] and stored["confidence"] == "confirmed"
    assert stored["value"]["allocation"]["model"] == "balanced"

    checked = service.run("policy_check", {"proposal": {"kind": "trade", "action": "buy", "symbol": "VOO",
                                                        "amount": 20000, "funding": "reserve", "tags": []}},
                          client_id="ana")
    assert checked["status"] == "ready" and checked["result"]["verdict"] == "violation"
    assert stored["id"] in checked["evidence_ids"]
    assert _status(checked["result"], "estate_situs") == ["warn"]

    # Amendment: a stricter profile supersedes the accepted policy, and the history keeps both.
    amended = service.run("policy_draft", {"propose": True, "overrides": {"profile": "conservative"}},
                          client_id="ana")
    second = service.decision("accept", "ana", {"decision_id": amended["decision"]["id"]})
    assert second["policy"] == {"key": "policy.ips", "id": second["policy"]["id"], "version": 2,
                                "supersedes": decision["id"]}
    current = service.inspect("ana", key="policy.ips")["facts"][0]["value"]
    assert current["allocation"]["model"] == "conservative" and current["supersedes"] == decision["id"]
    history = service.inspect("ana", detail="history", key="policy.ips")["history"]
    assert len(history) == 2
    # Accepting the same decision again changes nothing.
    again = service.decision("accept", "ana", {"decision_id": amended["decision"]["id"]})
    assert again["policy"]["version"] == 2
    assert len(service.inspect("ana", detail="history", key="policy.ips")["history"]) == 2
    # The profile page carries a compact policy block.
    block = profile_view(service, "ana")["policy"]
    assert block["profile"] == "conservative" and block["version"] == 2 and block["no_leverage"] is True
    assert [s["id"] for s in block["sleeves"]] == ["global_equity", "mx_fixed_income", "cash"]


def test_policy_lapses_at_its_review_date(tmp_path):
    service = _client(tmp_path, US)
    drafted = service.run("policy_draft", {"propose": True, "overrides": {"review_cadence": "quarterly"}},
                          client_id="ana")
    service.decision("accept", "ana", {"decision_id": drafted["decision"]["id"]})
    fact = service.inspect("ana", key="policy.ips")["facts"][0]
    assert fact["expires_on"] == fact["value"]["review"]["next_review"] > TODAY
    snapshot = service.inspect("ana")
    later = "2099-01-01"
    report = policy.run_task("policy_check", {"proposal": {"kind": "allocation", "target": {"global_equity": 1}}},
                             snapshot, None, later)
    assert report["status"] == "needs_input" and "passed its review date" in report["warnings"][0]


def test_changed_evidence_blocks_acceptance(tmp_path):
    service = _client(tmp_path, US)
    drafted = service.run("policy_draft", {"propose": True}, client_id="ana")
    service.remember("ana", [{"key": "cash.hysa", "value": {"amount": 5000}, "merge": True,
                              "source": {"kind": "user", "ref": "conversation", "observed_on": TODAY}}])
    with pytest.raises(IneligibleEvidenceError, match="cash.hysa changed"):
        service.decision("accept", "ana", {"decision_id": drafted["decision"]["id"]})
    assert service.inspect("ana", key="policy.ips").get("absent_keys") == ["policy.ips"]


def test_dismissed_draft_is_never_stored_and_incomplete_draft_cannot_be_proposed(tmp_path):
    service = _client(tmp_path, US)
    drafted = service.run("policy_draft", {"propose": True}, client_id="ana")
    service.decision("dismiss", "ana", {"decision_id": drafted["decision"]["id"]})
    assert service.inspect("ana", key="policy.ips").get("absent_keys") == ["policy.ips"]
    bare = _client(tmp_path / "bare", {"client.profile": {"residence": {"country": "US"}}})
    with pytest.raises(ValueError, match="cannot be proposed"):
        bare.run("policy_draft", {"propose": True}, client_id="ana")
    with pytest.raises(ValueError, match="needs client_id"):
        WealthService(tmp_path / "none.sqlite3").run("policy_draft", {"propose": True})


def test_policy_ips_cannot_be_written_with_a_broken_shape(tmp_path):
    service = _client(tmp_path, US)
    with pytest.raises(ValidationError, match="policy.ips"):
        service.remember("ana", [{"key": "policy.ips", "value": {"version": 1},
                                  "source": {"kind": "user", "ref": "conversation", "observed_on": TODAY}}])


def test_policy_draft_runs_without_a_client_from_inline_facts(tmp_path):
    service = WealthService(tmp_path / "wealth.sqlite3")
    report = service.run("policy_draft", {"as_of": AS_OF, "facts": [{"key": k, "value": v} for k, v in US.items()]})
    assert report["status"] == "ready" and report["result"]["ips"]["risk"]["profile"] == "growth"
    assert report["evidence_ids"] == []
