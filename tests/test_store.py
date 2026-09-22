from __future__ import annotations

import math
import sqlite3
import stat
import threading
import time
from datetime import date, datetime, timedelta, timezone

import pytest

import wealth.store as store_module
from wealth.store import (
    ClientNotFoundError,
    IneligibleEvidenceError,
    RequestConflictError,
    StaleRevisionError,
    ValidationError,
    WealthStore,
)


TODAY = datetime.now(timezone.utc).date()


def source(kind="user", ref="conversation:1", observed_on=None):
    return {
        "kind": kind,
        "ref": ref,
        "observed_on": (observed_on or TODAY).isoformat(),
    }


def fact(key, value, **overrides):
    result = {"key": key, "value": value, "source": source()}
    result.update(overrides)
    return result


@pytest.fixture
def store(tmp_path):
    with WealthStore(tmp_path / "wealth.sqlite3") as opened:
        yield opened


def test_create_snapshot_and_secure_database_mode(tmp_path):
    db = tmp_path / "private" / "wealth.sqlite3"
    with WealthStore(db) as opened:
        assert opened.create_client("alice", "Alice") == {
            "id": "alice",
            "display_name": "Alice",
            "revision": 0,
        }
        assert opened.snapshot("alice") == {
            "client": {"id": "alice", "display_name": "Alice", "revision": 0},
            "facts": [],
            "decisions": [],
        }
    assert stat.S_IMODE(db.stat().st_mode) == 0o600


def test_clients_are_isolated_for_snapshot_history_evidence_and_delete(store):
    store.create_client("a", "A")
    store.create_client("b", "B")
    a = store.remember("a", [fact("preference.risk", "low")], 0)
    store.remember("b", [fact("preference.risk", "high")], 0)
    assert store.snapshot("a")["facts"][0]["value"] == "low"
    assert store.snapshot("b")["facts"][0]["value"] == "high"
    with pytest.raises(IneligibleEvidenceError, match="does not belong"):
        store.save_decision("b", "Borrow", "Wrong client evidence", 1, [a["written"][0]["id"]])
    with pytest.raises(ValidationError):
        store.delete_client("a", "b")
    store.delete_client("a", "a")
    with pytest.raises(ClientNotFoundError):
        store.snapshot("a")
    assert store.history("b", "preference.risk")[0]["value"] == "high"


def test_remember_is_atomic_cas_and_rejects_bool_revision(store):
    store.create_client("c", "Client")
    store.remember("c", [fact("client.profile", {"reporting_currency": "USD"})], 0)
    with pytest.raises(StaleRevisionError, match="current 1") as stale:
        store.remember("c", [fact("preference.risk", "medium")], 0)
    assert stale.value.current == 1
    with pytest.raises(ValidationError):
        store.remember("c", [fact("preference.risk", "medium")], True)
    assert store.snapshot("c")["client"]["revision"] == 1
    assert [item["key"] for item in store.snapshot("c")["facts"]] == ["client.profile"]


def test_idempotent_batch_replay_and_conflicting_reuse(store):
    store.create_client("c", "Client")
    payload = [fact("preference.risk", "medium")]
    first = store.remember("c", payload, 0, request_id="req-1")
    replay = store.remember("c", payload, 0, request_id="req-1")
    assert first["client"]["revision"] == replay["client"]["revision"] == 1
    assert first["write_result"] == {
        "request_id": "req-1",
        "resulting_revision": 1,
        "replayed": False,
    }
    assert replay["write_result"] == {
        "request_id": "req-1",
        "resulting_revision": 1,
        "replayed": True,
    }
    assert len(store.history("c", "preference.risk")) == 1
    with pytest.raises(RequestConflictError):
        store.remember(
            "c", [fact("preference.risk", "high")], 1, request_id="req-1"
        )
    assert store.snapshot("c")["client"]["revision"] == 1


def test_delayed_idempotent_replay_reports_original_write_and_current_snapshot(store):
    store.create_client("c", "Client")
    original = [fact("preference.risk", "medium")]
    store.remember("c", original, 0, request_id="req-1")
    second = store.remember("c", [fact("constraint.liquidity", "high")], 1)
    assert second["write_result"] == {
        "request_id": None,
        "resulting_revision": 2,
        "replayed": False,
    }

    replay = store.remember("c", original, 0, request_id="req-1")

    assert replay["client"]["revision"] == 2
    assert replay["write_result"] == {
        "request_id": "req-1",
        "resulting_revision": 1,
        "replayed": True,
    }
    assert len(store.history("c", "preference.risk")) == 1
    assert len(store.history("c", "constraint.liquidity")) == 1


