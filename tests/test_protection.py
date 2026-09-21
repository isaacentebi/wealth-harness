"""Protection: life and disability needs, health cover, estate checklist and the life-event router."""
from __future__ import annotations

import pytest

from wealth import protection as p
from wealth.catalog import CATALOG
from wealth.service import WealthService
from wealth.situation import build

TODAY = "2026-09-21"


def _sit(country="MX", today=TODAY, **overrides):
    currency = "MXN" if country == "MX" else "USD"
    rows = {
        "client.profile": {"residence": {"country": country}, "birth_year": 1985, "dependents": 2,
                           "dependent_ages": [2, 6]},
        "income.salary": {"amount": 50000, "currency": currency, "frequency": "monthly", "net": False},
        "spending.monthly": {"essential": 20000, "total": 30000, "currency": currency},
        "cash.bank": {"amount": 120000, "currency": currency, "purpose": "reserve"},
        "reserve": {"target_months": 6},
        "investment.brk": {"amount": 280000, "currency": currency, "institution": "Broker"},
        "liability.car": {"kind": "auto", "balance": 100000, "currency": currency, "annual_rate": 0.12,
                          "payment": 5000, "payment_frequency": "monthly"},
        "goals": [{"id": "uni", "name": "Universidad", "action": "education", "target_amount": 600000,
                   "currency": currency, "target_date": "2040-08-01"}],
    }
    rows.update(overrides)
    snapshot = {"client": {"id": None, "revision": None}, "facts": [
        {"id": f"f:{k}", "key": k, "value": v, "confidence": "reported", "status": "active",
         "source": {"kind": "user", "ref": "test", "observed_on": today}} for k, v in rows.items() if v is not None],
        "decisions": []}
    return build(snapshot, None, today)


# ------------------------------------------------------------------ life


def test_life_need_is_needs_minus_resources_as_a_range():
    out = p.life_need(_sit(), policies=[{"kind": "life", "cover_amount": 500000, "currency": "MXN"}])
    c = out["components"]
    assert out["applies"] is True and out["complete"] is True
    assert c["years"] == [20, 23] and c["replacement_ratio"] == [0.6, 0.8]
    assert c["income_replacement"] == [600000 * 0.6 * 20, 600000 * 0.8 * 23]
    assert c["debts"] == 100000 and c["education"] == 600000 and c["liquid_assets"] == 400000
    low = 600000 * 0.6 * 20 + 100000 + 600000 - 400000 - 500000
    high = 600000 * 0.8 * 23 + 100000 + 600000 - 400000 - 500000
    assert out["need_range"] == [low, high] and out["need_range"][0] < out["need_range"][1]


def test_life_need_only_with_dependants_and_unknown_is_not_zero():
    none = p.life_need(_sit(**{"client.profile": {"residence": {"country": "MX"}, "dependents": 0}}))
    assert none["applies"] is False
    unknown = p.life_need(_sit(**{"client.profile": {"residence": {"country": "MX"}}}))
    assert unknown["applies"] is None and unknown["missing"] == ["client.profile.dependents"]


def test_life_need_without_known_cover_shows_only_the_gross_need():
    out = p.life_need(_sit())  # policies omitted: existing cover unknown
    assert out["need_range"] is None and out["gross_need_range"]
    assert "policies (existing life cover)" in out["missing"] and out["complete"] is False
    none = p.life_need(_sit(), policies=[])  # stated: no policies
    assert none["components"]["existing_cover"] == 0 and none["need_range"]


def test_life_need_unknown_ages_and_overrides():
    sit = _sit(**{"client.profile": {"residence": {"country": "MX"}, "dependents": 1}})
    out = p.life_need(sit, policies=[])
    assert out["components"]["years"] == [10, 20] and "client.profile.dependent_ages" in out["missing"]
    fixed = p.life_need(sit, policies=[], life={"years": 15, "replacement_ratio": [0.7, 0.7], "final_expenses": 80000})
    assert fixed["components"]["income_replacement"] == [600000 * 0.7 * 15] * 2
    assert fixed["components"]["final_expenses"] == 80000
    with pytest.raises(ValueError):
        p.life_need(sit, life={"years": [20, 10]})


# ------------------------------------------------------------------ disability


