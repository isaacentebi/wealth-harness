"""Friction a foreign MCP host hit with a generic prompt: contract size, needs_input retries, natural writes."""
from __future__ import annotations

import asyncio
import json
from datetime import date

import pytest

pytest.importorskip("mcp")
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402

from wealth import server as server_module  # noqa: E402
from wealth.proactive import CETES_28D_REFERENCE  # noqa: E402
from wealth.service import WealthService, client_slug  # noqa: E402

TODAY = date.today().isoformat()
USER = {"kind": "user", "ref": "chat", "observed_on": TODAY}


@pytest.fixture()
def db(tmp_path):
    return str(tmp_path / "w.sqlite3")


@pytest.fixture()
def server(db):
    return server_module.build_server(db)


def call(server, name, arguments):
    return asyncio.run(server.call_tool(name, arguments)).structured_content


def remember(server, client, *facts):
    return call(server, "wealth_remember", {"client_id": client, "facts": [
        {"key": key, "value": value, "source": USER, **extra} for key, value, extra in facts]})


def ana(server):
    call(server, "wealth_client", {"action": "create", "client_id": "ana", "inputs": {"display_name": "Ana"}})
    remember(server, "ana",
             ("client.profile", {"residence": {"country": "MX", "region": "CDMX"}}, {}),
             ("income.salary", {"amount": 70000, "currency": "MXN", "frequency": "monthly", "net": True}, {}),
             ("spending.monthly", {"total": 45000, "currency": "MXN"}, {}),
             ("cash.nu", {"amount": 150000, "currency": "MXN", "institution": "Nu"}, {}),
             ("liability.tarjeta", {"kind": "card", "balance": 25000, "currency": "MXN", "annual_rate": 0.42}, {}))


# ------------------------------------------------------------------ the fact contract


def test_a_task_read_carries_a_pointer_not_the_16k_contract(server):
    ana(server)
    debt = call(server, "wealth_context", {"client_id": "ana", "intent": "debt"})
    assert "fact_contract" not in debt and "intent=remember" in debt["fact_contract_pointer"]
    assert len(json.dumps(debt, ensure_ascii=False)) < 4_000
    full = call(server, "wealth_context", {"client_id": "ana", "intent": "remember"})
    assert full["fact_contract"]["schema"]["estate.family"]
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    assert "intent=remember" in tools["wealth_remember"].description


# ------------------------------------------------------------------ needs_input answered by the engine


def test_card_vs_cetes_runs_on_the_minimum_rule_and_the_dated_cetes_rate(server):
    ana(server)
    report = call(server, "wealth_run", {"task": "debt", "client_id": "ana", "inputs": {"mode": "prepay_vs_invest"}})
    assert report["status"] in {"ready", "partial"}
    result = report["result"]
    assert result["lump_sum"] == 25000 and result["debt"]["monthly_payment"] > 0
    assert result["investing"]["risk_free"]["rate"] == pytest.approx(float(CETES_28D_REFERENCE["rate"]), abs=1e-4)
    assert "Banxico" in result["investing"]["risk_free"]["source"]
    assert any(a.startswith("ASSUMED: tarjeta has no stated payment") for a in report["assumptions"])
    assert any("paying off the whole tarjeta balance" in a for a in report["assumptions"])
    stated = call(server, "wealth_run", {"task": "debt", "client_id": "ana", "inputs": {
        "mode": "prepay_vs_invest", "debt": {"id": "tarjeta", "monthly_payment": 3000}}})
    assert stated["result"]["debt"]["monthly_payment"] == 3000
    assert not any(a.startswith("ASSUMED: tarjeta") for a in stated["assumptions"])


def test_a_mortgage_needs_input_carries_a_retry_that_runs(server):
    call(server, "wealth_client", {"action": "create", "client_id": "sam", "inputs": {"display_name": "Sam"}})
    remember(server, "sam", ("liability.mortgage", {"kind": "mortgage", "balance": 400000, "currency": "USD",
                                                    "annual_rate": 0.065}, {}))
    report = call(server, "wealth_run", {"task": "debt", "client_id": "sam", "inputs": {"mode": "prepay_vs_invest"}})
    assert report["status"] == "needs_input"
    retry = report["retry_with"]
    inputs = retry["wealth_run"]["inputs"]
    assert inputs["mode"] == "prepay_vs_invest" and inputs["debt"]["id"] == "mortgage"
    assert inputs["debt"]["monthly_payment"].startswith("<") and inputs["extra_monthly"].startswith("<")
    assert "Never fill them from general knowledge" in retry["note"]
    inputs = {**inputs, "debt": {"id": "mortgage", "monthly_payment": 2800}, "extra_monthly": 500}
    ran = call(server, "wealth_run", {"task": "debt", "client_id": "sam", "inputs": inputs})
    assert ran["status"] in {"ready", "partial"} and ran["result"]["debt"]["balance"] == 400000
    assert "Treasury" in ran["result"]["investing"]["risk_free"]["source"]


