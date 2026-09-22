"""Estate and beneficiary register: mechanisms, heirs, gaps ranked by amount at risk, score, views, nudges."""
from __future__ import annotations

import pytest

from wealth import estate_register as er
from wealth import proactive, views
from wealth.catalog import CATALOG
from wealth.service import WealthService
from wealth.situation import build
from wealth.situation.schema import SchemaError, validate

TODAY = "2026-09-22"
FAMILY = {"marital_status": "married", "marriage_date": "2015-06-20", "spouse": "Laura",
          "children": [{"name": "Sofía", "birth_year": 2017}, {"name": "Mateo", "birth_year": 2021}],
          "parents_living": 2}


def _snapshot(rows: dict, today: str = TODAY) -> dict:
    return {"client": {"id": None, "revision": None}, "facts": [
        {"id": f"f:{k}", "key": k, "value": v, "confidence": "reported", "status": "active",
         "source": {"kind": "user", "ref": "test", "observed_on": today}} for k, v in rows.items() if v is not None],
        "decisions": []}


def _run(rows: dict, today: str = TODAY, **inputs) -> dict:
    rows = {"client.profile": {"residence": {"country": "MX"}, "us_person": False}, **rows}
    snapshot = _snapshot(rows, today)
    return er.register(build(snapshot, None, today), snapshot, inputs, today)


def _row(report: dict, key: str) -> dict:
    return next(r for r in report["result"]["rows"] if r["key"] == key)


def _codes(report: dict) -> list[str]:
    return [g["code"] for g in report["result"]["gaps"]]


# ------------------------------------------------------------------ schema


def test_schema_accepts_designations_and_rejects_bad_ones():
    validate("investment.gbm", {"amount": 1, "currency": "MXN", "titling": "mancomunada", "co_owners": ["Ana"],
                                "beneficiaries": [{"name": "Ana", "share": 0.5, "contingent": True}],
                                "designation_date": "2024-01-01", "plan_type": "401k", "spousal_consent": True})
    validate("insurance.vida", {"kind": "life", "coverage": 1_000_000, "currency": "MXN", "beneficiaries": []})
    validate("estate.will", {"exists": False})
    with pytest.raises(SchemaError, match="share"):
        validate("cash.bbva", {"amount": 1, "currency": "MXN", "beneficiaries": [{"name": "Ana", "share": 50}]})
    with pytest.raises(SchemaError, match="titling"):
        validate("cash.bbva", {"amount": 1, "currency": "MXN", "titling": "shared"})
    with pytest.raises(SchemaError, match="exists"):
        validate("estate.will", {"date": "2020-01-01"})
    with pytest.raises(SchemaError, match="name"):
        validate("investment.x", {"amount": 1, "currency": "MXN", "beneficiaries": [{"share": 1}]})
    with pytest.raises(SchemaError, match="currency"):
        validate("property.casa", {"kind": "home", "value": 100})


# ------------------------------------------------------------------ mechanisms and heirs


def test_no_beneficiary_account_goes_to_intestate_heirs_and_is_a_ranked_gap():
    report = _run({"investment.gbm": {"amount": 217000, "currency": "MXN", "institution": "GBM",
                                      "kind": "brokerage", "beneficiaries": []},
                   "estate.will": {"exists": False}, "estate.family": FAMILY})
    row = _row(report, "investment.gbm")
    assert row["mechanism"] == "intestate" and row["bypasses_court"] is False
    # CCF: children equally, the spouse takes a child's share.
    assert {h["name"]: h["amount"] for h in row["heirs"]} == pytest.approx(
        {"Sofía": 72333.33, "Mateo": 72333.33, "Laura": 72333.33}, abs=0.01)
    gap = next(g for g in report["result"]["gaps"] if g["code"] == "no_beneficiary")
    assert gap["es"] == "Tu cuenta de GBM ($217k) no tiene beneficiarios."
    will = next(g for g in report["result"]["gaps"] if g["code"] == "no_will")
    assert "Mes del Testamento" in will["es"] and "este septiembre" in will["es"]
    assert [g["rank"] for g in report["result"]["gaps"]] == list(range(1, len(report["result"]["gaps"]) + 1))
    amounts = [g["amount_at_risk"] for g in report["result"]["gaps"]]
    assert amounts == sorted(amounts, reverse=True)


