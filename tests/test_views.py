"""Engine-drawn views: specs from run results, the parser's view events, and the drawings."""
from __future__ import annotations

import copy
import json
from decimal import Decimal
from pathlib import Path

import pytest

from wealth import agent, views
from wealth.service import WealthService

FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "views_envelopes.json").read_text(encoding="utf-8"))

EXPECTED = {
    "spending_monthly": ["allocation", "series"],
    "spending_surplus": ["ticket"],
    "spending_income": ["ticket"],
    "spending_recurring": ["ticket"],
    "dca_backtest": ["ticket", "series"],
    "dca_schedule": ["ticket"],
    "dca_suggest": ["ticket"],
    "dca_adherence": ["ticket"],
    "performance": ["ticket"],
    "project": ["series", "ticket"],
    "income": ["comparison"],
    "stress": ["comparison"],
    "lot_selection": ["comparison"],
    "tax_rebalance": ["ticket"],
    "debt_payoff": ["payoff"],
    "situation": ["ticket", "ticket"],
}


def _specs(name):
    case = FIXTURES[name]
    return views.views_for(case["task"], copy.deepcopy(case["envelope"]))


def _leaves(value):
    """Every scalar in an envelope, as the raw value it carries."""
    if isinstance(value, dict):
        for item in value.values():
            yield from _leaves(item)
    elif isinstance(value, list):
        for item in value:
            yield from _leaves(item)
    elif not isinstance(value, bool) and value is not None:
        yield value


def _numbers(spec):
    """Every number a spec shows, except allocation shares (the one derived figure)."""
    data = spec["data"]

    def value(v):
        if v["t"] == "range":
            return [v["lo"], v["hi"]]
        if v["t"] in {"date", "text"}:
            return []
        return [v["v"]]

    out = []
    kind = spec["kind"]
    if kind == "ticket":
        for row in data["rows"] + ([data["total"]] if data["total"] else []):
            out += value(row["value"])
    elif kind == "allocation":
        out += [n for row in data["rows"] if "value" in row for n in value(row["value"])]
    elif kind == "series":
        out += [p["y"] for p in data["points"]]
        if "reference" in data:
            out.append(data["reference"]["y"])
        if "band" in data:
            out += [data["band"][k] for k in ("low", "mid", "high")]
    elif kind == "comparison":
        out += [n for metric in data["metrics"] for v in metric["values"] for n in value(v)]
    elif kind == "payoff":
        out += [n for row in data["rows"] for n in value(row["months"])]
        out += [n for key in ("months", "interest", "saved") for n in value(data["total"][key])]
    return [n for n in out if n is not None]


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_views_for_each_task_is_valid_bounded_and_stable(name):
    specs = _specs(name)
    assert [s["kind"] for s in specs] == EXPECTED[name]
    assert len(specs) <= views.MAX_VIEWS
    for spec in specs:
        views.validate(spec)
        assert views.VIEW_ID.match(spec["id"]) and spec["id"].startswith(FIXTURES[name]["task"] + "-")
        assert set(spec["title"]) == {"en", "es"} and all(spec["title"].values())
    # The same result always yields the same ids (the service lists them; the parser rebuilds them).
    assert [s["id"] for s in _specs(name)] == [s["id"] for s in specs]
    assert len({s["id"] for s in specs}) == len(specs)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_every_number_is_the_envelopes_own(name):
    envelope = FIXTURES[name]["envelope"]
    leaves = list(_leaves(envelope))
    for spec in _specs(name):
        for number in _numbers(spec):
            assert any(number == leaf and type(number) is type(leaf) for leaf in leaves), (spec["id"], number)


