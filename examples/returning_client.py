"""A fictional two-session client journey; temporary storage, no network."""
from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from wealth.service import WealthService
from wealth.store import IneligibleEvidenceError


def example_facts(home_target: int = 50000) -> list[dict]:
    today = datetime.now(timezone.utc).date()
    expiry = (today + timedelta(days=30)).isoformat()
    source = {"kind": "user", "ref": "fictional demo conversation", "observed_on": today.isoformat()}
    values = {
        "client.profile": {"reporting_currency": "USD", "household": "Fictional example; not the owner's finances"},
        "plan.resources": {"currency": "USD", "available_capital": 300000,
                           "monthly_essentials": 4000, "reserve_months": 6,
                           "reserve_outside_pool": 0, "debt_payments_from_pool": 0},
        "goals": [{"id": "home", "name": "Home deposit", "currency": "USD",
                   "due": (today + timedelta(days=540)).isoformat(),
                   "target_amount": home_target, "funded_outside_pool": 0, "protect_now": True}],
        "portfolio.snapshot": {
            "currency": "USD", "scope": "Fictional liquid accounts only", "complete": True,
            "positions": [
                {"account_id": "broker", "symbol": "BROAD", "value": 150000, "asset_class": "equity"},
                {"account_id": "broker", "symbol": "TECH", "value": 50000, "asset_class": "equity"},
                {"account_id": "second", "symbol": "TECH", "value": 25000, "asset_class": "equity"},
                {"account_id": "bank", "symbol": "CASH", "value": 75000, "asset_class": "cash"},
            ],
        },
        "constraint.leverage": "No borrowing to invest",
        "thesis.ai": {"view": "Explore infrastructure exposure", "reconsider_if": "The evidence for demand weakens"},
        "income.schedule": {
            "currency": "USD", "monthly_need": 2000,
            "months": [{"month": (today.replace(day=1) + timedelta(days=32)).strftime("%Y-%m"),
                        "expected_cash_received": 1500, "committed_outflow": 500}],
        },
    }
    return [{"key": key, "value": value, "source": source, "confidence": "confirmed",
             "expires_on": expiry} for key, value in values.items()]


def run_demo(path: Path) -> dict:
    service = WealthService(path)
    service.create("demo", "Fictional returning client")
    service.remember("demo", example_facts(), 0, "initial-demo")
    initial = service.prepare("demo", "plan")
    exposure = service.prepare("demo", "exposure")
    income = service.prepare("demo", "income")
    proposal = service.propose(
        "demo", "Protect the home deposit before exploring more risk",
        "Reserve and home funding come from the declared pool; the remainder is arithmetic, not an investment approval.",
        1, initial["evidence_ids"],
        ["Keep the current exposure while refreshing research", "Compare additional exposure only after goal funding"],
    )
    service.resolve("demo", proposal["id"], "accepted", 1)

    # A new service instance reads persisted state rather than conversation memory.
    returning = WealthService(path)
    before = returning.prepare("demo", "overview")
    correction = next(f for f in example_facts(100000) if f["key"] == "goals")
    returning.remember("demo", [correction], 1, "home-correction")
    after = returning.prepare("demo", "plan")
    stale = returning.inspect("demo")["decisions"][0]
    rejected = False
    try:
        returning.resolve("demo", proposal["id"], "accepted", 2)
    except IneligibleEvidenceError:
        rejected = True

    return {
        "data": "fictional; temporary database; no external calls or trades",
        "first_session": {
            "client_revision": initial["client_revision"],
            "plan": initial["calculations"],
            "allocation": exposure["calculations"],
            "income": income["calculations"],
            "decision": "accepted, not executed",
        },
        "returning_session": {
            "remembered_facts": len(before["facts"]),
            "client_revision": after["client_revision"],
            "revised_plan": after["calculations"],
            "old_decision_needs_review": stale["needs_review"],
            "stale_acceptance_blocked": rejected,
            "goal_history_revisions": len(returning.inspect("demo", "history", "goals")["history"]),
        },
    }


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="wealth-demo-") as directory:
        print(json.dumps(run_demo(Path(directory) / "demo.sqlite3"), indent=2, allow_nan=False))
