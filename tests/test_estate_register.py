"""Estate and beneficiary register: mechanisms, heirs, gaps ranked by amount at risk, score, views, nudges."""
from __future__ import annotations

import pytest

from wealth import estate_register as er
from wealth import proactive, views
from wealth.catalog import CATALOG
from wealth.service import WealthService
from wealth.situation import build
from wealth.situation.schema import SchemaError, designation_key, validate

TODAY = "2026-09-22"
FAMILY = {"marital_status": "married", "marriage_date": "2015-06-20", "spouse": "Laura",
          "marital_regime": "separacion_de_bienes",
          "children": [{"name": "Sofía", "birth_year": 2017}, {"name": "Mateo", "birth_year": 2021}],
          "parents_living": 2}
GBM = {"amount": 217000, "currency": "MXN", "institution": "GBM", "kind": "brokerage"}


def _d(account: str, **fields) -> dict:
    """One estate.designation.<slug> fact for ``account``."""
    return {designation_key(account): {"account": account, **fields}}


def _snapshot(rows: dict, today: str = TODAY, observed: dict | None = None) -> dict:
    observed = observed or {}
    return {"client": {"id": None, "revision": None}, "facts": [
        {"id": f"f:{k}", "key": k, "value": v, "confidence": "reported", "status": "active",
         "source": {"kind": "user", "ref": "test", "observed_on": observed.get(k, today)}}
        for k, v in rows.items() if v is not None], "decisions": []}


def _run(rows: dict, today: str = TODAY, **inputs) -> dict:
    rows = {"client.profile": {"residence": {"country": "MX"}, "us_person": False}, **rows}
    snapshot = _snapshot(rows, today)
    return er.register(build(snapshot, None, today), snapshot, inputs, today)


def _row(report: dict, key: str) -> dict:
    return next(r for r in report["result"]["rows"] if r["key"] == key)


def _codes(report: dict) -> list[str]:
    return [g["code"] for g in report["result"]["gaps"]]


def _amounts(row: dict) -> dict:
    return {h["name"]: h["amount"] if h["amount"] is not None else (h["amount_range"]["low"], h["amount_range"]["high"])
            for h in row["heirs"]}


# ------------------------------------------------------------------ schema


def test_schema_accepts_designations_and_rejects_bad_ones():
    validate("estate.designation.investment-gbm", {
        "account": "investment.gbm", "titling": "mancomunada", "co_owners": ["Ana"], "marital_property": False,
        "beneficiaries": [{"name": "Ana", "share": 0.5, "contingent": True}], "designation_date": "2024-01-01",
        "plan_type": "401k", "spousal_consent": True})
    validate("estate.designation.account-gbm-1", {"account": "account.gbm-1", "beneficiaries": []})
    validate("insurance.vida", {"kind": "life", "coverage": 1_000_000, "currency": "MXN"})
    validate("estate.will", {"exists": False})
    validate("estate.family", {"marital_regime": "sociedad_conyugal",
                               "spouse_assets": {"amount": 0, "currency": "MXN"}})
    with pytest.raises(SchemaError, match="must be 'estate.designation.investment-gbm'"):
        validate("estate.designation.gbm", {"account": "investment.gbm"})
    with pytest.raises(SchemaError, match="account"):
        validate("estate.designation.x", {"beneficiaries": []})
    with pytest.raises(SchemaError, match="share"):
        validate("estate.designation.cash-bbva", {"account": "cash.bbva",
                                                  "beneficiaries": [{"name": "A", "share": 50}]})
    with pytest.raises(SchemaError, match="titling"):
        validate("estate.designation.cash-bbva", {"account": "cash.bbva", "titling": "shared"})
    with pytest.raises(SchemaError, match="exists"):
        validate("estate.will", {"date": "2020-01-01"})
    with pytest.raises(SchemaError, match="name"):
        validate("estate.designation.investment-x", {"account": "investment.x", "beneficiaries": [{"share": 1}]})
    with pytest.raises(SchemaError, match="currency"):
        validate("estate.family", {"spouse_assets": {"amount": 5}})


