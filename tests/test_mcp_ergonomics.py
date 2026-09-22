"""What a foreign MCP host sees first: compact discovery, bounded instructions, honest annotations."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402

from wealth import server as server_module  # noqa: E402
from wealth.behavior import ASSISTANT_CONTRACT, HOST_CONTRACT  # noqa: E402
from wealth.catalog import CATALOG  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def server(tmp_path):
    return server_module.build_server(str(tmp_path / "w.sqlite3"))


def _call(server, name, arguments):
    return asyncio.run(server.call_tool(name, arguments)).structured_content


def test_overview_lists_every_task_with_a_one_line_purpose(server):
    overview = _call(server, "wealth_context", {})
    assert set(overview["tasks"]) == set(CATALOG)
    for name, purpose in overview["tasks"].items():
        assert isinstance(purpose, str) and 10 < len(purpose) <= 120, name
        assert CATALOG[name]["purpose"].startswith(purpose.rstrip("…")), name
    assert overview["tasks"]["mx_deductions"].startswith("Art. 151")  # an abbreviation is not a sentence end
    assert overview["release"] and "intent=<task name>" in overview["next_step"]
    assert len(json.dumps(overview)) < 9_000  # it was 23k with every required list and the catalog's prose


def test_one_task_or_detail_full_returns_the_example(server):
    single = _call(server, "wealth_context", {"intent": "debt_payoff"})
    assert list(single["tasks"]) == ["debt_payoff"] and "example" in single["tasks"]["debt_payoff"]
    full = _call(server, "wealth_context", {"detail": "full"})
    assert set(full["tasks"]) == set(CATALOG)
    assert all("example" in spec for spec in full["tasks"].values())


def test_unknown_task_points_at_discovery_instead_of_a_truncated_list(server):
    with pytest.raises(ToolError) as caught:
        asyncio.run(server.call_tool("wealth_run", {"task": "no_such_task"}))
    message = str(caught.value)
    assert "unknown task 'no_such_task'; call wealth_context" in message
    assert "debt_payoff" not in message


def test_instructions_carry_the_compact_contract_unless_the_full_policy_is_asked_for(server, monkeypatch):
    assert HOST_CONTRACT in server.instructions and ASSISTANT_CONTRACT not in server.instructions
    assert len(server.instructions) < 3_000

    built: dict = {}

    class Stub:
        def run(self, transport):
            pass

    def fake_build(**kwargs):
        built.update(kwargs)
        return Stub()

    monkeypatch.setattr(server_module, "build_server", fake_build)
    monkeypatch.setattr(server_module, "start_orphan_watchdog", lambda: None)  # it would take pytest's stdin
    monkeypatch.delenv("WEALTH_BEHAVIOR_IN_HOST", raising=False)
    monkeypatch.delenv("WEALTH_BEHAVIOR_IN_SERVER", raising=False)
    server_module.main()
    assert built["include_behavior"] is False
    monkeypatch.setenv("WEALTH_BEHAVIOR_IN_SERVER", "1")
    server_module.main()
    assert built["include_behavior"] is True
    monkeypatch.setenv("WEALTH_BEHAVIOR_IN_HOST", "1")  # the Wealth launcher gives the model the policy itself
    server_module.main()
    assert built["include_behavior"] is False


def test_annotations_and_descriptions(server):
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    run = tools["wealth_run"].annotations
    assert run.read_only_hint is False and run.open_world_hint is True and not run.destructive_hint
    assert tools["wealth_ingest"].annotations.open_world_hint is True
    for name in ("wealth_context", "wealth_recall", "wealth_inspect"):
        assert tools[name].annotations.read_only_hint is True, name
    assert "wealth_inspect" in tools["wealth_recall"].description
    assert "ClientExistsError" in tools["wealth_client"].description
    assert "detail" in tools["wealth_context"].input_schema["properties"]


def _statement_account(as_of):
    positions = [
        {"id": "gbm:BMV:AMX B", "account_id": "gbm", "instrument_id": "BMV:AMX B", "symbol": "AMX B", "series": "B",
         "currency": "MXN", "listing_currency": "MXN", "venue": "bmv", "quantity": "1000", "value": "15500"},
        {"id": "gbm:SIC:IVV", "account_id": "gbm", "instrument_id": "SIC:IVV", "symbol": "IVV", "asset_class": "fund",
         "currency": "MXN", "listing_currency": "MXN", "venue": "sic", "quantity": "3", "value": "36000"},
        {"id": "gbm:UDI", "account_id": "gbm", "instrument_id": "S UDIBONO 351122", "symbol": "S UDIBONO 351122",
         "asset_class": "fixed_income", "currency": "MXN", "quantity": "100", "value": "46000"},
        {"id": "gbm:CASH:MXN", "account_id": "gbm", "instrument_id": "CASH:MXN", "symbol": "MXN", "asset_class": "cash",
         "currency": "MXN", "quantity": "2500", "value": "2500"}]
    return {"account": {"id": "gbm", "institution": "GBM", "type": "brokerage", "currency": "MXN", "owner_id": "self"},
            "as_of": as_of, "currency": "MXN", "fx": [], "liabilities": [], "lots": [], "positions": positions}


def test_portfolio_tasks_use_the_saved_holdings_when_no_weights_are_given(server, tmp_path):
    """Foreign-host run: stress with client_id said weights were missing although 11 positions were saved."""
    from datetime import date

    service = server_module.WealthService(str(tmp_path / "w.sqlite3"))
    service.create("ana", "Ana")
    today = date.today().isoformat()
    service.remember("ana", [{"key": "account.gbm", "value": _statement_account(today), "confidence": "reported",
                              "source": {"kind": "document", "ref": "document:sha256:abc gbm.pdf", "observed_on": today}}])
    crash = {"scenarios": [{"name": "crash", "shocks": {"SPY": -0.3}}]}
    stress = _call(server, "wealth_run", {"task": "stress", "client_id": "ana", "inputs": crash})
    assert stress["status"] in {"ready", "partial"} and not stress["missing"], stress["missing"]
    weights = stress["result"]["weights"]
    assert weights == pytest.approx({"AMXB.MX": 0.155, "IVV.MX": 0.36, "SUDIBONO351122": 0.46, "CASH::MXN": 0.025})
    scenario = stress["result"]["scenarios"][0]
    assert scenario["portfolio_return"] == pytest.approx(-0.3 * 0.155 - 0.3 * 0.36 - 0.03 * 0.46)
    assert any("saved accounts: 4 positions" in a for a in stress["assumptions"])
    assert stress["evidence_ids"]  # the statement is the evidence

    exposure = _call(server, "wealth_run", {"task": "exposure", "client_id": "ana"})
    assert exposure["status"] in {"ready", "partial"} and exposure["result"]["known_nav"] == "100000"
    # Explicit weights still win, and without a client the shape to send is spelled out.
    explicit = _call(server, "wealth_run", {"task": "stress", "client_id": "ana", "inputs": {
        **crash, "currency": "MXN", "weights": {"SPY": 0.5, "CASH::MXN": 0.4995}}})
    assert explicit["result"]["weights"] == pytest.approx({"SPY": 0.5 / 0.9995, "CASH::MXN": 0.4995 / 0.9995})
    alone = _call(server, "wealth_run", {"task": "stress", "inputs": crash})
    assert alone["status"] == "needs_input" and '"weights": {' in alone["missing"][0]


def test_situation_brief_is_the_small_per_turn_read_and_the_contract_moved_to_remember(server, tmp_path):
    service = server_module.WealthService(str(tmp_path / "w.sqlite3"))
    service.create("ana", "Ana")
    today = {"kind": "user", "ref": "chat", "observed_on": __import__("datetime").date.today().isoformat()}
    service.remember("ana", [{"key": "income.salary", "source": today,
                              "value": {"amount": 60000, "currency": "MXN", "frequency": "monthly", "net": True}}])
    summary = _call(server, "wealth_context", {"client_id": "ana", "intent": "situation"})
    assert "fact_contract" not in summary and "situation" in summary
    brief = _call(server, "wealth_context", {"client_id": "ana", "intent": "situation", "detail": "brief"})
    assert brief["brief"] == summary["brief"] and brief["figures"]["income_monthly"] == 60000
    assert len(json.dumps(brief, ensure_ascii=False)) < 2_000 < len(json.dumps(summary, ensure_ascii=False))
    assert "fact_contract" in _call(server, "wealth_context", {"client_id": "ana", "intent": "plan"})

    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    remember = tools["wealth_remember"].description
    assert '"amount":60000' in remember and "income.<id>" in remember and "liability.<id>" in remember
    assert "brief" in tools["wealth_context"].input_schema["properties"]["detail"]["enum"]


def test_hosts_can_find_the_client_every_tool_and_every_task_family(server, tmp_path):
    service = server_module.WealthService(str(tmp_path / "w.sqlite3"))
    service.create("ana", "Ana Ruiz")
    service.create("beto", "Beto")
    assert _call(server, "wealth_client", {"action": "list"}) == {
        "clients": [{"client_id": "ana", "display_name": "Ana Ruiz"}, {"client_id": "beto", "display_name": "Beto"}]}
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    for name in tools:  # hosts that defer MCP tools (Codex code mode) look them up by name
        assert name in server.instructions, name
    assert "detail=brief" in server.instructions and "wealth_context(intent=<task>)" in server.instructions
    assert "language of their current message" in server.instructions
    run = tools["wealth_run"].description
    for family in ("manager_search {name}", "manager_mirror", "mx_foreign", "prepay_vs_invest", "stress", "analyze",
                   "compare", "tax_pack", "estate_register", "sic_premium"):
        assert family in run, family


def test_cli_ends_quietly_when_the_reader_closes_the_pipe(tmp_path):
    env = {**os.environ, "WEALTH_DB": str(tmp_path / "w.sqlite3")}
    command = f"'{sys.executable}' -m wealth.cli context </dev/null | head -c 20 >/dev/null"
    done = subprocess.run(["sh", "-c", command], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0
    assert done.stderr == "", done.stderr
