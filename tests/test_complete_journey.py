from __future__ import annotations

from examples.complete_journey import run_demo


def test_complete_fictional_journey_runs_offline_and_preserves_state(tmp_path):
    result = run_demo(tmp_path / "complete.sqlite3")

    assert set(result["statuses"].values()) == {"ready"}
    assert result["outcomes"]["known_nav"] == "2500"
    assert result["outcomes"]["project_all_withdrawals_fulfilled_probability_percent"] > 0
    assert result["outcomes"]["ladder_total_gap"] == {"currency": "USD", "amount": 0.0}
    assert result["memory"]["goal_history_revisions"] == 2
    assert {"goals", "preference.risk", "thesis.ACME"} <= set(result["memory"]["recalled_keys"])
    assert result["memory"]["accepted_decision_needs_review_after_correction"] is True
    assert result["memory"]["first_monitor_events"] == 1
    assert result["memory"]["unchanged_repeat_events"] == 0