def test_specific_numbers_come_through_verbatim():
    backtest, prices = _specs("dca_backtest")
    result = FIXTURES["dca_backtest"]["envelope"]["result"]
    assert backtest["data"]["rows"][1]["value"] == {"t": "money", "v": result["dca_end_value"], "cur": "MXN"}
    assert backtest["data"]["total"]["value"]["v"] == result["dca_minus_lump_sum"] == "500.00"
    assert [p["y"] for p in prices["data"]["points"]] == [b["price"] for b in result["legs"][0]["buys"]]
    assert prices["data"]["reference"]["y"] == result["legs"][0]["dca_average_price"]
    assert "not a forecast" in backtest["caption"]["en"]

    [payoff] = _specs("debt_payoff")
    chosen = FIXTURES["debt_payoff"]["envelope"]["result"]["chosen"]
    assert payoff["data"]["rows"][0]["date"] == {"t": "date", "v": chosen["payoff"][0]["date"]}
    assert payoff["data"]["rows"][0]["alt_date"]["v"] == "2028-08"  # minimums only
    assert payoff["data"]["total"]["interest"]["v"] == chosen["interest"]

    band, chances = _specs("project")
    percentiles = FIXTURES["project"]["envelope"]["result"]["terminal_wealth_percentiles"]
    assert band["data"]["points"] == []
    assert band["data"]["band"] == {"x": "2031-01-01", "low": percentiles["p10"]["amount"],
                                    "mid": percentiles["p50"]["amount"], "high": percentiles["p90"]["amount"]}

    [lots] = _specs("lot_selection")
    methods = FIXTURES["lot_selection"]["envelope"]["result"]["methods"]
    assert len(lots["data"]["options"]) == 4
    assert [o["best"] for o in lots["data"]["options"]].count(True) == 1
    assert lots["data"]["options"][0]["label"]["en"] == "Last in"  # lowest_tax_method
    assert lots["data"]["metrics"][0]["values"][-1]["v"] == methods["fifo"]["incremental_tax"]  # the baseline stays

    alloc, _ = _specs("spending_monthly")
    shares = [Decimal(r["share"]) for r in alloc["data"]["rows"]]
    assert shares == sorted(shares, reverse=True) and abs(sum(shares) - 1) < Decimal("0.001")
    assert alloc["data"]["total"]["v"] == FIXTURES["spending_monthly"]["envelope"]["result"]["months"]["2026-03"]["total"]


def test_unknown_values_stay_unknown_never_zero():
    [surplus] = _specs("spending_surplus")
    assert FIXTURES["spending_surplus"]["envelope"]["status"] == "needs_input"
    assert surplus["data"]["total"]["value"] == {"t": "money", "v": None, "cur": "MXN"}
    assert views.format_value(surplus["data"]["total"]["value"], "en") == "—"
    svg = views.render_svg(surplus, "en")
    assert "not known yet" in svg and ">—<" in svg
    # A partial situation shows the net worth as unknown rather than as a partial sum.
    envelope = copy.deepcopy(FIXTURES["situation"]["envelope"])
    envelope["net_worth"]["complete"] = False
    net_worth = views.views_for("situation", envelope)[0]
    assert net_worth["data"]["total"]["value"]["v"] is None


def test_unsuccessful_or_unknown_results_have_no_views():
    envelope = copy.deepcopy(FIXTURES["dca_backtest"]["envelope"])
    for status in ("needs_input", "error", "rejected"):
        envelope["status"] = status
        assert views.views_for("dca", envelope) == []
    assert views.views_for("dca", {"status": "ready"}) == []
    assert views.views_for("no_such_task", FIXTURES["dca_backtest"]["envelope"]) == []
    assert views.views_for("dca", None) == []
    broken = copy.deepcopy(FIXTURES["dca_backtest"]["envelope"])
    broken["result"]["legs"] = "not a list"
    assert [s["kind"] for s in views.views_for("dca", broken)] == ["ticket"]


