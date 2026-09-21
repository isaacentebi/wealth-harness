"""Today lines, the quarterly review page and Conexiones: endpoints, payload shapes and page safety."""
from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import date
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from wealth import connectors, ledger as ledger_module, profile, views, web
from wealth.catalog import _MX_REVIEW_EXAMPLE, _MX_REVIEW_FACTS, _MX_REVIEW_LEDGER
from wealth.service import WealthService
from wealth.store import WealthStore

from test_web_streaming import _post, serving

ROOT = Path(web.__file__).parent
TODAY = date(2026, 9, 21)
REVIEW_EXTRA = {k: _MX_REVIEW_EXAMPLE[k] for k in ("prices", "benchmarks", "ips", "fees", "goal_accounts", "tax", "sic_listed")}


@pytest.fixture(autouse=True)
def no_keychain(monkeypatch):
    """Connector status never reads the real keychain in tests."""
    def status(name=None):
        rows = [{"name": n, "institution": i, "read_only": True, "token": "keychain" if n == "ibkr_flex" else "missing",
                 "ready": n == "ibkr_flex", "needs": [], "setup": ["never shown"]}
                for n, i in (("alpaca", "Alpaca"), ("cuenca", "Cuenca"), ("ibkr_flex", "Interactive Brokers"))]
        return [r for r in rows if name in (None, r["name"])]
    monkeypatch.setattr(connectors, "status", status)


def _seed(db, client="ana"):
    service = WealthService(db)
    service.create(client, "Ana")
    source = {"kind": "user", "ref": "chat", "observed_on": "2026-09-10"}
    service.remember(client, [dict(fact, source=source) for fact in deepcopy(_MX_REVIEW_FACTS)])
    entries = [dict({k: v for k, v in e.items() if k not in {"id", "confidence", "source"}}, external_id=e["id"])
               for e in _MX_REVIEW_LEDGER["entries"]]
    entries += [{"account_id": "bbva", "kind": "income", "date": d, "amount": "60000", "currency": "MXN",
                 "subtype": "salary", "description": "PAGO DE NOMINA ACME", "external_id": f"q3-{d}"}
                for d in ("2026-07-01", "2026-08-01", "2026-09-01")]
    with WealthStore(db) as store:
        ledger_module.post(store, client, {
            "batch_id": "seed", "source": {"kind": "document", "ref": "estado de cuenta", "observed_on": "2026-09-10"},
            "accounts": _MX_REVIEW_LEDGER["accounts"], "instruments": _MX_REVIEW_LEDGER["instruments"],
            "transactions": entries, "fx": _MX_REVIEW_LEDGER["fx"]})
    return service


def _get(base, path, token=None, headers=None):
    return urlopen(Request(base + path, headers={**({"X-Wealth-Token": token} if token else {}), **(headers or {})}),
                   timeout=20)


def _json(response):
    with response:
        return json.loads(response.read())


# ------------------------------------------------------------------ endpoints: auth, Host, persistence


def test_surface_reads_need_the_token_and_a_local_host(tmp_path):
    db = tmp_path / "w.sqlite3"
    _seed(db)
    chat = web.Chat(db, "ana")
    with serving(chat) as (base, server):
        for path in ("/api/today", "/api/review", "/api/connections", "/api/export"):
            with pytest.raises(HTTPError) as missing:
                _get(base, path)
            assert missing.value.code == 403
            with pytest.raises(HTTPError) as wrong:
                _get(base, path, "not-the-token")
            assert wrong.value.code == 403
            with pytest.raises(HTTPError) as rebound:
                _get(base, path, chat.token, {"Host": f"evil.example:{server.server_port}"})
            assert rebound.value.code == 403
        with pytest.raises(HTTPError) as foreign:
            _get(base, "/review", headers={"Host": "evil.example"})
        assert foreign.value.code == 403
        with _get(base, "/review") as page:
            assert b"<html" in page.read().lower()
        with pytest.raises(HTTPError) as post:
            _post(base, "/api/today", "wrong", b'{"dismiss": "surplus"}')
        assert post.value.code == 403
        with pytest.raises(HTTPError) as origin:
            _post(base, "/api/today", chat.token, b'{"dismiss": "surplus"}', headers={"Origin": "http://evil.example"})
        assert origin.value.code == 403


def test_dismiss_and_snooze_persist_and_restore_brings_a_line_back(tmp_path):
    db = tmp_path / "w.sqlite3"
    _seed(db)
    chat = web.Chat(db, "ana")
    with serving(chat) as (base, _):
        first = _json(_get(base, "/api/today", chat.token))
        ids = [item["id"] for item in first["items"]]
        assert 0 < len(ids) <= 3
        target = ids[0]
        after = _json(_post(base, "/api/today", chat.token, json.dumps({"dismiss": target}).encode()))
        assert target not in [i["id"] for i in after["items"]]
        # A later snooze hides a second line for seven days.
        if len(after["items"]) > 1:
            other = after["items"][0]["id"]
            snoozed = _json(_post(base, "/api/today", chat.token, json.dumps({"snooze": other}).encode()))
            assert other not in [i["id"] for i in snoozed["items"]]
    # Persisted per client: a new server over the same database still hides it.
    again = web.Chat(db, "ana")
    with serving(again) as (base, _):
        reread = _json(_get(base, "/api/today", again.token))
        assert target not in [i["id"] for i in reread["items"]]
        restored = _json(_post(base, "/api/today", again.token, json.dumps({"restore": target}).encode()))
        assert target in [i["id"] for i in restored["items"]] or len(restored["items"]) == 3
    state = WealthService(db).run("today", {}, "ana")["result"]
    assert all(h["id"] != target for h in state["hidden"])


