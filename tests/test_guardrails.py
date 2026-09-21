"""Guardrails: play-money sleeve, education cards, panic circuit breaker, cool-off and scam screen."""
from __future__ import annotations

import re

import pytest

from wealth import guardrails as g
from wealth.catalog import CATALOG
from wealth.service import WealthService
from wealth.situation import build


TODAY = "2026-09-21"
TRADE_CALL = re.compile(
    r"\b(you should (buy|sell|short)|i recommend (buying|selling)|buy now|sell now|go long|go short|"
    r"te recomiendo (comprar|vender)|deberías (comprar|vender)|compra ya|vende ya)\b", re.IGNORECASE)


def _facts(**overrides):
    rows = {
        "client.profile": {"residence": {"country": "MX"}, "birth_year": 1988, "dependents": 1},
        "income.salary": {"amount": 60000, "currency": "MXN", "frequency": "monthly", "net": True},
        "spending.monthly": {"essential": 25000, "total": 35000, "currency": "MXN"},
        "cash.nu": {"amount": 150000, "currency": "MXN", "purpose": "reserve"},
        "reserve": {"target_months": 6},
        "investment.gbm": {"amount": 850000, "currency": "MXN", "institution": "GBM"},
    }
    rows.update(overrides)
    return [{"key": k, "value": v} for k, v in rows.items() if v is not None]


def _sit(**overrides):
    snapshot = {"client": {"id": None, "revision": None}, "facts": [
        {"id": f"f:{f['key']}", "key": f["key"], "value": f["value"], "confidence": "reported", "status": "active",
         "source": {"kind": "user", "ref": "test", "observed_on": TODAY}} for f in _facts(**overrides)],
        "decisions": []}
    return build(snapshot, None, TODAY)


def _buy(amount, instrument="crypto", **extra):
    return {"action": "buy", "instrument": instrument, "amount": amount, "currency": "MXN",
            "sleeve": {"value": 0, "peak": 0}, **extra}


def _texts(value):
    return list(g._no_trade_call_text(value))


# ------------------------------------------------------------------ speculation sleeve


def test_liquid_net_worth_and_reserve_are_what_the_example_expects():
    sit = _sit()
    assert sit["net_worth"]["liquid"] == 1_000_000
    assert sit["reserve"]["gap"] == 0


@pytest.mark.parametrize("amount, verdict", [(10000, "allow"), (50000, "decline_to_recommend")])
def test_cap_boundary_is_five_percent_of_liquid_net_worth(amount, verdict):
    policy = {"max_position_loss_share": 0.10}
    out = g.speculation_check(_buy(amount), _sit(), policy=policy)
    assert out["limits"]["cap"] == 50000
    assert out["verdict"] == "allow"  # 50,000 is exactly at the cap
    over = g.speculation_check(_buy(50001), _sit(), policy=policy)
    assert over["verdict"] == "decline_to_recommend"
    assert any(r["code"] == "cap" for r in over["reasons"])
    assert g.speculation_check(_buy(amount), _sit())["verdict"] == verdict


def test_existing_sleeve_counts_against_the_cap():
    out = g.speculation_check({**_buy(5000), "sleeve": {"value": 46000, "peak": 46000}}, _sit())
    assert out["verdict"] == "decline_to_recommend" and out["limits"]["sleeve_after"] == 51000


def test_single_position_loss_boundary_is_one_percent():
    assert g.speculation_check(_buy(10000), _sit())["verdict"] == "allow"
    out = g.speculation_check(_buy(10001), _sit())
    assert out["verdict"] == "decline_to_recommend"
    assert [r["code"] for r in out["reasons"]] == ["position_loss"]


def test_reserve_below_target_sets_the_cap_to_zero():
    sit = _sit(**{"cash.nu": {"amount": 149999, "currency": "MXN", "purpose": "reserve"}})
    out = g.speculation_check(_buy(100), sit)
    assert out["verdict"] == "decline_to_recommend" and out["limits"]["cap"] == 0
    assert out["reasons"][0]["code"] == "reserve" and out["reasons"][0]["es"]


@pytest.mark.parametrize("rate, verdict", [(0.15, "allow"), (0.1501, "decline_to_recommend")])
def test_debt_rate_boundary_is_fifteen_percent(rate, verdict):
    sit = _sit(**{"liability.card": {"kind": "card", "balance": 10000, "currency": "MXN", "annual_rate": rate,
                                     "payment": 1000, "payment_frequency": "monthly"}})
    assert g.speculation_check(_buy(1000), sit)["verdict"] == verdict


