"""The design critique fixes: honest unknowns, one primary action, quiet chrome, and the You page above the fold."""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from tests._pages import page_text
from wealth import profile
from wealth.profile import picture as profile_picture

CHAT = page_text("chat")
YOU = page_text("profile")
REVIEW = page_text("review")


def _fn(page: str, name: str) -> str:
    start = page.index(f"function {name}(")
    depth, i = 0, page.index(") {", start) + 2
    while True:
        depth += {"{": 1, "}": -1}.get(page[i], 0)
        if depth == 0:
            return page[start:i + 1]
        i += 1


# ------------------------------------------------------------------ the type scale and the focus token


def test_every_font_size_names_one_of_seven_tokens():
    for page, keep in ((CHAT, set()), (YOU, {"48", "80"}), (REVIEW, {"36", "44"})):
        css = page[page.index("<style>"):page.index("</style>")]
        literal = set(re.findall(r"font(?:-size)?: (?:\d{3} )?(\d+)px", css))
        assert literal <= keep, literal - keep  # only the hero figure and the letter's headline stay bespoke
        assert all(f"--fs-{n}:" in css for n in range(1, 8))


def test_one_focus_token_and_no_vermilion_selection_marks():
    for page in (CHAT, YOU, REVIEW):
        assert re.search(r":focus-visible \{ outline: 2px solid var\(--ink\)", page)
    assert "outline: 2px solid var(--vermilion)" not in YOU
    assert "text-decoration-color: var(--vermilion)" not in YOU  # selection is ink
    assert ".sec { border-top: 1px solid var(--hairline);" in YOU and "details.disc { border-top: 1px solid var(--hairline);" in YOU


def test_uppercase_kickers_stay_only_where_they_carry_meaning():
    css = CHAT[CHAT.index("<style>"):CHAT.index("</style>")]
    uppercase = [line.strip().split("{")[0].strip() for line in css.splitlines() if "text-transform: uppercase" in line]
    assert uppercase == [".order .mode", ".order .typed input"]  # practice vs live money, and the typed LIVE phrase
    assert "text-transform: uppercase" not in YOU and "text-transform: uppercase" not in REVIEW


# ------------------------------------------------------------------ chat: the working state, orders, charts


def test_the_person_sees_their_words_before_anything_leaves_the_screen():
    submit = _fn(CHAT, "submit")
    assert submit.index("userMessage(message || T.attachedOnly, files)") < submit.index("removeIntro();") < submit.index("setBusy(true);")
    assert submit.index("pendingMessage('', turn.lang)") < submit.index("removeIntro();")
    # The screen-reader table of a view sits in a 1px clipped box: a table never shrinks, a div does.
    table = _fn(CHAT, "viewDataTable")
    assert "const box = el('div', 'sr-only');" in table and "el('table')" in table


def test_presence_is_one_plain_mark_and_the_wait_says_how_long():
    assert "PRESENCE = {" in CHAT and "housing" not in CHAT and "M15 5l5-2v18l-5-2z" not in CHAT
    assert "['circle', { cx: 12, cy: 12, r: 4, fill: COBALT, class: 'channel' }]" in CHAT
    pending = _fn(CHAT, "pendingMessage")
    assert "ELAPSED_AFTER" in pending and "el('span', 'elapsed')" in pending and "article._timer = setInterval(" in pending
    assert "const ELAPSED_AFTER = 15;" in CHAT


def test_depth_is_a_quiet_menu_that_stays_accessible():
    assert 'id="depth-toggle"' in CHAT and 'aria-haspopup="true"' in CHAT and 'aria-controls="depth"' in CHAT
    assert '<fieldset class="depth" id="depth" hidden>' in CHAT
    for level in ("low", "medium", "high"):
        assert f'id="depth-{level}"' in CHAT
    assert "event.key === 'Escape'" in _fn(CHAT, "setBusy") or "openDepth(false); depthToggle.focus()" in CHAT
    # A timeout suggests "Rápido" only when a deeper level is on.
    assert "error.kind === 'timeout' && depth() !== 'low' && T.errors.timeoutDeep" in CHAT