def test_disability_gap_uses_the_sourced_40_to_65_percent_range():
    out = p.disability_gap(_sit(), policies=[{"kind": "disability", "monthly_benefit": 20000}])
    assert out["typical_benefit_range"] == [20000, 32500]
    assert out["gap_range"] == [0, 12500] and out["essential_covered_by_existing"] is True
    assert out["sources"][0]["status"] == "verified"


def test_disability_existing_benefit_unknown_is_not_zero():
    out = p.disability_gap(_sit())
    assert out["existing_monthly_benefit"] is None and out["gap_range"] is None and out["status"] == "partial"
    stated = p.disability_gap(_sit(), disability={"other_monthly_benefit": 0})
    assert stated["gap_range"] == [20000, 32500]


# ------------------------------------------------------------------ health


def test_mexico_gmm_policy_against_the_reserve():
    policies = [{"kind": "gmm", "deductible": 30000, "coinsurance": 0.1, "coinsurance_cap": 100000}]
    out = p.health_review(_sit(), policies)
    row = out["policies"][0]
    assert out["jurisdiction"] == "MX" and row["worst_case_out_of_pocket"] == 130000
    assert row["reserve_covers_deductible"] is True and row["reserve_covers_worst_case"] is False
    uncapped = p.health_review(_sit(), [{"kind": "gmm", "deductible": 30000, "coinsurance": 0.1}])
    assert "no ceiling" in uncapped["policies"][0]["coinsurance_note"]
    assert p.health_review(_sit(), [])["status"] == "gap"
    assert p.health_review(_sit(), None)["status"] == "unknown"


@pytest.mark.parametrize("deductible, eligible", [(1700, True), (1699, False)])
def test_us_hsa_eligibility_boundary(deductible, eligible):
    plan = {"kind": "health", "coverage": "self", "hdhp": True, "deductible": deductible, "out_of_pocket_max": 7000}
    out = p.health_review(_sit("US"), [plan])
    hsa = out["plans"][0]["hsa"]
    assert out["jurisdiction"] == "US" and hsa["eligible"] is eligible
    assert hsa["annual_limit"] == (4400 if eligible else None)


def test_us_hsa_catch_up_at_55():
    sit = _sit("US", **{"client.profile": {"residence": {"country": "US"}, "birth_year": 1971, "dependents": 0}})
    plan = {"kind": "health", "coverage": "family", "hdhp": True, "deductible": 3400, "out_of_pocket_max": 9000}
    assert p.health_review(sit, [plan])["plans"][0]["hsa"]["annual_limit"] == 8750 + 1000


def test_health_needs_a_jurisdiction():
    sit = _sit(**{"client.profile": {"dependents": 0}})
    assert p.health_review(sit, [])["missing"] == ["client.profile.residence"]


# ------------------------------------------------------------------ estate


def _items(out):
    return {i["item"]: i for i in out["items"]}


def test_mexico_estate_checklist_with_mes_del_testamento_in_september():
    estate = {"will": "no", "beneficiaries": {"afore": "yes"}, "married": True, "marital_regime": "separacion_de_bienes"}
    out = p.estate_checklist(_sit(), estate)
    items = _items(out)
    assert items["testamento"]["status"] == "missing" and items["testamento"]["refer"] == "notario"
    assert "Mes del Testamento now" in items["testamento"]["en"]
    assert items["beneficiarios_afore"]["status"] == "done"
    assert items["beneficiarios_seguros"]["status"] == "unknown"
    assert "separación de bienes" in items["regimen_matrimonial"]["en"]
    assert out["items"][0]["status"] == "missing"  # missing first
    march = p.estate_checklist(_sit(today="2026-03-10"), estate)
    assert "September 2026" in _items(march)["testamento"]["en"]


def test_old_will_needs_review_and_unknown_marriage_is_asked():
    out = p.estate_checklist(_sit(), {"will": "yes", "will_date": "2019-01-01"})
    items = _items(out)
    assert items["testamento"]["status"] == "review" and items["regimen_matrimonial"]["status"] == "unknown"


def test_us_estate_checklist_differs_from_mexico():
    out = _items(p.estate_checklist(_sit("US"), {"will": "yes", "will_date": "2025-01-01"}))
    assert {"will", "beneficiaries_retirement", "transfer_on_death", "healthcare_proxy", "revocable_trust"} <= set(out)
    assert "testamento" not in out and out["will"]["status"] == "done"