def test_unknown_debt_rate_warns_instead_of_counting_as_zero():
    sit = _sit(**{"liability.loan": {"kind": "personal", "balance": 10000, "currency": "MXN"}})
    out = g.speculation_check(_buy(1000), sit)
    assert out["verdict"] == "allow_with_warning"
    assert any("rate" in m for m in out["missing"])


def test_unknown_liquid_net_worth_is_not_treated_as_zero():
    sit = _sit(**{"investment.gbm": None, "cash.nu": None})
    out = g.speculation_check(_buy(1000), sit)
    assert out["verdict"] == "decline_to_recommend"
    assert "cap" not in out["limits"]
    assert any("liquid" in m for m in out["missing"])
    assert any(r["code"] == "cap" and "can't size" in r["en"] for r in out["reasons"])


def test_unknown_sleeve_warns():
    out = g.speculation_check({"action": "buy", "instrument": "crypto", "amount": 1000}, _sit())
    assert out["verdict"] == "allow_with_warning"
    assert any("sleeve" in m for m in out["missing"])


@pytest.mark.parametrize("value, verdict", [(70001, "allow"), (70000, "decline_to_recommend")])
def test_drawdown_stop_boundary_is_thirty_percent(value, verdict):
    out = g.speculation_check({**_buy(100), "sleeve": {"value": value / 10, "peak": 10000}}, _sit())
    assert out["verdict"] == verdict


def test_uncovered_call_and_short_have_no_ceiling():
    for proposal in ({"action": "buy", "instrument": "options", "side": "short", "option_type": "call", "amount": 500,
                      "sleeve": {"value": 0, "peak": 0}},
                     {"action": "buy", "instrument": "short", "amount": 500, "sleeve": {"value": 0, "peak": 0}}):
        out = g.speculation_check(proposal, _sit())
        assert out["verdict"] == "decline_to_recommend"
        assert out["limits"]["worst_case_loss"] is None
        assert any(r["code"] == "position_loss" for r in out["reasons"])


def test_leverage_multiplies_the_worst_case_and_needs_the_multiple():
    out = g.speculation_check({**_buy(2000, "cfd"), "leverage": 5}, _sit())
    assert out["limits"]["worst_case_loss"] == 10000 and out["verdict"] == "allow"
    unknown = g.speculation_check(_buy(2000, "margin"), _sit())
    assert unknown["verdict"] == "allow_with_warning" and unknown["limits"]["worst_case_loss"] is None


def test_ips_without_leverage_declines_to_recommend_leverage():
    ips = {"constraints": {"leverage": {"allowed": False}}}
    out = g.speculation_check({**_buy(1000, "cfd"), "leverage": 2}, _sit(), ips)
    assert out["verdict"] == "decline_to_recommend"
    assert any(r["code"] == "ips_leverage" for r in out["reasons"])
    assert g.speculation_check(_buy(1000, "crypto"), _sit(), ips)["verdict"] == "allow"


def test_ips_speculation_constraint_and_person_policy_apply_with_a_ceiling():
    ips = {"constraints": {"speculation": {"cap_share": 0.02}}}
    assert g.speculation_check(_buy(10000), _sit(), ips)["limits"]["cap"] == 20000
    assert g.speculation_check(_buy(10000), _sit(), policy={"cap_share": 0.1})["limits"]["cap"] == 100000
    with pytest.raises(ValueError, match="at most 0.10"):
        g.speculation_check(_buy(10000), _sit(), policy={"cap_share": 0.2})


def test_explain_never_declines_and_sell_reduces_risk():
    broke = _sit(**{"cash.nu": None, "investment.gbm": None})
    explain = g.speculation_check({"action": "explain", "instrument": "options"}, broke)
    assert explain["verdict"] == "allow" and explain["education"]["max_loss"]["en"]
    sell = g.speculation_check({"action": "sell", "instrument": "crypto", "amount": 5000}, broke)
    assert sell["verdict"] == "allow"


def test_speculation_includes_the_cool_off_flag():
    out = g.speculation_check(_buy(1000), _sit(), context={"now": "2026-09-21T23:30:00-06:00",
                                                           "timezone": "America/Mexico_City"})
    assert out["verdict"] == "allow_with_warning"
    assert out["cool_off"]["flag"] and out["reasons"][-1]["code"] == "late_night"


def test_bad_instrument_is_rejected():
    with pytest.raises(ValueError, match="instrument"):
        g.speculation_check({"action": "buy", "instrument": "lottery", "amount": 1}, _sit())


