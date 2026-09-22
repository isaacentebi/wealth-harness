"""Legal-audit regressions for the estate register: intestate orders with unknown relatives, UPC 2-102(2),
AFORE survivors' pensions, LMV Art. 201, 457(b) defaults, concubinato, patria potestad and gap ranking."""
from __future__ import annotations

import pytest

from wealth import estate_register as er
from wealth.situation import build
from wealth.situation.schema import SchemaError, designation_key, validate

TODAY = "2026-09-22"
GBM = {"amount": 217000, "currency": "MXN", "institution": "GBM", "kind": "brokerage"}
US_PROFILE = {"residence": {"country": "US"}, "us_person": True, "reporting_currency": "USD"}
KIDS = [{"name": "Sofía", "birth_year": 2017}, {"name": "Mateo", "birth_year": 2021}]


def _d(account, **fields):
    return {designation_key(account): {"account": account, **fields}}


def _snapshot(rows, observed=None):
    observed = observed or {}
    return {"client": {"id": None, "revision": None}, "facts": [
        {"id": f"f:{k}", "key": k, "value": v, "confidence": "reported", "status": "active",
         "source": {"kind": "user", "ref": "test", "observed_on": observed.get(k, TODAY)}}
        for k, v in rows.items() if v is not None], "decisions": []}


def _run(rows, profile=None, snapshot=None):
    rows = {"client.profile": profile or {"residence": {"country": "MX"}, "us_person": False}, **rows}
    snapshot = snapshot or _snapshot(rows)
    return er.register(build(snapshot, None, TODAY), snapshot, {}, TODAY)


def _row(report, key):
    return next(r for r in report["result"]["rows"] if r["key"] == key)


def _amounts(row):
    return {h["name"]: h["amount"] if h["amount"] is not None else (h["amount_range"]["low"], h["amount_range"]["high"])
            for h in row["heirs"]}


def _mx_spouse_no_children(**family):
    return _run({"investment.gbm": GBM, **_d("investment.gbm", beneficiaries=[]), "estate.will": {"exists": False},
                 "estate.family": {"marital_status": "married", "spouse": "Laura", "children": [],
                                   "marital_regime": "separacion_de_bienes", **family}})


def _us_spouse_no_children(amount, **family):
    return _run({"investment.schwab": {"amount": amount, "currency": "USD", "institution": "Schwab",
                                       "kind": "brokerage"},
                 **_d("investment.schwab", beneficiaries=[], country="US"), "estate.will": {"exists": False},
                 "estate.family": {"marital_status": "married", "spouse": "Dana", "children": [],
                                   "marital_regime": "separate_property", **family}}, profile=US_PROFILE)


# ------------------------------------------------------------------ E1: unknown parents or siblings are not "none"


def test_mx_unknown_parents_give_the_spouse_a_range_and_a_question():
    report = _mx_spouse_no_children()
    amounts = _amounts(_row(report, "investment.gbm"))
    assert amounts["Laura"] == (108500, 217000)  # CCF 1626 (half) up to 1629 (all)
    assert amounts["padres"] == (0, 108500)
    assert amounts["hermanos"] == (0, 72333.33)  # CCF 1627: a third when no parent survives
    assert "estate.family.parents_living" in report["missing"]
    assert "estate.family.siblings_living" in report["missing"]
    assert {q["code"] for q in report["result"]["questions"]} >= {"parents_unknown", "siblings_unknown"}
    assert report["status"] == "partial"


def test_mx_spouse_with_parents_or_siblings_follows_ccf_1626_1627_1629():
    assert _amounts(_row(_mx_spouse_no_children(parents_living=1), "investment.gbm")) == {
        "Laura": 108500, "padres": 108500}
    report = _mx_spouse_no_children(parents_living=0, siblings_living=2)
    assert _amounts(_row(report, "investment.gbm")) == {"Laura": 144666.67, "hermanos": 72333.33}
    assert "1627" in _row(report, "investment.gbm")["rule"]
    report = _mx_spouse_no_children(parents_living=0, siblings_living=0)
    assert _amounts(_row(report, "investment.gbm")) == {"Laura": 217000}
    assert report["missing"] == []
    # Only siblings unknown: two thirds to all.
    report = _mx_spouse_no_children(parents_living=0)
    assert _amounts(_row(report, "investment.gbm"))["Laura"] == (144666.67, 217000)
    assert report["missing"] == ["estate.family.siblings_living"]


def test_us_unknown_parents_give_a_range_not_spouse_takes_all():
    report = _us_spouse_no_children(500000)
    assert _amounts(_row(report, "investment.schwab"))["Dana"] == (450000, 500000)
    assert "estate.family.parents_living" in report["missing"]


def test_schema_accepts_siblings_living():
    validate("estate.family", {"marital_status": "married", "siblings_living": 3})
    with pytest.raises(SchemaError):
        validate("estate.family", {"siblings_living": -1})


# ------------------------------------------------------------------ E2: UPC 2-102(2)


def test_upc_spouse_and_parent_takes_300k_plus_three_quarters():
    row = _row(_us_spouse_no_children(500000, parents_living=1), "investment.schwab")
    assert _amounts(row) == {"Dana": 450000, "parents": 50000}
    assert "300,000" in row["rule"]
    # Below $300,000 the spouse takes all even next to a parent.
    assert _amounts(_row(_us_spouse_no_children(200000, parents_living=2), "investment.schwab"))["Dana"] == 200000


# ------------------------------------------------------------------ E8: AFORE survivors' pension


