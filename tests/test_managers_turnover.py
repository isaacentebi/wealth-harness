"""Turnover hardening (wealth/managers.py): gaps between filings, split evidence, units, incomplete quarters.

No network: books are built in memory or rendered into EDGAR pages with ``managers.build_snapshot``.
"""
from __future__ import annotations

import pytest

from wealth import managers


def _book(period, filing, holdings, options=(), *, issuers=None, extra=None):
    """holdings {name: (shares, price)}; issuers {name: issuer} shares an issuer across share classes."""
    issuers = issuers or {}
    extra = extra or {}
    rows = [{"issuer": issuers.get(t, t), "class": "COM", "cusip": t.ljust(9, "0"), "value": s * p, "shares": s,
             **extra.get(t, {})} for t, (s, p) in holdings.items()]
    rows += [{"issuer": issuers.get(t, t), "class": "COM", "cusip": t.ljust(9, "0"), "value": s * p, "shares": s,
              "put_call": k} for t, k, s, p in options]
    book = managers.positions_from_rows(rows)
    for p in book["equity"] + book["options"]:
        p["ticker"], p["ticker_confidence"] = p["issuer"], 0.99
    return {"period": period, "filing_date": filing, "book": book}


def _sec(t):
    return {"issuer": t, "class": "COM", "cusip": t.ljust(9, "0")}


def _client(people, figi):
    return managers.Edgar(transport=managers.Snapshot(managers.build_snapshot(people, figi=figi)))


# ------------------------------------------------------------------ gaps between filings


def test_a_missing_quarter_spreads_the_turnover_over_the_quarters_elapsed():
    gap = [_book("2025-12-31", "2026-02-13", {"AAA": (100, 10), "BBB": (100, 10)}),
           _book("2026-06-30", "2026-08-13", {"AAA": (150, 10), "BBB": (50, 10)})]
    profile = managers.profile_from_history(gap)
    (step,) = profile["turnover"]["per_quarter"]
    assert step["quarters_elapsed"] == 2 and step["turnover"] == 0.25 and step["turnover_per_quarter"] == 0.125
    assert profile["turnover"]["average_quarterly"] == 0.125 and profile["turnover"]["annualised"] == 0.5
    assert profile["holding_period"]["implied_years"] == 2.0
    assert profile["span_quarters"] == 3 and profile["holding_period"]["median_quarters"] == 3.0
    assert any("missing between 2025-12-31 and 2026-06-30" in c for c in profile["turnover"]["caveats"])
    assert "about 50% turnover a year" in profile["character"]["en"]

    full = [gap[0], _book("2026-03-31", "2026-05-14", {"AAA": (100, 10), "BBB": (100, 10)}), gap[1]]
    same = managers.profile_from_history(full)
    assert same["turnover"]["annualised"] == profile["turnover"]["annualised"]
    assert same["turnover"]["caveats"] == []


def test_the_gap_is_weighted_by_time_against_ordinary_quarters():
    quarters = [_book("2025-06-30", "2025-08-13", {"AAA": (100, 10), "BBB": (100, 10)}),
                _book("2025-09-30", "2025-11-13", {"AAA": (150, 10), "BBB": (50, 10)}),   # 0.25 in one quarter
                _book("2026-03-31", "2026-05-14", {"AAA": (150, 10), "BBB": (50, 10)})]   # 0 over two quarters
    turnover = managers.profile_from_history(quarters)["turnover"]
    assert turnover["average_quarterly"] == pytest.approx(0.25 / 3, abs=1e-4)
    assert turnover["annualised"] == pytest.approx(1 / 3, abs=1e-4)


def test_a_13f_notice_between_two_reports_does_not_double_the_annual_rate():
    people = [{"cik": "0009990004", "name": "Gap LP", "filings": [
        {"accession": "0009990004-26-000001", "form": "13F-HR", "filing_date": "2026-02-13", "period": "2025-12-31",
         "rows": [{**_sec("AAA"), "value": 1000, "shares": 100}, {**_sec("BBB"), "value": 1000, "shares": 100}]},
        {"accession": "0009990004-26-000002", "form": "13F-NT", "filing_date": "2026-05-14", "period": "2026-03-31",
         "other_managers": [{"cik": "0009990009", "name": "Parent"}]},
        {"accession": "0009990004-26-000003", "form": "13F-HR", "filing_date": "2026-08-13", "period": "2026-06-30",
         "rows": [{**_sec("AAA"), "value": 1500, "shares": 150}, {**_sec("BBB"), "value": 500, "shares": 50}]}]}]
    report = managers.profile("0009990004", 3, client=_client(people, {"AAA000000": "AAA", "BBB000000": "BBB"}),
                              sectors=False)
    assert report["result"]["turnover"]["annualised"] == 0.5
    assert any("missing between" in w for w in report["warnings"])


