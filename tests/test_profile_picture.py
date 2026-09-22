"""The You page's picture: chart payloads (shapes, sums, honest unknowns) and the page's safety contract."""
from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.fixtures.ingest import statements as fx
from wealth import onboarding, profile
from wealth.prices import Fetched, PriceProvider
from wealth.service import WealthService, upload_dir
from wealth.store import WealthStore

TODAY = date(2026, 9, 21)
PAGE = Path(profile.__file__).with_name("profile.html")


def _weekdays(first: str, last: str, start: float, drift: float, wave: float = 0.0) -> dict[str, float]:
    day, end, out, i = date.fromisoformat(first), date.fromisoformat(last), {}, 0
    while day <= end:
        if day.weekday() < 5:
            out[day.isoformat()] = round(start * (1 + drift) ** i * (1 + wave * math.sin(i / 7)), 4)
            i += 1
        day += timedelta(days=1)
    return out


MARKET = {
    "VOO": ("USD", _weekdays("2025-08-01", "2026-09-18", 505.0, 0.0005, 0.02)),
    "ACWI": ("USD", _weekdays("2025-08-01", "2026-09-18", 118.0, 0.0004, 0.01)),
    "BNDW": ("USD", _weekdays("2025-08-01", "2026-09-18", 68.5, 0.0001)),
    "USDMXN=X": ("MXN", _weekdays("2025-08-01", "2026-09-18", 18.9, -0.0001)),
}


def _fetch(symbol, start, end, adjusted):
    if symbol not in MARKET:
        raise LookupError(symbol)
    currency, points = MARKET[symbol]
    return Fetched({d: v for d, v in points.items() if start.isoformat() <= d <= end.isoformat()}, currency, f"fake {symbol}")


def said(key, value, observed_on="2026-09-20"):
    return {"key": key, "value": value, "source": {"kind": "user", "ref": "chat", "observed_on": observed_on},
            "confidence": "confirmed"}


