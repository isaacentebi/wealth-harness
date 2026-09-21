from __future__ import annotations

from wealth.workflows import prepare


AS_OF = "2026-09-20"


def fact(key, value, *, fact_id=None, expiry="2026-10-20", confidence="reported", observed="2026-09-19"):
    return {
        "id": fact_id or f"id-{key}",
        "key": key,
        "value": value,
        "source": {"kind": "user", "ref": "test", "observed_on": observed},
        "confidence": confidence,
        "expires_on": expiry,
        "revision": 1,
    }


def snapshot(*facts):
    return {"client": {"id": "c1", "display_name": "Client", "revision": 7}, "facts": list(facts), "decisions": []}


def test_plan_reserves_debt_and_only_protected_goals_are_reserved():
    packet = prepare(
        snapshot(
            fact("constraint.liquidity", {"note": "keep liquid"}),
            fact("plan.resources", {
                "currency": "USD", "available_capital": "100000", "monthly_essentials": "5000",
                "reserve_months": 6, "reserve_outside_pool": "10000", "debt_payments_from_pool": "5000",
                "outside_sources": [{"id": "outside-1", "currency": "USD", "balance": "15000"}],
                "reserve_funding": [{"source_id": "outside-1", "amount": "10000"}],
            }),
            fact("goals", [
                {"id": "later", "name": "Later", "currency": "USD", "due": "2027-01-01", "target_amount": "80000", "funded_outside_pool": "0", "outside_funding": [], "protect_now": False},
                {"id": "safe", "name": "Safe", "currency": "USD", "due": "2028-01-01", "target_amount": "30000", "funded_outside_pool": "5000", "outside_funding": [{"source_id": "outside-1", "amount": "5000"}], "protect_now": True},
            ]),
        ),
        "plan", AS_OF,
    )
    assert packet["status"] == "ready"
    assert packet["calculations"]["reserve_required_from_pool"]["amount"] == "20000"
    later, protected = packet["calculations"]["goal_requirements"]
    assert (protected["id"], protected["required_from_pool"]["amount"]) == ("safe", "25000")
    assert (later["id"], later["required_from_pool"]["amount"]) == ("later", "80000")
    assert packet["calculations"]["protected_goals_from_pool"]["amount"] == "25000"
    assert packet["calculations"]["uncommitted_capital"]["amount"] == "50000"
    assert packet["calculations"]["funding_shortfall"]["amount"] == "0"
    assert "id-constraint.liquidity" in packet["evidence_ids"]


def test_explicit_empty_goals_is_known_and_valid():
    packet = prepare(snapshot(
        fact("plan.resources", {
            "currency": "USD", "available_capital": 10, "monthly_essentials": 1,
            "reserve_months": 2, "reserve_outside_pool": 2, "debt_payments_from_pool": 0,
            "outside_sources": [{"id": "outside-1", "currency": "USD", "balance": 2}],
            "reserve_funding": [{"source_id": "outside-1", "amount": 2}],
        }),
        fact("goals", []),
    ), "plan", AS_OF)
    assert packet["status"] == "ready"
    assert packet["calculations"]["goal_requirements"] == []
    assert packet["calculations"]["uncommitted_capital"]["amount"] == "10"


def test_plan_preserves_signed_deficit_and_does_not_earmark_nonprotected_goal():
    packet = prepare(snapshot(
        fact("plan.resources", {"currency": "USD", "available_capital": 10, "monthly_essentials": 10, "reserve_months": 2, "reserve_outside_pool": 0, "debt_payments_from_pool": 5}),
        fact("goals", [
            {"id": "idea", "name": "Optional", "currency": "USD", "due": "2027-01-01", "target_amount": 100, "funded_outside_pool": 0, "protect_now": False},
        ]),
    ), "plan", AS_OF)
    assert packet["status"] == "ready"
    assert packet["calculations"]["protected_goals_from_pool"]["amount"] == "0"
    assert packet["calculations"]["uncommitted_capital"]["amount"] == "-15"
    assert packet["calculations"]["funding_shortfall"]["amount"] == "15"
    requirement = packet["calculations"]["goal_requirements"][0]
    assert requirement["required_from_pool"]["amount"] == "100"
    assert "allocated_now" not in requirement


def test_goal_outside_funding_cannot_exceed_target():
    packet = prepare(snapshot(
        fact("plan.resources", {"currency": "USD", "available_capital": 10, "monthly_essentials": 0, "reserve_months": 0, "reserve_outside_pool": 0, "debt_payments_from_pool": 0}),
        fact("goals", [
            {"id": "g", "name": "Goal", "currency": "USD", "due": "2027-01-01", "target_amount": 10, "funded_outside_pool": 11, "protect_now": True},
        ]),
    ), "plan", AS_OF)
    assert packet["calculations"] == {}
    assert "cannot exceed" in packet["missing"][0]["detail"]