# ------------------------------------------------------------------ splits


def test_doubling_the_shares_while_the_price_moves_is_a_trade_without_corroboration():
    # Shares x2 with the price down 40%: a split candidate, but the value rose 20%, so it is a purchase.
    quarters = [_book("2025-12-31", "2026-02-13", {"AAA": (100, 100), "BBB": (100, 100)}),
                _book("2026-03-31", "2026-05-14", {"AAA": (200, 60), "BBB": (100, 100)})]
    profile = managers.profile_from_history(quarters)
    (step,) = profile["turnover"]["per_quarter"]
    assert profile["turnover"]["splits_ignored"] == []
    assert step["buys_estimate"] == 6000 and step["added_to"] == 1
    assert profile["turnover"]["possible_splits_counted_as_trades"] == [
        {"period": "2026-03-31", "cusip": "AAA000000", "issuer": "AAA", "ratio": 2}]
    assert any("nothing corroborates a split" in c for c in profile["turnover"]["caveats"])


def test_a_clean_ratio_with_the_value_unchanged_is_a_split():
    quarters = [_book("2025-12-31", "2026-02-13", {"AAA": (100, 100), "BBB": (100, 100)}),
                _book("2026-03-31", "2026-05-14", {"AAA": (200, 51), "BBB": (100, 100)})]   # value +2%
    profile = managers.profile_from_history(quarters)
    (step,) = profile["turnover"]["per_quarter"]
    assert [s["ratio"] for s in profile["turnover"]["splits_ignored"]] == [2]
    assert step["buys_estimate"] == 0 and step["added_to"] == 0
    # A share ratio that is not a clean multiple is not accepted on its own.
    rough = [_book("2025-12-31", "2026-02-13", {"AAA": (1000, 100), "BBB": (100, 100)}),
             _book("2026-03-31", "2026-05-14", {"AAA": (2003, 50), "BBB": (100, 100)})]
    assert managers.profile_from_history(rough)["turnover"]["splits_ignored"] == []


def test_a_split_corroborated_by_the_other_share_class_measures_a_trim_on_split_adjusted_shares():
    issuers = {"GOOGL": "ALPHABET INC", "GOOG": "ALPHABET INC"}
    before = _book("2025-12-31", "2026-02-13", {"GOOGL": (100, 2000), "GOOG": (100, 2000), "BBB": (100, 100)},
                   options=[("GOOGL", "call", 50, 2000)], issuers=issuers)
    # 20-for-1 with the price up 10% over the quarter (value +10%), and 10% of GOOGL sold.
    after = _book("2026-03-31", "2026-05-14", {"GOOGL": (1800, 110), "GOOG": (2000, 110), "BBB": (100, 100)},
                  options=[("GOOGL", "call", 1000, 110)], issuers=issuers)
    profile = managers.profile_from_history([before, after])
    splits = profile["turnover"]["splits_ignored"]
    assert sorted(s["cusip"] for s in splits) == ["GOOG00000", "GOOGL0000"] and {s["ratio"] for s in splits} == {20}
    assert all("lines show the same split" in s["basis"] for s in splits)
    (step,) = profile["turnover"]["per_quarter"]
    assert step["sells_estimate"] == 200 * 110 and step["buys_estimate"] == 0
    assert step["trimmed"] == 1 and step["added_to"] == 0

    # Two lines where one was also trimmed: only one line shows the ratio, so neither is taken as a split.
    lone = [_book("2025-12-31", "2026-02-13", {"GOOGL": (100, 2000), "GOOG": (100, 2000)}, issuers=issuers),
            _book("2026-03-31", "2026-05-14", {"GOOGL": (1800, 110), "GOOG": (2000, 110)}, issuers=issuers)]
    assert managers.profile_from_history(lone)["turnover"]["splits_ignored"] == []