def test_afore_balance_may_fund_a_survivors_pension_instead_of_cash():
    family = {"marital_status": "married", "spouse": "Laura", "children": KIDS, "parents_living": 2,
              "marital_regime": "sociedad_conyugal"}
    report = _run({"investment.afore": {"amount": 400000, "currency": "MXN", "institution": "Afore XXI",
                                        "kind": "afore"}, **_d("investment.afore", beneficiaries=[]),
                   "investment.afore2": {"amount": 100000, "currency": "MXN", "institution": "Afore Sura",
                                         "kind": "afore"},
                   **_d("investment.afore2", beneficiaries=[{"name": "Pedro", "relationship": "sibling", "share": 1}],
                        designation_date="2024-01-01"),
                   "estate.will": {"exists": True, "date": "2024-01-01"}, "estate.family": family})
    for key, total in (("investment.afore", 400000), ("investment.afore2", 100000)):
        row = _row(report, key)
        assert row["pension_possible"] is True and row["lump_sum_range"] == {"low": 0, "high": total}
        assert all(h["amount"] is None and h["amount_range"]["low"] == 0 for h in row["heirs"])
        assert any("LSS Arts. 64 and 193" in n["en"] for n in row["notes"])
    assert _amounts(_row(report, "investment.afore2")) == {"Pedro": (0, 100000)}
    # A single person with adult children and no parents: nobody could draw the pension, the balance is paid out.
    report = _run({"investment.afore": {"amount": 400000, "currency": "MXN", "institution": "Afore XXI",
                                        "kind": "afore"},
                   **_d("investment.afore", beneficiaries=[{"name": "Ana", "relationship": "child", "share": 1}],
                        designation_date="2025-01-01"),
                   "estate.will": {"exists": True, "date": "2025-01-01"},
                   "estate.family": {"marital_status": "single", "children": [{"name": "Ana", "birth_year": 1980}],
                                     "parents_living": 0}})
    assert _amounts(_row(report, "investment.afore")) == {"Ana": 400000}


# ------------------------------------------------------------------ nits


def test_lmv_201_is_verified_and_only_labels_casas_de_bolsa():
    assert er.SOURCES["lmv_201"]["status"] == "verified"
    report = _run({"investment.gbm": GBM,
                   **_d("investment.gbm", beneficiaries=[{"name": "Ana", "share": 1}], designation_date="2025-01-01"),
                   "investment.ppr": {"amount": 50000, "currency": "MXN", "institution": "Allianz", "kind": "ppr"},
                   **_d("investment.ppr", beneficiaries=[{"name": "Ana", "share": 1}], designation_date="2025-01-01"),
                   "estate.will": {"exists": True, "date": "2025-01-01"}})
    assert "LMV Art. 201" in _row(report, "investment.gbm")["mechanism_label"]["en"]
    assert "LMV" not in _row(report, "investment.ppr")["mechanism_label"]["en"]


def test_governmental_457b_without_beneficiaries_follows_the_plan_default():
    report = _run({"investment.gov457": {"amount": 50000, "currency": "USD", "institution": "State plan",
                                         "kind": "retirement", "plan_type": "457b"},
                   **_d("investment.gov457", beneficiaries=[]), "estate.will": {"exists": True, "date": "2025-01-01"},
                   "estate.family": {"marital_status": "married", "spouse": "Dana", "children": [],
                                     "marital_regime": "community_property", "parents_living": 0}},
                  profile=US_PROFILE)
    row = _row(report, "investment.gov457")
    assert row["mechanism"] == "plan_default" and _amounts(row) == {"Dana": 50000}
    assert "457(b)" in row["rule"]


def test_free_union_asks_no_regime_and_explains_concubinato():
    report = _run({"cash.bbva": {"amount": 100000, "currency": "MXN", "institution": "BBVA"},
                   **_d("cash.bbva", designation_date="2025-01-10",
                        beneficiaries=[{"name": "Laura", "relationship": "spouse", "share": 0.5},
                                       {"name": "Sofía", "relationship": "child", "share": 0.5}]),
                   "estate.will": {"exists": False},
                   "estate.family": {"marital_status": "free_union", "spouse": "Laura",
                                     "children": [{"name": "Sofía", "birth_year": 2017}]}})
    assert "estate.family.marital_regime" not in report["missing"]
    assert any("1635" in a and "five years" in a for a in report["assumptions"])
    # A minor named directly while the other parent lives (patria potestad, CCF 425): low severity, ranked last.
    minor = next(g for g in report["result"]["gaps"] if g["code"] == "minor_direct")
    assert minor["severity"] == "low" and "425" in minor["note"]
    assert minor["rank"] == len(report["result"]["gaps"])


def test_unknown_amount_gaps_rank_by_the_last_stated_balance():
    rows = {"client.profile": {"residence": {"country": "MX"}, "us_person": False},
            "investment.gbm": GBM, **_d("investment.gbm", beneficiaries=[]),
            "investment.old": {"amount": 500000, "currency": "MXN", "institution": "Actinver"},
            **_d("investment.old", beneficiaries=[]), "estate.will": {"exists": True, "date": "2024-01-01"}}
    snapshot = _snapshot(rows, observed={"investment.old": "2023-01-01"})
    for fact in snapshot["facts"]:
        if fact["key"] == "investment.old":
            fact["expires_on"] = "2023-07-01"
    gaps = _run(rows, snapshot=snapshot)["result"]["gaps"]
    assert gaps[0]["key"] == "investment.old" and gaps[0]["amount_at_risk"] is None
    assert gaps[0]["amount_estimate"] == 500000 and "last stated" in gaps[0]["en"]
