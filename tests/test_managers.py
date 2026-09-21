"""Follow a manager (wealth/managers.py): 13F parsing, amendments, mapping, profile, mirror, monitor.

No network: every EDGAR and OpenFIGI answer comes from tests/fixtures/sec through ``managers.Snapshot``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from wealth import managers, views
from wealth.monitor import evaluate
from wealth.service import WealthService
from wealth.situation.schema import SchemaError, validate

FIX = Path(__file__).parent / "fixtures" / "sec"
CIK = "0009990001"
PARENT = "0009990009"
PATIENT, FASTLANE = "0009990002", "0009990003"


def read(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


FIGI = {k: v for k, v in json.loads(read("openfigi.json")).items() if not k.startswith("_")}
TICKERS = list(json.loads(read("company_tickers.json")).values())
QUARTERS = json.loads(read("manager_quarters.json"))


def fixture_pages(amendments: tuple[str, ...] = (), notice: bool = False) -> dict:
    filings = [
        {"accession": "0009990001-23-000001", "form": "13F-HR", "filing_date": "2023-01-02", "period": "2022-12-31",
         "infotable_xml": read("infotable_2022q4_thousands.xml")},
        {"accession": "0009990001-23-000002", "form": "13F-HR", "filing_date": "2023-05-15", "period": "2023-03-31",
         "infotable_xml": read("infotable_2023q1_dollars.xml"), "primary_xml": read("primary_doc_2023q1.xml")},
    ]
    if "restatement" in amendments:
        filings.append({"accession": "0009990001-23-000003", "form": "13F-HR/A", "filing_date": "2023-06-01",
                        "period": "2023-03-31", "infotable_xml": read("infotable_2023q1_restated.xml"),
                        "primary_xml": read("primary_doc_restatement.xml")})
    if "new_holdings" in amendments:
        filings.append({"accession": "0009990001-23-000004", "form": "13F-HR/A", "filing_date": "2023-08-14",
                        "period": "2023-03-31", "infotable_xml": read("infotable_2023q1_new_holdings.xml"),
                        "primary_xml": read("primary_doc_new_holdings.xml")})
    if notice:
        filings.append({"accession": "0009990001-23-000005", "form": "13F-NT", "filing_date": "2023-08-14",
                        "period": "2023-06-30", "primary_xml": read("primary_doc_notice.xml")})
    people = [
        {"cik": CIK, "name": "Fixture Capital LP", "filings": filings},
        {"cik": "0009990007", "name": "Fixture Capital Fund I LP", "filings": [],
         "other_forms": [{"accession": "0009990007-23-000001", "form": "D", "filing_date": "2023-02-01"}]},
        {"cik": PARENT, "name": "Fixture Parent Inc.", "filings": [
            {"accession": "0009990009-23-000001", "form": "13F-HR", "filing_date": "2023-08-14", "period": "2023-06-30",
             "rows": [{"issuer": "APPLE INC", "class": "COM", "cusip": "037833100", "value": 900000, "shares": 5000}]}]},
    ]
    pages = managers.build_snapshot(people, tickers=TICKERS,
                                    searches={"fixture capital": [CIK, "0009990007"], "fixture fund": ["0009990007"]})
    pages["openfigi"] = FIGI
    return pages


def quarter_pages() -> dict:
    sec = QUARTERS["securities"]
    people = []
    for cik, spec in QUARTERS["managers"].items():
        filings = []
        for n, q in enumerate(spec["quarters"]):
            rows = [{"issuer": sec[t][0], "class": sec[t][1], "cusip": sec[t][2], "value": s * p, "shares": s}
                    for t, (s, p) in q["holdings"].items()]
            rows += [{"issuer": sec[t][0], "class": sec[t][1], "cusip": sec[t][2], "value": s * p, "shares": s,
                      "put_call": kind} for t, (kind, s, p) in (q.get("options") or {}).items()]
            filings.append({"accession": f"{cik}-{q['filing_date'][2:4]}-{n + 1:06d}", "form": "13F-HR",
                            "filing_date": q["filing_date"], "period": q["period"], "rows": rows})
        people.append({"cik": cik, "name": spec["name"], "filings": filings})
    issuers = {str(v[3]): {"name": v[0], "sic": v[4], "stateOfIncorporation": "DE"} for v in sec.values()}
    pages = managers.build_snapshot(people, tickers=TICKERS, issuers=issuers)
    pages["openfigi"] = FIGI
    return pages


def edgar(pages: dict) -> managers.Edgar:
    return managers.Edgar(transport=managers.Snapshot(pages))


# ------------------------------------------------------------------ parsing


def test_value_units_follow_the_filing_date():
    old = managers.parse_infotable(read("infotable_2022q4_thousands.xml"), filing_date="2023-01-02")
    assert old["value_unit"] == "thousands"
    assert old["rows"][0]["value"] == 10_394_000 and old["rows"][0]["shares"] == 80000
    assert old["warnings"] == []

    # The same thousands table filed after 2023-01-03 is read as dollars, and the implied prices are flagged.
    misread = managers.parse_infotable(read("infotable_2022q4_thousands.xml"), filing_date="2023-01-03")
    assert misread["value_unit"] == "dollars" and misread["rows"][0]["value"] == 10394
    assert any("look like thousands" in w for w in misread["warnings"])

    new = managers.parse_infotable(read("infotable_2023q1_dollars.xml"), filing_date="2023-05-15")
    assert new["value_unit"] == "dollars" and new["warnings"] == []
    apple = new["rows"][0]
    assert (apple["issuer"], apple["class"], apple["cusip"], apple["value"], apple["shares"]) == \
        ("APPLE INC", "COM", "037833100", 16_000_000, 100000)
    assert apple["voting"] == {"sole": 100000, "shared": 0, "none": 0} and apple["discretion"] == "SOLE"
    calls = [r for r in new["rows"] if r["put_call"]]
    assert {(r["cusip"], r["put_call"]) for r in calls} == {("67066G104", "call"), ("78462F103", "put")}
    assert next(r for r in new["rows"] if r["cusip"] == "093712AH0")["amount_type"] == "PRN"


def test_primary_doc_reports_amendment_type_and_notice():
    assert managers.parse_primary_doc(read("primary_doc_restatement.xml"))["amendment_type"] == "RESTATEMENT"
    added = managers.parse_primary_doc(read("primary_doc_new_holdings.xml"))
    assert added["is_amendment"] and added["amendment_type"] == "NEW HOLDINGS" and added["period"] == "2023-03-31"
    notice = managers.parse_primary_doc(read("primary_doc_notice.xml"))
    assert notice["report_type"] == "13F NOTICE"
    assert notice["other_managers"] == [{"cik": PARENT, "name": "Fixture Parent Inc.", "file_number": "028-99990"}]
    assert managers.parse_primary_doc(read("primary_doc_2023q1.xml"))["table_value_total"] == 40_290_000


def test_documents_with_a_dtd_are_refused():
    with pytest.raises(managers.ManagerDataError, match="DTD"):
        managers.parse_infotable('<!DOCTYPE x [<!ENTITY a "b">]><informationTable>&a;</informationTable>')


# ------------------------------------------------------------------ holdings


def test_holdings_separate_options_and_debt_from_long_equity_weights():
    report = managers.holdings(CIK, client=edgar(fixture_pages()), as_of="2023-09-01")
    result = report["result"]
    assert result["period"] == "2023-03-31" and result["filing"]["accession"] == "0009990001-23-000002"
    assert result["total_reported_value"] == 40_290_000
    assert result["long_equity_value"] == 29_440_000
    assert result["options_value"] == {"call": 5_560_000, "put": 4_090_000} and result["other_value"] == 1_200_000
    assert result["summary_total_check"]["matches"] is True
    tickers = {p["ticker"] for p in result["positions"]}
    assert "NVDA" not in tickers and "SPY" not in tickers  # options are not long equity
    assert all(p["put_call"] is None for p in result["positions"])
    assert sum(p["weight"] for p in result["positions"]) == pytest.approx(1, abs=1e-5)
    msft = next(p for p in result["positions"] if p["ticker"] == "MSFT")
    assert (msft["shares"], msft["value"]) == (30000, 8_640_000)  # two rows, one position
    assert next(p for p in result["positions"] if p["ticker"] == "AAPL")["weight"] == pytest.approx(16 / 29.44, abs=1e-6)
    options = {(o["ticker"], o["put_call"]): o for o in result["options"]}
    assert options[("NVDA", "call")]["value"] == 5_560_000 and ("SPY", "put") in options
    assert "underlying" in result["options_note"] and "premium" in result["options_note"]
    assert result["lag"] == {"period_end": "2023-03-31", "filed": "2023-05-15", "days_since_period_end": 154,
                             "days_since_filing": 109, "filing_delay_days": 45,
                             "deadline": "45 days after the quarter ends"}
    assert report["status"] == "partial"  # ZZQX has no ticker
    assert result["unmapped"] == [{"cusip": "99999Z109", "issuer": "ZZQX PRIVATE HOLDCO",
                                   "weight": pytest.approx(0.8 / 29.44, abs=1e-6)}]
    urls = [s["url"] for s in report["sources"]]
    assert "https://data.sec.gov/submissions/CIK0009990001.json" in urls
    assert managers.DOCS["form_13f_faq"] in urls

    with_options = managers.holdings(CIK, client=edgar(fixture_pages()), include_options=True)["result"]
    nvda = next(o for o in with_options["options"] if o["ticker"] == "NVDA")
    assert nvda["weight_with_options"] == pytest.approx(5.56 / (29.44 + 5.56 + 4.09), abs=1e-6)


def test_amendments_restate_or_add_in_filing_order():
    restated = managers.holdings(CIK, client=edgar(fixture_pages(("restatement",))))["result"]
    assert [(p["ticker"], p["shares"]) for p in restated["positions"]] == [("AAPL", 120000), ("MSFT", 30000)]
    assert restated["options"] == [] and restated["amendments"][0]["amendment_type"] == "RESTATEMENT"

    added = managers.holdings(CIK, client=edgar(fixture_pages(("new_holdings",))))["result"]
    assert len(added["positions"]) == 6 and "KO" in {p["ticker"] for p in added["positions"]}
    assert added["amendments"] == [{"accession": "0009990001-23-000004", "form": "13F-HR/A",
                                    "filing_date": "2023-08-14", "amendment_type": "NEW HOLDINGS",
                                    "effect": "added 1 holdings"}]
    assert added["long_equity_value"] == 29_440_000 + 6_200_000

    both = managers.holdings(CIK, client=edgar(fixture_pages(("restatement", "new_holdings"))))["result"]
    assert [p["ticker"] for p in both["positions"]] == ["AAPL", "MSFT", "KO"]
    assert [a["amendment_type"] for a in both["amendments"]] == ["RESTATEMENT", "NEW HOLDINGS"]
    assert "RESTATEMENT replaces" in both["amendment_policy"]


def test_quarter_over_quarter_diff():
    changes = managers.holdings(CIK, client=edgar(fixture_pages()))["result"]["changes"]
    assert changes["counts"] == {"new": 3, "exited": 1, "increased": 1, "decreased": 1, "unchanged": 0}
    aapl = changes["increased"][0]
    assert (aapl["ticker"], aapl["previous_shares"], aapl["shares"], aapl["share_change"], aapl["share_change_pct"]) == \
        ("AAPL", 80000, 100000, 20000, 0.25)
    previous = 10_394_000 / (10_394_000 + 9_593_000 + 1_322_000)
    assert aapl["weight_change"] == pytest.approx(16 / 29.44 - previous, abs=1e-5)
    assert changes["decreased"][0]["ticker"] == "MSFT" and changes["decreased"][0]["share_change"] == -10000
    assert changes["exited"][0]["issuer"] == "INTEL CORP" and changes["exited"][0]["weight"] == 0
    assert {r["ticker"] for r in changes["new"]} == {"BTDR", "NWND", None}


def test_a_13f_notice_points_to_the_manager_that_reports_the_holdings():
    pages = fixture_pages(notice=True)
    report = managers.holdings(CIK, client=edgar(pages))
    assert report["status"] == "needs_input"
    assert PARENT in report["missing"][0] and "13F-NT" in report["warnings"][0]
    assert report["result"]["latest_holdings_period"] == "2023-03-31"
    assert report["result"]["what_is_13f"]["es"].startswith("Qué es un 13F")
    earlier = managers.holdings(CIK, "2023Q1", client=edgar(pages))
    assert earlier["result"]["period"] == "2023-03-31" and earlier["status"] in {"ready", "partial"}


# ------------------------------------------------------------------ find


def test_find_returns_ciks_latest_filings_and_says_who_does_not_file():
    report = managers.find("Fixture Capital", client=edgar(fixture_pages(notice=True)))
    assert report["status"] == "ready"
    by_cik = {c["cik"]: c for c in report["result"]["candidates"]}
    lead = by_cik[CIK]
    assert lead["files_13f"] and lead["latest_13f"]["form"] == "13F-NT"
    assert lead["latest_holdings_report"] == {"form": "13F-HR", "filing_date": "2023-05-15",
                                              "period": "2023-03-31", "accession": "0009990001-23-000002"}
    assert lead["notice"]["reported_by"][0]["cik"] == PARENT
    assert by_cik[PARENT]["files_13f"] and "notice" in by_cik[PARENT]["found_via"]  # followed from the notice
    assert by_cik["0009990007"]["files_13f"] is False
    assert any("Fixture Parent Inc." in w for w in report["warnings"])

    none = managers.find("Fixture Fund", client=edgar(fixture_pages()))
    assert none["status"] == "partial" and "files Form 13F" in none["warnings"][-1]


def test_find_knows_people_by_their_firm(monkeypatch):
    monkeypatch.setitem(managers.ALIASES, "jane fixture", "Fixture Capital")
    report = managers.find("Jane Fixture", client=edgar(fixture_pages()))
    assert report["result"]["search_terms"] == ["Jane Fixture", "Fixture Capital"]
    assert report["result"]["candidates"][0]["cik"] == CIK


# ------------------------------------------------------------------ CUSIP -> ticker


def test_cusip_mapping_prefers_openfigi_then_names_with_confidence():
    result = managers.holdings(CIK, client=edgar(fixture_pages()))["result"]
    got = {p["cusip"]: (p["ticker"], p["ticker_confidence"], p["mapping_method"]) for p in result["positions"]}
    assert got["037833100"] == ("AAPL", 0.99, "openfigi")
    assert got["G11448100"] == ("BTDR", 0.9, "name_match")      # OpenFIGI has no match; SEC name does
    assert got["66600A101"] == ("NWND", 0.9, "name_match")
    assert got["99999Z109"] == (None, 0.0, "unmapped")

    supplied = managers.holdings(CIK, client=edgar(fixture_pages()), tickers={"99999z109": "zzqx"})
    assert next(p for p in supplied["result"]["positions"] if p["cusip"] == "99999Z109")["ticker_confidence"] == 1.0
    assert supplied["status"] == "ready"

    client = edgar(fixture_pages())
    fuzzy = managers.map_cusips(client, [{"cusip": "000000001", "issuer": "NORTHWND TRADERS", "class": "COM"},
                                         {"cusip": "000000002", "issuer": "BERKSHIRE HATHAWAY INC DEL", "class": "CL B NEW"},
                                         {"cusip": "000000003", "issuer": "BERKSHIRE HATHAWAY INC DEL", "class": "CL A"},
                                         {"cusip": "093712AH0", "issuer": "BLOOM ENERGY CORP", "class": "NOTE"}])
    assert fuzzy["000000001"]["ticker"] == "NWND" and 0.6 <= fuzzy["000000001"]["confidence"] < 0.9
    assert fuzzy["000000002"]["ticker"] == "BRK-B" and fuzzy["000000003"]["ticker"] == "BRK-A"
    assert fuzzy["093712AH0"]["confidence"] == 0.7  # a bond line, not a US equity listing


def test_openfigi_outage_falls_back_to_name_matching():
    pages = fixture_pages()
    snapshot = managers.Snapshot(pages)

    def transport(method, url, headers, body=None):
        if url == managers.OPENFIGI_URL:
            raise managers.TransportError(429, url)
        return snapshot(method, url, headers, body)

    report = managers.holdings(CIK, client=managers.Edgar("Test test@example.com", transport=transport))
    apple = next(p for p in report["result"]["positions"] if p["cusip"] == "037833100")
    assert (apple["ticker"], apple["mapping_method"], apple["ticker_confidence"]) == ("AAPL", "name_match", 0.9)
    assert any("OpenFIGI was not available" in w for w in report["warnings"])


# ------------------------------------------------------------------ user agent, cache


def test_missing_user_agent_is_refused_before_any_request(monkeypatch, tmp_path):
    monkeypatch.delenv(managers.SEC_UA_ENV, raising=False)
    monkeypatch.setenv(managers.CACHE_ENV, str(tmp_path))
    calls = []

    def transport(*args):
        calls.append(args)
        raise AssertionError("EDGAR must not be called without a User-Agent")

    for client in (managers.Edgar(transport=transport), managers.Edgar("wealth-bot", transport=transport)):
        report = managers.holdings(CIK, client=client)
        assert report["status"] == "needs_input" and report["missing"] == [managers.SEC_UA_ENV]
        assert "contact e-mail" in report["warnings"][0] and "fair-access" in report["warnings"][0]
        assert managers.find("anything", client=client)["missing"] == [managers.SEC_UA_ENV]
    assert calls == []
    service = WealthService(tmp_path / "db.sqlite3").run("manager_holdings", inputs={"cik": CIK})
    assert service["status"] == "needs_input" and service["missing"] == [managers.SEC_UA_ENV]


def test_disk_cache_serves_repeat_reads_and_stale_copies_on_outage(tmp_path, monkeypatch):
    snapshot = managers.Snapshot(fixture_pages())
    calls = []

    def counting(method, url, headers, body=None):
        calls.append(url)
        assert "@" in headers["User-Agent"]
        return snapshot(method, url, headers, body)

    ua = "Test Fixture test@example.com"
    first = managers.holdings(CIK, client=managers.Edgar(ua, transport=counting, cache_dir=tmp_path))
    assert calls
    calls.clear()
    second = managers.holdings(CIK, client=managers.Edgar(ua, transport=counting, cache_dir=tmp_path))
    assert calls == [] and second["result"]["positions"] == first["result"]["positions"]

    def down(*_):
        raise managers.TransportError(None, "offline")

    monkeypatch.setitem(managers.TTL, "submissions", -1)
    stale = managers.holdings(CIK, client=managers.Edgar(ua, transport=down, cache_dir=tmp_path))
    assert stale["result"]["period"] == "2023-03-31"
    assert any("cached copy" in w for w in stale["warnings"])


# ------------------------------------------------------------------ profile and compare


def _book(period, filing, holdings, options=()):
    rows = [{"issuer": t, "class": "COM", "cusip": t.ljust(9, "0"), "value": s * p, "shares": s}
            for t, (s, p) in holdings.items()]
    rows += [{"issuer": t, "class": "COM", "cusip": t.ljust(9, "0"), "value": s * p, "shares": s, "put_call": k}
             for t, k, s, p in options]
    book = managers.positions_from_rows(rows)
    for p in book["equity"] + book["options"]:
        p["ticker"], p["ticker_confidence"] = p["issuer"], 0.99
    return {"period": period, "filing_date": filing, "book": book}


def test_turnover_holding_period_and_concentration_are_computed_from_share_changes():
    quarters = [_book("2025-12-31", "2026-02-13", {"AAA": (100, 10), "BBB": (100, 10)}),
                _book("2026-03-31", "2026-05-14", {"AAA": (150, 10), "BBB": (50, 10)}),
                _book("2026-06-30", "2026-08-13", {"AAA": (150, 12), "BBB": (50, 12), "CCC": (20, 10)},
                      options=[("AAA", "call", 100, 12)])]
    profile = managers.profile_from_history(quarters)
    first, second = profile["turnover"]["per_quarter"]
    assert (first["buys_estimate"], first["sells_estimate"], first["turnover"]) == (500, 500, 0.25)
    assert second["turnover"] == 0 and second["new_positions"] == 1 and second["new_position_weights"] == [0.0769]
    assert profile["turnover"]["annualised"] == pytest.approx(0.5)
    assert profile["turnover"]["estimate"] is True and "invisible" in profile["turnover"]["caveat"]
    assert profile["holding_period"]["implied_years"] == 2.0
    assert profile["holding_period"]["median_quarters"] == 3.0
    latest = profile["concentration"]["latest"]
    assert latest["positions"] == 3 and latest["top5"] == 1.0
    assert latest["hhi"] == pytest.approx((1800 / 2600) ** 2 + (600 / 2600) ** 2 + (200 / 2600) ** 2, abs=1e-4)
    assert profile["options"]["by_quarter"][-1]["calls_share"] == pytest.approx(1200 / 3800, abs=1e-4)
    assert profile["conviction"]["added_to_existing"] == 1 and profile["conviction"]["new_positions"] == 1


def test_profiles_read_character_from_multi_quarter_filings():
    client = edgar(quarter_pages())
    patient = managers.profile(PATIENT, 8, client=client)
    result = patient["result"]
    assert patient["status"] == "ready" and result["quarters"] == 5
    assert result["turnover"]["annualised"] < 0.05
    assert result["turnover"]["splits_ignored"] == [{"period": "2026-03-31", "cusip": "22160K105",
                                                    "issuer": "COSTCO WHSL CORP NEW", "ratio": 4.0}]
    assert result["holding_period"]["median_quarters"] == 5.0
    assert result["concentration"]["latest"]["positions"] == 10
    assert result["character"]["en"].startswith(
        "A long-term, concentrated investor: under 5% turnover a year, 10 holdings, top 10 about 100%.")
    assert result["character"]["es"].startswith("Un inversionista de largo plazo y concentrado")
    weights = result["sectors"]["by_quarter"][-1]["weights"]
    assert set(weights) >= {"Technology", "Financials", "Consumer staples", "Health care"}
    assert result["sectors"]["drift_first_to_last"] is not None and result["sectors"]["coverage"] == 1.0
    assert "What a 13F is" in result["what_is_13f"]["en"] and "45 días" in result["what_is_13f"]["es"]

    fast = managers.profile(FASTLANE, 8, client=client)["result"]
    assert fast["turnover"]["annualised"] > 1.2
    assert fast["character"]["en"].startswith("A fast-trading, concentrated investor")
    assert fast["options"]["average_calls_share"] > 0 and fast["options"]["by_quarter"][-1]["puts_share"] > 0
    assert fast["conviction"]["new_positions"] == 7  # AMD, MSTR, AAPL; then NVDA, COIN, TSLA, AMZN

    compared = managers.compare([PATIENT, FASTLANE], client=client)
    rows = compared["result"]["managers"]
    assert [r["name"] for r in rows] == ["Patient Compounders LLC", "Fastlane Momentum LP"]
    assert rows[0]["annual_turnover"] < rows[1]["annual_turnover"]
    assert rows[1]["options_share"] > 0 and rows[0]["options_share"] == 0


def test_service_tasks_draw_validated_views(tmp_path):
    service = WealthService(tmp_path / "db.sqlite3")
    pages = quarter_pages()
    holding = service.run("manager_holdings", inputs={"cik": PATIENT, "snapshot": pages})
    assert [v["kind"] for v in holding["views"]] == ["allocation", "comparison"]
    specs = views.views_for("manager_holdings", holding)
    assert specs[0]["data"]["rows"][0]["share"] == holding["result"]["positions"][0]["weight"]
    profile = service.run("manager_profile", inputs={"cik": FASTLANE, "snapshot": pages})
    assert [v["kind"] for v in profile["views"]] == ["ticket", "comparison"]
    compared = service.run("manager_compare", inputs={"ciks": [PATIENT, FASTLANE], "snapshot": pages})
    spec = views.views_for("manager_compare", compared)[0]
    assert spec["kind"] == "comparison" and len(spec["data"]["options"]) == 2
    assert spec["data"]["metrics"][0]["values"][1]["v"] == compared["result"]["managers"][1]["annual_turnover"]


# ------------------------------------------------------------------ mirror


def _positions(weights):
    return [{"ticker": t, "issuer": t, "cusip": t.ljust(9, "0"), "weight": w, "ticker_confidence": 0.99}
            for t, w in weights.items()]


def test_target_weights_cap_and_redistribute_pro_rata():
    out = managers.target_weights(_positions({"A": 0.5, "B": 0.3, "C": 0.1, "D": 0.1}), cap=0.35)
    assert out["weights"] == {"A": 0.35, "B": 0.35, "C": 0.15, "D": 0.15}
    assert out["capped"] == ["A", "B"] and out["unallocated"] == 0

    infeasible = managers.target_weights(_positions({"A": 0.5, "B": 0.3, "C": 0.1, "D": 0.1}), cap=0.2)
    assert set(infeasible["weights"].values()) == {0.2} and infeasible["unallocated"] == pytest.approx(0.2)

    trimmed = managers.target_weights(_positions({"A": 0.5, "B": 0.3, "C": 0.12, "D": 0.08}), top_n=2, min_weight=0.1)
    assert trimmed["weights"] == {"A": 0.625, "B": 0.375}
    assert [r["ticker"] for r in trimmed["dropped"]["below_min_weight"]] == ["D"]
    assert [r["ticker"] for r in trimmed["dropped"]["outside_top_n"]] == ["C"]

    unmapped = _positions({"A": 0.6, "B": 0.4})
    unmapped[1].update(ticker=None, ticker_confidence=0.0)
    low = managers.target_weights(unmapped + [{"ticker": "N", "cusip": "N", "weight": 0.1, "ticker_confidence": 0.7}])
    assert low["weights"] == {"A": 1.0} and len(low["dropped"]["unmapped"]) == 2


IPS = {"currency": "USD", "allocation": {"model": "growth", "sleeves": [
    {"id": "equity", "name": "Equity", "asset": "equity", "target": 0.8, "min": 0.75, "max": 0.85},
    {"id": "fixed_income", "name": "Fixed income", "asset": "fixed_income", "target": 0.2, "min": 0.15, "max": 0.25}]},
    "constraints": {"concentration": {"limit": 0.1}, "leverage": {"allowed": False},
                    "estate_situs": {"prefer": "non_us_domiciled"}}}


def test_mirror_applies_the_policy_limit_checks_each_buy_and_flags_a_satellite():
    held = managers.holdings(CIK, client=edgar(fixture_pages()))
    report = managers.mirror(held, 10000, "USD", ips=IPS,
                             constraints={"portfolio": {"currency": "USD", "positions": [
                                 {"symbol": "VTI", "value": 5000, "asset_class": "fund"}]}})
    result = report["result"]
    assert result["cap"] == {"single_name": 0.1, "portfolio_limit": 0.1,
                             "basis": "sleeve (no portfolio_value given, so the limit is applied inside the sleeve)",
                             "source": "policy.ips concentration limit", "capped": ["AAPL", "MSFT", "BTDR", "NWND"]}
    assert result["cash_weight"] == pytest.approx(0.6)
    assert [r["ticker"] for r in result["dropped"]["unmapped"]] == [None]
    assert result["policy_check"]["verdict"] == "violation"  # 1,000 is 16.7% of a 6,000 portfolio
    aapl = next(c for c in result["policy_check"]["by_name"] if c["ticker"] == "AAPL")
    assert {r["rule"] for r in aapl["rules"]} >= {"concentration", "estate_situs"}
    btdr = next(c for c in result["policy_check"]["by_name"] if c["ticker"] == "BTDR")
    assert "estate_situs" not in {r["rule"] for r in btdr["rules"]}  # a Cayman issuer is not US-situs
    assert result["speculation"]["verdict"] == "satellite" and result["speculation"]["status"] == "warn"
    assert result["expected_trades"]["initial"] == 4 and result["execution_ready"] is False
    assert result["tracking_caveats"] == managers.TRACKING_CAVEATS

    whole = managers.mirror(held, 10000, "USD", ips=IPS, constraints={"portfolio_value": 100000})["result"]
    assert whole["cap"]["single_name"] == 1.0 and whole["cash_weight"] == 0
    assert whole["target_weights"]["AAPL"] == pytest.approx(16 / 28.64, abs=1e-6)  # ZZQX's weight redistributed
    assert whole["speculation"]["share_of_portfolio"] == 0.1


def test_mirror_for_a_mexican_resident_flags_sic_situs_and_whole_shares():
    held = managers.holdings(CIK, client=edgar(fixture_pages()))
    report = managers.mirror(held, 20000, "MXN", situation={"profile": {"residence": {"country": "MX"}}},
                             constraints={"usdmxn": 20, "sic_listed": {"AAPL": True}, "portfolio_value": 200000})
    rows = {r["ticker"]: r for r in report["result"]["targets"]}
    assert rows["AAPL"]["mexico"]["sic_listed"] is True and rows["MSFT"]["mexico"]["sic_listed"] == "unknown"
    assert rows["AAPL"]["mexico"]["estate_situs"] == "us" and rows["BTDR"]["mexico"]["estate_situs"] == "not_us"
    aapl = rows["AAPL"]["mexico"]
    assert aapl["whole_share_cost_mxn"] == 3200.0  # 13F value / shares = $160, x 20
    assert aapl["whole_shares"] == int(rows["AAPL"]["amount"] // 3200)
    assert rows["AAPL"]["price_basis"].startswith("13F quarter-end value / shares")
    text = " ".join(report["warnings"])
    assert "SIC availability is unknown for MSFT, BTDR, NWND" in text and "US-situs" in text
    assert report["missing"] == ["policy.ips (accepted investment policy) to check the sleeve"]


def test_mirror_hands_off_to_rebalance_for_a_trade_list():
    held = managers.holdings(CIK, client=edgar(fixture_pages()))
    household = {"currency": "USD", "as_of": "2023-09-01", "complete": True, "people": [{"id": "p1"}],
                 "accounts": [{"id": "brk", "owner_id": "p1", "type": "taxable", "currency": "USD"}],
                 "positions": [{"id": "c", "account_id": "brk", "instrument_id": "cash:USD", "symbol": "CASH",
                                "quantity": 10000, "value": 10000, "currency": "USD", "asset_class": "cash"}],
                 "lots": [], "liabilities": [], "external_assets": [], "income_exposures": [], "fx": [],
                 "fund_holdings": []}
    report = managers.mirror(held, 10000, "USD", ips=IPS, constraints={
        "portfolio_value": 100000, "prices": {"AAPL": 160, "MSFT": 288, "BTDR": 12, "NWND": 40},
        "household": household,
        "jurisdiction_context": {"jurisdiction": "US", "trade_date": "2023-09-01",
                                 "accounts": {"brk": {"commission_rate": 0, "fractional": True}}}})
    plan = report["result"]["plan"]
    assert plan["status"] in {"ready", "partial"}
    buys = {t["instrument_id"] for t in plan["result"]["trades"] if t["side"] == "buy"}
    assert buys == {"AAPL", "MSFT", "BTDR", "NWND"}
    assert plan["result"]["execution_ready"] is False
    assert [v["kind"] for v in views.views_for("manager_mirror", report)] == ["allocation", "ticket"]


def test_mirror_without_tickers_still_carries_the_caveats():
    report = managers.mirror({"positions": [{"ticker": None, "weight": 1.0, "cusip": "X"}]}, 1000, "USD")
    assert report["status"] == "needs_input" and report["result"]["tracking_caveats"] == managers.TRACKING_CAVEATS
    service = managers.run("manager_mirror", {"sleeve_amount": 1000, "currency": "USD"})
    assert service["status"] == "needs_input" and service["result"]["tracking_caveats"]


def test_backtest_copies_each_13f_from_its_filing_date():
    quarters = [_book("2025-12-31", "2026-02-13", {"AAA": (100, 10)}),
                _book("2026-03-31", "2026-05-15", {"AAA": (50, 10), "BBB": (50, 10)})]
    prices = pd.DataFrame({"AAA": [100.0, 110.0, 121.0], "BBB": [None, 50.0, 40.0], "SPY": [400.0, 404.0, 420.0]},
                          index=pd.to_datetime(["2026-02-13", "2026-05-15", "2026-06-30"]))
    out = managers.backtest_from_history(quarters, prices, benchmark="SPY")
    first, second = out["periods"]
    assert (first["from"], first["to"], first["return"]) == ("2026-02-13", "2026-05-15", 0.1)
    assert second["return"] == pytest.approx(0.5 * 0.1 + 0.5 * -0.2, abs=1e-4)
    assert out["cumulative_return"] == pytest.approx(1.1 * 0.95 - 1, abs=1e-4)
    assert out["historical"] and out["lagged"] and "not a forecast" in out["label"]
    assert first["benchmark_return"] == 0.01


# ------------------------------------------------------------------ caveats, follow, monitor


def test_every_holdings_and_profile_result_explains_what_a_13f_is():
    client = edgar(fixture_pages())
    for result in (managers.holdings(CIK, client=client)["result"],
                   managers.profile(CIK, 2, client=client)["result"]):
        assert set(result["what_is_13f"]) == {"en", "es"}
        assert "45 days" in result["what_is_13f"]["en"] and "not a return" in result["what_is_13f"]["en"]
        assert any("short" in e for e in result["excludes"]) and any("cash" in e for e in result["excludes"])
        assert result["lag"]["days_since_filing"] is not None


def test_follow_facts_are_validated():
    validate("follow.0002045724", {"cik": "0002045724", "name": "Situational Awareness LP", "since": "2026-09-21",
                                   "mirror": {"sleeve_amount": 50000, "currency": "MXN", "top_n": 10}})
    for key, value in (("follow.2045724", {"cik": "2045724", "name": "x"}),
                       ("follow.0002045724", {"cik": "0001777813", "name": "x"}),
                       ("follow.0002045724", {"cik": "0002045724"}),
                       ("follow.0002045724", {"cik": "0002045724", "name": "x", "ticker": "SA"})):
        with pytest.raises(SchemaError):
            validate(key, value)


def _fact(key, value):
    return {"key": key, "value": value, "confidence": "reported", "expires_on": None}


def test_a_new_13f_for_a_followed_manager_is_a_proactive_item():
    snapshot = {"facts": [_fact("follow.0002045724", {"cik": "0002045724", "name": "Situational Awareness LP"})],
                "decisions": []}
    rule = {"rules": [{"id": "follow-13f", "kind": "manager_filing"}], "timezone": "UTC"}
    q1 = {"0002045724": {"accession": "0002045724-26-000008", "form": "13F-HR", "filing_date": "2026-05-18",
                         "period": "2026-03-31"}}
    q2 = {"0002045724": {"accession": "0000935836-26-000418", "form": "13F-HR", "filing_date": "2026-08-14",
                         "period": "2026-06-30"}}

    def run(state, filings, **extra):
        report = evaluate(snapshot, state, {**rule, "manager_filings": filings, **extra})
        return report["result"], report.pop("state")

    first, state = run({}, q1)
    assert first["checks"][0]["status"] == "clear" and first["events"] == []  # what is already filed is not news
    second, state = run(state, q2)
    assert second["checks"][0]["status"] == "active"
    item = second["events"][0]["detail"]["new_filings"][0]
    assert second["events"][0]["event"] == "review_needed"
    assert item["next_step"] == {"task": "manager_holdings", "inputs": {"cik": "0002045724"}}
    assert "Situational Awareness LP filed a new 13F" in item["title"]["en"] and item["title"]["es"]
    repeat, state = run(state, q2)
    assert repeat["events"] == []
    seen, state = run(state, q2, acknowledge=["follow-13f"])
    assert seen["checks"][0]["status"] == "clear" and seen["events"][0]["event"] == "acknowledged"

    nobody = evaluate({"facts": [], "decisions": []}, {}, {**rule, "manager_filings": {}})
    assert nobody["result"]["checks"][0]["status"] == "unknown"


def test_monitor_without_a_user_agent_reports_unchecked(monkeypatch, tmp_path):
    monkeypatch.delenv(managers.SEC_UA_ENV, raising=False)
    monkeypatch.setenv(managers.CACHE_ENV, str(tmp_path))
    snapshot = {"facts": [_fact("follow.0002045724", {"cik": "0002045724", "name": "SA"})], "decisions": []}
    report = evaluate(snapshot, {}, {"rules": [{"id": "f", "kind": "manager_filing"}], "timezone": "UTC"})
    check = report["result"]["checks"][0]
    assert check["status"] == "unknown" and managers.SEC_UA_ENV in check["detail"]["unchecked"]["0002045724"]