# ------------------------------------------------------------------ education cards


@pytest.mark.parametrize("instrument", ["options", "margin", "crypto", "fintech_yield", "stock"])
def test_every_card_has_mechanics_max_loss_liquidation_and_protection_in_both_languages(instrument):
    card = g.education_card(instrument)
    for field in ("mechanics", "max_loss", "liquidation", "protection"):
        assert card[field]["en"] and card[field]["es"]
    assert card["trade_call"] is None


def test_crypto_and_fintech_cards_state_ipab_non_coverage():
    crypto = g.education_card("crypto")
    assert "IPAB" in crypto["protection"]["en"] and "IPAB" in crypto["protection"]["es"]
    fintech = g.education_card("sofipo", udi_value=8.8)
    insurance = fintech["deposit_insurance_mx"]
    assert insurance["ipab_udis"] == 400000 and insurance["prosofipo_udis"] == 25000
    assert insurance["udi_value_status"] == "supplied" and insurance["ipab_mxn_approx"] == 3520000
    assert "crypto" in insurance["not_covered"]
    default = g.education_card("fintech_yield")["deposit_insurance_mx"]
    assert default["udi_value_status"] == "needs_verification"


def test_leverage_card_cites_esma_and_finra():
    card = g.education_card("cfd")
    titles = " ".join(s["title"] for s in card["sources"])
    assert "ESMA" in titles and "FINRA" in titles
    assert all(s["checked_on"] == TODAY for s in card["sources"])
    assert g.PARAMETERS["esma_leverage_caps"]["value"]["crypto"] == 2


# ------------------------------------------------------------------ cool-off


@pytest.mark.parametrize("size, minutes, flagged", [
    (-0.05, 10, False), (-0.051, 10, True), (0.06, 60, True), (-0.06, 61, False), (-0.06, -1, False)])
def test_cool_off_move_boundaries(size, minutes, flagged):
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 9, 21, 16, 0, tzinfo=timezone.utc)
    move = {"size": size, "at": (now - timedelta(minutes=minutes)).isoformat()}
    out = g.cool_off(now.isoformat(), "UTC", move)
    assert out["flag"] is flagged and out["blocks_choice"] is False


@pytest.mark.parametrize("hour, flagged", [(22, False), (23, True), (2, True), (5, False)])
def test_cool_off_late_night_uses_local_time(hour, flagged):
    local = f"2026-09-21T{hour:02d}:00:00-06:00"
    assert g.cool_off(local, "America/Mexico_City")["flag"] is flagged


def test_cool_off_unknown_timezone_is_not_checked_not_assumed():
    out = g.cool_off("2026-09-21T03:00:00+00:00", None)
    assert out["flag"] is False and "timezone (late-night check)" in out["not_checked"]
    with pytest.raises(ValueError, match="IANA"):
        g.cool_off("2026-09-21T03:00:00+00:00", "Mars/Olympus")


# ------------------------------------------------------------------ panic circuit breaker


def _goals():
    return [{"id": "retiro", "name": "Retiro", "target_amount": 9000000, "currency": "MXN", "target_date": "2053-01-01"},
            {"id": "auto", "name": "Coche", "target_amount": 250000, "currency": "MXN", "target_date": "2027-06-01"}]


@pytest.mark.parametrize("drawdown, share, action, triggered", [
    (0.10, None, "sell_all", False), (0.1001, None, "sell_all", True), (0.2, 0.49, "sell", False),
    (0.2, 0.5, "sell", True)])
def test_panic_trigger_boundaries(drawdown, share, action, triggered):
    request = {"action": action, "drawdown": drawdown, **({"share": share} if share is not None else {})}
    out = g.panic_check(request, _sit(goals=_goals()))
    assert out["result"]["triggered"] is triggered
    assert out["result"]["blocks_choice"] is False and out["result"]["the_person_decides"] is True


def test_panic_separates_near_goals_and_computes_the_paper_loss():
    res = g.panic_check({"action": "sell_all", "drawdown": 0.2, "portfolio_value": 800000},
                        _sit(goals=_goals()))["result"]
    assert res["at_risk"]["paper_loss"] == 200000
    assert res["at_risk"]["near_goals"] == ["Coche"]
    assert res["at_risk"]["reserve_months"] == 6