@pytest.fixture
def mx(tmp_path, monkeypatch):
    """A Mexican resident: GBM statement confirmed, a car loan, a card without terms, a goal, a DCA plan,
    three months of bank movements and a year of a US brokerage bought monthly."""
    monkeypatch.delenv("WEALTH_UPLOAD_DIR", raising=False)
    db = tmp_path / "w.sqlite3"
    prices = PriceProvider(db, fetcher=_fetch, offline=False, clock=lambda: datetime(2026, 9, 21, 23, tzinfo=timezone.utc))
    service = WealthService(db, prices=prices)
    service.create("ana", "Ana")
    service.remember("ana", [
        said("client.profile", {"name": "Ana", "language": "es", "residence": {"country": "MX"}, "tax_residence": ["MX"],
                                "reporting_currency": "MXN"}),
        said("income.salary", {"amount": 85000, "currency": "MXN", "frequency": "monthly", "net": True, "kind": "salary"}),
        said("cash.nu", {"amount": 96000, "currency": "MXN", "institution": "Nu", "purpose": "reserve"}),
        said("cash.casa", {"amount": 150000, "currency": "MXN", "institution": "Cetesdirecto", "purpose": "goal:casa"}),
        said("investment.afore", {"institution": "AFORE XXI Banorte", "kind": "afore", "currency": "MXN", "balance_unknown": True}),
        said("liability.auto", {"kind": "auto", "lender": "BBVA", "balance": 182000, "currency": "MXN", "annual_rate": 0.135,
                                "payment": 6400, "payment_frequency": "monthly"}),
        said("liability.card", {"kind": "card", "lender": "Banamex", "balance": 18500, "currency": "MXN"}),
        said("goals", [{"id": "casa", "name": "Enganche de casa", "target_amount": 600000, "currency": "MXN",
                        "target_date": "2029-06-30", "monthly_contribution": 8000}]),
        said("reserve", {"target_months": 6}),
        said("planning.dca", {"plans": [{"id": "voo", "name": "VOO cada mes", "currency": "USD", "cadence": "monthly",
                                         "start_date": "2026-01-05", "day_of_month": 5, "account_id": "ibkr",
                                         "legs": [{"instrument_id": "VOO", "amount": "500"}]}]}),
    ])
    upload_dir("ana", db).mkdir(parents=True, exist_ok=True)
    (upload_dir("ana", db) / "gbm.pdf").write_bytes(fx.gbm_multicurrency())
    shown = service.ingest("ana", "file", {"path": "gbm.pdf"})
    service.ingest("ana", "confirm", {"proposal_id": shown["result"]["proposal_id"],
                                      "acknowledge_discrepancies": shown["status"] == "needs_review"})
    owners = [{"person_id": "ana", "share": 1}]
    bank = [{"account_id": "bbva", "kind": "opening_balance", "date": "2026-06-01", "amount": "42000", "currency": "MXN"}]
    for month in ("2026-06", "2026-07", "2026-08"):
        bank.append({"account_id": "bbva", "kind": "income", "date": f"{month}-01", "amount": "85000", "currency": "MXN",
                     "description": "PAGO DE NOMINA", "subtype": "salary"})
        for name, amount, day in (("RENTA DEPTO", 16000, 2), ("WALMART", 4200, 10), ("NETFLIX", 299, 12), ("LIVERPOOL", 3100, 25)):
            bank.append({"account_id": "bbva", "kind": "expense", "date": f"{month}-{day:02d}", "amount": f"-{amount}",
                         "currency": "MXN", "description": name})
    ib = [{"account_id": "ibkr", "kind": "opening_balance", "date": "2025-09-30", "instrument_id": "VOO", "quantity": "40",
           "cost_basis": "19000", "acquired_on": "2024-06-03", "currency": "USD"},
          {"account_id": "ibkr", "kind": "opening_balance", "date": "2025-09-30", "amount": "600", "currency": "USD"}]
    voo = MARKET["VOO"][1]
    for month in ("2026-01", "2026-02", "2026-03", "2026-05", "2026-06", "2026-07", "2026-08"):  # April skipped
        day = date.fromisoformat(f"{month}-05")
        while day.isoformat() not in voo:
            day += timedelta(days=1)
        qty = round(500 / voo[day.isoformat()], 4)
        ib += [{"account_id": "ibkr", "kind": "deposit", "date": day.isoformat(), "amount": "500", "currency": "USD"},
               {"account_id": "ibkr", "kind": "buy", "date": day.isoformat(), "instrument_id": "VOO", "quantity": str(qty),
                "price": str(voo[day.isoformat()]), "amount": f"-{round(qty * voo[day.isoformat()], 2)}", "currency": "USD"}]
    source = {"kind": "document", "ref": "test", "observed_on": "2026-09-20"}
    with WealthStore(db) as store:
        store.post_ledger("ana", {"batch_id": "bank", "source": source, "instruments": [], "transactions": bank,
                                  "accounts": [{"id": "bbva", "institution": "BBVA", "type": "checking", "currency": "MXN", "owners": owners}]})
        store.post_ledger("ana", {"batch_id": "ib", "source": source, "transactions": ib,
                                  "accounts": [{"id": "ibkr", "institution": "Interactive Brokers", "type": "brokerage",
                                                "currency": "USD", "owners": owners}],
                                  "instruments": [{"id": "VOO", "symbol": "VOO", "currency": "USD", "asset_class": "equity",
                                                   "venue": "us", "issuer_domicile": "US", "underlying_symbol": "VOO"}]})
    return service


@pytest.fixture
def thin(tmp_path):
    service = WealthService(tmp_path / "thin.sqlite3")
    service.create("leo", "Leo")
    service.remember("leo", [said("client.profile", {"name": "Leo", "language": "es", "residence": {"country": "MX"}}),
                             said("income.salary", {"amount": 32000, "currency": "MXN", "frequency": "monthly", "kind": "salary"}),
                             said("cash.banorte", {"amount": 12000, "currency": "MXN", "institution": "Banorte"})])
    return service