def test_options_on_the_same_cusip_corroborate_a_split():
    before = _book("2025-12-31", "2026-02-13", {"AAA": (100, 100), "BBB": (100, 100)}, options=[("AAA", "call", 50, 100)])
    after = _book("2026-03-31", "2026-05-14", {"AAA": (400, 30), "BBB": (100, 100)}, options=[("AAA", "call", 200, 30)])
    profile = managers.profile_from_history([before, after])
    assert [s["ratio"] for s in profile["turnover"]["splits_ignored"]] == [4]
    assert profile["turnover"]["per_quarter"][0]["buys_estimate"] == 0


def test_build_up_ignores_split_adjusted_share_changes():
    quarters = [_book("2025-12-31", "2026-02-13", {"BBB": (100, 100)}),
                _book("2026-03-31", "2026-05-14", {"BBB": (100, 100), "AAA": (100, 100)}),
                _book("2026-06-30", "2026-08-13", {"BBB": (100, 100), "AAA": (200, 50)})]
    build = managers.profile_from_history(quarters)["conviction"]["build_up"]
    assert [n["built_up"] for n in build["names"]] == [False] and build["share_built_up"] == 0.0

    added = quarters[:2] + [_book("2026-06-30", "2026-08-13", {"BBB": (100, 100), "AAA": (200, 50)}),
                            _book("2026-09-30", "2026-11-13", {"BBB": (100, 100), "AAA": (300, 50)})]
    assert managers.profile_from_history(added)["conviction"]["build_up"]["names"][0]["built_up"] is True


# ------------------------------------------------------------------ identifiers and zero-value lines


def test_a_cusip_change_with_the_same_ticker_or_figi_is_not_turnover():
    by_ticker = [_book("2025-12-31", "2026-02-13", {"AAA": (100, 10), "BBB": (100, 10)}),
                 _book("2026-03-31", "2026-05-14", {"AAA2": (100, 10), "BBB": (100, 10)})]
    by_ticker[1]["book"]["equity"][0]["ticker"] = "AAA"
    profile = managers.profile_from_history(by_ticker)
    assert profile["turnover"]["per_quarter"][0]["turnover"] == 0.0
    assert profile["turnover"]["identifier_changes"] == [{"period": "2026-03-31", "issuer": "AAA2",
                                                         "old_cusip": "AAA000000", "new_cusip": "AAA200000",
                                                         "matched_by": "ticker"}]
    assert profile["holding_period"]["median_quarters"] == 2.0

    figi = {"AAA": {"figi": "BBG000000001"}, "AAA2": {"figi": "BBG000000001"}}
    by_figi = [_book("2025-12-31", "2026-02-13", {"AAA": (100, 10), "BBB": (100, 10)}, extra=figi),
               _book("2026-03-31", "2026-05-14", {"AAA2": (100, 10), "BBB": (100, 10)}, extra=figi)]
    changes = managers.profile_from_history(by_figi)["turnover"]["identifier_changes"]
    assert [c["matched_by"] for c in changes] == ["figi"]


def test_a_cusip_change_without_an_identifier_is_counted_with_a_caveat():
    issuers = {"AAA": "ACME CORP", "AAA2": "ACME CORP"}
    quarters = [_book("2025-12-31", "2026-02-13", {"AAA": (100, 10), "BBB": (100, 10)}, issuers=issuers),
                _book("2026-03-31", "2026-05-14", {"AAA2": (100, 10), "BBB": (100, 10)}, issuers=issuers)]
    for p in quarters[1]["book"]["equity"]:
        p["ticker"], p["ticker_confidence"] = None, 0.0
    profile = managers.profile_from_history(quarters)
    assert profile["turnover"]["per_quarter"][0]["turnover"] == 0.5
    assert any("new CUSIP" in c for c in profile["turnover"]["caveats"])


def test_zero_value_lines_are_not_positions():
    book = managers.positions_from_rows([
        {"issuer": "X", "class": "COM", "cusip": "X00000000", "value": 100, "shares": 10},
        {"issuer": "Z", "class": "COM", "cusip": "Z00000000", "value": 0, "shares": 3}])
    assert [p["cusip"] for p in book["equity"]] == ["X00000000"] and book["zero_value_lines"] == 1
    assert managers.profile_from_history([{"period": "2025-12-31", "filing_date": "2026-02-13", "book": book}]
                                         )["concentration"]["latest"]["positions"] == 1


# ------------------------------------------------------------------ units and incomplete quarters


