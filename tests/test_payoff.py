"""Writing options is opening risk, and the payoff sandbox for options, crypto and leveraged positions."""
from __future__ import annotations

import pytest

from wealth import guardrails as g
from wealth import views
from wealth.catalog import CATALOG
from wealth.service import WealthService

from test_guardrails import TRADE_CALL, _sit, _texts


def _write_puts(**extra):
    return {"action": "sell", "instrument": "options", "symbol": "AAPL", "option_type": "put", "covered": False,
            "strike": 180, "contracts": 2, "premium": 3.5, "currency": "MXN", "sleeve": {"value": 0, "peak": 0},
            **extra}


# ------------------------------------------------------------------ the sell-to-open bug


def test_writing_uncovered_puts_is_sized_not_waved_through():
    out = g.speculation_check(_write_puts(), _sit())
    assert out["verdict"] == "decline_to_recommend"
    assert out["position_effect"] == "open"
    assert "reduce_risk" not in [r["rule"] for r in out["rules"]]
    # strike x 100 x contracts less the premium received: 180 x 100 x 2 - 3.5 x 100 x 2
    assert out["limits"]["worst_case_loss"] == 35300
    assert out["limits"]["sleeve_after"] == 35300  # the capital at risk counts against the cap, not the premium
    assert [r["code"] for r in out["reasons"]] == ["position_loss"]
    assert out["payoff"]["strategy"] == "naked_short_put" and out["payoff"]["breakevens"] == [176.5]


def test_a_small_uncovered_put_fits_the_limits_and_a_bigger_one_does_not():
    small = g.speculation_check(_write_puts(strike=20, contracts=1, premium=0.5), _sit())
    assert small["verdict"] == "allow" and small["limits"]["worst_case_loss"] == 1950
    assert small["payoff"]["sizing"]["share_of_speculation_budget"] == pytest.approx(1950 / 50000)
    over = g.speculation_check(_write_puts(strike=101, contracts=1, premium=0), _sit())  # 10,100 > 1% of 1,000,000
    assert over["verdict"] == "decline_to_recommend"


def test_writing_uncovered_calls_has_no_ceiling():
    out = g.speculation_check(_write_puts(option_type="call"), _sit())
    assert out["verdict"] == "decline_to_recommend"
    assert out["limits"]["worst_case_loss"] is None
    assert out["payoff"]["max_loss_unbounded"] is True and out["payoff"]["capital_at_risk"] is None
    assert out["payoff"]["sizing"]["share_of_net_worth"] is None
    assert "no ceiling" in out["payoff"]["sizing"]["share_of_net_worth_reason"]


def test_a_naked_put_is_leverage_for_the_ips_but_a_cash_secured_put_is_not():
    ips = {"constraints": {"leverage": {"allowed": False}}}
    small = dict(strike=20, contracts=1, premium=0.5)
    naked = g.speculation_check(_write_puts(**small), _sit(), ips)
    assert any(r["code"] == "ips_leverage" for r in naked["reasons"])
    secured = g.speculation_check(_write_puts(covered=True, **small), _sit(), ips)
    assert secured["verdict"] == "allow" and secured["payoff"]["strategy"] == "cash_secured_put"
    assert secured["limits"]["worst_case_loss"] == 1950  # cash-secured is still strike x 100 less the premium


def test_a_covered_call_adds_no_new_downside():
    out = g.speculation_check(_write_puts(option_type="call", covered=True), _sit())
    assert out["verdict"] == "allow" and out["limits"]["worst_case_loss"] == 0


def test_closing_reduces_risk_and_an_unlabelled_option_sell_is_read_as_a_write():
    close = g.speculation_check({"action": "sell", "instrument": "options", "position_effect": "close",
                                 "amount": 500}, _sit())
    assert close["verdict"] == "allow" and close["reasons"][0]["code"] == "reduce_risk"
    buy_back = g.speculation_check({"action": "buy", "instrument": "put", "position_effect": "close",
                                    "amount": 500}, _sit())
    assert buy_back["verdict"] == "allow"
    unlabelled = g.speculation_check({**_write_puts(strike=20, contracts=1, premium=0.5), "covered": None}, _sit())
    assert unlabelled["verdict"] == "allow_with_warning"
    assert unlabelled["reasons"][0]["code"] == "position_effect" and unlabelled["reasons"][0]["es"]
    assert any("position_effect" in m for m in unlabelled["missing"])
    with pytest.raises(ValueError, match="position_effect"):
        g.speculation_check(_write_puts(position_effect="sideways"), _sit())


def test_instrument_put_means_a_put():
    out = g.speculation_check({"action": "sell", "instrument": "put", "covered": False, "strike": 20,
                               "contracts": 1, "premium": 0.5, "sleeve": {"value": 0, "peak": 0}}, _sit())
    assert out["limits"]["worst_case_loss"] == 1950