def test_stress_equity_shock_without_holdings_retries_with_the_saved_accounts(server):
    call(server, "wealth_client", {"action": "create", "client_id": "sam", "inputs": {"display_name": "Sam"}})
    remember(server, "sam",
             ("investment.401k", {"amount": 300000, "currency": "USD", "kind": "retirement"}, {}),
             ("investment.schwab", {"amount": 150000, "currency": "USD", "institution": "Schwab"}, {}))
    scenarios = [{"name": "30% crash", "equity_shock": -0.3}]
    report = call(server, "wealth_run", {"task": "stress", "client_id": "sam", "inputs": {"scenarios": scenarios}})
    assert report["status"] == "needs_input"
    inputs = report["retry_with"]["wealth_run"]["inputs"]
    assert inputs["scenarios"] == scenarios and inputs["household"]["currency"] == "USD"
    assert [(p["symbol"], p["value"]) for p in inputs["household"]["positions"]] == [("401K", 300000),
                                                                                    ("SCHWAB", 150000)]
    filled = [{**p, "asset_class": "equity"} for p in inputs["household"]["positions"]]
    ran = call(server, "wealth_run", {"task": "stress", "client_id": "sam", "inputs": {
        "scenarios": scenarios, "household": {"currency": "USD", "positions": filled}}})
    assert ran["result"]["scenarios"][0]["portfolio_return"] == pytest.approx(-0.3)


def test_tax_pack_reads_a_flat_country_and_puts_the_overdue_return_first(server):
    call(server, "wealth_client", {"action": "create", "client_id": "ana", "inputs": {"display_name": "Ana"}})
    # {"country": "MX"} is how a host writes where the person lives: it is saved as the residence country.
    receipt = remember(server, "ana", ("client.profile", {"country": "MX"}, {}))
    assert any("residence.country" in w for w in receipt["warnings"])
    report = call(server, "wealth_run", {"task": "tax_pack", "client_id": "ana", "inputs": {"as_of": "2026-09-22"}})
    result = report["result"]
    assert result["tax_year"] == 2025 and result["jurisdictions"] == ["MX"]
    first = result["deadlines"][0]
    assert first["status"] == "overdue" and first["date"] == "2026-04-30"
    assert first["overdue_es"] == ("La declaración anual 2025 venció el 30 de abril de 2026; presenta cuanto antes, "
                                   "hay recargos.")
    assert "was due April 30, 2026" in first["overdue_en"]
    assert report["warnings"][0] == first["overdue_es"]
    assert {d["status"] for d in result["deadlines"][1:]} <= {"passed", "upcoming"}
    early = call(server, "wealth_run", {"task": "tax_pack", "client_id": "ana",
                                        "inputs": {"as_of": "2026-03-10", "year": 2025}})
    assert early["result"]["tax_year"] == 2025
    assert not any(d["status"] == "overdue" for d in early["result"]["deadlines"])


# ------------------------------------------------------------------ natural writes


def test_create_without_an_id_derives_one_from_the_name(server):
    created = call(server, "wealth_client", {"action": "create", "inputs": {"display_name": "Ana López"}})
    assert created["id"] == "ana-lopez" and client_slug("  José  María ") == "jose-maria"
    with pytest.raises(ToolError, match="display_name"):
        call(server, "wealth_client", {"action": "create", "inputs": {}})