def test_future_rebalance_and_asset_location_shapes_are_read_defensively():
    rebalance = {"status": "ready", "result": {
        "currency": "USD", "trades": [{"instrument_id": "VTI", "side": "sell", "amount": "1200.00"},
                                      {"symbol": "BND", "action": "buy", "value": 1200}],
        "estimated_tax": "38.00", "target_weights": {"VTI": 0.6, "BND": 0.4}}}
    ticket, allocation = views.views_for("rebalance", rebalance)
    assert [r["label"]["en"] for r in ticket["data"]["rows"]] == ["Sell VTI", "Buy BND"]
    assert ticket["data"]["rows"][1]["value"]["v"] == 1200 and ticket["data"]["total"]["value"]["v"] == "38.00"
    assert [r["share"] for r in allocation["data"]["rows"]] == [0.6, 0.4]
    location = {"status": "ready", "result": {"currency": "USD", "placements": [
        {"instrument_id": "BND", "account_id": "ira", "value": "5000"}], "annual_tax_saved": "120"}}
    [placed] = views.views_for("asset_location", location)
    assert placed["data"]["rows"][0]["label"] == "BND → ira"
    assert views.views_for("rebalance", {"status": "ready", "result": {"trades": "nope"}}) == []
    assert views.views_for("asset_location", {"status": "ready", "result": {}}) == []


def test_validate_rejects_malformed_specs():
    [spec] = _specs("performance")
    views.validate(spec)
    for mutate in (
        lambda s: s.update(id="Performance-XYZ"),
        lambda s: s.update(kind="pie"),
        lambda s: s.update(extra=1),
        lambda s: s["data"]["rows"].append({"label": "x", "value": {"t": "money", "v": "12abc"}}),
        lambda s: s["data"]["rows"].__setitem__(0, {"label": "", "value": {"t": "money", "v": "1"}}),
        lambda s: s["data"].update(rows=[]),
    ):
        bad = copy.deepcopy(spec)
        mutate(bad)
        with pytest.raises(ValueError):
            views.validate(bad)


def test_placed_ids_need_their_own_line_and_are_bounded():
    a, b, c = "dca-0123456789", "tax-abcdefabcd", "spending-ffffffffff"
    text = f"Intro\n\n[[view:{a}]]\nsee [[view:{b}]] inline\n  [[view:{b}]]  \n[[view:{a}]]\n[[view:{c}]]\n"
    assert views.placed_ids(text) == [a, b]  # deduplicated, inline mention ignored, at most two
    assert views.placed_ids(text, {b: {}, c: {}}) == [b, c]  # only ids that were offered
    assert views.placed_ids("[[view:../../etc]]") == []


def test_svg_escapes_text_and_draws_only_known_primitives():
    [spec] = _specs("spending_recurring")
    spec = copy.deepcopy(spec)
    spec["data"]["rows"][0]["label"] = '<script>alert("x")</script>'
    svg = views.render_svg(spec, "es")
    assert "<script" not in svg and "&lt;script&gt;" in svg
    assert svg.startswith("<svg") and "href" not in svg and "foreignObject" not in svg
    assert "Cargos recurrentes" in svg and ">Gastos<" in svg  # the source line reads in sentence case
    for name in EXPECTED:
        for spec in _specs(name):
            for lang in ("en", "es"):
                assert views.render_svg(spec, lang).endswith("</svg>")


def test_png_rendering_when_pillow_is_present():
    pytest.importorskip("PIL")
    for name in ("dca_backtest", "lot_selection", "debt_payoff", "project", "spending_monthly"):
        for spec in _specs(name):
            png = views.render_png(spec, "es")
            assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 2000


def test_money_and_dates_are_formatted_per_language():
    assert views.format_value({"t": "money", "v": "-110.00", "cur": "USD"}, "en") == "−$110"
    assert views.format_value({"t": "money", "v": "97.30", "cur": "MXN"}, "en") == "$97.30"
    assert views.format_value({"t": "ratio", "v": "0.233333"}, "es") == "23.3%"
    assert views.format_value({"t": "months", "v": 7}, "es") == "7 meses"
    assert views.format_date("2027-04", "es") == "abr 2027" and views.format_date("2026-01-15", "en") == "Jan 15, 2026"