def test_saving_a_beneficiary_never_refreshes_a_stale_balance(tmp_path):
    service = WealthService(tmp_path / "w.sqlite3")
    service.create("ana", "Ana")
    old = {"kind": "user", "ref": "chat", "observed_on": "2026-01-10"}
    now = {"kind": "user", "ref": "chat", "observed_on": TODAY}
    service.remember("ana", [
        {"key": "client.profile", "value": {"residence": {"country": "MX"}}, "source": old},
        {"key": "investment.gbm", "value": GBM, "source": old}])
    service.remember("ana", [{"key": "estate.designation.investment-gbm",
                              "value": {"account": "investment.gbm", "beneficiaries": [{"name": "Ana", "share": 1}]},
                              "source": now}])
    sit = service.situation("ana", today=TODAY)
    assert "investment.gbm" in sit["stale"]  # the balance still asks to be reconfirmed
    assert sit["meta"]["investment.gbm"]["observed_on"] == "2026-01-10"
    report = service.run("estate_register", inputs={"as_of": TODAY}, client_id="ana")
    row = _row(report, "investment.gbm")
    assert row["designation_key"] == "estate.designation.investment-gbm"
    assert row["mechanism"] == "beneficiary" and row["heirs"][0]["name"] == "Ana"
    with pytest.raises(Exception, match="estate.designation.investment-gbm"):
        service.remember("ana", [{"key": "estate.designation.gbm",
                                  "value": {"account": "investment.gbm", "beneficiaries": []}, "source": now}])


def test_legacy_inline_beneficiaries_are_read_and_the_designation_fact_wins():
    legacy = _run({"investment.gbm": {**GBM, "beneficiaries": [{"name": "Pedro", "share": 1}]}})
    row = _row(legacy, "investment.gbm")
    assert row["designation_key"] == "inline" and row["heirs"][0]["name"] == "Pedro"
    both = _run({"investment.gbm": {**GBM, "beneficiaries": [{"name": "Pedro", "share": 1}]},
                 **_d("investment.gbm", beneficiaries=[{"name": "Ana", "share": 1}])})
    assert _row(both, "investment.gbm")["heirs"][0]["name"] == "Ana"


def test_designation_joins_a_statement_account_and_an_orphan_is_flagged():
    sit = {"as_of": TODAY, "currency": "MXN", "profile": {"residence": {"country": "MX"}}, "cash": [],
           "investments": [], "fx": [], "holdings": {},
           "accounts": [{"key": "account.gbm-1", "id": "gbm-1", "label": "GBM", "institution": "GBM",
                         "type": "brokerage", "currency": "MXN", "value": 217000}]}
    unknown = er.register(sit, {"facts": []}, {}, TODAY)
    row = _row(unknown, "account.gbm-1")
    assert row["statement_only"] and row["mechanism"] == "unknown"
    assert unknown["result"]["questions"][0] == {
        "code": "beneficiaries_unknown", "key": "account.gbm-1", "label": "GBM", "amount": 217000, "currency": "MXN",
        "field": "estate.designation.account-gbm-1.beneficiaries", "afore": None}
    snapshot = _snapshot({**_d("account.gbm-1", beneficiaries=[{"name": "Ana", "share": 1}]),
                          **_d("investment.gone", beneficiaries=[{"name": "Ana"}])})
    joined = er.register(sit, snapshot, {}, TODAY)
    assert _row(joined, "account.gbm-1")["heirs"] == [
        {"name": "Ana", "relationship": None, "share": 1, "amount": 217000}]
    assert _row(joined, "investment.gone")["orphan"] and "investment.gone" in joined["warnings"][0]


# ------------------------------------------------------------------ mechanisms and heirs


def test_no_beneficiary_account_goes_to_intestate_heirs_and_is_a_ranked_gap():
    report = _run({"investment.gbm": GBM, **_d("investment.gbm", beneficiaries=[]),
                   "estate.will": {"exists": False},
                   "estate.family": {**FAMILY, "spouse_assets": {"amount": 0, "currency": "MXN"}}})
    row = _row(report, "investment.gbm")
    assert row["mechanism"] == "intestate" and row["bypasses_court"] is False
    # Separación de bienes and a spouse with nothing of their own: a child's share each (CCF Arts. 1624-1625).
    assert _amounts(row) == pytest.approx({"Sofía": 72333.33, "Mateo": 72333.33, "Laura": 72333.33}, abs=0.01)
    gap = next(g for g in report["result"]["gaps"] if g["code"] == "no_beneficiary")
    assert gap["es"] == "Tu cuenta de GBM ($217k) no tiene beneficiarios."
    will = next(g for g in report["result"]["gaps"] if g["code"] == "no_will")
    assert "Mes del Testamento" in will["es"] and "este septiembre" in will["es"]
    assert [g["rank"] for g in report["result"]["gaps"]] == list(range(1, len(report["result"]["gaps"]) + 1))
    amounts = [g["amount_at_risk"] for g in report["result"]["gaps"]]
    assert amounts == sorted(amounts, reverse=True)


