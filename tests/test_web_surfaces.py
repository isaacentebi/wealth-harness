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


def test_review_names_read_in_the_page_language_and_never_show_fixture_notes(tmp_path):
    service = _seed(tmp_path / "w.sqlite3")
    rich = profile.review_view(service, "ana", "2026-Q2", today=TODAY, inputs=REVIEW_EXTRA)
    sections = rich["sections"]
    names = {s["id"]: s["name"] for s in sections["allocation"]["sleeves"]}
    assert names["global_equity"] == {"en": "Global equity", "es": "Renta variable global"}
    assert names["mx_fixed_income"] == {"en": "Mexican government fixed income", "es": "Deuda gubernamental mexicana"}
    assert names["cash"] == {"en": "Cash (MXN)", "es": "Efectivo (MXN)"}
    # The benchmark is structured (weights + index names), never the engine's free-text label.
    bench = sections["performance"]["benchmark"]
    assert "name" not in bench and bench["label"]["es"] == "Referencia de tu política"
    assert [p["weight"]["v"] for p in bench["parts"]] == [0.6, 0.35, 0.05]
    assert [p["index"]["es"] for p in bench["parts"]] == ["MSCI ACWI en MXN", "CETES 364 días", "CETES 28 días"]
    assert "fictional" not in json.dumps(sections, ensure_ascii=False)
    # A recurring plan reads by what it buys and how often, not by its id.
    assert sections["dca"]["items"][0]["name"] == {"en": "S&P 500 monthly", "es": "S&P 500 mensual"}
    assert "sp500" not in json.dumps(sections, ensure_ascii=False)
    # Only free text (no structured spec): a short generic label, no parts.
    plain = profile.review_view(service, "ana", "2026-Q2", today=TODAY,
                                inputs={k: v for k, v in REVIEW_EXTRA.items() if k != "benchmarks"})
    bench = plain["sections"]["performance"]["benchmark"]
    assert bench["parts"] == [] and bench["label"]["es"] == "Referencia de tu política"


def test_review_labels_read_both_ways_and_keep_the_persons_own_words():
    assert profile._label("Renta variable global") == {"en": "Global equity", "es": "Renta variable global"}
    assert profile._label(None, "fixed_income") == {"en": "Fixed income", "es": "Renta fija"}
    assert profile._label("Mi colchón", "cash") == {"en": "Mi colchón", "es": "Mi colchón"}
    assert profile._index_label("Global aggregate bonds in MXN (fictional levels)") == {
        "en": "Global aggregate bonds in MXN", "es": "Bonos globales agregados en MXN"}
    assert profile._index_label("ips.cash") is None
    named = profile._plan_names({"plans": [{"id": "p", "name": "Ahorro niños", "cadence": "monthly", "legs": []}]}, [])
    assert named == {"p": {"en": "Ahorro niños", "es": "Ahorro niños"}}
    weekly = profile._plan_names([{"id": "q", "cadence": "weekly", "legs": [{"instrument_id": "X1"}]}],
                                 [{"id": "X1", "underlying_symbol": "CNDX"}])
    assert weekly == {"q": {"en": "Nasdaq-100 weekly", "es": "Nasdaq-100 semanal"}}


def test_review_page_draws_names_from_the_payload_language():
    _, script = _script("review.html")
    assert "L(x.name)" in script and "L(p.name)" in script and "L(b.label)" in script
    assert "benchmark?.name" not in script and "composition(b.parts)" in script


def test_chat_today_lines_fold_their_actions_behind_one_control_on_touch():
    page = (ROOT / "chat.html").read_text()
    block = page[page.index("// ------------------------------------------------------------------ today (Hoy)"):
                 page.index("// ------------------------------------------------------------------ start")]
    touch = "(hover: none), (pointer: coarse), (max-width: 560px)"
    assert f"@media {touch}" in page and f"window.matchMedia('{touch}')" in block
    css = page[page.index(f"@media {touch}"):page.index("</style>")]
    assert ".hoy-later, .hoy-x { display: none; }" in css  # on touch the line is the title and its date
    assert "-webkit-line-clamp: 2" in css and "text-overflow: ellipsis" not in css
    assert "width: 44px; height: 44px" in css  # the "⋯" target
    assert ".hoy li[hidden] { display: none; }" in page
    assert "'aria-expanded'" in block and "'Escape'" in block
    assert "HOY_SHARE = 0.3" in block and "más`" in block
    # Desktop keeps the hover-revealed pair.
    assert ".hoy li:not(:hover):not(:focus-within) :is(.hoy-later, .hoy-x) { opacity: 0; }" in page


def test_review_needs_a_ledger_and_an_ended_quarter(tmp_path):
    service = WealthService(tmp_path / "w.sqlite3")
    service.create("new", "New")
    empty = profile.review_view(service, "new", today=TODAY)
    assert empty["status"] == "needs_input" and empty["sections"] is None and empty["quarters"] == []
    with pytest.raises(ValueError):
        profile.review_view(service, "new", "2026-Q3", today=TODAY)
    assert profile.quarter_bounds("2026-Q4") == (date(2026, 10, 1), date(2026, 12, 31))
    assert profile.review_quarters(date(2025, 11, 3), TODAY) == ["2026-Q2", "2026-Q1", "2025-Q4"]