def test_outside_funding_must_reconcile_to_declared_sources_without_double_use():
    base_resources = {
        "currency": "USD", "available_capital": 100, "monthly_essentials": 10,
        "reserve_months": 1, "reserve_outside_pool": 6, "debt_payments_from_pool": 0,
        "outside_sources": [{"id": "outside-1", "currency": "USD", "balance": 10}],
        "reserve_funding": [{"source_id": "outside-1", "amount": 6}],
    }
    goal = {
        "id": "g", "name": "Goal", "currency": "USD", "due": "2027-01-01",
        "target_amount": 10, "funded_outside_pool": 5,
        "outside_funding": [{"source_id": "outside-1", "amount": 5}], "protect_now": True,
    }
    packet = prepare(snapshot(fact("plan.resources", base_resources), fact("goals", [goal])), "plan", AS_OF)
    assert packet["calculations"] == {}
    assert "exceeds balance" in packet["missing"][0]["detail"]

    base_resources["outside_sources"][0]["balance"] = 11
    base_resources["reserve_funding"][0]["amount"] = 5
    packet = prepare(snapshot(fact("plan.resources", base_resources), fact("goals", [goal])), "plan", AS_OF)
    assert packet["calculations"] == {}
    assert "sum exactly" in packet["missing"][0]["detail"]


def test_outside_funding_validates_sources_refs_currency_and_amounts():
    resources = {
        "currency": "USD", "available_capital": 100, "monthly_essentials": 0,
        "reserve_months": 0, "reserve_outside_pool": 1, "debt_payments_from_pool": 0,
        "outside_sources": [{"id": "outside-1", "currency": "MXN", "balance": 1}],
        "reserve_funding": [{"source_id": "outside-1", "amount": 1}],
    }
    packet = prepare(snapshot(fact("plan.resources", resources), fact("goals", [])), "plan", AS_OF)
    assert "currency conflicts" in packet["missing"][0]["detail"]

    resources["outside_sources"] = [{"id": "outside-1", "currency": "USD", "balance": 1}]
    resources["reserve_funding"] = [{"source_id": "undeclared", "amount": 1}]
    packet = prepare(snapshot(fact("plan.resources", resources), fact("goals", [])), "plan", AS_OF)
    assert "declared outside source" in packet["missing"][0]["detail"]

    resources["reserve_funding"] = [{"source_id": "outside-1", "amount": "NaN"}]
    packet = prepare(snapshot(fact("plan.resources", resources), fact("goals", [])), "plan", AS_OF)
    assert "nonnegative finite" in packet["missing"][0]["detail"]

    resources["outside_sources"].append({"id": "outside-1", "currency": "USD", "balance": 1})
    resources["reserve_funding"] = [{"source_id": "outside-1", "amount": 1}]
    packet = prepare(snapshot(fact("plan.resources", resources), fact("goals", [])), "plan", AS_OF)
    assert "duplicate outside source" in packet["missing"][0]["detail"]


def test_missing_and_known_none_are_distinct_and_ask_one_question():
    missing = prepare(snapshot(), "plan", AS_OF)
    known_none = prepare(snapshot(fact("plan.resources", None), fact("goals", [])), "plan", AS_OF)
    assert missing["missing"][0]["reason"] == "missing_fact"
    assert known_none["missing"][0]["reason"] == "known_none"
    assert isinstance(missing["next_question"], str)
    assert "goals" not in missing["next_question"].lower()


def test_invalid_plan_data_never_produces_material_output():
    resources = {"currency": "USD", "available_capital": True, "monthly_essentials": -1, "reserve_months": "NaN", "reserve_outside_pool": 0, "debt_payments_from_pool": 0}
    goals = [
        {"id": "x", "name": "A", "currency": "USD", "due": "bad", "target_amount": 1, "funded_outside_pool": 0, "protect_now": True},
        {"id": "x", "name": "B", "currency": "USD", "due": "2027-01-01", "target_amount": 1, "funded_outside_pool": 0, "protect_now": False},
    ]
    packet = prepare(snapshot(fact("plan.resources", resources), fact("goals", goals)), "plan", AS_OF)
    assert packet["status"] == "partial"
    assert packet["calculations"] == {}
    assert packet["missing"][0]["reason"] == "invalid"