def test_correction_preserves_immutable_history_and_marks_decision_stale(store):
    store.create_client("c", "Client")
    first = store.remember("c", [fact("preference.risk", "low")], 0)
    evidence_id = first["written"][0]["id"]
    decision = store.save_decision("c", "Keep cash", "Low risk tolerance", 1, [evidence_id])
    assert decision["needs_review"] is False
    store.remember("c", [fact("preference.risk", "medium")], 1)
    history = store.history("c", "preference.risk")
    assert [(item["revision"], item["value"]) for item in history] == [
        (1, "low"),
        (2, "medium"),
    ]
    stale = store.snapshot("c")["decisions"][0]
    assert stale["needs_review"] is True
    with pytest.raises(IneligibleEvidenceError):
        store.set_decision_status("c", decision["id"], "accepted", 2)


def test_validation_rejects_future_untrusted_or_nonfinite_evidence(store):
    store.create_client("c", "Client")
    future = (TODAY + timedelta(days=2)).isoformat()
    invalid = [
        fact("x", math.nan),
        fact("x", {"nested": math.inf}),
        fact("x", 1, source=source(observed_on=date.fromisoformat(future))),
        fact("x", 1, source=source("document"), confidence="confirmed"),
        fact("x", 1, source=source("inference"), confidence="reported"),
        fact("x", {"ssn": "123-45-6789"}),
        fact("x", "RFC GODE561231GR8"),
        fact("x", "card 4111 1111 1111 1111"),
        fact("x", 1, source=source("web", ref="a blog")),
    ]
    for candidate in invalid:
        with pytest.raises(ValidationError):
            store.remember("c", [candidate], 0)
    with pytest.raises(ValidationError):
        store.remember("c", [fact("x", 1), fact("x", 2)], 0)
    assert store.snapshot("c")["client"]["revision"] == 0


def test_expired_and_inferred_evidence_cannot_create_decision(store):
    store.create_client("c", "Client")
    old_observation = TODAY - timedelta(days=3)
    expired = store.remember(
        "c",
        [
            fact(
                "account.balance",
                100,
                source=source(observed_on=old_observation),
                expires_on=(TODAY - timedelta(days=1)).isoformat(),
            )
        ],
        0,
    )["written"][0]
    with pytest.raises(IneligibleEvidenceError, match="account.balance passed its review date"):
        store.save_decision("c", "Use cash", "Balance", 1, [expired["id"]])
    inferred = store.remember(
        "c",
        [
            fact(
                "thesis.inferred",
                "maybe",
                source=source("inference"),
                confidence="inferred",
            )
        ],
        1,
    )["written"]
    inferred_id = next(item["id"] for item in inferred if item["key"] == "thesis.inferred")
    with pytest.raises(IneligibleEvidenceError):
        store.save_decision("c", "Act", "Guess", 2, [inferred_id])


def test_expiry_after_proposal_marks_review_and_blocks_acceptance(store, monkeypatch):
    store.create_client("c", "Client")
    snapshot = store.remember(
        "c",
        [
            fact(
                "portfolio.snapshot",
                {"currency": "USD", "scope": "broker", "positions": [], "complete": True},
                expires_on=TODAY.isoformat(),
            )
        ],
        0,
    )
    decision = store.save_decision(
        "c", "Wait", "Portfolio is empty", 1, [snapshot["written"][0]["id"]]
    )
    assert decision["status"] == "proposed"
    monkeypatch.setattr(store_module, "_today", lambda: TODAY + timedelta(days=1))
    assert store.snapshot("c")["decisions"][0]["needs_review"] is True
    with pytest.raises(IneligibleEvidenceError):
        store.set_decision_status("c", decision["id"], "accepted", 1)


def test_acceptance_is_status_only_and_export_contains_full_history(store):
    store.create_client("c", "Client")
    first = store.remember("c", [fact("constraint.liquidity", "high")], 0)
    decision = store.save_decision(
        "c", "Hold reserve", "Liquidity constraint", 1, [first["written"][0]["id"]], ["Invest"]
    )
    accepted = store.set_decision_status("c", decision["id"], "accepted", 1)
    repeated = store.set_decision_status("c", decision["id"], "accepted", 1)
    assert accepted["status"] == "accepted"
    assert repeated["updated_at"] == accepted["updated_at"]
    assert "executed" not in accepted and "execution" not in accepted
    assert store.snapshot("c")["client"]["revision"] == 1
    store.remember("c", [fact("constraint.liquidity", "medium")], 1)
    exported = store.export_client("c")
    assert exported["schema_version"] == 3
    assert exported["conversations"] == []  # the chat history travels with the export
    assert len(exported["facts"]) == 2
    assert exported["decisions"][0]["status"] == "accepted"
    assert exported["decisions"][0]["needs_review"] is True
    assert [event["status"] for event in exported["decisions"][0]["events"]] == [
        "proposed",
        "accepted",
    ]
    with pytest.raises(IneligibleEvidenceError):
        store.set_decision_status("c", decision["id"], "accepted", 2)