def test_a_quarter_in_thousands_is_rescaled_and_makes_the_profile_partial():
    people = [{"cik": "0009990001", "name": "Mixed LP", "filings": [
        {"accession": "0009990001-22-000001", "form": "13F-HR", "filing_date": "2022-11-14", "period": "2022-09-30",
         "rows": [{**_sec("AAA"), "value": 1000, "shares": 100000}]},                 # thousands -> $1.0m
        {"accession": "0009990001-23-000001", "form": "13F-HR", "filing_date": "2023-02-14", "period": "2022-12-31",
         "rows": [{**_sec("BBB"), "value": 2000, "shares": 200000}]},                 # still thousands (filer error)
        {"accession": "0009990001-23-000002", "form": "13F-HR", "filing_date": "2023-05-15", "period": "2023-03-31",
         "rows": [{**_sec("BBB"), "value": 2000000, "shares": 200000}]}]}]
    report = managers.profile("0009990001", 3, client=_client(people, {"AAA000000": "AAA", "BBB000000": "BBB"}),
                              sectors=False)
    assert report["status"] == "partial"
    first, second = report["result"]["turnover"]["per_quarter"]
    assert (first["buys_estimate"], first["sells_estimate"], first["turnover"]) == (2000000, 1000000, 0.6667)
    assert second["turnover"] == 0.0
    assert any("rescaled x1000" in w for w in report["warnings"])


def test_a_quarter_with_tables_in_different_units_is_left_out():
    people = [{"cik": "0009990005", "name": "Split Units LP", "filings": [
        {"accession": "0009990005-23-000001", "form": "13F-HR", "filing_date": "2023-05-15", "period": "2023-03-31",
         "rows": [{**_sec("AAA"), "value": 1000000, "shares": 100000}]},
        {"accession": "0009990005-23-000002", "form": "13F-HR", "filing_date": "2023-08-14", "period": "2023-06-30",
         "rows": [{**_sec("AAA"), "value": 1000, "shares": 100000}]},                  # thousands by mistake
        {"accession": "0009990005-23-000003", "form": "13F-HR/A", "filing_date": "2023-09-01", "period": "2023-06-30",
         "amendment_type": "NEW HOLDINGS", "rows": [{**_sec("BBB"), "value": 500000, "shares": 5000}]},
        {"accession": "0009990005-23-000004", "form": "13F-HR", "filing_date": "2023-11-14", "period": "2023-09-30",
         "rows": [{**_sec("AAA"), "value": 1000000, "shares": 100000}]}]}]
    report = managers.profile("0009990005", 3, client=_client(people, {"AAA000000": "AAA", "BBB000000": "BBB"}),
                              sectors=False)
    assert report["status"] == "partial"
    assert report["result"]["periods"] == ["2023-03-31", "2023-09-30"]
    assert any("different units" in w for w in report["warnings"])


def test_a_new_holdings_amendment_without_its_original_is_excluded_from_turnover():
    people = [{"cik": "0009990002", "name": "Amend LP", "filings": [
        {"accession": "0009990002-23-000001", "form": "13F-HR", "filing_date": "2023-02-14", "period": "2022-12-31",
         "rows": [{**_sec("AAA"), "value": 1000000, "shares": 100000}]},
        {"accession": "0009990002-23-000002", "form": "13F-HR/A", "filing_date": "2023-06-01", "period": "2023-03-31",
         "amendment_type": "NEW HOLDINGS", "rows": [{**_sec("BBB"), "value": 1000, "shares": 10}]},
        {"accession": "0009990002-23-000003", "form": "13F-HR", "filing_date": "2023-08-14", "period": "2023-06-30",
         "rows": [{**_sec("AAA"), "value": 1000000, "shares": 100000}]}]}]
    client = _client(people, {"AAA000000": "AAA", "BBB000000": "BBB"})
    report = managers.profile("0009990002", 3, client=client, sectors=False)
    result = report["result"]
    assert report["status"] == "partial"
    assert result["periods"] == ["2022-12-31", "2023-06-30"]
    (step,) = result["turnover"]["per_quarter"]
    assert step["quarters_elapsed"] == 2 and step["turnover"] == 0.0 and step["new_positions"] == 0
    assert result["conviction"]["new_positions"] == 0
    assert any("NEW HOLDINGS amendment" in w and "incomplete" in w for w in report["warnings"])

    held = managers.holdings("0009990002", "2023-03-31", client=client)
    assert held["status"] == "partial"
    later = managers.holdings("0009990002", "2023-06-30", client=client)
    assert later["result"]["changes"] is None
    assert any("2023-03-31 is incomplete" in w for w in later["warnings"])