def test_today_post_validates_its_body(tmp_path):
    db = tmp_path / "w.sqlite3"
    _seed(db)
    chat = web.Chat(db, "ana")
    with serving(chat) as (base, _):
        for body in (b"{}", b'{"dismiss": "a", "snooze": "b"}', b'{"dismiss": 3}', b'{"hide": "a"}',
                     b'{"dismiss": "not-a-current-item"}'):
            with pytest.raises(HTTPError) as bad:
                _post(base, "/api/today", chat.token, body)
            assert bad.value.code == 400


def test_review_endpoint_validates_the_quarter(tmp_path):
    db = tmp_path / "w.sqlite3"
    _seed(db)
    chat = web.Chat(db, "ana")
    with serving(chat) as (base, _):
        default = _json(_get(base, "/api/review", chat.token))
        assert re.match(r"^\d{4}-Q[1-4]$", default["period"]) and default["period"] in default["quarters"]
        for bad in ("2026-Q5", "Q2-2026", "2099-Q1", "2026-Q2%27"):
            with pytest.raises(HTTPError) as error:
                _get(base, "/api/review?period=" + bad, chat.token)
            assert error.value.code == 400


def test_export_downloads_the_full_private_export(tmp_path):
    db = tmp_path / "w.sqlite3"
    _seed(db)
    chat = web.Chat(db, "ana")
    with serving(chat) as (base, _):
        with _get(base, "/api/export", chat.token) as response:
            assert "attachment" in response.headers["Content-Disposition"]
            assert response.headers["Cache-Control"] == "no-store"
            export = json.loads(response.read())
    assert export["ledger"]["entries"] and export["facts"]


# ------------------------------------------------------------------ payload shapes


def test_today_payload_has_only_what_the_lines_draw(tmp_path):
    service = _seed(tmp_path / "w.sqlite3")
    view = profile.today_view(service, "ana")
    assert set(view) == {"as_of", "items"} and len(view["items"]) <= 3
    for item in view["items"]:
        assert set(item) == {"id", "severity", "title", "emphasis", "next_step", "due", "days"}
        assert item["severity"] in {"act", "consider", "fyi"}
        for lang in ("en", "es"):
            assert item["title"][lang] and item["next_step"][lang]
            for a, b in item["emphasis"][lang]:
                assert 0 <= a < b <= len(item["title"][lang])
        # A due date is shown only inside fourteen days.
        assert item["due"] is None or 0 <= item["days"] <= 14
    assert profile._value_spans("Tienes $312,712 sin destino") == [[7, 15]]
    assert profile._value_spans("Fondo de emergencia: 3.7 de 6 meses") == [[21, 35]]


def test_review_payload_sections_use_view_values_and_unknown_is_null(tmp_path):
    service = _seed(tmp_path / "w.sqlite3")
    plain = profile.review_view(service, "ana", "2026-Q2", today=TODAY)
    assert plain["quarters"][0] == "2026-Q2" and plain["period"] == "2026-Q2"
    sections = plain["sections"]
    assert set(sections) == {"net_worth", "performance", "allocation", "cash_flow", "goals", "decisions", "dca", "taxes",
                             "fees", "next_quarter"}
    # Without prices the value is unknown (null, drawn as "—") and the section says why in one line.
    nw = sections["net_worth"]
    assert nw["total"]["value"] == {"t": "money", "v": None, "cur": "MXN"}
    assert nw["note"]["es"] and "CETES" in nw["note"]["en"]
    assert all(s["weight"]["v"] is None for s in sections["allocation"]["sleeves"])
    assert sections["cash_flow"]["current"]["savings_rate"] == {"t": "ratio", "v": "0.4500"}
    assert plain["letter"]["narrative"] is None and len(plain["letter"]["summary"]["es"]) == 2

    rich = profile.review_view(service, "ana", "2026-Q2", today=TODAY, inputs=REVIEW_EXTRA)
    nw = rich["sections"]["net_worth"]
    values = [row["value"]["v"] for row in nw["rows"]]
    from decimal import Decimal
    assert sum(Decimal(v) for v in values) == Decimal(nw["total"]["value"]["v"])  # start + contributions + market (+ income) = end
    assert nw["note"] is None
    sleeves = rich["sections"]["allocation"]["sleeves"]
    assert {s["id"] for s in sleeves} == {"global_equity", "mx_fixed_income", "cash"}
    assert all(s["min"]["v"] is not None and s["max"]["v"] is not None for s in sleeves)
    assert rich["sections"]["fees"]["bps"]["lo"] is not None
    two = rich["sections"]["next_quarter"]["items"]
    assert len(two) == 2 and all(i["title"]["es"] and i["prompt"]["es"] and i["prompt"]["en"] for i in two)
    assert rich["letter"]["summary"]["es"][0].startswith("Tu patrimonio pasó de $1,375,160 a $1,478,578")
    for spec_values in (row["value"] for row in nw["rows"]):
        assert views._value_ok(spec_values)


