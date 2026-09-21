"""Vest has no API for individual users: statements come in through a clearly labelled generic CSV preset.

The fixture is fictional; Vest documents no export layout (see the ``vest``
preset in ``wealth/ingest/tabular.py``).
"""
from __future__ import annotations

from pathlib import Path

from wealth import connectors
from wealth.ingest import ingest_file
from wealth.ingest.tabular import PRESETS, VEST_NOTE


FIXTURES = Path(__file__).parent / "fixtures" / "vest"


def load(**options):
    return ingest_file(FIXTURES / "vest_statement.csv", allowed_roots=[FIXTURES], **options)


def test_vest_is_not_a_live_connector():
    assert "vest" not in connectors.names()
    assert PRESETS["vest"]["generic"] is True


def test_vest_statement_is_detected_and_labelled_generic():
    proposal = load()
    result = proposal["result"]
    assert result["provenance"]["parser"] == "export:vest"
    assert VEST_NOTE in proposal["assumptions"]
    assert "not a documented Vest export" in VEST_NOTE and "no API" in VEST_NOTE
    [account] = result["household"]["accounts"]
    assert (account["id"], account["institution"], account["currency"]) == ("vest-7788", "Vest", "USD")


def test_vest_holdings_reconcile_and_activity_maps_to_ledger_types():
    proposal = load()
    result = proposal["result"]
    assert proposal["status"] == "ready_to_confirm"
    assert result["as_of"] == "2026-08-31"
    held = {p["instrument_id"]: p for p in result["household"]["positions"]}
    assert (held["VOO"]["quantity"], held["VOO"]["value"], held["VOO"]["cost_basis"]) == ("2.5", "1250", "1150")
    assert held["MSFT"]["quantity"] == "1.25"
    assert held["CASH:USD"]["value"] == "120.5"
    [recon] = result["reconciliation"]["accounts"]
    assert (recon["computed_total"], recon["reported_total"], recon["status"]) == ("1870.5", "1870.5", "reconciled")
    kinds = sorted((t["type"], t["amount"]) for t in result["transactions"])
    assert kinds == [("buy", "-249"), ("deposit", "300"), ("dividend", "3.1"), ("tax_withheld", "-0.31")]
    [buy] = [t for t in result["transactions"] if t["type"] == "buy"]
    assert (buy["date"], buy["quantity"], buy["symbol"]) == ("2026-08-06", "0.5", "VOO")


def test_the_preset_can_be_forced_on_an_unlabelled_file(tmp_path):
    text = (FIXTURES / "vest_statement.csv").read_text().split("\n", 2)[2]  # drop the two title lines
    path = tmp_path / "mis_inversiones.csv"
    path.write_text("Account ****7788\n" + text.split("\n", 1)[1])
    proposal = ingest_file(path, allowed_roots=[tmp_path], preset="vest", as_of="2026-08-31")
    assert proposal["result"]["provenance"]["parser"] == "export:vest"
    assert proposal["result"]["household"]["accounts"][0]["institution"] == "Vest"
    assert VEST_NOTE in proposal["assumptions"]
