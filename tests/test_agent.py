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


def test_first_conversation_creates_profile_and_resume_preserves_it(monkeypatch, tmp_path):
    database = tmp_path / "new.sqlite3"
    seen = []

    def fake_turn(text, **kwargs):
        service = WealthService(kwargs["db_path"])
        snapshot = service.inspect(kwargs["client_id"])
        seen.append(snapshot["client"]["revision"])
        if not snapshot["facts"]:
            service.remember(kwargs["client_id"], [{
                "key": "preference.currency", "value": "MXN",
                "source": {"kind": "user", "ref": "conversation", "observed_on": "2026-09-21"},
                "confidence": "confirmed",
            }], snapshot["client"]["revision"])
        return "Ready."

    monkeypatch.setattr(agent, "run_turn", fake_turn)
    args = ["--client", "new-client", "--db", str(database), "--prompt", "I spend in pesos"]
    assert agent.main(args) == 0
    assert agent.main(args) == 0
    assert seen == [0, 1]


def test_partial_onboarding_facts_are_discoverable_and_not_calculated_as_zero(tmp_path):
    service = WealthService(tmp_path / "partial.sqlite3")
    service.create("client", "Client")
    context = service.context("client", intent="plan")
    contract = context["fact_contract"]
    assert "goals" in contract["keys"]
    facts = {
        "client.profile": {"residence": "Mexico", "spending_currency": "MXN"},
        "goals": [{"id": "home", "name": "Buy a home", "timing": "about three years"}],
        "plan.resources": {"currency": "MXN", "available_capital": 100000,
            "monthly_essentials": 1000, "reserve_months": 6,
            "reserve_outside_pool": 0, "debt_payments_from_pool": 0},
    }
    service.remember("client", [{"key": key, "value": value,
        "source": {"kind": "user", "ref": "conversation", "observed_on": agent.datetime.now(agent.timezone.utc).date().isoformat()},
        "confidence": "confirmed", "expires_on": contract["default_review_on"]}
        for key, value in facts.items()], 0)
    later = service.context("client", intent="research", query="unrelated ticker")
    assert "goals" in later["known_fact_keys"]
    result = service.run("plan", client_id="client")
    # Calculations stay unavailable until required fields are present; the status
    # is needs_input for missing fields and partial for invalid ones.
    assert result["status"] in {"needs_input", "partial"}
    assert not result["result"]


def test_demo_seed_repairs_interrupted_initialization(tmp_path):
    database = tmp_path / "demo.sqlite3"
    service = WealthService(database)
    # Simulate a crash between client creation and the seed write: revision 0,
    # no facts, no decisions.
    service.create(agent.DEMO_CLIENT_ID, "Fictional Wealth Demo")

    assert agent.seed_demo(database) is True
    snapshot = service.inspect(agent.DEMO_CLIENT_ID)
    assert snapshot["client"]["revision"] == 1
    assert {fact["key"] for fact in snapshot["facts"]} >= {
        "client.profile",
        "plan.resources",
        "goals",
        "household",
    }


def test_demo_seed_preserves_client_with_recorded_state(tmp_path):
    database = tmp_path / "demo.sqlite3"
    service = WealthService(database)
    service.create(agent.DEMO_CLIENT_ID, "Fictional Wealth Demo")
    service.remember(
        agent.DEMO_CLIENT_ID,
        [
            {
                "key": "client.profile",
                "value": {"note": "user-written"},
                "source": {
                    "kind": "user",
                    "ref": "test",
                    "observed_on": "2026-09-20",
                },
                "confidence": "confirmed",
                "expires_on": None,
            }
        ],
        0,
        "user-write",
    )

    assert agent.seed_demo(database) is False
    snapshot = service.inspect(agent.DEMO_CLIENT_ID)
    assert [fact["key"] for fact in snapshot["facts"]] == ["client.profile"]
    assert snapshot["facts"][0]["value"] == {"note": "user-written"}


def test_interactive_onboarding_starts_before_input_and_only_for_empty_profile(monkeypatch, tmp_path, capsys):
    database = tmp_path / "onboarding.sqlite3"
    captured = []
    replies = iter(["hey", "/quit"])
    monkeypatch.setattr("builtins.input", lambda _: next(replies))
    def turn(text, **kwargs):
        captured.append((text, kwargs))
        return "What would you like to achieve financially?"
    monkeypatch.setattr(agent, "run_turn", turn)
    args = ["--client", "opaque-id", "--db", str(database)]
    assert agent.main(args) == 0
    assert agent.ONBOARDING_WELCOME in capsys.readouterr().out
    assert captured[0][1]["profile_empty"] is True
    assert ("assistant", agent.ONBOARDING_WELCOME) in captured[0][1]["history"]
    service = WealthService(database)
    service.remember("opaque-id", [{"key": "preference.focus", "value": "retirement",
        "source": {"kind": "user", "ref": "conversation", "observed_on": "2026-09-21"},
        "confidence": "confirmed"}], 0)
    replies = iter(["hey", "/quit"])
    assert agent.main(args) == 0
    assert agent.ONBOARDING_WELCOME not in capsys.readouterr().out
    assert captured[-1][1]["profile_empty"] is False
