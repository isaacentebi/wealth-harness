from __future__ import annotations

import json

import pytest

from wealth import agent
from wealth.service import WealthService


def _event(event: dict) -> str:
    return json.dumps(event)


def test_run_turn_builds_isolated_codex_command_without_extracting_auth(
    monkeypatch, tmp_path, capsys
):
    captured = {}

    def fake_run(command, prompt, timeout):
        captured.update(command=list(command), prompt=prompt, timeout=timeout)
        return 0, "\n".join(
            [
                _event(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "mcp_tool_call",
                            "server": "wealth",
                            "tool": "wealth_context",
                            "status": "completed",
                            "result": {"private": "must not be printed"},
                        },
                    }
                ),
                _event(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "A concise answer."},
                    }
                ),
                _event({"type": "turn.completed", "usage": {"input_tokens": 42}}),
            ]
        )

    monkeypatch.setattr(agent, "_run_process", fake_run)
    answer = agent.run_turn(
        "How am I doing?",
        client_id="stable-client",
        db_path=tmp_path / "wealth.sqlite3",
        model="sol",
        timeout=12,
    )

    command = captured["command"]
    joined = " ".join(command)
    assert answer == "A concise answer."
    assert command[:2] == ["codex", "exec"]
    assert "--ignore-user-config" in command
    assert "--ephemeral" in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert command[command.index("--model") + 1] == "gpt-5.6-sol"
    assert "features.shell_tool=false" in command
    assert "features.apps=false" in command
    assert "features.plugins=false" in command
    assert "features.multi_agent=false" in command
    assert "features.skip_host_skill_discovery=true" in command
    assert 'web_search="disabled"' in command
    assert "mcp_servers.wealth.command=" in joined
    assert "mcp_servers.wealth.env.WEALTH_DB=" in joined
    assert "mcp_servers.wealth.default_tools_approval_mode=" in joined
    assert "TOKEN" not in joined.upper()
    assert "API_KEY" not in joined.upper()
    assert "stable-client" not in joined
    assert "stable-client" in captured["prompt"]
    assert "private" not in capsys.readouterr().err


def test_turn_failure_event_is_not_silent(monkeypatch, tmp_path):
    monkeypatch.setattr(
        agent,
        "_run_process",
        lambda command, prompt, timeout: (
            0,
            _event(
                {"type": "turn.failed", "error": {"message": "authentication unavailable"}}
            ),
        ),
    )
    with pytest.raises(agent.AgentError, match="authentication unavailable"):
        agent.run_turn(
            "question", client_id="client", db_path=tmp_path / "wealth.sqlite3"
        )


def test_partial_response_without_completed_turn_is_not_success(monkeypatch, tmp_path):
    monkeypatch.setattr(agent, "_run_process", lambda *args: (0, _event({
        "type": "item.completed", "item": {"type": "agent_message", "text": "Working..."}
    })))
    with pytest.raises(agent.AgentError, match="before completing"):
        agent.run_turn("question", client_id="client", db_path=tmp_path / "wealth.sqlite3")


def test_demo_seed_is_idempotent_and_preserves_changed_state(tmp_path):
    database = tmp_path / "demo.sqlite3"
    assert agent.seed_demo(database) is True
    service = WealthService(database)
    before = service.inspect(agent.DEMO_CLIENT_ID)
    service.remember(
        agent.DEMO_CLIENT_ID,
        [
            {
                "key": "constraint.demo-note",
                "value": "Keep this persisted change",
                "source": {
                    "kind": "user",
                    "ref": "test confirmation",
                    "observed_on": "2026-09-20",
                },
                "confidence": "confirmed",
                "expires_on": None,
            }
        ],
        before["client"]["revision"],
        "test-persisted-change",
    )

    assert agent.seed_demo(database) is False
    after = service.inspect(agent.DEMO_CLIENT_ID)
    values = {fact["key"]: fact["value"] for fact in after["facts"]}
    assert values["constraint.demo-note"] == "Keep this persisted change"
    assert after["client"]["revision"] == before["client"]["revision"] + 1