def test_us_situs_exposure_runs_through_estate():
    request = {"year": 2026, "decedent": {"us_citizen": False, "green_card": False, "us_domiciled": False},
               "assets": [{"id": "voo", "type": "us_domiciled_fund", "value_usd": 200000}]}
    out = p.estate_checklist(_sit(), {"us_situs": request})
    assert out["us_situs"]["status"] in ("ready", "partial") and out["us_situs"]["exposure"]
    assert _items(out)["us_situs_estate"]["refer"] == "estate attorney"


# ------------------------------------------------------------------ review and life events


def test_protection_review_lists_gaps_and_missing():
    out = p.protection_review(_sit(), {"policies": [], "estate": {"will": "no"}})
    assert set(out["gaps"]) >= {"life", "health", "estate"}
    assert out["trade_call"] is None


@pytest.mark.parametrize("kind", p.EVENTS)
def test_every_life_event_is_ordered_bilingual_and_routes_to_real_tasks(kind):
    tasks = set(CATALOG)
    for country in ("MX", "US", None):
        out = p.life_event(kind, "2026-09-21", {"jurisdiction": country} if country else None)
        assert [s["order"] for s in out["steps"]] == list(range(1, len(out["steps"]) + 1))
        for step in out["steps"]:
            assert step["en"] and step["es"] and step["en"] != step["es"]
            assert set(step["tasks"]) <= tasks, step["tasks"]
            if country:
                assert step["jurisdiction"] in ("all", country)
        assert out["nudges"] and all(n["date"] > "2026-09-21" and n["es"] for n in out["nudges"])


def test_life_event_jurisdiction_differences():
    mx = p.life_event("job_change", "2026-09-21", {"jurisdiction": "MX"})
    us = p.life_event("job_change", "2026-09-21", {"jurisdiction": "US"})
    both = p.life_event("job_change", "2026-09-21")
    assert any("AFORE" in s["en"] for s in mx["steps"]) and not any("401(k)" in s["en"] for s in mx["steps"])
    assert any("401(k)" in s["en"] for s in us["steps"]) and not any("AFORE" in s["en"] for s in us["steps"])
    assert len(both["steps"]) > max(len(mx["steps"]), len(us["steps"])) and both["jurisdiction_note"]


def test_life_event_aliases_nudge_dates_and_errors():
    out = p.life_event("birth", "2026-09-21", {"jurisdiction": "MX"})
    assert out["kind"] == "birth_or_adoption" and out["nudges"][0]["date"] == "2026-10-05"
    assert "client.profile" in out["facts_to_update"] and "protection_review" in out["tasks_to_run"]
    with pytest.raises(ValueError, match="kind"):
        p.life_event("lottery_win")
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        p.life_event("marriage", "next week")


def test_service_runs_life_event_from_the_saved_residence(tmp_path):
    service = WealthService(tmp_path / "w.sqlite3")
    service.create("luis", "Luis")
    service.remember("luis", [{"key": "client.profile", "value": {"residence": {"country": "US"}},
                               "source": {"kind": "user", "ref": "chat", "observed_on": TODAY}}])
    report = service.run("life_event", inputs={"kind": "layoff", "date": TODAY}, client_id="luis")
    assert report["result"]["jurisdiction"] == "US"
    assert any("COBRA" in s["en"] for s in report["result"]["steps"])
    assert report["evidence_ids"]


def test_life_need_range_is_ordered_and_inputs_are_validated():
    sit = {"currency": "USD", "profile": {"dependents": 2, "dependent_ages": [5, 9]}, "income": {"monthly": -1000},
           "liabilities": [{"name": "mortgage", "value": 200000}], "net_worth": {"liquid": 50000}, "goals": []}
    out = p.life_need(sit, [], {})
    low, high = out["need_range"]
    assert low <= high and out["gross_need_range"][0] <= out["gross_need_range"][1]
    assert any("negative" in a for a in out["assumptions"])
    assert p.life_need({**sit, "profile": {"dependents": "0"}}, [], {})["applies"] is False
    assert p.life_need({**sit, "profile": {"dependents": "2"}}, [], {})["applies"] is True
    negative_liquid = p.life_need({**sit, "income": {"monthly": 10000}, "net_worth": {"liquid": -80000}}, [], {})
    assert negative_liquid["need_range"] == negative_liquid["gross_need_range"]  # debt is not a resource
    with pytest.raises(ValueError):
        p.life_need(sit, [], {"years": [-5, 10]})
