"""Browser chat views: stored on the message only when placed, a views event, the image endpoint, page safety."""
from __future__ import annotations

import http.client
import json
import re
import threading
from contextlib import contextmanager
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from wealth import agent, views, web
from wealth.agent import TurnEvent

FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "views_envelopes.json").read_text(encoding="utf-8"))
PAGE = Path(web.__file__).with_name("chat.html").read_text(encoding="utf-8")


def _specs(name):
    case = FIXTURES[name]
    return views.views_for(case["task"], case["envelope"])


@contextmanager
def serving(chat):
    server = web.create_server(chat, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", server
    finally:
        turn = chat.turn
        if turn is not None and not turn.finished:
            turn.control.cancel()
        server.shutdown()
        server.server_close()
        thread.join()


def _events(base, turn_id, token):
    request = Request(f"{base}/api/turns/{turn_id}/events", headers={"X-Wealth-Token": token})
    with urlopen(request, timeout=10) as response:
        out = []
        for block in response.read().decode().split("\n\n"):
            data = [line[6:] for line in block.splitlines() if line.startswith("data: ")]
            if data:
                out.append(json.loads(data[0]))
        return out


def _chat(tmp_path, monkeypatch, answer, offered):
    seen = {}

    def fake_stream(message, **kwargs):
        seen.update(kwargs)
        yield TurnEvent("progress", "Running the numbers")
        yield TurnEvent("view", data={"views": offered})
        yield TurnEvent("answer", answer)

    monkeypatch.setattr(web, "run_turn", agent.run_turn)
    monkeypatch.setattr(web, "stream_turn", fake_stream)
    return web.Chat(tmp_path / "w.sqlite3", "personal"), seen


def test_message_stores_only_placed_views_and_the_stream_emits_them(tmp_path, monkeypatch):
    backtest, prices = _specs("dca_backtest")
    [payoff] = _specs("debt_payoff")
    answer = (f"Buying monthly won by $500.\n\n[[view:{prices['id']}]]\n\nIt also mentions [[view:{payoff['id']}]] "
              f"inline.\n[[view:dca-0000000000]]\n[[view:{prices['id']}]]")
    chat, seen = _chat(tmp_path, monkeypatch, answer, [backtest, prices, payoff])
    with serving(chat) as (base, _):
        body = json.dumps({"message": "Would monthly investing have won?"}).encode()
        request = Request(base + "/api/turns", data=body, method="POST",
                          headers={"Content-Type": "application/json", "X-Wealth-Token": chat.token})
        turn = json.load(urlopen(request, timeout=10))["turn"]
        events = _events(base, turn["id"], chat.token)
        assert [e["type"] for e in events] == ["progress", "views", "answer", "done"]
        assert [v["id"] for v in events[1]["items"]] == [prices["id"]]
        message = events[2]["message"]
        assert events[1]["message_id"] == message["id"]
        assert [v["id"] for v in message["views"]] == [prices["id"]]  # unplaced and unknown ids never travel
        assert message["views"][0] == prices
        state = json.load(urlopen(base + "/api/state", timeout=5))
        assert [v["id"] for v in state["messages"][-1]["views"]] == [prices["id"]]
    assert "views" in seen  # the saved picture's views are offered to the turn


def test_answer_without_placements_carries_no_views(tmp_path, monkeypatch):
    chat, _ = _chat(tmp_path, monkeypatch, "Plain answer.", _specs("performance"))
    turn = chat.start("How did I do?")
    turn.wait(10)
    assert "views" not in chat.messages[-1]
    assert not any(e["type"] == "views" for e in turn.events)
    assert chat.view(_specs("performance")[0]["id"]) is None


def test_placements_are_capped_at_two(tmp_path, monkeypatch):
    specs = _specs("dca_backtest") + _specs("project")
    answer = "\n\n".join(f"[[view:{s['id']}]]" for s in specs)
    chat, _ = _chat(tmp_path, monkeypatch, answer, specs)
    chat.ask("Show me everything")
    assert [v["id"] for v in chat.messages[-1]["views"]] == [s["id"] for s in specs[:2]]


def test_situation_views_are_offered_from_the_saved_picture(tmp_path):
    from tests import test_situation as ts

    service = ts._client(tmp_path, dict(ts.CANONICAL))
    offered = agent.situation_views(service.db_path, "ana")
    assert [s["kind"] for s in offered] == ["ticket", "ticket"]
    assert offered[0]["data"]["total"]["value"]["v"] == service.situation("ana")["net_worth"]["total"]
    assert agent.situation_views(tmp_path / "missing.sqlite3", "nobody") == []


def test_render_endpoint_needs_token_and_local_host(tmp_path, monkeypatch):
    [lots] = _specs("lot_selection")
    unplaced = _specs("performance")[0]
    chat, _ = _chat(tmp_path, monkeypatch, f"Sell the December lot.\n\n[[view:{lots['id']}]]", [lots, unplaced])
    chat.ask("Which shares should I sell?")
    with serving(chat) as (base, server):
        path = f"/api/views/{lots['id']}.svg"
        with pytest.raises(HTTPError) as denied:
            urlopen(Request(base + path), timeout=5)
        assert denied.value.code == 403
        with pytest.raises(HTTPError) as wrong:
            urlopen(Request(base + path, headers={"X-Wealth-Token": "nope"}), timeout=5)
        assert wrong.value.code == 403
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request("GET", path, headers={"Host": f"evil.example:{server.server_port}", "X-Wealth-Token": chat.token})
        assert connection.getresponse().status == 403  # DNS rebinding: a foreign Host is refused
        connection.close()
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request("GET", path, headers={"X-Wealth-Token": chat.token, "Origin": "http://evil.example"})
        assert connection.getresponse().status == 403
        connection.close()

        with urlopen(Request(base + path + "?lang=en", headers={"X-Wealth-Token": chat.token}), timeout=5) as response:
            assert response.headers.get_content_type() == "image/svg+xml"
            assert response.headers["X-Content-Type-Options"] == "nosniff"
            assert "default-src 'self'" in response.headers["Content-Security-Policy"]
            svg = response.read().decode()
        assert svg.startswith("<svg") and "Which shares to sell" in svg and "<script" not in svg
        with urlopen(Request(base + path.replace(".svg", ".svg?lang=es"), headers={"X-Wealth-Token": chat.token}),
                     timeout=5) as response:
            assert "Qué acciones vender" in response.read().decode()

        png_path = path.replace(".svg", ".png")
        if views.png_available():
            with urlopen(Request(base + png_path, headers={"X-Wealth-Token": chat.token}), timeout=10) as response:
                assert response.headers["Content-Type"] == "image/png"
                assert response.read()[:8] == b"\x89PNG\r\n\x1a\n"
        # A view the answer did not place, an unknown id, or a malformed path is not served.
        for missing in (f"/api/views/{unplaced['id']}.svg", "/api/views/tax-0000000000.svg"):
            with pytest.raises(HTTPError) as gone:
                urlopen(Request(base + missing, headers={"X-Wealth-Token": chat.token}), timeout=5)
            assert gone.value.code == 404
        for bad in ("/api/views/..%2F..%2Fetc.svg", f"/api/views/{lots['id']}.gif", "/api/views/TAX-0000000000.svg"):
            with pytest.raises(HTTPError) as nope:
                urlopen(Request(base + bad, headers={"X-Wealth-Token": chat.token}), timeout=5)
            assert nope.value.code == 404


def test_png_falls_back_to_a_clear_error_without_pillow(tmp_path, monkeypatch):
    [lots] = _specs("lot_selection")
    chat, _ = _chat(tmp_path, monkeypatch, f"[[view:{lots['id']}]]", [lots])
    chat.ask("Which shares?")
    monkeypatch.setattr(web, "png_available", lambda: False)
    with serving(chat) as (base, _):
        with pytest.raises(HTTPError) as unsupported:
            urlopen(Request(f"{base}/api/views/{lots['id']}.png", headers={"X-Wealth-Token": chat.token}), timeout=5)
        assert unsupported.value.code == 501
        assert ".svg" in json.loads(unsupported.value.read())["error"]


def _function(name):
    start = PAGE.index(f"function {name}(")
    depth, i = 0, PAGE.index("{", start)
    while True:
        depth += {"{": 1, "}": -1}.get(PAGE[i], 0)
        if depth == 0:
            return PAGE[start:i + 1]
        i += 1


def test_page_draws_views_with_dom_and_svg_only():
    section = PAGE[PAGE.index("// ------------------------------------------------------------------ views"):
                   PAGE.index("// ------------------------------------------------------------------ memory line")]
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function",
                 "setAttribute('href'", "setAttribute('on", ".src ="):
        assert sink not in section
    assert "createElementNS(SVG_NS" in section
    # The placement syntax is the engine's, character for character.
    js = re.search(r"const VIEW_LINE = /(.+)/;", section).group(1)
    assert js.replace("\\/", "/") == views.PLACEMENT.pattern.replace("^[ \\t]*", "^[ \\t]*")
    assert "const MAX_PLACED = 2;" in section and views.MAX_VIEWS == 2
    # Only placed views render; unknown, repeated or excess placements vanish.
    render = _function("renderAnswer")
    assert "specs.get(match[1])" in render and "shown.size >= MAX_PLACED" in render
    # Every view has a screen-reader table; the drawing itself is hidden from assistive tech.
    view = _function("renderView")
    assert "viewDataTable(title, head, rows)" in view and "setAttribute('aria-hidden', 'true')" in view
    # Unknown is a dash plus a note, never zero.
    assert "return '—'" in _function("viewValue") and "STRINGS[lang].views.unknown" in view
    assert "assistantMessage(item.content, { memory: item.memory, message: item, lang: spoken, views: item.views })" in PAGE
    assert "event.type === 'views'" in PAGE