def test_duplicate_goal_ids_and_currency_conflicts_are_rejected():
    base = {"currency": "USD", "available_capital": 10, "monthly_essentials": 1, "reserve_months": 1, "reserve_outside_pool": 0, "debt_payments_from_pool": 0}
    duplicate = [
        {"id": "x", "name": "A", "currency": "USD", "due": "2027-01-01", "target_amount": 1, "funded_outside_pool": 0, "protect_now": True},
        {"id": "x", "name": "B", "currency": "USD", "due": "2028-01-01", "target_amount": 1, "funded_outside_pool": 0, "protect_now": False},
    ]
    packet = prepare(snapshot(fact("plan.resources", base), fact("goals", duplicate)), "plan", AS_OF)
    assert "duplicate goal id" in packet["missing"][0]["detail"]
    duplicate[1]["id"] = "y"
    duplicate[1]["currency"] = "MXN"
    packet = prepare(snapshot(fact("plan.resources", base), fact("goals", duplicate)), "plan", AS_OF)
    assert "conflicts" in packet["missing"][0]["detail"]


def test_currency_is_uppercase_iso_shaped_and_must_match_reporting_currency():
    plan = {"currency": "usd", "available_capital": 10, "monthly_essentials": 0, "reserve_months": 0, "reserve_outside_pool": 0, "debt_payments_from_pool": 0}
    packet = prepare(snapshot(fact("plan.resources", plan), fact("goals", [])), "plan", AS_OF)
    assert packet["calculations"] == {}
    assert "uppercase" in packet["missing"][0]["detail"]

    plan["currency"] = "USD"
    packet = prepare(snapshot(
        fact("client.profile", {"reporting_currency": "MXN"}, expiry=None),
        fact("plan.resources", plan), fact("goals", []),
    ), "plan", AS_OF)
    assert packet["calculations"] == {}
    assert "implicit FX" in packet["missing"][0]["detail"]


def test_exposure_aggregates_symbols_across_accounts_without_normalizing_unknown_wealth():
    portfolio = {
        "currency": "USD", "scope": "named brokerage accounts", "complete": False,
        "positions": [
            {"account_id": "a", "symbol": "VOO", "value": "40", "asset_class": "equity"},
            {"account_id": "b", "symbol": "VOO", "value": "20", "asset_class": "equity"},
            {"account_id": "b", "symbol": "CASH", "value": "40"},
        ],
    }
    packet = prepare(snapshot(fact("portfolio.snapshot", portfolio)), "exposure", AS_OF)
    assert packet["status"] == "partial"
    assert packet["calculations"]["scope"] == "named brokerage accounts"
    assert packet["calculations"]["symbol_allocation"][0] == {"symbol": "VOO", "value": "60", "weight": "0.6"}
    assert packet["calculations"]["asset_class_coverage"]["classified_weight"] == "0.6"
    assert packet["calculations"]["concentration"]["herfindahl_index"] == "0.52"
    assert packet["calculations"]["concentration"]["effective_symbol_count"].startswith("1.923")


def test_exposure_rejects_nonfinite_bool_negative_and_zero_total():
    for bad in (True, -1, "NaN", "Infinity"):
        portfolio = {"currency": "USD", "scope": "a", "complete": True, "positions": [{"account_id": "a", "symbol": "X", "value": bad}]}
        packet = prepare(snapshot(fact("portfolio.snapshot", portfolio)), "exposure", AS_OF)
        assert packet["calculations"] == {}
        assert packet["missing"][0]["reason"] == "invalid"
    zero = {"currency": "USD", "scope": "a", "complete": True, "positions": []}
    packet = prepare(snapshot(fact("portfolio.snapshot", zero)), "exposure", AS_OF)
    assert packet["calculations"] == {}
    assert packet["missing"][0]["key"] == "portfolio.snapshot.total_value"


def test_income_gap_includes_need_and_committed_outflow_and_reports_calendar_holes():
    schedule = {"currency": "USD", "monthly_need": "1000", "months": [
        {"month": "2026-10", "expected_cash_received": "900", "committed_outflow": "200"},
        {"month": "2026-12", "expected_cash_received": "1400", "committed_outflow": "100"},
    ]}
    packet = prepare(snapshot(fact("income.schedule", schedule)), "income", AS_OF)
    assert packet["status"] == "partial"
    assert packet["calculations"]["months"][0]["gap"]["amount"] == "300"
    assert packet["calculations"]["months"][1]["surplus"]["amount"] == "300"
    assert packet["calculations"]["scheduled_months_total_gap"]["amount"] == "300"
    assert packet["calculations"]["scheduled_months_total_surplus"]["amount"] == "300"
    assert packet["calculations"]["calendar_coverage"]["scheduled_month_count"] == 2
    assert packet["calculations"]["calendar_coverage"]["missing_months_between"] == ["2026-11"]