def test_designated_bank_account_bypasses_the_court_with_lic_56_note():
    report = _run({"cash.bbva": {"amount": 100000, "currency": "MXN", "institution": "BBVA",
                                 "beneficiaries": [{"name": "Laura", "share": 0.7}, {"name": "Pedro", "share": 0.3}],
                                 "designation_date": "2025-01-10"}})
    row = _row(report, "cash.bbva")
    assert row["mechanism"] == "beneficiary" and row["bypasses_court"] is True
    assert row["mechanism_label"]["es"] == "Beneficiarios bancarios (LIC Art. 56)"
    assert [h["amount"] for h in row["heirs"]] == [70000, 30000]
    assert any("Art. 56 LIC" in n["es"] for n in row["notes"])
    assert not row["gaps"]


def test_unknown_is_a_question_not_a_gap_and_afore_none_is_its_own_gap():
    unknown = _run({"investment.afore": {"amount": 410000, "currency": "MXN", "kind": "afore"}})
    assert _row(unknown, "investment.afore")["mechanism"] == "unknown"
    assert not _codes(unknown)
    assert unknown["result"]["questions"][0]["field"] == "investment.afore.beneficiaries"
    assert unknown["status"] == "partial"
    none = _run({"investment.afore": {"amount": 410000, "currency": "MXN", "kind": "afore", "beneficiaries": []},
                 "estate.family": FAMILY})
    row = _row(none, "investment.afore")
    assert row["mechanism"] == "legal_beneficiaries" and "LFT Art. 501" in row["rule"]
    assert _codes(none)[0] == "afore_beneficiaries"


def test_mancomunada_is_not_a_beneficiary_and_only_the_persons_share_passes():
    report = _run({"cash.banorte": {"amount": 200000, "currency": "MXN", "institution": "Banorte",
                                    "titling": "mancomunada", "co_owners": ["Laura"],
                                    "beneficiaries": [{"name": "Sofía", "share": 1}]}})
    row = _row(report, "cash.banorte")
    assert row["estate_value"] == 100000 and row["heirs"][0]["amount"] == 100000
    assert any("mancomunada no es un beneficiario" in n["es"] for n in row["notes"])


def test_shares_minor_guardian_predeceased_and_ex_spouse():
    rows = {"investment.cetes": {"amount": 120000, "currency": "MXN", "institution": "Cetesdirecto",
                                 "beneficiaries": [{"name": "Laura", "share": 0.6},
                                                   {"name": "Sofía", "relationship": "child", "share": 0.3}]},
            "estate.family": FAMILY}
    report = _run(rows)
    assert {"shares_not_100", "minor_direct", "no_guardian"} <= set(_codes(report))
    guarded = _run({**rows, "estate.guardianship": {"guardian": "Rosa"}})
    assert "minor_direct" not in _codes(guarded) and "no_guardian" not in _codes(guarded)
    trust = _run({**rows, "investment.cetes": {**rows["investment.cetes"], "beneficiaries": [
        {"name": "Laura", "share": 0.5}, {"name": "Sofía", "share": 0.5, "via_trust": True}]}})
    assert "minor_direct" not in _codes(trust) and "shares_not_100" not in _codes(trust)
    died = _run({"cash.x": {"amount": 1000, "currency": "MXN", "institution": "HSBC", "beneficiaries": [
        {"name": "Papá", "share": 1, "deceased": True}, {"name": "Ana", "contingent": True}]}})
    assert "beneficiary_predeceased" in _codes(died)
    assert [(h["name"], h.get("contingent")) for h in _row(died, "cash.x")["heirs"]] == [("Ana", True)]
    ex = _run({"cash.x": {"amount": 1000, "currency": "MXN", "institution": "HSBC",
                          "beneficiaries": [{"name": "Marta", "share": 1}]},
               "estate.family": {"marital_status": "divorced", "ex_spouses": ["Marta"]}})
    assert "beneficiary_ex_spouse" in _codes(ex)