def test_the_picture_adds_up(mx):
    sit = mx.situation("ana", today=TODAY)
    pic = profile.profile_view(mx, "ana", today=TODAY, language="es")["picture"]
    nw = sit["net_worth"]

    # Net worth: the AFORE has no balance, so the total is unknown and the known part is named as such.
    worth = pic["worth"]
    assert worth["total"] is None and worth["known_total"] == pytest.approx(nw["known_total"])
    assert worth["without"] == ["AFORE XXI Banorte"]
    assert worth["liquid"] + worth["illiquid"] - worth["debts"] == pytest.approx(worth["known_total"], abs=0.01)

    # The month: the segments add up to the income, on one baseline.
    flow = pic["flow"]
    assert flow["status"] == "ready" and flow["income"] == 85000
    assert sum(s["value"] for s in flow["segments"]) == pytest.approx(85000, abs=0.05)
    assert [s["id"] for s in flow["segments"]][:2] == ["essentials", "other"] and flow["segments"][-1]["id"] == "unallocated"
    assert flow["debt_unknown"] == ["Banamex"]  # the card's payment is unknown, and said so
    assert 0 < flow["savings_rate"] < 1
    months = pic["spending"]["months"]
    assert len(months) >= 2 and months[0]["month"] == "2026-06" and len(pic["spending"]["top"]) <= 3
    assert pic["spending"]["top"][0]["id"] == "housing"

    # What you own: weights of each breakdown are fractions of the same assets.
    alloc = pic["allocation"]
    assert alloc["total"] == pytest.approx(nw["assets"], abs=0.05)
    for split in ("asset_class", "currency"):
        assert sum(r["weight"] for r in alloc[split]) == pytest.approx(1, abs=0.002)
        assert sum(r["value"] for r in alloc[split]) == pytest.approx(nw["assets"], abs=0.05)
    assert {r["id"] for r in alloc["currency"]} == {"MXN", "USD"}
    usd = next(r for r in alloc["currency"] if r["id"] == "USD")
    assert usd["native"] > 0
    top = alloc["top"][0]
    assert top["name"] == "S&P 500" and set(top["symbols"]) == {"CSPX", "IVV", "VOO"}
    assert alloc["overlaps"][0]["name"] == "S&P 500"
    assert sum(r["weight"] for r in alloc["venue"]) == pytest.approx(1, abs=0.002)  # a Mexican resident sees where it trades
    assert {r["id"] for r in alloc["domicile"]} >= {"us", "non_us"}
    afore = next(a for a in pic["accounts"] if a["name"] == "AFORE XXI Banorte")
    assert afore["value"] is None and afore["unknown"] == "balance"
    assert {a["name"] for a in pic["accounts"]} >= {"GBM (MXN)", "GBM (USD)", "BBVA", "Interactive Brokers"}

    # Debts: a line from today's balance to zero at the payoff month; the card says what is missing.
    auto, card = pic["debts"]
    assert auto["points"][0] == [0, 182000.0] and auto["points"][-1] == [auto["months"], 0.0]
    assert all(a[1] >= b[1] for a, b in zip(auto["points"], auto["points"][1:]))
    assert auto["payoff"] and auto["interest"] > 0 and len(auto["points"]) <= profile.PICTURE_POINTS + 1
    assert card["points"] is None and card["payoff"] is None and set(card["missing"]) == {"rate", "payment"}

    # Goals and the plan behind the monthly contribution.
    casa = next(g for g in pic["goals"] if g["id"] == "casa")
    assert casa["funded"] == 150000 and casa["ratio"] == pytest.approx(0.25) and casa["status"] == "behind"
    assert casa["needed"] == pytest.approx((600000 - 150000) / casa["months"], abs=0.01)
    plan = next(g for g in pic["goals"] if g["plan"])
    states = [i["state"] for i in plan["plan"]["adherence"]["installments"]]
    assert "skipped" in states and states.count("on_time") >= 6

    reserve = pic["reserve"]
    assert reserve["target_months"] == 6 and reserve["months"] == pytest.approx(96000 / reserve["essential"], abs=0.05)

    # History and returns come from the ledger at provider prices; what cannot be valued is named, never guessed.
    history = pic["history"]
    assert history and history["coverage"] >= 0.5 and len(history["points"]) >= 2
    returns = pic["returns"]
    assert returns["status"] == "ready" and "Interactive Brokers" in returns["accounts"]
    assert "GBM (MXN)" in returns["left_out"]
    assert returns["series"][0]["p"] == 100.0 and returns["benchmark"]
    assert set(returns["periods"]) >= {"3m", "all"}
    for period in returns["periods"].values():
        assert period["twr"] is not None and period["mwr"] is not None and period["bench"] is not None
        start = next(p for p in returns["series"] if p["d"] == period["start"])
        assert period["twr"] == pytest.approx(returns["series"][-1]["p"] / start["p"] - 1, abs=1e-4)


def test_thin_data_stays_unknown_not_zero(thin):
    pic = profile.profile_view(thin, "leo", today=TODAY, language="es")["picture"]
    assert pic["flow"]["status"] == "unknown" and pic["flow"]["missing"] == ["spending"]
    assert pic["flow"]["segments"] == [] and pic["flow"]["savings_rate"] is None
    assert pic["spending"] is None and pic["history"] is None
    assert pic["returns"]["status"] == "insufficient"
    assert pic["reserve"]["months"] is None and pic["reserve"]["target_months"] is None
    assert pic["debts"] == [] and pic["goals"] == []
    assert pic["worth"]["total"] == 12000 and pic["allocation"]["asset_class"] == [{"id": "cash", "value": 12000.0, "weight": 1.0}]
    assert pic["allocation"]["venue"] is None  # no securities: no venue split