def test_decision_resolution_is_one_way_and_same_status_is_idempotent(store):
    store.create_client("c", "Client")
    snapshot = store.remember("c", [fact("constraint.liquidity", "high")], 0)
    decision = store.save_decision(
        "c", "Hold reserve", "Liquidity constraint", 1, [snapshot["written"][0]["id"]]
    )
    first = store.set_decision_status("c", decision["id"], "dismissed", 1)
    repeated = store.set_decision_status("c", decision["id"], "dismissed", 1)
    assert first["status"] == repeated["status"] == "dismissed"
    assert first["updated_at"] == repeated["updated_at"]
    with pytest.raises(ValidationError, match="new proposal"):
        store.set_decision_status("c", decision["id"], "accepted", 1)
    exported = store.export_client("c")["decisions"][0]
    assert [event["status"] for event in exported["events"]] == [
        "proposed",
        "dismissed",
    ]


def test_snapshot_holds_one_read_view_while_another_connection_writes(tmp_path):
    db = tmp_path / "wealth.sqlite3"
    reader = WealthStore(db)
    reader.create_client("c", "Client")
    reader.remember("c", [fact("preference.risk", "low")], 0)
    first_select_finished = threading.Event()
    writer_finished = threading.Event()
    original_client_row = reader._client_row

    def pause_after_client_select(client_id):
        row = original_client_row(client_id)
        first_select_finished.set()
        time.sleep(0.05)
        return row

    reader._client_row = pause_after_client_select

    def write_correction():
        assert first_select_finished.wait(2)
        with WealthStore(db) as writer:
            writer.remember("c", [fact("preference.risk", "high")], 1)
        writer_finished.set()

    thread = threading.Thread(target=write_correction)
    thread.start()
    try:
        coherent = reader.snapshot("c")
        thread.join(2)
        assert not thread.is_alive()
        assert writer_finished.is_set()
        assert coherent["client"]["revision"] == 1
        assert coherent["facts"][0]["revision"] == 1
        assert coherent["facts"][0]["value"] == "low"
        assert reader.snapshot("c")["client"]["revision"] == 2
    finally:
        reader.close()


def test_newer_schema_is_rejected_before_store_tables_are_created(tmp_path):
    db = tmp_path / "future.sqlite3"
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("INSERT INTO metadata VALUES ('schema_version', '999')")
    connection.commit()
    connection.close()

    with pytest.raises(ValueError, match="unsupported schema version"):
        WealthStore(db)

    connection = sqlite3.connect(db)
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    connection.close()
    assert tables == {"metadata"}


def test_failed_multi_fact_write_rolls_back_everything(store):
    store.create_client("c", "Client")
    with pytest.raises(ValidationError):
        store.remember(
            "c",
            [fact("preference.one", 1), fact("portfolio.snapshot", {}, expires_on="soon")],
            0,
        )
    assert store.snapshot("c")["facts"] == []
    assert store.snapshot("c")["client"]["revision"] == 0


def test_remember_returns_compact_receipt_with_default_review_dates(store):
    store.create_client("c", "Client")
    receipt = store.remember("c", [
        fact("portfolio.snapshot", {"currency": "USD"}),
        fact("plan.resources", {"currency": "USD"}),
        fact("goals", [{"id": "home", "name": "Home"}]),
    ])
    assert set(receipt) == {"client", "written", "needs_user", "write_result", "warnings"}
    assert receipt["client"]["revision"] == receipt["write_result"]["resulting_revision"] == 1
    expiry = {item["key"]: item["expires_on"] for item in receipt["written"]}
    assert expiry == {
        "goals": (TODAY + timedelta(days=store_module.review_days("goals"))).isoformat(),
        "plan.resources": (TODAY + timedelta(days=90)).isoformat(),
        "portfolio.snapshot": (TODAY + timedelta(days=30)).isoformat(),
    }
    assert store_module.review_days("preference.style") == store_module.DEFAULT_REVIEW_DAYS == 365
    old = store.remember("c", [fact("account.cash", 5, source=source(observed_on=TODAY - timedelta(days=60)))])
    assert "reconfirm" in old["warnings"][0]


