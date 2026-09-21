"""Exercise the actual stdio boundary in a separate process, not just wrappers."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("mcp")
from mcp import Client, StdioServerParameters

from examples.returning_client import example_facts
from wealth.service import WealthService


ROOT = Path(__file__).resolve().parents[1]


def test_stdio_tool_journey_and_annotations(tmp_path, capfd):
    async def journey():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "wealth.server"],
            cwd=ROOT,
            env={**os.environ, "WEALTH_DB": str(tmp_path / "mcp.sqlite3")},
        )
        async with Client(params, read_timeout_seconds=15) as client:
            tools = {item.name: item for item in (await client.list_tools()).tools}
            assert set(tools) == {
                "wealth_context",
                "wealth_remember",
                "wealth_run",
                "wealth_recall",
                "wealth_decision",
                "wealth_inspect",
                "wealth_client",
            }
            assert all(
                item.input_schema.get("additionalProperties") is False
                for item in tools.values()
            )
            assert tools["wealth_context"].annotations.read_only_hint is True
            assert tools["wealth_recall"].annotations.read_only_hint is True
            assert tools["wealth_inspect"].annotations.read_only_hint is True
            assert tools["wealth_run"].annotations.read_only_hint is False
            assert not any(item.annotations.destructive_hint for item in tools.values())
            assert tools["wealth_client"].input_schema["properties"]["action"]["enum"] == ["create", "index"]

            async def call(name, arguments):
                result = await client.call_tool(name, arguments)
                assert not result.is_error, result.content
                return result.structured_content

            discovery = await call("wealth_context", {})
            assert discovery["release"] == "0.2.0"
            assert "plan" in discovery["tasks"]

            await call(
                "wealth_client",
                {
                    "action": "create",
                    "client_id": "mcp-client",
                    "inputs": {"display_name": "MCP Client"},
                },
            )
            remembered = await call(
                "wealth_remember",
                {
                    "client_id": "mcp-client",
                    "facts": example_facts(),
                    "expected_revision": 0,
                    "request_id": "initial",
                },
            )
            assert remembered["write_result"]["resulting_revision"] == 1
            assert "facts" not in remembered and len(remembered["written"]) == len(example_facts())

            plan = await call(
                "wealth_run", {"task": "plan", "client_id": "mcp-client"}
            )
            assert plan["result"]["uncommitted_capital"]["amount"] == "226000"

            recalled = await call(
                "wealth_recall", {"client_id": "mcp-client", "query": "home"}
            )
            assert recalled["matches"][0]["key"] == "goals"
            context = await call(
                "wealth_context",
                {"client_id": "mcp-client", "intent": "plan", "query": "reserve"},
            )
            assert context["client_revision"] == 1
            assert "available_tasks" in context

            proposal = await call(
                "wealth_decision",
                {
                    "action": "propose",
                    "client_id": "mcp-client",
                    "inputs": {
                        "title": "Protect the home goal",
                        "rationale": "Keep goal funding available",
                        "expected_revision": 1,
                        "evidence_ids": plan["evidence_ids"],
                    },
                },
            )
            correction = next(
                item for item in example_facts(100000) if item["key"] == "goals"
            )
            await call(
                "wealth_remember",
                {
                    "client_id": "mcp-client",
                    "facts": [correction],
                    "expected_revision": 1,
                },
            )
            rejected = await client.call_tool(
                "wealth_decision",
                {
                    "action": "accept",
                    "client_id": "mcp-client",
                    "inputs": {
                        "decision_id": proposal["id"],
                        "expected_revision": 2,
                    },
                },
            )
            assert rejected.is_error
            assert "IneligibleEvidenceError" in str(rejected.content)
            assert "goals changed at revision 2" in str(rejected.content)

            monitor = await call(
                "wealth_run",
                {"task": "monitor", "client_id": "mcp-client"},
            )
            assert monitor["result"]["delivery"].startswith("returned to caller only")

            exported = await call(
                "wealth_inspect",
                {"detail": "export", "client_id": "mcp-client"},
            )
            assert exported["client"]["revision"] == 2
            selected = await call(
                "wealth_inspect",
                {"client_id": "mcp-client", "keys": ["goals", "nope"]},
            )
            assert [f["key"] for f in selected["facts"]] == ["goals"]
            assert selected["absent_keys"] == ["nope"]

            forget = await client.call_tool(
                "wealth_client",
                {"action": "forget", "client_id": "mcp-client",
                 "inputs": {"confirm_client_id": "mcp-client"}},
            )
            assert forget.is_error
            missing_title = await client.call_tool(
                "wealth_decision",
                {"action": "propose", "client_id": "mcp-client",
                 "inputs": {"rationale": "r", "expected_revision": 2, "evidence_ids": ["x"]}},
            )
            assert missing_title.is_error and "missing ['title']" in str(missing_title.content)
            stale = await client.call_tool(
                "wealth_remember",
                {"client_id": "mcp-client", "facts": [correction], "expected_revision": 0},
            )
            assert stale.is_error and "current 2" in str(stale.content)
            patched = await call(
                "wealth_remember",
                {"client_id": "mcp-client",
                 "facts": [{**correction, "value": [{"id": "home", "target_amount": 1}], "merge": True}]},
            )
            assert patched["client"]["revision"] == 3

            wrong_client = await client.call_tool(
                "wealth_inspect",
                {"client_id": "someone-else"},
            )
            assert wrong_client.is_error
            assert "ClientNotFoundError" in str(wrong_client.content)
            assert "someone-else" not in str(wrong_client.content)

            bool_revision = await client.call_tool(
                "wealth_remember",
                {
                    "client_id": "mcp-client",
                    "facts": [correction],
                    "expected_revision": True,
                },
            )
            assert bool_revision.is_error
            unknown_task = await client.call_tool(
                "wealth_run", {"task": "definitely-not-a-task"}
            )
            assert unknown_task.is_error
            assert "unknown task" in str(unknown_task.content)

            unknown = await client.call_tool(
                "wealth_inspect",
                {
                    "client_id": "mcp-client",
                    "extra": "must-not-be-ignored",
                },
            )
            assert unknown.is_error
            unknown_read = await client.call_tool(
                "wealth_context", {"unexpected": True}
            )
            assert unknown_read.is_error

    asyncio.run(journey())
    assert "Traceback" not in capfd.readouterr().err


def test_watch_once_prints_only_monitor_events(tmp_path):
    database = tmp_path / "watch.sqlite3"
    service = WealthService(database)
    service.create("watch-client", "Watch Client")
    today = datetime.now(timezone.utc).date().isoformat()
    service.remember(
        "watch-client",
        [
            {
                "key": "monitor.rules",
                "value": [
                    {
                        "id": "profile-expiry",
                        "kind": "expiry",
                        "keys": ["missing.fact"],
                    }
                ],
                "source": {
                    "kind": "user",
                    "ref": "watch test",
                    "observed_on": today,
                },
                "confidence": "confirmed",
            }
        ],
        0,
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "wealth.cli",
            "watch",
            "--client",
            "watch-client",
            "--db",
            str(database),
            "--once",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    output = [json.loads(line) for line in completed.stdout.splitlines()]
    assert output == [
        {
            "rule_id": "profile-expiry",
            "kind": "expiry",
            "status": "active",
            "detail": {"stale_or_missing": ["missing.fact"]},
            "event": "review_needed",
        }
    ]
    assert completed.stderr == ""
