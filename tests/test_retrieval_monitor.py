from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from wealth.monitor import evaluate
from wealth.recall import recall
from wealth.store import WealthStore


TODAY = datetime.now(timezone.utc).date()


def _snapshot(value=None, *, expires_on=None):
    facts = []
    if value is not None:
        facts.append(
            {
                "id": "thesis-fact",
                "key": "thesis.example",
                "value": value,
                "confidence": "reported",
                "expires_on": expires_on,
            }
        )
    return {"facts": facts, "decisions": []}


def test_thesis_monitor_preserves_baseline_across_stale_evidence():
    inputs = {
        "rules": [
            {"id": "thesis-watch", "kind": "thesis", "fact_key": "thesis.example"}
        ]
    }
    first = evaluate(
        _snapshot({"view": "old"}, expires_on=TODAY.isoformat()), {}, inputs
    )
    state = first.pop("state")
    assert first["result"]["events"] == []

    stale = evaluate(
        _snapshot(
            {"view": "new"},
            expires_on=(TODAY - timedelta(days=1)).isoformat(),
        ),
        state,
        inputs,
    )
    state = stale.pop("state")
    assert stale["result"]["checks"][0]["status"] == "unknown"
    assert stale["result"]["events"][0]["event"] == "review_needed"
    assert "value_hash" in state["thesis-watch"]

    recovered = evaluate(
        _snapshot(
            {"view": "new"},
            expires_on=(TODAY + timedelta(days=1)).isoformat(),
        ),
        state,
        inputs,
    )
    assert recovered["result"]["checks"][0]["status"] == "active"
    assert recovered["result"]["events"][0]["event"] == "review_needed"


def test_monitor_does_not_call_clear_rule_edit_a_resolution():
    snapshot = {
        "facts": [
            {
                "key": "metric",
                "value": 1,
                "confidence": "reported",
                "expires_on": None,
            }
        ],
        "decisions": [],
    }
    first = evaluate(
        snapshot,
        {},
        {"rules": [{"id": "limit", "kind": "threshold", "fact_key": "metric", "op": "gt", "value": 10}]},
    )
    state = first.pop("state")
    edited = evaluate(
        snapshot,
        state,
        {"rules": [{"id": "limit", "kind": "threshold", "fact_key": "metric", "op": "gt", "value": 20}]},
    )
    assert edited["result"]["checks"][0]["status"] == "clear"
    assert edited["result"]["events"] == []


def test_recall_fills_limit_from_candidates_after_oversized_matches():
    observed = TODAY.isoformat()

    def fact(index, large):
        return {
            "id": f"{index:02}",
            "key": f"goal.{index}",
            "value": "goal " + ("x" * 3000 if large else "small"),
            "source": {"kind": "user", "ref": "test", "observed_on": observed},
            "confidence": "reported",
            "expires_on": None,
        }

    snapshot = {
        "client": {"id": "client", "revision": 1},
        "facts": [fact(index, index < 12) for index in range(13)],
        "decisions": [],
    }
    result = recall(snapshot, "goal", limit=12, max_chars=3000)
    assert [item["id"] for item in result["matches"]] == ["00", "12"]
    assert result["characters"] <= result["max_chars"]


def test_auxiliary_read_does_not_create_state_but_atomic_update_does(tmp_path):
    database = tmp_path / "wealth.sqlite3"
    with WealthStore(database) as store:
        store.create_client("client", "Client")
        assert store.auxiliary("client", "embeddings") == {}

        connection = sqlite3.connect(database)
        assert connection.execute("SELECT COUNT(*) FROM auxiliary").fetchone()[0] == 0
        connection.close()

        stored = store.update_auxiliary(
            "client", "embeddings", lambda old: {**old, "fact": {"vector": [1.0]}}
        )
        assert stored == {"fact": {"vector": [1.0]}}
        assert store.auxiliary("client", "embeddings") == stored

        connection = sqlite3.connect(database)
        assert connection.execute("SELECT COUNT(*) FROM auxiliary").fetchone()[0] == 1
        connection.close()


def test_saved_calculation_cites_only_consulted_memory(tmp_path):
    from examples.returning_client import example_facts
    from wealth.service import WealthService

    service = WealthService(tmp_path / 'provenance.sqlite3')
    service.create('client', 'Client')
    facts = example_facts()
    facts.append({'key': 'thesis.unrelated', 'value': 'Unrelated company case',
                  'source': {'kind': 'user', 'ref': 'fixture', 'observed_on': TODAY.isoformat()},
                  'expires_on': (TODAY + timedelta(days=1)).isoformat()})
    snapshot = service.remember('client', facts, 0)
    unrelated = next(f['id'] for f in snapshot['facts'] if f['key'] == 'thesis.unrelated')
    report = service.run('plan', client_id='client', save_as='analysis.plan',
                         expires_on=(TODAY + timedelta(days=30)).isoformat())
    assert unrelated not in report['evidence_ids']
    assert report['saved']['expires_on'] != (TODAY + timedelta(days=1)).isoformat()
    assert report['evidence_ids']