def test_family_facts_merge_by_default_and_children_need_no_names(server):
    ana(server)
    first = remember(server, "ana", ("estate.family", {"marital_status": "married", "spouse": "Luis",
                                                       "marital_regime": "sociedad_conyugal",
                                                       "children": [{"minor": True}, {"minor": True}]}, {}))
    assert first["written"][0]["key"] == "estate.family"
    # A later turn names them: no merge flag, no revision; the object merges and the list is replaced whole.
    assert "wealth_run(task=estate_register" in first["next_step"]
    remember(server, "ana", ("estate.family", {"children": [{"name": "Mateo", "birth_year": 2018},
                                                            {"name": "Sofía", "birth_year": 2021,
                                                             "relationship": "child"}]}, {}))
    family = call(server, "wealth_inspect", {"client_id": "ana", "keys": ["estate.family"]})["facts"][0]["value"]
    assert family["spouse"] == "Luis" and [c["name"] for c in family["children"]] == ["Mateo", "Sofía"]
    remember(server, "ana", ("client.profile", {"dependents": 2}, {}))
    profile = call(server, "wealth_inspect", {"client_id": "ana", "keys": ["client.profile"]})["facts"][0]["value"]
    assert profile == {"residence": {"country": "MX", "region": "CDMX"}, "dependents": 2}
    # Replacing wholesale is explicit: merge=false, and it still needs the revision read.
    with pytest.raises(ToolError, match=r'already has a value.*Corrected: \{"key":"client.profile"'):
        remember(server, "ana", ("client.profile", {"dependents": 3}, {"merge": False}))


def test_spending_amount_is_read_as_total_and_errors_show_a_valid_value(server):
    call(server, "wealth_client", {"action": "create", "client_id": "ana", "inputs": {"display_name": "Ana"}})
    receipt = remember(server, "ana", ("spending.monthly", {"amount": 45000, "currency": "MXN"}, {}))
    assert any("spending.monthly.amount was saved as spending.monthly.total" in w for w in receipt["warnings"])
    saved = call(server, "wealth_inspect", {"client_id": "ana", "keys": ["spending.monthly"]})["facts"][0]["value"]
    assert saved == {"total": 45000, "currency": "MXN"}
    with pytest.raises(ToolError, match=r'unknown fields \[.kids.\].*a valid estate.family value: \{"children"'):
        remember(server, "ana", ("estate.family", {"kids": 2}, {}))


def test_the_estate_register_counts_nameless_children(db):
    service = WealthService(db)
    service.create("ana", "Ana")
    service.remember("ana", [
        {"key": "client.profile", "value": {"residence": {"country": "MX"}}, "source": USER},
        {"key": "cash.nu", "value": {"amount": 150000, "currency": "MXN", "institution": "Nu"}, "source": USER},
        {"key": "estate.family", "source": USER, "value": {"marital_status": "married", "spouse": "Luis",
                                                           "marital_regime": "sociedad_conyugal",
                                                           "children": [{"minor": True}, {"birth_year": 2021}]}},
        {"key": "estate.will", "value": {"exists": False}, "source": USER}])
    result = service.run("estate_register", {}, client_id="ana")["result"]
    assert result["guardian"]["minors"] == 2  # {"minor": true} and a 2021 birth year both count
    assert {g["code"] for g in result["gaps"]} >= {"no_will", "no_guardian"}


# ------------------------------------------------------------------ protection_review points at the register


def test_protection_review_says_unknown_plainly_and_routes_to_the_estate_register(server):
    ana(server)
    before = call(server, "wealth_run", {"task": "protection_review", "client_id": "ana"})["result"]["estate"]
    items = {i["item"]: i for i in before["items"]}
    assert items["beneficiarios_cuentas"]["status"] == "unknown"
    assert items["beneficiarios_cuentas"]["status_text"]["es"].startswith("No sé si tienes beneficiarios")
    assert items["testamento"]["task"] == "estate_register" and before["next_step"]["task"] == "estate_register"
    assert "Ask who is in the family" in before["next_step"]["en"]
    remember(server, "ana", ("estate.will", {"exists": False}, {}),
             ("estate.family", {"marital_status": "married", "marital_regime": "sociedad_conyugal"}, {}))
    after = call(server, "wealth_run", {"task": "protection_review", "client_id": "ana"})["result"]["estate"]
    items = {i["item"]: i for i in after["items"]}
    assert items["testamento"]["status"] == "missing"
    assert items["testamento"]["status_text"]["es"] == "Me dijiste que no tienes testamento."
    assert items["regimen_matrimonial"]["status"] == "done"
    assert "run wealth_run(task=estate_register)" in after["next_step"]["en"]