def test_income_duplicate_or_invalid_month_withholds_calculation():
    for months in (
        [{"month": "2026-13", "expected_cash_received": 1, "committed_outflow": 0}],
        [{"month": "2026-10", "expected_cash_received": 1, "committed_outflow": 0}, {"month": "2026-10", "expected_cash_received": 1, "committed_outflow": 0}],
    ):
        packet = prepare(snapshot(fact("income.schedule", {"currency": "USD", "monthly_need": 1, "months": months})), "income", AS_OF)
        assert packet["calculations"] == {}
        assert packet["missing"][0]["reason"] == "invalid"


def test_income_rejects_old_gross_field_and_empty_schedule_has_no_totals():
    old = {"currency": "USD", "monthly_need": 1, "months": [
        {"month": "2026-10", "expected_income": 2, "committed_outflow": 0},
    ]}
    packet = prepare(snapshot(fact("income.schedule", old)), "income", AS_OF)
    assert packet["calculations"] == {}
    assert "unsupported" in packet["missing"][0]["detail"]

    empty = {"currency": "USD", "monthly_need": 1, "months": []}
    packet = prepare(snapshot(fact("income.schedule", empty)), "income", AS_OF)
    assert packet["calculations"] == {}
    assert packet["missing"][0]["key"] == "income.schedule.months"


def test_stale_future_inferred_and_timeless_facts_cannot_drive_arithmetic():
    portfolio = {"currency": "USD", "scope": "a", "complete": True, "positions": [{"account_id": "a", "symbol": "X", "value": 1}]}
    cases = [
        fact("portfolio.snapshot", portfolio, expiry="2026-09-19"),
        fact("portfolio.snapshot", portfolio, observed="2026-09-21"),
        fact("portfolio.snapshot", portfolio, confidence="inferred"),
        fact("portfolio.snapshot", portfolio, expiry=None),
    ]
    for item in cases:
        packet = prepare(snapshot(item), "exposure", AS_OF)
        assert packet["calculations"] == {}
        assert packet["status"] in {"needs_input", "partial"}


def test_as_of_is_a_selection_seam_not_historical_reconstruction():
    profile = fact("client.profile", {"reporting_currency": "USD"}, observed="2026-09-21")
    packet = prepare(snapshot(profile), "overview", AS_OF)
    assert packet["facts"] == []
    assert any("after as_of" in warning for warning in packet["warnings"])
    assert packet["capability"]["historical_reconstruction"] is False


def test_overview_includes_compact_decisions_and_needs_review_state():
    state = snapshot(fact("client.profile", {"reporting_currency": "USD"}))
    state["decisions"] = [{
        "id": "d1", "title": "Reserve cash", "rationale": "Known goal",
        "revision": 6, "evidence_ids": ["id-client.profile"], "alternatives": ["invest"],
        "status": "proposed", "needs_review": True, "created_at": "ignored", "updated_at": "ignored",
    }]
    packet = prepare(state, "overview", AS_OF)
    assert packet["decisions"] == [{
        "id": "d1", "title": "Reserve cash", "rationale": "Known goal",
        "revision": 6, "evidence_ids": ["id-client.profile"],
        "status": "proposed", "needs_review": True,
    }]


def test_every_personal_intent_carries_goals_constraints_and_decision_review_state():
    state = snapshot(
        fact("goals", []),
        fact("constraint.liquidity", {"minimum": "high"}),
    )
    state["decisions"] = [{"id": "d1", "title": "Old", "status": "proposed", "needs_review": True}]
    for intent in ("overview", "plan", "exposure", "income", "research", "tax"):
        packet = prepare(state, intent, AS_OF)
        assert "goals" in [item["key"] for item in packet["facts"]]
        assert "constraint.liquidity" in [item["key"] for item in packet["facts"]]
        assert packet["decisions"][0]["needs_review"] is True


def test_research_requires_host_and_never_manufactures_external_claims():
    packet = prepare(snapshot(fact("thesis.ai", {"claim": "demand grows"}), fact("research.memo", None)), "research", AS_OF)
    assert packet["status"] == "research_required"
    assert packet["capability"]["research"] == "host_required"
    assert set(packet["calculations"]) == {"evidence_checklist"}


def test_tax_is_unsupported_and_requests_jurisdiction_accounts_and_lots():
    packet = prepare(snapshot(fact("tax.jurisdiction", None)), "tax", AS_OF)
    assert packet["status"] == "unsupported"
    assert packet["calculations"] == {}
    assert [item["key"] for item in packet["missing"]] == ["tax.jurisdiction", "account.*", "lot.*"]
    assert packet["capability"]["tax_computation"] == "unsupported"


def test_invalid_intent_and_snapshot_return_structured_packets():
    packet = prepare(None, "bogus", AS_OF)
    assert packet["status"] == "unsupported"
    assert packet["missing"][0]["key"] == "intent"
    packet = prepare(None, "overview", AS_OF)
    assert packet["status"] == "needs_input"
    assert packet["missing"][0]["key"] == "snapshot"