def test_spouse_share_depends_on_what_the_spouse_owns():
    base = {"investment.gbm": GBM, **_d("investment.gbm", beneficiaries=[]), "estate.will": {"exists": False}}
    # Owns 50,000: only what brings them to a child's portion, x = (217,000 - 2 x 50,000) / 3 = 39,000.
    some = _run({**base, "estate.family": {**FAMILY, "spouse_assets": {"amount": 50000, "currency": "MXN"}}})
    assert _amounts(_row(some, "investment.gbm")) == pytest.approx({"Sofía": 89000, "Mateo": 89000, "Laura": 39000})
    # Owns more than a child's portion: nothing by intestacy.
    rich = _run({**base, "estate.family": {**FAMILY, "spouse_assets": {"amount": 500000, "currency": "MXN"}}})
    assert _amounts(_row(rich, "investment.gbm")) == pytest.approx({"Sofía": 108500, "Mateo": 108500, "Laura": 0})
    # Unknown: a range from nothing to a child's share, and the question is asked.
    unknown = _run({**base, "estate.family": FAMILY})
    heirs = _amounts(_row(unknown, "investment.gbm"))
    assert heirs["Laura"] == pytest.approx((0, 72333.33), abs=0.01)
    assert heirs["Sofía"] == pytest.approx((72333.33, 108500), abs=0.01)
    assert "estate.family.spouse_assets" in unknown["missing"]
    assert any("Arts. 1624-1625" in a for a in unknown["assumptions"])
    # With parents instead of children, the spouse takes half whatever they own (Arts. 1626, 1628).
    parents = _run({**base, "estate.family": {"marital_status": "married", "spouse": "Laura", "children": [],
                                              "marital_regime": "separacion_de_bienes", "parents_living": 2}})
    assert _amounts(_row(parents, "investment.gbm")) == {"Laura": 108500, "padres": 108500}


def test_sociedad_conyugal_keeps_the_spouses_half_out_of_the_estate():
    family = {**FAMILY, "marital_regime": "sociedad_conyugal"}
    report = _run({"investment.gbm": GBM, **_d("investment.gbm", beneficiaries=[]), "estate.will": {"exists": False},
                   "estate.family": family})
    row = _row(report, "investment.gbm")
    assert row["value"] == 217000 and row["estate_value"] == 108500
    assert any("half of this is already your spouse's" in n["en"] for n in row["notes"])
    # Their half of the gananciales (108,500) already exceeds a child's portion of the estate: nothing more,
    # whatever else they own, so nothing is asked about it.
    assert _amounts(row) == {"Sofía": 54250, "Mateo": 54250, "Laura": 0}
    assert "estate.family.spouse_assets" not in report["missing"]
    assert next(g for g in report["result"]["gaps"] if g["code"] == "no_beneficiary")["es"] == \
        "Tu cuenta de GBM ($217k) no tiene beneficiarios."
    own = _run({"investment.gbm": GBM, **_d("investment.gbm", beneficiaries=[], marital_property=False),
                "estate.will": {"exists": False}, "estate.family": family})
    assert _row(own, "investment.gbm")["estate_value"] == 217000
    afore = _run({"investment.afore": {"amount": 400000, "currency": "MXN", "kind": "afore"},
                  "estate.family": family})
    assert _row(afore, "investment.afore")["estate_value"] == 400000  # the AFORE follows the LSS, not the regime


def test_unknown_regime_is_a_range_and_a_question():
    report = _run({"investment.gbm": GBM, **_d("investment.gbm", beneficiaries=[]), "estate.will": {"exists": False},
                   "estate.family": {k: v for k, v in FAMILY.items() if k != "marital_regime"}})
    row = _row(report, "investment.gbm")
    assert row["estate_value"] is None and row["estate_value_range"] == {"low": 108500, "high": 217000}
    assert report["result"]["total"] is None and report["result"]["total_range"] == {"low": 108500, "high": 217000}
    assert "estate.family.marital_regime" in report["missing"]
    assert any("marital regime is unknown" in a for a in report["assumptions"])
    specs = views.views_for("estate_register", {**report, "task": "estate_register"})
    for spec in specs:
        views.validate(spec)
    assert specs[0]["data"]["rows"][0]["value"] == {"t": "range", "lo": 108500, "hi": 217000, "cur": "MXN"}