def test_panic_history_matches_market_stress_windows():
    from wealth import legacy
    res = g.panic_check({"action": "sell_all", "drawdown": 0.3}, _sit())["result"]
    windows = {(w["name"], w["start"], w["end"]) for w in res["history"]["stress_request"]["inputs"]["scenarios"]}
    assert windows <= {(n, s, e) for n, s, e in legacy.REGIMES}
    assert res["history"]["similar_or_deeper"] == ["2020 COVID crash", "2007-09 financial crisis", "2000-02 dot-com bust"]
    assert all(e["status"] == "needs_verification" for e in res["history"]["episodes"])


def test_panic_tax_cost_mexico_versus_us():
    holdings = [{"symbol": "CSPX", "value": 120000, "cost_basis": 100000, "acquired_on": "2020-01-01"},
                {"symbol": "NEW", "value": 50000, "cost_basis": 40000, "acquired_on": "2026-06-01"},
                {"symbol": "LOSS", "value": 5000, "cost_basis": 8000},
                {"symbol": "IRA", "value": 70000, "account_type": "ira"}]
    mx = g.panic_check({"action": "sell_all", "drawdown": 0.2, "holdings": holdings}, _sit())["result"]["tax_cost"]
    assert mx["tax_range"] == [3000, 3000] and mx["realized_loss"] == 3000
    assert mx["inside_tax_deferred_accounts"] == 70000 and "Art. 129" in mx["basis"]
    us = g.panic_check({"action": "sell_all", "drawdown": 0.2, "holdings": holdings, "jurisdiction": "US"},
                       _sit())["result"]["tax_cost"]
    assert us["gains"] == {"short": 10000, "long": 20000}
    assert us["tax_range"] == [1000, round(0.238 * 20000 + 0.408 * 10000, 2)]


def test_panic_unknown_basis_and_drawdown_stay_unknown():
    out = g.panic_check({"action": "sell_all", "holdings": [{"symbol": "X", "value": 100}]}, _sit())
    assert out["missing"] == ["drawdown (fall from the peak, e.g. 0.14)"]
    assert out["result"]["triggered"] is False
    tax = out["result"]["tax_cost"]
    assert tax["status"] == "partial" and tax["unknown"] == ["X: cost_basis"] and tax["realized_gain"] == 0
    none = g.panic_check({"action": "sell_all", "drawdown": 0.2}, _sit())["result"]["tax_cost"]
    assert none["status"] == "unknown" and "tax_range" not in none


def test_panic_reminds_of_the_precommitment():
    sit = _sit(**{"preference.risk": {"drop_reaction": "hold", "experience": "some"}})
    cool = g.panic_check({"action": "sell_all", "drawdown": 0.25}, sit)["result"]["cool_off"]
    assert cool["suggested_hours"] == 48 and "mantendrías" in cool["precommitment"]["es"]


# ------------------------------------------------------------------ scam screen


@pytest.mark.parametrize("text, code", [
    ("Guaranteed returns with no risk", "guaranteed_return"),
    ("Rendimientos garantizados, invierte ya", "guaranteed_return"),
    ("Please read me the verification code we just sent", "credentials"),
    ("Dame el código que te llegó por SMS", "credentials"),
    ("Move your savings to a safe account to protect your money", "safe_account"),
    ("Paga una comisión para retirar tus ganancias", "pay_to_withdraw"),
    ("Para liberar tu crédito necesitamos un anticipo de 3,000 pesos", "advance_fee_loan"),
    ("Tu cuenta será bloqueada hoy mismo", "urgency"),
    ("Pay with gift cards", "hard_to_trace_payment"),
    ("Le escribe el SAT: tiene un adeudo", "impersonation"),
    ("Sorry, wrong number! By the way my uncle runs a trading platform with great profit", "pig_butchering"),
    ("We can recover your lost funds for a small fee", "recovery_scam"),
])
def test_scam_patterns(text, code):
    out = g.scam_check(text)
    assert code in [r["code"] for r in out["reasons"]]
    assert out["risk"] in ("medium", "high")


def test_ordinary_words_do_not_trip_the_agency_acronyms():
    assert g.scam_check("I sat down with my accountant to review the budget")["risk"] == "low"
    assert g.scam_check("¿Cómo va el presupuesto de este mes?")["risk"] == "low"


@pytest.mark.parametrize("text, risk", [
    ("Rendimiento de 2% mensual", "low"), ("Rendimiento de 2.1% mensual", "medium"),
    ("Rendimiento de 5% mensual", "high"), ("1.5% semanal", "high"), ("0.2% daily return", "high")])
def test_promised_monthly_return_thresholds(text, risk):
    assert g.scam_check(text)["risk"] == risk


