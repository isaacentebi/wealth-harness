"""The deferred memory step writes once, first time: saved keys up front, one batched call, idempotent writes."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

from wealth import agent
from wealth.store import ValidationError, WealthStore

TODAY = datetime.now(timezone.utc).date().isoformat()


def fact(key, value, **overrides):
    return {"key": key, "value": value, "source": {"kind": "user", "ref": "chat", "observed_on": TODAY},
            **overrides}


CAR = {"kind": "auto", "balance": 240000, "currency": "MXN", "payment": 7900, "payment_frequency": "monthly"}


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "w.sqlite3"
    with WealthStore(path) as store:
        store.create_client("ana", "Ana")
        store.remember("ana", [fact("liability.auto", CAR), fact("goals", [{"id": "casa", "name": "Casa"}])])
    return path


def test_a_repeated_write_is_a_no_op_not_an_error(db):
    with WealthStore(db) as store:
        again = store.remember("ana", [fact("liability.auto", dict(CAR), source={
            "kind": "user", "ref": "chat, retried", "observed_on": TODAY})])  # no merge, no revision
        assert again["unchanged"] == ["liability.auto"] and again["written"] == []
        assert store.snapshot("ana")["client"]["revision"] == 1
        merged = store.remember("ana", [fact("liability.auto", {"payment": 7900}, merge=True)])
        assert merged["unchanged"] == ["liability.auto"] and merged["client"]["revision"] == 1
        changed = store.remember("ana", [fact("liability.auto", {"payment": 7200}, merge=True),
                                         fact("goals", [{"id": "casa", "name": "Casa"}], merge=True)])
        assert [w["key"] for w in changed["written"]] == ["liability.auto"]
        assert changed["unchanged"] == ["goals"] and changed["client"]["revision"] == 2


def test_a_new_key_that_breaks_the_schema_names_the_saved_keys_of_its_kind(db):
    with WealthStore(db) as store, pytest.raises(ValidationError) as caught:
        store.remember("ana", [fact("liability.car", {"payment": 7200, "payment_frequency": "monthly"}, merge=True)])
    message = str(caught.value)
    assert "liability.car.kind is required" in message
    assert "saved liability keys: liability.auto" in message and "merge=true" in message


def test_merging_a_list_of_plain_values_replaces_it(db):
    thread = {"kind": "advice", "text": "No vender hoy.", "status": "open", "related": ["goals"]}
    with WealthStore(db) as store:
        store.remember("ana", [fact("thread.caida", thread)])
        store.remember("ana", [fact("thread.caida", {"related": ["investment.gbm", "goals"]}, merge=True)])
        saved = {f["key"]: f["value"] for f in store.snapshot("ana")["facts"]}
    assert saved["thread.caida"]["related"] == ["investment.gbm", "goals"]


def test_the_memory_prompt_lists_saved_keys_revisions_and_values(db):
    saved = agent.saved_facts_block(db, "ana")
    assert saved.splitlines()[0] == "client_revision: 1"
    assert "liability.auto (revision 1, user, reported): " in saved and '"payment":7900' in saved
    prompt = agent.build_memory_prompt("el carro es 7,200", "Gracias.", "ana", saved=saved)
    assert "<saved_facts>\nclient_revision: 1" in prompt
    assert agent.saved_facts_block(db.parent / "missing.sqlite3", "ana") is None
    assert agent.saved_facts_block(db, "nobody") is None


def test_memory_instructions_carry_the_schema_and_ask_for_one_batched_write():
    text = agent.memory_instructions().read_text(encoding="utf-8")
    assert "## Schema" in text and "liability.<id>: kind: auto|mortgage|card|personal|student|other" in text
    assert "one wealth_remember call with every fact" in text and "do\n  not read anything first" in text
    assert "wealth_context" not in agent.MEMORY_TOOLS


def test_the_step_sends_saved_facts_and_stops_once_a_write_is_stored(db, monkeypatch):
    seen = {}

    def fake(command, prompt, timeout, control=None, cwd=None):
        seen["prompt"] = prompt
        try:
            yield ("line", json.dumps({"type": "item.completed", "item": {
                "type": "mcp_tool_call", "server": "wealth", "tool": "wealth_remember", "status": "completed",
                "arguments": {"client_id": "ana", "facts": [{"key": "liability.auto"}]},
                "result": {"structured_content": {"written": [{"key": "liability.auto"}]}}}}))
            seen["kept_going"] = True
            yield ("line", json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}))
            yield ("exit", 0, "")
        finally:
            seen["closed"] = True

    monkeypatch.setattr(agent, "_stream_process", fake)
    keys = agent.remember_exchange("el carro es 7,200", "Gracias.", client_id="ana", db_path=db)
    assert keys == ["liability.auto"]
    assert "liability.auto (revision 1" in seen["prompt"]
    assert seen["closed"] and "kept_going" not in seen


def test_a_receipt_says_warnings_are_not_failures(db):
    pytest.importorskip("mcp")
    from wealth import server as server_module

    server = server_module.build_server(str(db))
    receipt = asyncio.run(server.call_tool("wealth_remember", {
        "client_id": "ana", "facts": [fact("liability.auto", dict(CAR))]})).structured_content
    assert receipt["unchanged"] == ["liability.auto"] and "do not resend" in receipt["next_step"]


def test_an_unknown_intent_names_it_and_where_the_fact_contract_is(tmp_path):
    pytest.importorskip("mcp")
    from mcp.server.mcpserver.exceptions import ToolError
    from wealth import server as server_module

    server = server_module.build_server(str(tmp_path / "w.sqlite3"))
    with pytest.raises(ToolError) as caught:
        asyncio.run(server.call_tool("wealth_context", {"intent": "fact_schema"}))
    message = str(caught.value)
    assert "unknown task 'fact_schema'" in message and ";;" not in message
    assert "fact contract" in message
    # The contract itself needs no client: intent=remember (or fact_contract) returns it without one.
    for intent in ("remember", "fact_contract"):
        contract = asyncio.run(server.call_tool("wealth_context", {"intent": intent})).structured_content
        assert contract["fact_contract"]["schema"]["estate.family"]