def test_designation_and_will_older_than_life_events_or_review_period():
    report = _run({"insurance.vida": {"kind": "life", "coverage": 1500000, "currency": "MXN", "insurer": "GNP",
                                      "designation_date": "2014-05-01",
                                      "beneficiaries": [{"name": "Laura", "relationship": "spouse"}]},
                   "cash.bbva": {"amount": 85000, "currency": "MXN", "institution": "BBVA",
                                 "designation_date": "2020-03-10", "beneficiaries": [{"name": "Laura"}]},
                   "estate.will": {"exists": True, "date": "2016-02-01"}, "estate.family": FAMILY,
                   "estate.guardianship": {"guardian": "Rosa"}})
    gaps = {(g["code"], g["key"]): g for g in report["result"]["gaps"]}
    assert "matrimonio (2015)" in gaps[("designation_before_event", "insurance.vida")]["es"]
    assert "naciera Mateo (2021)" in gaps[("designation_before_event", "cash.bbva")]["es"]
    gaps = {g["code"]: g for g in report["result"]["gaps"]}
    assert "Sofía" in gaps["will_before_event"]["en"]
    old = _run({"cash.bbva": {"amount": 1, "currency": "MXN", "designation_date": "2019-03-10",
                              "beneficiaries": [{"name": "Laura"}]}})
    assert _codes(old) == ["designation_old"]
    # Year-only births count from January 1: a designation made in the birth year is not "before" it.
    same_year = _run({"cash.bbva": {"amount": 1, "currency": "MXN", "designation_date": "2021-03-10",
                                    "beneficiaries": [{"name": "Laura"}]}, "estate.family": FAMILY},
                     review_years=10)
    assert "designation_before_event" not in _codes(same_year)


def test_us_401k_needs_spousal_consent_and_ira_carries_the_secure_act_note():
    rows = {"client.profile": {"residence": {"country": "US"}, "us_person": True},
            "investment.k401": {"amount": 380000, "currency": "USD", "institution": "Fidelity", "kind": "retirement",
                                "plan_type": "401k", "designation_date": "2024-02-01",
                                "beneficiaries": [{"name": "Tom", "relationship": "sibling", "share": 1}]},
            "investment.joint": {"amount": 60000, "currency": "USD", "institution": "Schwab", "titling": "joint",
                                 "co_owners": ["Dana"]},
            "estate.family": {"marital_status": "married", "spouse": "Dana", "children": []}}
    report = _run(rows)
    assert "erisa_spousal_consent" in _codes(report)
    k401 = _row(report, "investment.k401")
    assert k401["country"] == "US" and any("SECURE" in n["en"] for n in k401["notes"])
    joint = _row(report, "investment.joint")
    assert joint["mechanism"] == "survivorship" and joint["heirs"][0]["name"] == "Dana"
    consented = _run({**rows, "investment.k401": {**rows["investment.k401"], "spousal_consent": True}})
    assert "erisa_spousal_consent" not in _codes(consented)


def test_us_situs_over_60k_for_a_non_resident_reuses_the_estate_module():
    report = _run({"investment.ibkr": {"amount": 150000, "currency": "USD", "institution": "IBKR", "country": "US",
                                       "beneficiaries": [{"name": "Andrés", "share": 1}]},
                   "client.profile": {"residence": {"country": "MX"}, "us_person": False,
                                      "reporting_currency": "USD"}},
                  us_situs={"year": 2026, "decedent": {"us_citizen": False, "green_card": False,
                                                       "us_domiciled": False},
                            "assets": [{"id": "voo", "type": "us_domiciled_fund", "value_usd": 150000}]})
    gap = next(g for g in report["result"]["gaps"] if g["code"] == "us_situs_nra")
    assert gap["amount_at_risk"] == 150000 and gap["tax_range_usd"]["high"] == "25800.00"
    assert "706-NA" in gap["en"]
    assert any("2102" in s["title"] for s in report["sources"])


def test_complete_plan_scores_100_and_statutes_live_in_sources_and_assumptions():
    report = _run({"cash.bbva": {"amount": 100000, "currency": "MXN", "institution": "BBVA",
                                 "designation_date": "2025-01-01", "beneficiaries": [{"name": "Laura", "share": 1}]},
                   "estate.will": {"exists": True, "date": "2025-09-10"},
                   "estate.family": {"marital_status": "married", "spouse": "Laura", "children": []}})
    result = report["result"]
    assert result["completeness"]["score"] == 100 and not result["gaps"]
    assert "guardian" not in result["completeness"]["parts"]
    titles = " ".join(s["title"] for s in report["sources"])
    for statute in ("Art. 56", "Art. 193", "Sistemas de Ahorro", "Código Civil", "SECURE", "ERISA"):
        assert statute in titles
    assert any("not legal advice" in a for a in report["assumptions"])
    assert not any("legal advice" in g["en"] for g in result["gaps"])