def test_page_view_styles_follow_dot():
    css = PAGE[PAGE.index("/* Engine-drawn views"):PAGE.index("/* Multi-column tables stay typographic")]
    # Cobalt only marks a live/current point; everything else is ink and hairline.
    assert [line.strip().split("{")[0].strip() for line in css.splitlines() if "var(--cobalt)" in line] == \
        [".view .series-plot .mark.live"]
    assert "var(--vermilion)" not in css and "gradient" not in css and "box-shadow: 0 0 0 2px var(--surface)" in css
    assert re.search(r"@media \(max-width: 600px\) \{\s*\.view \.compare-table \{ display: none; \}", css)
    # Markdown tables of three or more columns stack on a phone instead of being cut off.
    assert "width >= 3 ? 'table-wrap stack' : 'table-wrap'" in PAGE and "content: attr(data-label)" in PAGE


def test_page_view_strings_exist_in_both_languages():
    blocks = re.findall(r"views: \{(.*?)\n        \},", PAGE, re.S)
    assert len(blocks) == 2
    keys = [set(re.findall(r"(\w+):", block)) for block in blocks]
    assert keys[0] == keys[1] and {"unknown", "best", "months", "debtFree", "interest"} <= keys[0]
    for block in blocks:
        used = set(re.findall(r"\bS\.(\w+)", PAGE[PAGE.index("function allocationView"):PAGE.index("const VIEW_KINDS")]))
        assert used <= set(re.findall(r"(\w+):", block)), used - set(re.findall(r"(\w+):", block))


def test_series_reference_is_drawn_as_the_dashed_rule_the_legend_names():
    series = PAGE[PAGE.index("function seriesView("):PAGE.index("function comparisonView(")]
    # The legend and the rule share one condition, so neither appears without the other.
    assert series.count("if (ref !== null) {") == 1 and "el('span', 'ref')" in series
    assert "if (data.reference && ref !== null) body.append(el('p', 'series-legend'" in series
    css = PAGE[PAGE.index("/* Engine-drawn views"):PAGE.index("/* Multi-column tables stay typographic")]
    assert ".view .series-plot .ref { position: absolute; left: 0; right: 0; border-top: 1px dashed var(--muted); }" in css
    assert "border-top: 1px dashed var(--muted); }" in css.split(".view .series-legend::before")[1].split("\n")[0]
    # No other view carries a legend without a plot behind it.
    for name in ("allocationView", "ticketView", "comparisonView", "payoffView"):
        body = PAGE[PAGE.index(f"function {name}("):]
        body = body[:body.index("\n    }\n")]
        assert "legend" not in body