def test_review_needs_a_ledger_and_an_ended_quarter(tmp_path):
    service = WealthService(tmp_path / "w.sqlite3")
    service.create("new", "New")
    empty = profile.review_view(service, "new", today=TODAY)
    assert empty["status"] == "needs_input" and empty["sections"] is None and empty["quarters"] == []
    with pytest.raises(ValueError):
        profile.review_view(service, "new", "2026-Q3", today=TODAY)
    assert profile.quarter_bounds("2026-Q4") == (date(2026, 10, 1), date(2026, 12, 31))
    assert profile.review_quarters(date(2025, 11, 3), TODAY) == ["2026-Q2", "2026-Q1", "2025-Q4"]


def test_connections_payload_never_carries_a_secret(tmp_path, monkeypatch):
    service = _seed(tmp_path / "w.sqlite3")
    monkeypatch.setenv("WEALTH_ALPACA_SECRET", "sk-live-SHOULD-NEVER-APPEAR")
    view = profile.connections_view(service, "ana", today=TODAY)
    assert [c["name"] for c in view["connectors"]] == ["ibkr_flex", "alpaca", "cuenca"]
    ibkr = view["connectors"][0]
    assert ibkr["configured"] is True and ibkr["via"] == "keychain"
    assert set(ibkr) == {"name", "institution", "configured", "via", "last_sync", "accounts", "setup"}
    assert ibkr["setup"]["commands"] == ['security add-generic-password -U -s wealth-ibkr-flex -a "$USER" -w']
    assert [a["id"] for a in ibkr["accounts"]] == ["ibkr"]
    assert {a["id"] for a in view["statements"]["accounts"]} == {"bbva", "gbm", "afore"}
    for account in ibkr["accounts"] + view["statements"]["accounts"]:
        assert set(account) == {"id", "label", "institution", "as_of", "days", "stale"}
    assert view["data"]["forget_command"] == "uv run wealth forget --client ana"
    assert "SHOULD-NEVER-APPEAR" not in json.dumps(view) and "never shown" not in json.dumps(view)
    # The commands match the documented setup exactly.
    docs = (ROOT.parent / "docs" / "cli.md").read_text()
    for connector in view["connectors"]:
        for command in connector["setup"]["commands"]:
            assert command in docs


# ------------------------------------------------------------------ pages: safe DOM, print, isolation


def _script(name):
    page = (ROOT / name).read_text()
    script = "\n".join(re.findall(r"<script>(.*?)</script>", page, re.S))
    return page, re.sub(r"(?m)(^|\s)//.*$", r"\1", script)  # comments may name what the code avoids


@pytest.mark.parametrize("name", ["review.html", "profile.html"])
def test_pages_never_write_markup(name):
    _, script = _script(name)
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert sink not in script


def test_chat_today_block_writes_text_only_and_stays_in_one_place():
    page = (ROOT / "chat.html").read_text()
    block = page[page.index("// ------------------------------------------------------------------ today (Hoy)"):
                 page.index("// ------------------------------------------------------------------ start")]
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "location.search"):
        assert sink not in block
    # One insertion point in the existing flow; hidden while a turn runs without moving the layout.
    assert page.count("startToday(") == 2 and page.count("await startToday(state);") == 1
    assert ".dock:has(.send.stopping) .hoy { visibility: hidden; }" in page
    assert ".app:has(.messages .setup) .hoy { display: none; }" in page
    assert "sessionStorage" in block and "days: 7" not in block  # snooze length is the server's


def test_review_page_prints_on_a4_and_letter_with_the_browser_dialog():
    page, script = _script("review.html")
    assert "@media print" in page and "@page" in page
    margins = re.search(r"@page \{ margin: ([^;]+); \}", page).group(1)
    assert "mm" in margins and "size:" not in page.split("@page", 1)[1].split("}", 1)[0]  # the paper is the reader's
    assert "window.print()" in script and "printToPDF" not in page
    assert "max-width: 180mm" in page  # fits A4 (210mm) and Letter (216mm) inside the 15mm margins
    assert "data-slot': 'narrative'" in script


def test_profile_never_shows_or_takes_a_secret():
    page, script = _script("profile.html")
    assert 'type="password"' not in page and "type: 'password'" not in script
    assert "security add-generic-password" not in page  # the commands come from the server, from the docs
    assert "'/api/connections'" in script and "'/api/export'" in script and "forget_command" in script
