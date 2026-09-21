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
    b = store.remember("b", [fact("preference.risk", "high")], 0)
    assert a["facts"][0]["value"] == "low"
    assert b["facts"][0]["value"] == "high"
    with pytest.raises(IneligibleEvidenceError):
        store.save_decision("b", "Borrow", "Wrong client evidence", 1, [a["facts"][0]["id"]])
    with pytest.raises(ValidationError):
        store.delete_client("a", "b")
    store.delete_client("a", "a")
    with pytest.raises(ClientNotFoundError):
        store.snapshot("a")
    assert store.history("b", "preference.risk")[0]["value"] == "high"


def test_remember_is_atomic_cas_and_rejects_bool_revision(store):
    store.create_client("c", "Client")
    store.remember("c", [fact("client.profile", {"reporting_currency": "USD"})], 0)
    with pytest.raises(StaleRevisionError):
        store.remember("c", [fact("preference.risk", "medium")], 0)
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
    evidence_id = first["facts"][0]["id"]
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
    future = (TODAY + timedelta(days=1)).isoformat()
    invalid = [
        fact("x", math.nan),
        fact("x", {"nested": math.inf}),
        fact("x", 1, source=source(observed_on=date.fromisoformat(future))),
        fact("x", 1, source=source("document"), confidence="confirmed"),
        fact("x", 1, source=source("inference"), confidence="reported"),
        fact("plan.resources", {"currency": "USD"}),
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
    )["facts"][0]
    with pytest.raises(IneligibleEvidenceError):
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
    )["facts"]
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
        "c", "Wait", "Portfolio is empty", 1, [snapshot["facts"][0]["id"]]
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
        "c", "Hold reserve", "Liquidity constraint", 1, [first["facts"][0]["id"]], ["Invest"]
    )
    accepted = store.set_decision_status("c", decision["id"], "accepted", 1)
    repeated = store.set_decision_status("c", decision["id"], "accepted", 1)
    assert accepted["status"] == "accepted"
    assert repeated["updated_at"] == accepted["updated_at"]
    assert "executed" not in accepted and "execution" not in accepted
    assert store.snapshot("c")["client"]["revision"] == 1
    store.remember("c", [fact("constraint.liquidity", "medium")], 1)
    exported = store.export_client("c")
    assert exported["schema_version"] == 1
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
        "c", "Hold reserve", "Liquidity constraint", 1, [snapshot["facts"][0]["id"]]
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
            [fact("preference.one", 1), fact("portfolio.snapshot", {})],
            0,
        )
    assert store.snapshot("c")["facts"] == []
    assert store.snapshot("c")["client"]["revision"] == 0
