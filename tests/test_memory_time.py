"""Valid time, contradictions, tombstones and patterns in the fact store."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from wealth.service import WealthService, dispatch
from wealth.store import SCHEMA_VERSION, ValidationError, WealthStore

TODAY = datetime.now(timezone.utc).date()
RECENT = (TODAY - timedelta(days=60)).isoformat()
STATEMENT_DAY = (TODAY - timedelta(days=2)).isoformat()


def src(kind="user", observed_on=None, ref="conversation:1"):
    return {"kind": kind, "ref": ref, "observed_on": observed_on or TODAY.isoformat()}


def salary(amount):
    return {"amount": amount, "currency": "MXN", "frequency": "monthly"}


def fact(key, value, kind="user", observed_on=None, **extra):
    ref = "statement:gbm-2026-09.pdf" if kind == "document" else "conversation:1"
    return {"key": key, "value": value, "source": src(kind, observed_on, ref), **extra}


def statement(amount, account_id="gbm-1", institution="GBM"):
    return fact(f"account.{account_id}", {
        "account": {"id": account_id, "institution": institution, "currency": "MXN", "type": "brokerage"},
        "positions": [{"symbol": "IVVPESO", "value": amount, "currency": "MXN"}],
        "as_of": STATEMENT_DAY,
    }, kind="document", observed_on=STATEMENT_DAY)


@pytest.fixture
def store(tmp_path):
    with WealthStore(tmp_path / "w.sqlite3") as opened:
        opened.create_client("c", "Client")
        yield opened


def current(store, key):
    return next((f for f in store.snapshot("c")["facts"] if f["key"] == key), None)


# ------------------------------------------------------------------ valid time


def test_new_value_closes_the_old_one_and_timeline_reads_naturally(store):
    store.remember("c", [fact("income.salary", salary(78000), observed_on="2025-01-10",
                              valid_from="2025-01-01")])
    receipt = store.remember("c", [fact("income.salary", salary(85000), valid_from="2026-03-01")], 1)
    assert receipt["written"][0]["action"] == "update"
    assert receipt["written"][0]["valid_from"] == "2026-03-01"

    old, new = store.history("c", "income.salary")
    assert (old["valid_from"], old["valid_to"], old["status"]) == ("2025-01-01", "2026-03-01", "active")
    assert (new["valid_from"], new["valid_to"]) == ("2026-03-01", None)
    assert old["value"]["amount"] == 78000  # closed, never erased

    timeline = store.timeline("c", "income.salary")
    assert timeline["text"] == "MXN 85,000 a month since 2026-03; MXN 78,000 a month from 2025-01 to 2026-03"
    assert [e["valid_from"] for e in timeline["entries"]] == ["2026-03-01", "2025-01-01"]

    history = WealthService(store.path).inspect("c", detail="history", key="income.salary")
    assert history["text"] == timeline["text"] and len(history["history"]) == 2


def test_valid_from_defaults_to_observation_and_rejects_the_future(store):
    receipt = store.remember("c", [fact("reserve", {"target_months": 6})])
    assert receipt["written"][0]["valid_from"] == TODAY.isoformat()
    with pytest.raises(ValidationError, match="valid_from must not be in the future"):
        store.remember("c", [fact("constraint.x", "y", valid_from=(TODAY + timedelta(days=5)).isoformat())])
    with pytest.raises(ValidationError, match="valid_from must be an ISO date"):
        store.remember("c", [fact("constraint.x", "y", valid_from="March")])


# ------------------------------------------------------------------ migration


def test_version_two_database_migrates_with_valid_time_and_tombstones(tmp_path):
    db = tmp_path / "v2.sqlite3"
    with WealthStore(db) as fresh:
        fresh.create_client("c", "Client")
    connection = sqlite3.connect(db)
    connection.executescript("""
        DROP INDEX facts_client_valid;
        DROP TABLE contradictions;
        ALTER TABLE facts DROP COLUMN status;
        ALTER TABLE facts DROP COLUMN valid_to;
        ALTER TABLE facts DROP COLUMN valid_from;
        UPDATE metadata SET value = '2' WHERE key = 'schema_version';
        UPDATE clients SET revision = 3 WHERE id = 'c';
    """)
    rows = [
        ("a", "income.salary", '{"amount":78000,"currency":"MXN","frequency":"monthly"}', "2025-01-10", 1),
        ("b", "income.salary", '{"amount":85000,"currency":"MXN","frequency":"monthly"}', "2026-03-05", 2),
        ("c1", "constraint.leverage", '"none"', "2025-06-01", 1),
        ("c2", "constraint.leverage", "null", "2026-04-01", 3),
    ]
    for fact_id, key, value, observed, revision in rows:
        connection.execute(
            "INSERT INTO facts(id, client_id, key, value_json, source_kind, source_ref, observed_on, "
            "confidence, expires_on, revision, recorded_at) VALUES (?, 'c', ?, ?, 'user', 'chat', ?, "
            "'reported', NULL, ?, '2026-01-01T00:00:00Z')",
            (fact_id, key, value, observed, revision),
        )
    connection.commit()
    connection.close()

    with WealthStore(db) as migrated:
        by_id = {f["id"]: f for key in ("income.salary", "constraint.leverage")
                 for f in migrated.history("c", key)}
        assert (by_id["a"]["valid_from"], by_id["a"]["valid_to"]) == ("2025-01-10", "2026-03-05")
        assert (by_id["b"]["valid_from"], by_id["b"]["valid_to"]) == ("2026-03-05", None)
        assert by_id["c1"]["valid_to"] == "2026-04-01"
        assert by_id["c2"]["status"] == "forgotten"
        assert [f["key"] for f in migrated.snapshot("c")["facts"]] == ["income.salary"]
        assert migrated.contradictions("c") == []
        assert migrated.export_client("c")["schema_version"] == SCHEMA_VERSION == 3
    connection = sqlite3.connect(db)
    assert connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0] == "3"
    indexes = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"facts_client_valid", "contradictions_client_status"} <= indexes
    connection.close()


# ------------------------------------------------------------------ contradictions


def test_evidence_never_overwrites_what_the_person_said(store):
    store.remember("c", [fact("income.salary", salary(85000), observed_on=RECENT)])
    receipt = store.remember("c", [fact("income.salary", salary(90000), kind="document")], 1)
    assert receipt["written"] == [] and receipt["write_result"]["resulting_revision"] == 1
    [held] = receipt["needs_user"]
    assert held["kind"] == "same_key" and held["key"] == held["proposed_key"] == "income.salary"
    assert held["current_value"]["amount"] == 85000 and held["proposed_value"]["amount"] == 90000
    assert held["sources"]["current"]["kind"] == "user" and held["sources"]["proposed"]["kind"] == "document"
    assert held["question"].startswith("You told me your salary is $85,000 a month")
    assert "income.salary" not in held["question"]
    assert "$90,000 a month" in held["question"] and held["choices"] == ["keep", "use_new", "changed"]
    assert any("never pick a side" in w for w in receipt["warnings"])
    assert current(store, "income.salary")["value"]["amount"] == 85000

    again = store.remember("c", [fact("income.salary", salary(90000), kind="document")])
    assert [c["id"] for c in again["needs_user"]] == [held["id"]]  # one question, not two
    assert [c["id"] for c in store.contradictions("c")] == [held["id"]]

    kept = store.resolve_contradiction("c", held["id"], "keep")
    assert kept["contradiction"]["status"] == "kept" and kept["written"] == []
    assert store.contradictions("c") == []
    quiet = store.remember("c", [fact("income.salary", salary(90000), kind="document")])
    assert quiet["needs_user"] == [] and "already chose to keep" in quiet["warnings"][0]
    with pytest.raises(ValidationError, match="already resolved"):
        store.resolve_contradiction("c", held["id"], "use_new")


def test_use_new_marks_the_old_value_corrected(store):
    store.remember("c", [fact("income.salary", salary(85000), observed_on=RECENT)])
    held = store.remember("c", [fact("income.salary", salary(90000), kind="document")])["needs_user"][0]
    result = store.resolve_contradiction("c", held["id"], "use_new")
    assert result["contradiction"]["status"] == "used_new"
    assert result["written"][0]["valid_from"] == RECENT  # it was always so
    old, new = store.history("c", "income.salary")
    assert old["status"] == "corrected" and old["valid_to"] == old["valid_from"]
    assert new["value"]["amount"] == 90000 and new["source"]["kind"] == "document"
    assert "MXN 85,000 a month (corrected)" in store.timeline("c", "income.salary")["text"]


def test_changed_keeps_both_values_in_turn(store):
    store.remember("c", [fact("income.salary", salary(85000), observed_on=RECENT)])
    held = store.remember("c", [fact("income.salary", salary(90000), kind="document")])["needs_user"][0]
    changed_on = (TODAY - timedelta(days=20)).isoformat()
    with pytest.raises(ValidationError, match="must not precede"):
        store.resolve_contradiction("c", held["id"], "changed", valid_from="2020-01-01")
    with pytest.raises(ValidationError, match="only to choice=changed"):
        store.resolve_contradiction("c", held["id"], "keep", valid_from=changed_on)
    result = store.resolve_contradiction("c", held["id"], "changed", valid_from=changed_on)
    assert result["contradiction"]["resolution"]["valid_from"] == changed_on
    old, new = store.history("c", "income.salary")
    assert (old["status"], old["valid_to"]) == ("active", changed_on)
    assert (new["valid_from"], new["valid_to"]) == (changed_on, None)


def test_statement_on_another_key_opens_the_same_kind_of_question(tmp_path):
    service = WealthService(tmp_path / "w.sqlite3")
    service.create("c", "Client")
    service.remember("c", [fact("investment.gbm", {"amount": 500000, "currency": "MXN", "institution": "GBM"},
                                observed_on=RECENT)])
    receipt = service.remember("c", [statement(612000)])
    assert [w["key"] for w in receipt["written"]] == ["account.gbm-1"]  # the statement itself is saved
    [held] = receipt["needs_user"]
    assert (held["kind"], held["key"], held["proposed_key"]) == ("stated_vs_statement", "investment.gbm",
                                                                 "account.gbm-1")
    assert held["proposed_value"]["statement"] == {"MXN": 612000}
    assert "at GBM" in held["question"] and "$612,000" in held["question"]
    assert "investment.gbm" not in held["question"]
    assert [c["id"] for c in service.situation("c")["contradictions"]] == [held["id"]]
    assert dispatch("contradictions", {"client_id": "c"}, service.db_path)["contradictions"][0]["id"] == held["id"]

    result = dispatch("resolve_contradiction", {"client_id": "c", "contradiction_id": held["id"],
                                                "choice": "changed"}, service.db_path)
    assert result["contradiction"]["resolution"]["valid_from"] == STATEMENT_DAY
    assert "investment.gbm" not in {f["key"] for f in service.inspect("c")["facts"]}
    stated, retired = service.inspect("c", detail="history", key="investment.gbm")["history"]
    assert stated["valid_to"] == STATEMENT_DAY and retired["status"] == "replaced"
    text = dispatch("history", {"client_id": "c", "key": "investment.gbm"}, service.db_path)["text"]
    assert text == f"replaced by a statement from {STATEMENT_DAY}; MXN 500,000 from {RECENT[:7]} to {STATEMENT_DAY[:7]}"


def test_uploaded_statement_asks_about_the_stated_balance(tmp_path, monkeypatch):
    from tests.fixtures.ingest import statements as fixtures
    from tests.test_situation import CANONICAL, _client
    from wealth.service import upload_dir

    monkeypatch.delenv("WEALTH_UPLOAD_DIR", raising=False)
    service = _client(tmp_path, CANONICAL)
    folder = upload_dir("ana", service.db_path)
    folder.mkdir(parents=True)
    (folder / "gbm.pdf").write_bytes(fixtures.gbm_multicurrency())
    proposal = service.ingest("ana", "file", {"path": "gbm.pdf"})
    saved = service.ingest("ana", "confirm", {"proposal_id": proposal["result"]["proposal_id"]})
    [held] = saved["result"]["needs_user"]
    assert (held["kind"], held["key"]) == ("stated_vs_statement", "investment.gbm")
    assert held["proposed_key"].startswith("account.")
    assert [c["id"] for c in service.contradictions("ana")["contradictions"]] == [held["id"]]


def test_statement_close_to_the_stated_figure_asks_nothing(store):
    store.remember("c", [fact("investment.gbm", {"amount": 600000, "currency": "MXN", "institution": "GBM",
                                                  "approximate": True}, observed_on=RECENT)])
    assert store.remember("c", [statement(612000)])["needs_user"] == []
    store.remember("c", [fact("cash.bbva", {"amount": 50000, "currency": "MXN", "institution": "BBVA"},
                              observed_on=RECENT)])
    held = store.remember("c", [statement(80000, "bbva-1", "BBVA")], 3)["needs_user"]
    assert held == []  # stated cash has no statement figure to compare in differences


def test_statement_use_new_retires_the_stated_balance(store):
    store.remember("c", [fact("investment.gbm", {"amount": 500000, "currency": "MXN", "institution": "GBM"},
                              observed_on=RECENT)])
    held = store.remember("c", [statement(612000)])["needs_user"][0]
    store.resolve_contradiction("c", held["id"], "use_new")
    stated, retired = store.history("c", "investment.gbm")
    assert stated["status"] == "corrected" and retired["status"] == "replaced"
    assert current(store, "investment.gbm") is None


def test_a_changed_fact_invalidates_its_open_question(store):
    store.remember("c", [fact("income.salary", salary(85000), observed_on=RECENT)])
    held = store.remember("c", [fact("income.salary", salary(90000), kind="document")])["needs_user"][0]
    store.remember("c", [fact("income.salary", salary(88000))], 1)
    with pytest.raises(ValidationError, match="changed after this question"):
        store.resolve_contradiction("c", held["id"], "keep")


# ------------------------------------------------------------------ inferred


def test_inferred_values_are_replaced_by_better_evidence(store):
    store.remember("c", [fact("spending.monthly", {"total": 30000, "currency": "MXN"}, kind="inference",
                              confidence="inferred")])
    with pytest.raises(ValidationError, match="already has a value"):
        store.remember("c", [fact("spending.monthly", {"total": 31000, "currency": "MXN"}, kind="inference",
                                  confidence="inferred")])
    receipt = store.remember("c", [fact("spending.monthly", {"total": 42000, "currency": "MXN"},
                                        kind="document")])
    assert receipt["needs_user"] == []
    assert receipt["written"][0]["action"] == "supersede"
    assert current(store, "spending.monthly")["value"]["total"] == 42000


# ------------------------------------------------------------------ tombstones


def test_forget_leaves_a_tombstone_and_the_value_is_not_used(tmp_path):
    service = WealthService(tmp_path / "w.sqlite3")
    service.create("c", "Client")
    service.remember("c", [fact("income.salary", salary(85000))])
    assert service.situation("c")["income"]["monthly"] == 85000
    receipt = service.remember("c", [fact("income.salary", None)], 1)
    assert receipt["written"][0]["action"] == "forget"
    assert service.inspect("c")["facts"] == []
    assert service.situation("c")["income"]["monthly"] is None
    assert service.run("spending", {}, client_id="c")["evidence_ids"] == []
    told, tomb = service.inspect("c", detail="history", key="income.salary")["history"]
    assert told["valid_to"] == TODAY.isoformat() and told["value"]["amount"] == 85000
    assert (tomb["status"], tomb["value"]) == ("forgotten", None)
    assert service.history("c", "income.salary")["text"].startswith(f"forgotten on {TODAY.isoformat()}; ")
    again = service.remember("c", [fact("income.salary", salary(90000))])  # a fresh key again
    assert again["written"][0]["action"] == "add"


# ------------------------------------------------------------------ patterns


def test_patterns_stay_inferred_and_apart_from_told_facts(tmp_path):
    service = WealthService(tmp_path / "w.sqlite3")
    service.create("c", "Client")
    mom = {"amount": 6000, "currency": "MXN", "frequency": "monthly", "to": "mamá"}
    with pytest.raises(ValidationError, match="inferred confidence"):
        service.remember("c", [fact("pattern.transfer_mom", mom, kind="pattern")])
    service.remember("c", [fact("pattern.transfer_mom", mom, kind="pattern", confidence="inferred"),
                           fact("income.salary", salary(85000))])
    sit = service.situation("c")
    assert [p["key"] for p in sit["patterns"]] == ["pattern.transfer_mom"]
    assert "pattern.transfer_mom" not in sit["meta"] and sit["inferred"] == []

    held = service.remember("c", [fact("income.salary", salary(80000), kind="pattern",
                                       confidence="inferred")])["needs_user"]
    assert held and held[0]["sources"]["proposed"]["kind"] == "pattern"

    confirmed = service.remember("c", [fact("pattern.transfer_mom", mom, confidence="confirmed")])
    assert confirmed["written"][0]["action"] == "supersede"
    sit = service.situation("c")
    assert sit["patterns"] == [] and "pattern.transfer_mom" in sit["meta"]


# ------------------------------------------------------------------ receipt


def test_receipt_shape_and_replay(store):
    store.remember("c", [fact("income.salary", salary(85000), observed_on=RECENT)])
    receipt = store.remember("c", [fact("income.salary", salary(90000), kind="document"),
                                   fact("reserve", {"target_months": 6}, kind="document")],
                             request_id="upload-1")
    assert set(receipt) == {"client", "written", "needs_user", "write_result", "warnings"}
    assert set(receipt["written"][0]) == {"key", "id", "confidence", "expires_on", "source_kind",
                                          "valid_from", "action"}
    assert set(receipt["needs_user"][0]) == {"id", "kind", "key", "proposed_key", "current_value",
                                             "proposed_value", "sources", "valid_from", "question", "choices",
                                             "status", "created_at", "resolved_at", "resolution"}
    assert [w["key"] for w in receipt["written"]] == ["reserve"]
    replay = store.remember("c", [fact("income.salary", salary(90000), kind="document"),
                                  fact("reserve", {"target_months": 6}, kind="document")],
                            request_id="upload-1")
    assert replay["write_result"]["replayed"] is True
    assert [c["id"] for c in replay["needs_user"]] == [c["id"] for c in receipt["needs_user"]]
    assert replay["written"] == receipt["written"]
    exported = store.export_client("c")
    assert exported["contradictions"][0]["status"] == "pending"
    assert {"valid_from", "valid_to", "status"} <= set(exported["facts"][0])