def test_designated_bank_account_bypasses_the_court_with_lic_56_note():
    report = _run({"cash.bbva": {"amount": 100000, "currency": "MXN", "institution": "BBVA"},
                   **_d("cash.bbva", beneficiaries=[{"name": "Laura", "share": 0.7}, {"name": "Pedro", "share": 0.3}],
                        designation_date="2025-01-10")})
    row = _row(report, "cash.bbva")
    assert row["mechanism"] == "beneficiary" and row["bypasses_court"] is True
    assert row["mechanism_label"]["es"] == "Beneficiarios bancarios (LIC Art. 56)"
    assert [h["amount"] for h in row["heirs"]] == [70000, 30000]
    assert any("Art. 56 LIC" in n["es"] for n in row["notes"])
    assert not row["gaps"]


def test_unknown_is_a_question_not_a_gap_and_afore_none_is_its_own_gap():
    afore = {"amount": 410000, "currency": "MXN", "kind": "afore"}
    unknown = _run({"investment.afore": afore})
    assert _row(unknown, "investment.afore")["mechanism"] == "unknown"
    assert not _codes(unknown)
    assert unknown["result"]["questions"][0]["field"] == "estate.designation.investment-afore.beneficiaries"
    assert unknown["status"] == "partial"
    none = _run({"investment.afore": afore, **_d("investment.afore", beneficiaries=[]), "estate.family": FAMILY})
    row = _row(none, "investment.afore")
    assert row["mechanism"] == "legal_beneficiaries" and "LFT Art. 501" in row["rule"]
    assert _codes(none)[0] == "afore_beneficiaries"


def test_mancomunada_is_not_a_beneficiary_and_only_the_persons_share_passes():
    report = _run({"cash.banorte": {"amount": 200000, "currency": "MXN", "institution": "Banorte"},
                   **_d("cash.banorte", titling="mancomunada", co_owners=["Laura"],
                        beneficiaries=[{"name": "Sofía", "share": 1}])})
    row = _row(report, "cash.banorte")
    assert row["estate_value"] == 100000 and row["heirs"][0]["amount"] == 100000
    assert any("mancomunada no es un beneficiario" in n["es"] for n in row["notes"])


def test_shares_minor_guardian_predeceased_and_ex_spouse():
    cetes = {"amount": 120000, "currency": "MXN", "institution": "Cetesdirecto"}
    named = [{"name": "Laura", "share": 0.6}, {"name": "Sofía", "relationship": "child", "share": 0.3}]
    rows = {"investment.cetes": cetes, **_d("investment.cetes", beneficiaries=named), "estate.family": FAMILY}
    report = _run(rows)
    assert {"shares_not_100", "minor_direct", "no_guardian"} <= set(_codes(report))
    guarded = _run({**rows, "estate.guardianship": {"guardian": "Rosa"}})
    assert "minor_direct" not in _codes(guarded) and "no_guardian" not in _codes(guarded)
    trust = _run({**rows, **_d("investment.cetes", beneficiaries=[
        {"name": "Laura", "share": 0.5}, {"name": "Sofía", "share": 0.5, "via_trust": True}])})
    assert "minor_direct" not in _codes(trust) and "shares_not_100" not in _codes(trust)
    cash = {"amount": 1000, "currency": "MXN", "institution": "HSBC"}
    died = _run({"cash.x": cash, **_d("cash.x", beneficiaries=[
        {"name": "Papá", "share": 1, "deceased": True}, {"name": "Ana", "contingent": True}])})
    assert "beneficiary_predeceased" in _codes(died)
    assert [(h["name"], h.get("contingent")) for h in _row(died, "cash.x")["heirs"]] == [("Ana", True)]
    ex = _run({"cash.x": cash, **_d("cash.x", beneficiaries=[{"name": "Marta", "share": 1}]),
               "estate.family": {"marital_status": "divorced", "ex_spouses": ["Marta"]}})
    assert "beneficiary_ex_spouse" in _codes(ex)