def test_two_medium_signals_make_high_risk():
    out = g.scam_check("URGENTE: el SAT te contacta")
    assert {r["code"] for r in out["reasons"]} == {"urgency", "impersonation"} and out["risk"] == "high"


def test_large_transfer_to_new_payee_uses_liquid_assets_then_fallbacks():
    sit = _sit()  # liquid 1,000,000 MXN
    at = g.scam_check({"amount": 200000, "currency": "MXN", "payee": "Nuevo", "new_payee": True}, sit)
    below = g.scam_check({"amount": 199999, "currency": "MXN", "payee": "Nuevo", "new_payee": True}, sit)
    known = g.scam_check({"amount": 900000, "currency": "MXN", "payee": "Casero", "new_payee": False}, sit)
    assert at["risk"] == "medium" and below["risk"] == "low" and known["risk"] == "low"
    typical = g.scam_check({"amount": 3000, "currency": "EUR", "payee": "X", "new_payee": True}, typical_transfer=1000)
    assert typical["risk"] == "medium"
    unknown = g.scam_check({"amount": 3000, "currency": "EUR", "payee": "X", "new_payee": True})
    assert unknown["risk"] == "low" and "unknown" in unknown["amount_note"]
    unsure = g.scam_check({"amount": 90000, "currency": "MXN", "payee": "X"})
    assert unsure["amount_note"] == "Whether the payee is new is unknown."


def test_scam_verify_places_follow_jurisdiction():
    mx = {p["name"] for p in g.scam_check("hola", jurisdiction="MX")["verify"]}
    us = {p["name"] for p in g.scam_check("hello", jurisdiction="US")["verify"]}
    both = g.scam_check("hello")["verify"]
    assert any("SIPRES" in n for n in mx) and any("CNBV" in n for n in mx) and not any("BrokerCheck" in n for n in mx)
    assert any("BrokerCheck" in n for n in us) and any("Investor.gov" in n for n in us)
    assert {p["country"] for p in both} == {"MX", "US"}
    assert g.scam_check("hola", _sit())["jurisdiction"] == "MX"


def test_scam_next_steps_and_registry_step():
    out = g.scam_check("Inversión garantizada con 10% mensual", jurisdiction="MX")
    codes = [s["code"] for s in out["next_steps"]]
    assert codes == ["pause", "call_back", "report", "registry"]
    assert "CONDUSEF" in out["next_steps"][2]["en"]


def test_scam_input_errors():
    with pytest.raises(ValueError):
        g.scam_check({})
    with pytest.raises(ValueError):
        g.scam_check(42)


# ------------------------------------------------------------------ service, catalog, no trade calls


GUARD_TASKS = ("speculation_check", "panic_check", "scam_check", "protection_review", "life_event")


def _catalog_outputs(tmp_path):
    service = WealthService(tmp_path / "w.sqlite3")
    for task in GUARD_TASKS:
        entry = CATALOG[task]
        for inputs in [entry["example"], *(entry.get("variants") or {}).values()]:
            yield task, service.run(task, inputs=inputs)


def test_no_trade_calls_in_any_output(tmp_path):
    for task, report in _catalog_outputs(tmp_path):
        for text in _texts(report):
            assert not TRADE_CALL.search(text), (task, text)
        assert report["result"].get("trade_call") is None


def test_every_sourced_parameter_is_dated_and_has_a_status():
    for key, row in g.PARAMETERS.items():
        assert row["checked_on"] == TODAY and row["status"] in {"verified", "statutory", "policy", "needs_verification"}, key
        assert row["source"]["title"], key


def test_service_uses_the_saved_picture_and_preference(tmp_path):
    service = WealthService(tmp_path / "w.sqlite3")
    service.create("ana", "Ana")
    facts = [{"key": f["key"], "value": f["value"], "source": {"kind": "user", "ref": "chat", "observed_on": TODAY}}
             for f in _facts()]
    facts.append({"key": "preference.speculation", "value": {"cap_share": 0.02},
                  "source": {"kind": "user", "ref": "chat", "observed_on": TODAY}})
    service.remember("ana", facts)
    report = service.run("speculation_check", inputs={"proposal": _buy(1000)}, client_id="ana")
    assert report["status"] == "ready" and report["result"]["verdict"] == "allow"
    assert report["result"]["limits"]["cap"] == 20000
    assert report["evidence_ids"]
    with pytest.raises(ValueError, match="unknown"):
        service.run("scam_check", inputs={"item": "x", "bogus": 1})