def test_service_run_lists_the_views_the_parser_rebuilds(tmp_path):
    from tests import test_dca as td

    prices = {"VOO-SIC": {"2026-01-15": 100, "2026-02-16": 80, "2026-03-16": 120}}
    report = WealthService(tmp_path / "w.sqlite3").run("dca", {
        "view": "backtest", "plan": dict(td.PLAN, end_date="2026-03-15"), "prices": prices,
        "start": "2026-01-15", "end": "2026-03-16"})
    assert [v["kind"] for v in report["views"]] == ["ticket", "series"]
    assert set(report["views"][0]) == {"id", "kind", "title"} and isinstance(report["views"][0]["title"], str)
    # The model receives the envelope as JSON; the parser rebuilds the same specs from it.
    item = {"type": "mcp_tool_call", "server": "wealth", "tool": "wealth_run", "status": "completed",
            "arguments": {"task": "dca", "inputs": {}},
            "result": {"content": [], "structured_content": json.loads(json.dumps(report))}}
    parser = agent._TurnParser()
    events = parser.feed(json.dumps({"type": "item.completed", "item": item}))
    [event] = [e for e in events if e.type == "view"]
    assert [s["id"] for s in event.data["views"]] == [v["id"] for v in report["views"]]
    assert event.data["views"][0]["data"]["rows"][0]["value"]["v"] == report["result"]["invested"]


def test_parser_emits_views_only_for_successful_results():
    envelope = copy.deepcopy(FIXTURES["debt_payoff"]["envelope"])

    def completed(**overrides):
        item = {"type": "mcp_tool_call", "server": "wealth", "tool": "wealth_run", "status": "completed",
                "arguments": json.dumps({"task": "debt_payoff", "inputs": {"monthly_amount": 10000}}),
                "result": {"content": [{"type": "text", "text": json.dumps(envelope)}]}}
        item.update(overrides)
        return json.dumps({"type": "item.completed", "item": item})

    kinds = lambda line: [e.type for e in agent._TurnParser().feed(line)]  # noqa: E731
    parser = agent._TurnParser()
    [view] = [e for e in parser.feed(completed()) if e.type == "view"]
    assert view.data["views"][0]["kind"] == "payoff"
    assert "view" not in kinds(completed(status="failed"))
    assert "view" not in kinds(completed(error={"message": "boom"}))
    assert "view" not in kinds(completed(result={"isError": True, "content": []}))
    started = json.dumps({"type": "item.started", "item": json.loads(completed())["item"]})
    assert "view" not in kinds(started)
    context = {"type": "mcp_tool_call", "server": "wealth", "tool": "wealth_context", "status": "completed",
               "arguments": {"client_id": "ana", "intent": "situation"},
               "result": {"structured_content": {"situation": FIXTURES["situation"]["envelope"]}}}
    [view] = [e for e in agent._TurnParser().feed(json.dumps({"type": "item.completed", "item": context}))
              if e.type == "view"]
    assert [s["kind"] for s in view.data["views"]] == ["ticket", "ticket"]


def test_prompt_lists_offered_views_and_stream_announces_them():
    specs = views.views_for("situation", FIXTURES["situation"]["envelope"])
    prompt = agent.build_prompt("How am I doing?", "ana", views=specs, brief="Net worth: 290000 MXN")
    assert "<views>" in prompt and all(s["id"] in prompt for s in specs)
    assert "<views>" not in agent.build_prompt("How am I doing?", "ana", brief="x")


def test_instructions_tell_the_model_to_place_not_draw():
    text = agent.conversation_instructions().read_text(encoding="utf-8")
    assert "[[view:<id>]]" in text and "at most two" in text
    assert "## Views" not in agent.memory_instructions().read_text(encoding="utf-8")