def test_percent_words_keep_whole_numbers():
    assert profile._pct_words("0.9", 0) == "90%"
    assert profile._pct_words("0.1", 0) == "10%"
    assert profile._pct_words("0.125") == "12.5%"
    assert profile._pct_words("-0.05", 1) == "-5%"


def test_review_summary_never_turns_unknown_into_zero():
    text = profile._review_summary({"net_worth": {"contributions": "0"}}, "2026-Q2")
    assert "$0" not in text["es"][0] and "Aún no se sabe" in text["es"][0]
    assert "precios" not in " ".join(text["es"])


def test_account_types_are_words_in_both_languages():
    row = {"label": "Charles Schwab · roth_ira", "type": "roth_ira", "institution": "Charles Schwab"}
    assert profile.account_label(row, "en") == "Charles Schwab · Roth IRA"
    assert profile.account_label({"label": "BBVA México · checking", "type": "checking"}, "es") == "BBVA México · cuenta de cheques"
    assert profile.account_label({"label": "GBM (USD)", "type": "brokerage"}, "es") == "GBM (USD)"


def test_names_and_origins_are_the_persons(mx):
    view = profile.profile_view(mx, "ana", today=TODAY, language="es")
    about = next(g for g in view["groups"] if g["id"] == "about")
    assert "Birth year" not in {e["label"] for e in about["entries"]} and "Nombre" in {e["label"] for e in about["entries"]}
    assert not any(i["key"].endswith(".activity") for i in view["upcoming"] if i.get("key"))
    invest = next(g for g in view["memory"]["es"]["groups"] if g["id"] == "invest")
    for fact in invest["facts"]:  # the S&P 500 sits at GBM and IBKR: credited to the calculation, not to one statement
        assert fact["origin"] in ({"kind": "calculated"}, {"kind": "statement", "institution": "GBM", "as_of": "2026-08-31"})
    assert any("tres veces" in f["text"] for f in invest["facts"])
    own = " ".join(f["text"] for g in view["memory"]["es"]["groups"] for f in g["facts"])
    assert "Tienes una cuenta en AFORE XXI Banorte; aún no sé el saldo." in own


def test_onboarding_keeps_the_city():
    (_, value), = onboarding._identity_writer({"name": "Mariana", "country": "MX", "city": "Ciudad de México"}, {"language": "es"})
    assert value["residence"] == {"country": "MX", "city": "Ciudad de México"}


# ------------------------------------------------------------------ the page


def _script() -> str:
    page = PAGE.read_text()
    script = "\n".join(re.findall(r"<script>(.*?)</script>", page, re.S))
    return re.sub(r"(?m)(^|\s)//.*$", r"\1", script)


def test_page_writes_no_markup_and_sends_the_token_on_reads():
    script = _script()
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert sink not in script
    assert re.search(r"fetch\('/api/profile\?lang=' \+ lang, \{[^}]*'X-Wealth-Token': token", script)
    assert "read(factPath(item.key))" in script and "read('/api/profile/fact/'" in script
    assert "fetch(factPath" not in script.replace("fetch(factPath(pending", "")  # every other fact read goes through read()


def test_every_chart_has_a_text_alternative():
    script = _script()
    # Charts are drawn by chart() (visual aria-hidden plus a table) or by lineChart, which appends its own table.
    assert "function chart(caption, visual, head, rows, extra)" in script
    assert "h('div', { 'aria-hidden': 'true' }, visual), srTable(caption, head, rows)" in script
    drawn = re.findall(r"\b(stack|sparkline|lineChart|payoffBar)\(", script)
    assert drawn and "srTable(t('returnsTable')" in script
    for fn in ("renderMonthTile", "renderMonthDetail", "renderSpending", "renderHave", "payoffBar", "renderGoalsTile", "renderReserveTile", "sparkline"):
        body = script[script.index(f"function {fn}("):]
        body = body[:body.index("\n    }\n")]
        assert "chart(" in body, fn
    assert "font-variant-numeric: tabular-nums" in PAGE.read_text()


def test_memory_reads_as_sentences_with_one_focused_pass():
    script = _script()
    # No per-row confirm, reconfirm or contradiction buttons: one line opens one pass.
    for gone in ("yesRight", "keepMine", "useStatement", "itChanged", "renderCheck", "renderConflict", "cue("):
        assert gone not in script
    assert "function openPass()" in script and "passQueue(mem)" in script
    assert "e.key === 'Escape'" in script and "historySource" in script


def test_page_boots_in_the_saved_language():
    script = _script()
    boot = script[script.index("(async () => {"):]
    assert boot.index("fetch('/api/state'") < boot.index("setLang(") < boot.index("await load()")
    assert "state.language" in boot
