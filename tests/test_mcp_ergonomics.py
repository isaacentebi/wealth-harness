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


def test_overview_lists_every_task_with_purpose_and_required_inputs_only(server):
    overview = _call(server, "wealth_context", {})
    assert set(overview["tasks"]) == set(CATALOG)
    for name, spec in overview["tasks"].items():
        assert set(spec) == {"purpose", "required"}, name
        assert spec["purpose"] == CATALOG[name]["purpose"]
    assert overview["release"] and "intent=<task name>" in overview["next_step"]
    assert len(json.dumps(overview)) < 30_000  # the full catalog is several times larger


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


def test_cli_ends_quietly_when_the_reader_closes_the_pipe(tmp_path):
    env = {**os.environ, "WEALTH_DB": str(tmp_path / "w.sqlite3")}
    command = f"'{sys.executable}' -m wealth.cli context </dev/null | head -c 20 >/dev/null"
    done = subprocess.run(["sh", "-c", command], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0
    assert done.stderr == "", done.stderr