def test_statement_account_without_a_stated_fact_has_unknown_beneficiaries():
    sit = {"as_of": TODAY, "currency": "MXN", "profile": {"residence": {"country": "MX"}}, "cash": [],
           "investments": [], "fx": [], "holdings": {},
           "accounts": [{"key": "account.gbm-1", "id": "gbm-1", "label": "GBM", "institution": "GBM",
                         "type": "brokerage", "currency": "MXN", "value": 217000}]}
    report = er.register(sit, {"facts": []}, {}, TODAY)
    row = _row(report, "account.gbm-1")
    assert row["statement_only"] and row["mechanism"] == "unknown"
    assert report["result"]["questions"][0]["amount"] == 217000


# ------------------------------------------------------------------ service, views, nudges, You page


def test_catalog_example_runs_through_the_service_with_two_valid_views(tmp_path):
    service = WealthService(tmp_path / "w.sqlite3")
    report = service.run("estate_register", CATALOG["estate_register"]["example"])
    assert report["status"] in {"ready", "partial"}
    specs = views.views_for("estate_register", report)
    assert len(specs) == 2
    for spec in specs:
        views.validate(spec)
    assert specs[0]["data"]["total"]["value"] == {"t": "percent", "v": report["result"]["completeness"]["score"]}


def test_saved_client_run_reads_designations_and_cites_evidence(tmp_path):
    service = WealthService(tmp_path / "w.sqlite3")
    service.create("ana", "Ana")
    source = {"kind": "user", "ref": "chat", "observed_on": TODAY}
    service.remember("ana", [
        {"key": "client.profile", "value": {"residence": {"country": "MX"}}, "source": source},
        {"key": "investment.gbm", "value": {"amount": 217000, "currency": "MXN", "institution": "GBM",
                                            "beneficiaries": []}, "source": source},
        {"key": "estate.will", "value": {"exists": False}, "source": source}])
    report = service.run("estate_register", inputs={"as_of": TODAY}, client_id="ana")
    assert "no_beneficiary" in _codes(report)
    assert len(report["evidence_ids"]) == 3
    with pytest.raises(ValueError, match="unknown"):
        service.run("estate_register", inputs={"bogus": 1}, client_id="ana")


def test_today_nudges_the_top_estate_gaps_and_folds_the_will_calendar_item(tmp_path):
    facts = [{"key": "client.profile", "value": {"residence": {"country": "MX"}, "us_person": False}},
             {"key": "investment.gbm", "value": {"amount": 217000, "currency": "MXN", "institution": "GBM",
                                                 "beneficiaries": []}},
             {"key": "estate.will", "value": {"exists": True, "date": "2025-01-01"}}]
    service = WealthService(tmp_path / "w.sqlite3")
    result = service.run("today", {"as_of": TODAY, "facts": facts})["result"]
    items = [i for i in result["today"] + result["upcoming"] if i["kind"] == "estate_gap"]
    assert items[0]["title"]["es"] == "Tu cuenta de GBM ($217k) no tiene beneficiarios."
    assert items[0]["data"]["task"] == "estate_register"
    no_will = service.run("today", {"as_of": TODAY, "facts": facts[:2] + [
        {"key": "estate.will", "value": {"exists": False}}]})["result"]
    ids = [i["id"] for i in no_will["today"] + no_will["upcoming"]]
    assert "estate_gap:no_will" in ids and not any(i.startswith("mx_testamento") for i in ids)
    quiet = service.run("today", {"as_of": TODAY, "facts": facts[:1] + [
        {"key": "investment.gbm", "value": {"amount": 217000, "currency": "MXN", "institution": "GBM"}}]})["result"]
    assert not any(i["kind"] == "estate_gap" for i in quiet["today"] + quiet["upcoming"])


def test_you_page_summary_has_score_and_top_gap():
    rows = {"client.profile": {"residence": {"country": "MX"}},
            "investment.gbm": {"amount": 217000, "currency": "MXN", "institution": "GBM", "beneficiaries": []}}
    snapshot = _snapshot(rows)
    line = er.summary(build(snapshot, None, TODAY), snapshot, TODAY)
    assert line["score"] == 0 and line["top_gap"]["es"].startswith("Tu cuenta de GBM")
    assert proactive._estate_facts(snapshot)
    assert not proactive._estate_facts(_snapshot({"client.profile": {"residence": {"country": "MX"}}}))