def test_foreign_currency_needs_a_rate_and_converts_with_one():
    usd = _write_puts(strike=20, contracts=1, premium=0.5, currency="USD")
    unknown = g.speculation_check(usd, _sit())
    assert unknown["verdict"] == "allow_with_warning"
    assert any(r["code"] == "currency" for r in unknown["reasons"])
    assert unknown["payoff"]["sizing"]["capital_at_risk"] is None
    converted = g.speculation_check({**usd, "fx_rate": 18}, _sit())
    assert converted["limits"]["worst_case_loss"] == 1950 * 18 and converted["limits"]["currency"] == "MXN"
    assert converted["verdict"] == "decline_to_recommend"  # 35,100 MXN is above the 10,000 MXN position limit
    assert converted["payoff"]["max_loss"] == 1950  # the payoff itself stays in the option's currency


def test_sizing_shares_say_why_when_the_budget_is_zero():
    sit = _sit(**{"cash.nu": {"amount": 149999, "currency": "MXN", "purpose": "reserve"}})
    out = g.speculation_check(_write_puts(strike=20, contracts=1, premium=0.5), sit)
    sizing = out["payoff"]["sizing"]
    assert sizing["share_of_speculation_budget"] is None and "budget is 0" in sizing["share_of_speculation_budget_reason"]
    assert sizing["share_of_net_worth"] == pytest.approx(1950 / sit["net_worth"]["total"], abs=1e-6)


# ------------------------------------------------------------------ the payoff engine


def _call(side, strike, premium, contracts=1):
    return {"type": "call", "side": side, "strike": strike, "premium": premium, "contracts": contracts}


def _put(side, strike, premium, contracts=1, **extra):
    return {"type": "put", "side": side, "strike": strike, "premium": premium, "contracts": contracts, **extra}


def test_long_call_and_long_put():
    call = g.payoff([_call("long", 100, 5)])
    assert (call["max_loss"], call["max_gain"], call["max_gain_unbounded"]) == (500, None, True)
    assert call["breakevens"] == [105] and call["strategy"] == "long_call"
    put = g.payoff([_put("long", 100, 5)])
    assert (put["max_loss"], put["max_gain"], put["breakevens"]) == (500, 9500, [95])


def test_verticals_have_capped_loss_and_gain():
    bull = g.payoff([_call("long", 650, 12), _call("short", 670, 5)], spot=650)
    assert bull["strategy"] == "bull_call_spread"
    assert (bull["max_loss"], bull["max_gain"], bull["breakevens"], bull["net_premium"]) == (700, 1300, [657], -700)
    bear_put = g.payoff([_put("long", 100, 6), _put("short", 90, 2)])
    assert bear_put["strategy"] == "bear_put_spread" and (bear_put["max_loss"], bear_put["max_gain"]) == (400, 600)
    bull_put = g.payoff([_put("short", 100, 6), _put("long", 90, 2)])
    assert bull_put["strategy"] == "bull_put_spread" and (bull_put["max_loss"], bull_put["max_gain"]) == (600, 400)
    bear_call = g.payoff([_call("short", 100, 6), _call("long", 110, 2)])
    assert bear_call["strategy"] == "bear_call_spread" and bear_call["breakevens"] == [104]


def test_covered_call_and_cash_secured_put():
    covered = g.payoff([{"type": "stock", "side": "long", "entry_price": 100, "quantity": 100}, _call("short", 110, 3)])
    assert covered["strategy"] == "covered_call"
    assert (covered["max_loss"], covered["max_gain"], covered["breakevens"]) == (9700, 1300, [97])
    csp = g.payoff([_put("short", 50, 2, cash_secured=True)])
    assert csp["strategy"] == "cash_secured_put" and csp["capital_at_risk"] == 4800 and csp["max_gain"] == 200


def test_straddle_has_two_breakevens():
    out = g.payoff([_call("long", 100, 4), _put("long", 100, 3)])
    assert out["strategy"] == "long_straddle" and out["breakevens"] == [93, 107] and out["max_loss"] == 700


def test_leveraged_crypto_with_and_without_negative_balance_protection():
    protected = g.payoff([{"type": "leveraged", "side": "long", "entry_price": 60000, "margin": 2000, "leverage": 5,
                           "negative_balance_protection": True}])
    assert protected["legs"][0]["liquidation_price"] == 48000
    assert protected["max_loss"] == 2000 and protected["loss_if_liquidated"] is None
    assert protected["max_gain_unbounded"] and protected["breakevens"] == [60000]
    exposed = g.payoff([{"type": "leveraged", "side": "long", "entry_price": 60000, "margin": 2000, "leverage": 5}])
    assert exposed["max_loss"] == 10000 and exposed["loss_if_liquidated"] == 2000
    assert any("gap" in a for a in exposed["assumptions"])
    short = g.payoff([{"type": "leveraged", "side": "short", "entry_price": 60000, "margin": 2000, "leverage": 5}])
    assert short["max_loss_unbounded"] is True and short["max_gain"] == 10000
    safe_short = g.payoff([{"type": "leveraged", "side": "short", "entry_price": 60000, "margin": 2000,
                            "leverage": 5, "negative_balance_protection": True}])
    assert safe_short["max_loss"] == 2000 and not safe_short["max_loss_unbounded"]
    spot = g.payoff([{"type": "crypto", "side": "long", "entry_price": 60000, "amount": 3000}])
    assert spot["strategy"] == "crypto_long" and spot["max_loss"] == 3000