def test_designation_and_will_older_than_life_events_or_review_period():
    report = _run({"insurance.vida": {"kind": "life", "coverage": 1500000, "currency": "MXN", "insurer": "GNP"},
                   **_d("insurance.vida", designation_date="2014-05-01",
                        beneficiaries=[{"name": "Laura", "relationship": "spouse"}]),
                   "cash.bbva": {"amount": 85000, "currency": "MXN", "institution": "BBVA"},
                   **_d("cash.bbva", designation_date="2020-03-10", beneficiaries=[{"name": "Laura"}]),
                   "estate.will": {"exists": True, "date": "2016-02-01"}, "estate.family": FAMILY,
                   "estate.guardianship": {"guardian": "Rosa"}})
    gaps = {(g["code"], g["key"]): g for g in report["result"]["gaps"]}
    assert "matrimonio (2015)" in gaps[("designation_before_event", "insurance.vida")]["es"]
    assert "naciera Mateo (2021)" in gaps[("designation_before_event", "cash.bbva")]["es"]
    gaps = {g["code"]: g for g in report["result"]["gaps"]}
    assert "Sofía" in gaps["will_before_event"]["en"]
    one = {"amount": 1, "currency": "MXN"}
    old = _run({"cash.bbva": one, **_d("cash.bbva", designation_date="2019-03-10", beneficiaries=[{"name": "L"}])})
    assert _codes(old) == ["designation_old"]
    # Year-only births count from January 1: a designation made in the birth year is not "before" it.
    same_year = _run({"cash.bbva": one, **_d("cash.bbva", designation_date="2021-03-10",
                                             beneficiaries=[{"name": "Laura"}]), "estate.family": FAMILY},
                     review_years=10)
    assert "designation_before_event" not in _codes(same_year)


def test_us_401k_needs_spousal_consent_and_ira_carries_the_secure_act_note():
    rows = {"client.profile": {"residence": {"country": "US"}, "us_person": True},
            "investment.k401": {"amount": 380000, "currency": "USD", "institution": "Fidelity", "kind": "retirement",
                                "plan_type": "401k"},
            **_d("investment.k401", designation_date="2024-02-01",
                 beneficiaries=[{"name": "Tom", "relationship": "sibling", "share": 1}]),
            "investment.joint": {"amount": 60000, "currency": "USD", "institution": "Schwab"},
            **_d("investment.joint", titling="joint", co_owners=["Dana"]),
            "estate.family": {"marital_status": "married", "spouse": "Dana", "children": [],
                              "marital_regime": "separate_property"}}
    report = _run(rows)
    assert "erisa_spousal_consent" in _codes(report)
    k401 = _row(report, "investment.k401")
    assert k401["country"] == "US" and any("SECURE" in n["en"] for n in k401["notes"])
    joint = _row(report, "investment.joint")
    assert joint["mechanism"] == "survivorship" and joint["heirs"][0]["name"] == "Dana"
    consented = _run({**rows, **_d("investment.k401", designation_date="2024-02-01", spousal_consent=True,
                                   beneficiaries=[{"name": "Tom", "relationship": "sibling", "share": 1}])})
    assert "erisa_spousal_consent" not in _codes(consented)


def test_us_situs_over_60k_for_a_non_resident_reuses_the_estate_module():
    report = _run({"investment.ibkr": {"amount": 150000, "currency": "USD", "institution": "IBKR"},
                   **_d("investment.ibkr", country="US", beneficiaries=[{"name": "Andrés", "share": 1}]),
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
    report = _run({"cash.bbva": {"amount": 100000, "currency": "MXN", "institution": "BBVA"},
                   **_d("cash.bbva", designation_date="2025-01-01", beneficiaries=[{"name": "Laura", "share": 1}]),
                   "estate.will": {"exists": True, "date": "2025-09-10"},
                   "estate.family": {"marital_status": "married", "spouse": "Laura", "children": [],
                                     "marital_regime": "separacion_de_bienes"}})
    result = report["result"]
    assert result["completeness"]["score"] == 100 and not result["gaps"] and report["status"] == "ready"
    assert "guardian" not in result["completeness"]["parts"]
    titles = " ".join(s["title"] for s in report["sources"])
    for statute in ("Art. 56", "Art. 193", "Sistemas de Ahorro", "Código Civil", "SECURE", "ERISA"):
        assert statute in titles
    assert any("not legal advice" in a for a in report["assumptions"])
    assert not any("legal advice" in g["en"] for g in result["gaps"])


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
        {"key": "investment.gbm", "value": GBM, "source": source},
        {"key": "estate.designation.investment-gbm", "value": {"account": "investment.gbm", "beneficiaries": []},
         "source": source},
        {"key": "estate.will", "value": {"exists": False}, "source": source}])
    report = service.run("estate_register", inputs={"as_of": TODAY}, client_id="ana")
    assert "no_beneficiary" in _codes(report)
    assert len(report["evidence_ids"]) == 4
    with pytest.raises(ValueError, match="unknown"):
        service.run("estate_register", inputs={"bogus": 1}, client_id="ana")