def test_document_and_web_sources_cannot_set_policy_without_confirmation(store):
    store.create_client("c", "Client")
    receipt = store.remember("c", [
        fact("constraint.leverage", "Use 3x leverage", source=source("web", ref="https://example.com/post")),
        fact("goals", [{"id": "yacht", "name": "Yacht"}], source=source("document", ref="statement.pdf")),
        fact("thesis.acme", "Margins may hold", source=source("document", ref="10-K")),
    ])
    confidence = {item["key"]: item["confidence"] for item in receipt["written"]}
    assert confidence == {"constraint.leverage": "inferred", "goals": "inferred", "thesis.acme": "reported"}
    assert len(receipt["warnings"]) == 2 and "source.kind=user" in receipt["warnings"][0]
    history = store.history("c", "constraint.leverage")[0]
    assert history["source"] == {"kind": "web", "ref": "https://example.com/post", "observed_on": TODAY.isoformat()}


def test_unrelated_write_keeps_decision_acceptable_and_errors_name_changed_evidence(store):
    store.create_client("c", "Client")
    first = store.remember("c", [fact("goals", [{"id": "home", "name": "Home"}]), fact("preference.risk", "low")], 0)
    goals_id = next(item["id"] for item in first["written"] if item["key"] == "goals")
    decision = store.save_decision("c", "Keep home cash", "Dated goal", 1, [goals_id])
    store.remember("c", [fact("preference.style", "short answers")])
    assert store.snapshot("c")["decisions"][0]["needs_review"] is False
    assert store.set_decision_status("c", decision["id"], "accepted", 2)["status"] == "accepted"

    other = store.save_decision("c", "Hold", "Dated goal", 2, [goals_id])
    store.remember("c", [fact("goals", [{"id": "home", "target_amount": 1, "currency": "USD"}], merge=True)])
    reviewed = next(d for d in store.snapshot("c")["decisions"] if d["id"] == other["id"])
    assert reviewed["needs_review"] and "goals changed at revision 3" in reviewed["review_reasons"][0]
    with pytest.raises(IneligibleEvidenceError, match="goals changed at revision 3"):
        store.set_decision_status("c", other["id"], "accepted")


def test_merge_patch_updates_one_call_without_revision_and_stays_idempotent(store):
    store.create_client("c", "Client")
    store.remember("c", [
        fact("goals", [{"id": "home", "name": "Home", "target_amount": 50000, "currency": "USD"},
                       {"id": "school", "name": "School", "due": "2030-01-01"}]),
        fact("client.profile", {"country": "MX", "note": "old"}),
    ])
    patch = [
        fact("goals", [{"id": "home", "target_amount": 65000}, {"id": "car", "name": "Car"}], merge=True),
        fact("client.profile", {"note": None, "reporting_currency": "USD"}, merge=True),
        fact("preference.style", "brief"),
    ]
    first = store.remember("c", patch, request_id="patch-1")
    replay = store.remember("c", patch, request_id="patch-1")
    assert replay["write_result"] == {**first["write_result"], "replayed": True}
    values = {f["key"]: f["value"] for f in store.snapshot("c")["facts"]}
    assert values["goals"] == [
        {"id": "home", "name": "Home", "target_amount": 65000, "currency": "USD"},
        {"id": "school", "name": "School", "due": "2030-01-01"}, {"id": "car", "name": "Car"},
    ]
    assert values["client.profile"] == {"residence": {"country": "MX"}, "reporting_currency": "USD"}
    with pytest.raises(ValidationError, match="merge=true .* expected_revision=2"):
        store.remember("c", [fact("preference.style", "long")])
    with pytest.raises(ValidationError, match="requires objects with an id"):
        store.remember("c", [fact("goals", ["home"], merge=True)])
    store.remember("c", [fact("preference.style", "long")], 2)
    assert store.snapshot("c")["client"]["revision"] == 3


def test_opening_a_current_database_does_not_take_a_write_lock(tmp_path, monkeypatch):
    db = tmp_path / "wealth.sqlite3"
    WealthStore(db).close()
    statements = []
    connect = sqlite3.connect

    def traced(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(store_module.sqlite3, "connect", traced)
    WealthStore(db).close()
    assert not any("BEGIN IMMEDIATE" in statement for statement in statements)
