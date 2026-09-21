from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from examples.returning_client import run_demo


ROOT = Path(__file__).resolve().parents[1]


def test_two_session_correction_changes_plan_and_invalidates_decision(tmp_path):
    result = run_demo(tmp_path / "journey.sqlite3")
    first = result["first_session"]
    later = result["returning_session"]
    assert first["plan"]["uncommitted_capital"]["amount"] == "226000"
    assert later["revised_plan"]["uncommitted_capital"]["amount"] == "176000"
    assert later["remembered_facts"] == 7
    assert later["client_revision"] == 2
    assert later["goal_history_revisions"] == 2
    assert later["old_decision_needs_review"] is True
    assert later["stale_acceptance_blocked"] is True
    tech = next(row for row in first["allocation"]["symbol_allocation"] if row["symbol"] == "TECH")
    assert tech["weight"] == "0.25"
    assert first["income"]["scheduled_months_total_gap"]["amount"] == "1000"


def cli(tmp_path, operation, arguments):
    return subprocess.run(
        [sys.executable, "-m", "wealth.cli", operation, "--db", str(tmp_path / "cli.sqlite3")],
        input=json.dumps(arguments), text=True, capture_output=True, cwd=ROOT,
        timeout=10,
    )


def test_cli_round_trip_across_processes_and_error_channel(tmp_path):
    created = cli(tmp_path, "client", {"action": "create", "client_id": "a", "inputs": {"display_name": "Client A"}})
    assert created.returncode == 0, created.stderr
    assert json.loads(created.stdout)["revision"] == 0
    recalled = cli(tmp_path, "context", {"client_id": "a", "intent": "plan"})
    assert recalled.returncode == 0, recalled.stderr
    assert "fact_contract" in json.loads(recalled.stdout)
    wrong_client = cli(tmp_path, "client", {"action": "inspect", "client_id": "b"})
    assert wrong_client.returncode == 2
    assert wrong_client.stdout == ""
    assert json.loads(wrong_client.stderr)["error_type"] == "ClientNotFoundError"