def test_saved_line_belongs_to_the_live_turn_and_falls_back_to_the_memory_event():
    body = _fn(CHAT, "assistantMessage")
    assert "options.announce ? memoryPhrase(saved" in body
    assert "options.memory || (event && (event.items || event.keys))" in body


def test_hoy_builds_one_set_of_controls_and_never_repeats_a_date():
    line = _fn(CHAT, "hoyLine")
    assert "if (!hoyTouch.matches) {" in line and "line.append(later, x);" in line and "line.append(more, acts);" in line
    assert "line.append(go, later, x, more, acts)" not in line
    assert "const named = due &&" in line and "if (due && !named)" in line
    assert "hoyTouch.addEventListener('change', () => hoyRender(hoyItems));" in CHAT


def test_reveal_ticket_needs_two_known_figures_and_says_not_yet_known():
    ticket = _fn(CHAT, "pictureTicket")
    assert "if (rows.filter(r => !r[4]).length < 2) return null;" in ticket
    assert "unknownRow(S.surplus)" in ticket and "known(picture.income) && picture.income > 0 && known(picture.spending)" in ticket
    assert "S.reserveMoney : S.liquid" in ticket and "picture.reserve_parts" in ticket
    for text in ("unknown: 'aún no sé'", "unknown: 'not yet known'", "reserveMoney: 'Dinero de reserva'"):
        assert text in CHAT


def test_links_survive_a_url_with_spaces_and_a_dropped_source_title_becomes_a_mark():
    assert "match[6].replace(/\\s+/g, '')" in CHAT
    cites = _fn(CHAT, "citations")
    assert "const between = source && detachedBefore(prev)" in cites and "} else if (between) {" in cites


def test_debt_items_render_yes_no_as_chips():
    assert "if (spec.type === 'chips') { const made = chipsField(spec); made.node.classList.add('wide'); return made; }" in CHAT
    assert ".item .row > .field.wide { flex: 1 1 100%; }" in CHAT


# ------------------------------------------------------------------ the You page


def test_you_page_puts_hero_hoy_and_three_tiles_above_the_fold():
    render = _fn(YOU, "render")
    assert "const tiles = [renderMonthTile(p), renderGoalsTile(p), renderReserveTile(p)].filter(Boolean);" in render
    assert render.index("renderHero(p)") < render.index("renderToday()") < render.index("class: 'span tiles'")
    for name in ("renderMonthDetail", "renderHave", "renderOwe", "renderReturns", "renderConnections"):
        assert "disc(" in _fn(YOU, name), name
    assert "details.disc > summary" in YOU and "grid-template-columns: repeat(2, minmax(0, 1fr))" in YOU


def test_you_page_collects_unknowns_in_one_list_and_draws_no_empty_sections():
    unknowns = _fn(YOU, "renderUnknowns")
    for key in ("missIncome", "debtsAsk", "noGoals", "reserveNoTarget", "returnsPrices", "returnsNo"):
        assert key in unknowns
    assert "return null;  // \"do you owe anything?\" is asked once" in _fn(YOU, "renderOwe")
    assert "unknownHead: 'Aún no sé…'" in YOU and "unknownHead: 'Not known yet'" in YOU


def test_you_page_charts_lost_their_chart_for_charts_sake():
    assert "function payoffChart(" not in YOU and "function payoffBar(" in YOU
    have = _fn(YOU, "renderHave")
    assert "rows.length > 1 ? stack(" in have  # a single 100% segment is a sentence, not a bar
    assert "isNum(r.weight) && rows.length > 1 ? h('div', { class: 'track' }" in _fn(YOU, "barList")
    spending = _fn(YOU, "renderSpending")
    assert ".slice(-12)" in spending and "class: 'h3-meta'" in spending and "t('avg', { x: m(sp.average) })" in spending
    assert "max-width: 320px" in YOU
    chart = _fn(YOU, "lineChart")
    assert "class: 'zero'" in chart and "const key = h('div', { class: 'key' }" not in chart
    assert "plot.style.marginRight = width; axis.style.marginRight = width;" in chart