def _august_only(db, client="ana"):
    """A person whose only statement is August's: Q3 is under way and no quarter has closed."""
    service = WealthService(db)
    service.create(client, "Ana")
    service.remember(client, [{"key": "client.profile", "value": {"name": "Ana", "language": "es",
                                                                   "reporting_currency": "MXN"},
                               "source": {"kind": "user", "ref": "chat", "observed_on": "2026-09-10"}}])
    entries = [{"account_id": "bbva", "kind": "opening_balance", "date": "2026-07-31", "amount": "90000",
                "currency": "MXN", "external_id": "open"},
               {"account_id": "bbva", "kind": "income", "date": "2026-08-01", "amount": "60000", "currency": "MXN",
                "subtype": "salary", "description": "PAGO DE NOMINA ACME", "external_id": "n1"},
               {"account_id": "bbva", "kind": "expense", "date": "2026-08-28", "amount": "-25000", "currency": "MXN",
                "description": "RENTA DEPTO", "external_id": "r1"}]
    with WealthStore(db) as store:
        ledger_module.post(store, client, {
            "batch_id": "aug", "source": {"kind": "document", "ref": "estado de cuenta agosto", "observed_on": "2026-09-02"},
            "accounts": [_MX_REVIEW_LEDGER["accounts"][0]], "instruments": [], "transactions": entries, "fx": []})
    return service


def test_review_offers_the_quarter_in_progress_to_date(tmp_path):
    service = _august_only(tmp_path / "w.sqlite3")
    view = profile.review_view(service, "ana", today=TODAY)
    assert view["quarters"] == []  # no closed quarter yet ...
    assert view["period"] == "2026-Q3" and view["partial"] is True  # ... so the quarter so far is the review
    assert view["label"] == {"en": "Q3 2026 · to date", "es": "T3 2026 · en curso"}
    assert view["start"] == "2026-07-01" and view["end"] == "2026-08-28" and view["quarter_end"] == "2026-09-30"
    assert view["current"]["label"]["es"] == "T3 2026 · en curso" and view["current"]["end"] == "2026-08-28"
    assert view["sections"] is not None and view["status"] != "needs_input"
    assert view["sections"]["cash_flow"]["current"]["income"]["v"] == "60000.00"
    assert "lo que va del T3 2026" in " ".join(view["letter"]["summary"]["es"])
    assert "Q3 2026 so far" in " ".join(view["letter"]["summary"]["en"])


def test_closed_quarters_stay_the_letter_and_the_current_one_is_offered(tmp_path):
    service = _seed(tmp_path / "w.sqlite3")
    closed = profile.review_view(service, "ana", today=TODAY)
    assert closed["period"] == "2026-Q2" and closed["partial"] is False
    assert closed["label"] == {"en": "Q2 2026", "es": "T2 2026"} and closed["end"] == "2026-06-30"
    assert closed["current"]["period"] == "2026-Q3"  # offered alongside, not instead
    to_date = profile.review_view(service, "ana", "2026-Q3", today=TODAY)
    assert to_date["partial"] is True and to_date["end"] == "2026-09-01" and to_date["sections"] is not None
    page = (ROOT / "review.html").read_text()
    assert "data.current.period" in page and "L(data.current.label)" in page


def test_the_review_engine_takes_a_quarter_to_date():
    from wealth import review
    context = {"ledger": _MX_REVIEW_LEDGER, "currency": "MXN"}
    partial = review.quarterly(context, "2026-04-01", "2026-05-31")
    period = partial["result"]["period"]
    assert period["to_date"] is True and period["quarter_end"] == "2026-06-30" and period["end"] == "2026-05-31"
    assert partial["result"]["narrative_inputs"]["period"]["to_date"] is True
    # The comparison is the same stretch of the quarter before, not the two months just before.
    assert "The prior window is 2026-01-01..2026-02-28." in partial["result"]["sections"]["cash_flow"]["assumptions"]
    full = review.quarterly(context, "2026-04-01", "2026-06-30")
    assert full["result"]["period"]["to_date"] is False
    assert "The prior window is 2026-01-01..2026-03-31." in full["result"]["sections"]["cash_flow"]["assumptions"]


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


def test_delete_my_data_is_one_folded_line_and_the_hoy_value_is_bold_whole():
    page, script = _script("profile.html")
    data = script[script.index("function renderData("):script.index("function setLang(")]
    # One quiet line; the Terminal command and its copy button stay folded until the line is tapped.
    assert "'aria-expanded': 'false', 'aria-controls': 'erase-how'" in data and "hidden: true" in data
    assert "text: t('copy')" in data and "navigator.clipboard.writeText(command)" in data
    # Three quiet lines: the tax pack, the export, and the folded delete.
    assert data.count("class: 'data-row'") == 3 and ".erase-how[hidden] { display: none; }" in page
    assert "eraseLine: 'Only from the Terminal, so nothing does it by accident.'" in script
    # The emphasis span is snapped to the value it lands on, on both pages, with the same rule.
    chat = Path(web.__file__).with_name("chat.html").read_text(encoding="utf-8")
    for source in (script, chat):
        fn = source[source.index("function hoyEmphasis("):]
        fn = fn[:fn.index("\n    }\n")]
        assert "if (inNumber(a) && inNumber(a - 1)) { while (inNumber(a - 1)) a -= 1; }" in fn
        assert "while (inNumber(b) || word(b)) b += 1;" in fn and "/[$€£]/.test(at(a - 1))" in fn and "at(b) === '%'" in fn
    assert "const span = hoyEmphasis(title, (item.emphasis?.[lang] || [])[0]);" in script
    assert "const span = hoyEmphasis(title, ((item.emphasis || {})[LANG] || [])[0]);" in chat