def test_grid_is_the_payoff_at_each_price_and_input_errors_are_loud():
    out = g.payoff([_call("long", 100, 5)], grid=[80, 100, 120])
    assert out["grid"] == [{"price": 80, "pnl": -500}, {"price": 100, "pnl": -500}, {"price": 120, "pnl": 1500}]
    default = g.payoff([_call("long", 100, 5)], spot=100)
    prices = [row["price"] for row in default["grid"]]
    assert prices == sorted(prices) and 100 in prices and 105 in prices and min(prices) <= 50
    for bad in ([], [{"type": "call", "side": "long", "strike": 100, "contracts": 1}],
                [{"type": "swap", "side": "long"}], [_call("sideways", 100, 1)]):
        with pytest.raises(ValueError):
            g.payoff(bad)


def test_every_strategy_explains_itself_in_both_languages_without_a_trade_call():
    cases = [[_call("long", 100, 5)], [_put("short", 100, 5)], [_call("long", 100, 5), _call("short", 110, 2)],
             [{"type": "leveraged", "side": "short", "entry_price": 10, "margin": 5, "leverage": 3}]]
    for legs in cases:
        out = g.payoff(legs)
        assert out["explanation"]["en"] and out["explanation"]["es"]
        assert not [t for t in _texts(out) if TRADE_CALL.search(t)]


def test_explain_with_legs_returns_the_payoff_without_a_verdict():
    out = g.speculation_check({"action": "explain", "instrument": "options", "currency": "MXN", "spot": 650,
                               "legs": [_call("long", 650, 12), _call("short", 670, 5)]}, _sit())
    assert out["verdict"] == "allow" and out["reasons"] == []
    assert out["payoff"]["sizing"]["capital_at_risk"] == 700
    assert out["payoff"]["sizing"]["share_of_net_worth"] == pytest.approx(0.0007)


# ------------------------------------------------------------------ through the service, with views


@pytest.mark.parametrize("variant", ["write_uncovered_puts", "vertical_spread_payoff", "leveraged_crypto_payoff"])
def test_catalog_payoff_variants_draw_the_pnl_chart_and_the_limits(tmp_path, variant):
    report = WealthService(tmp_path / "w.sqlite3").run(
        "speculation_check", inputs=CATALOG["speculation_check"]["variants"][variant])
    assert report["status"] in {"ready", "partial"}
    assert report["result"]["payoff"]["grid"]
    # The payoff draws as a P&L chart over the price (x = price, y = P&L at expiry), then the limits ticket.
    assert [v["kind"] for v in report["views"]] == ["pnl", "ticket"]
    specs = views.views_for("speculation_check", report)
    for spec in specs:
        views.validate(spec)
        assert views.render_svg(spec, "es").startswith("<svg")
    pay = report["result"]["payoff"]
    envelope_numbers = {row["pnl"] for row in pay["grid"]}
    chart = specs[0]["data"]
    assert {p["y"] for p in chart["points"]} <= envelope_numbers and {p["x"] for p in chart["points"]} <= {row["price"] for row in pay["grid"]}
    assert chart["spot"] == pay["spot"] and chart["breakevens"] == pay["breakevens"][:4]
    assert chart["loss"] == {"v": pay["max_loss"], "unbounded": pay["max_loss_unbounded"]}
    assert chart["gain"] == {"v": pay["max_gain"], "unbounded": pay["max_gain_unbounded"]}
    assert any("expiry" in a or "leveraged" in a for a in report["assumptions"])


def test_a_credit_spread_bought_by_amount_is_still_sized_by_what_it_can_lose():
    # A put credit spread: 10 x (short 180 / long 120). The premium in is small; the loss is up to 60 a share.
    legs = [_put("short", 180, 5, 10), _put("long", 120, 1, 10)]
    out = g.speculation_check({"action": "buy", "side": "long", "instrument": "options", "currency": "MXN",
                               "spot": 185, "amount": 4000, "legs": legs,
                               "sleeve": {"value": 0}}, _sit())
    worst = out["payoff"]["max_loss"]
    assert worst > 4000
    assert out["limits"]["sleeve_after"] == pytest.approx(worst)