def test_you_page_delights_ledger_dots_and_the_counting_delta():
    hero = _fn(YOU, "renderHero")
    assert "class: 'ledger'" in hero and "text: x.neg ? '−' : '+'" in hero and "text: '='" in hero
    spark = _fn(YOU, "sparkline")
    assert "countUp(delta, change" in spark and "lastDay === today ? ' live' : ''" in spark
    assert "matchMedia('(prefers-reduced-motion: reduce)')" in YOU
    dots = _fn(YOU, "dotsFor")
    assert "while (inst.length < 12 && last)" in dots and "next.next = true" in dots
    assert ".dots i.pending.next { background: var(--cobalt); box-shadow: none; }" in YOU


def test_you_page_goal_status_and_reads_the_address_language():
    tile = _fn(YOU, "renderGoalsTile")
    assert "const known = ['on_track', 'behind', 'funded'].includes(g.status);" in tile
    assert "isNum(g.funded) ? m(g.funded, c) : '—'" in tile and "t('fundedUnknown')" in tile
    assert "new URLSearchParams(location.search).get('lang')" in YOU


def test_memory_reads_one_sentence_per_group_that_opens():
    memory = _fn(YOU, "renderMemory")
    assert "h('details', { class: 'area'" in memory and "const lead = g.summary || g.facts[0];" in memory


# ------------------------------------------------------------------ the review letter


def test_review_hides_what_has_nothing_to_say():
    assert "hasDecisions(s.decisions) ? decisions(s.decisions) : null" in REVIEW
    assert "hasTaxes(s.taxes) ? taxes(s.taxes) : null" in REVIEW
    assert "const paid = n(s.paid?.v) === 0 && !s.complete ? '—' : V(s.paid);" in REVIEW
    alloc = _fn(REVIEW, "allocation")
    assert "if (!bands || !weighed) return note(s.note) || h('p', { class: 'quiet'" in alloc
    assert "class: 'part'" in _fn(REVIEW, "composition") and "@media (max-width: 480px) { .row .sub .part { white-space: normal; } }" in REVIEW


# ------------------------------------------------------------------ the payload behind the page


def test_goal_funding_is_unknown_without_an_account_tied_to_the_goal():
    goal = {"id": "casa", "currency": "MXN", "target_amount": 600000}
    assert profile._goal_funded(goal, {"cash": [], "currency": "MXN"}) is None
    assert profile._goal_funded(goal, {"cash": [{"purpose": "goal:casa", "currency": "MXN", "amount": 150000, "counted": True}],
                                        "currency": "MXN"}) == 150000


def test_returns_need_priced_instruments_not_a_balance_carried_forward(tmp_path):
    from wealth.service import WealthService
    from wealth.store import WealthStore

    db = tmp_path / "w.sqlite3"
    service = WealthService(db)
    service.create("p", "P")
    service.remember("p", [{"key": "client.profile", "value": {"name": "P", "residence": {"country": "MX"}, "reporting_currency": "MXN"},
                            "source": {"kind": "user", "ref": "chat", "observed_on": "2026-09-01"}}])
    with WealthStore(db) as store:
        store.post_ledger("p", {"batch_id": "afore", "source": {"kind": "document", "ref": "afore", "observed_on": "2026-09-01"},
                                "accounts": [{"id": "afore", "institution": "Afore", "type": "afore", "currency": "MXN",
                                              "owners": [{"person_id": "p", "share": 1}]}],
                                "instruments": [], "transactions": [{"account_id": "afore", "kind": "opening_balance", "date": "2025-12-31",
                                                                     "amount": "450000", "currency": "MXN"}]})
    pic = profile.profile_view(service, "p", today=date(2026, 9, 21), language="es")["picture"]
    # A bare balance has no price to measure: the return is unknown, never a flat 0%.
    assert pic["returns"]["status"] == "insufficient"
    assert pic["returns"].get("left_out", []) == []


def test_reference_is_named_once():
    assert "referencia ACWI" not in Path(profile_picture.__file__).read_text(encoding="utf-8").split("def _market_picture")[1]


def test_risk_summary_reads_as_their_own_reaction():
    from wealth.onboarding import _risk_summary
    assert _risk_summary({"drop_reaction": "sell"}, "es", {}) == "Si cae 20%: venderías"
    assert _risk_summary({"drop_reaction": "hold", "experience": "some"}, "en", {}) == "If it falls 20%: you would hold · experience: some"