def test_today_nudges_the_top_estate_gaps_and_folds_the_will_calendar_item(tmp_path):
    facts = [{"key": "client.profile", "value": {"residence": {"country": "MX"}, "us_person": False}},
             {"key": "investment.gbm", "value": GBM},
             {"key": "estate.designation.investment-gbm", "value": {"account": "investment.gbm", "beneficiaries": []}},
             {"key": "estate.will", "value": {"exists": True, "date": "2025-01-01"}}]
    service = WealthService(tmp_path / "w.sqlite3")
    result = service.run("today", {"as_of": TODAY, "facts": facts})["result"]
    items = [i for i in result["today"] + result["upcoming"] if i["kind"] == "estate_gap"]
    assert items[0]["title"]["es"] == "Tu cuenta de GBM ($217k) no tiene beneficiarios."
    assert items[0]["data"]["task"] == "estate_register"
    no_will = service.run("today", {"as_of": TODAY, "facts": facts[:3] + [
        {"key": "estate.will", "value": {"exists": False}}]})["result"]
    ids = [i["id"] for i in no_will["today"] + no_will["upcoming"]]
    assert "estate_gap:no_will" in ids and not any(i.startswith("mx_testamento") for i in ids)
    quiet = service.run("today", {"as_of": TODAY, "facts": facts[:2]})["result"]
    assert not any(i["kind"] == "estate_gap" for i in quiet["today"] + quiet["upcoming"])


def test_you_page_summary_has_score_and_top_gap():
    rows = {"client.profile": {"residence": {"country": "MX"}}, "investment.gbm": GBM,
            **_d("investment.gbm", beneficiaries=[])}
    snapshot = _snapshot(rows)
    line = er.summary(build(snapshot, None, TODAY), snapshot, TODAY)
    assert line["score"] == 0 and line["top_gap"]["es"].startswith("Tu cuenta de GBM")
    assert proactive._estate_facts(snapshot)
    assert not proactive._estate_facts(_snapshot({"client.profile": {"residence": {"country": "MX"}}}))


def test_a_will_with_one_share_missing_keeps_the_stated_shares_and_flags_the_rest():
    report = _run({"cash.bbva": {"amount": 100000, "currency": "MXN", "institution": "BBVA"},
                   **_d("cash.bbva", beneficiaries=[]),
                   "estate.will": {"exists": True, "date": "2025-09-10",
                                   "heirs": [{"name": "Ana", "share": 0.75}, {"name": "Beto"}]}})
    row = _row(report, "cash.bbva")
    assert row["mechanism"] == "will"
    heirs = {h["name"]: h for h in row["heirs"]}
    assert heirs["Ana"]["share"] == 0.75 and heirs["Ana"]["amount"] == 75000
    assert heirs["Beto"]["share"] is None and heirs["Beto"]["share_range"] == {"low": 0, "high": 0.25}
    assert heirs["Beto"]["amount_range"] == {"low": 0, "high": 25000}
    assert any("Beto" in w and "not split equally" in w for w in report["warnings"])
    assert "estate.will.heirs[1].share" in report["missing"]
    assert any(q["code"] == "will_share_unknown" for q in report["result"]["questions"])
    assert report["status"] == "partial"
    # Stated shares that already reach 100% leave the unrecorded heir unknown, never equalized.
    full = _run({"cash.bbva": {"amount": 100000, "currency": "MXN", "institution": "BBVA"},
                 **_d("cash.bbva", beneficiaries=[]),
                 "estate.will": {"exists": True, "date": "2025-09-10",
                                 "heirs": [{"name": "Ana", "share": 1}, {"name": "Beto"}]}})
    heirs = {h["name"]: h for h in _row(full, "cash.bbva")["heirs"]}
    assert heirs["Ana"]["amount"] == 100000
    assert heirs["Beto"]["share"] is None and heirs["Beto"]["amount"] is None
