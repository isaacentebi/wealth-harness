from __future__ import annotations

from decimal import Decimal
import sqlite3

import pytest

from wealth import ledger as L
from wealth.household import run as household_run, validate_household
from wealth.store import RequestConflictError, ValidationError, WealthStore


A = "a" * 64
B = "b" * 64


@pytest.fixture
def store():
    with WealthStore(":memory:") as db:
        db.create_client("c", "Client")
        yield db


def batch(batch_id, transactions=(), *, file_hash=A, ref="statement", accounts=None, instruments=None, **extra):
    return {
        "batch_id": batch_id,
        "source": {"kind": "document", "ref": ref, "file_hash": file_hash, "observed_on": "2026-02-01"},
        "accounts": accounts if accounts is not None else [],
        "instruments": instruments if instruments is not None else [],
        "transactions": list(transactions),
        **extra,
    }


BANK = {"id": "bank", "institution": "BBVA", "type": "checking", "currency": "MXN",
        "owners": [{"person_id": "ana", "share": 1}]}
GBM = {"id": "gbm", "institution": "GBM", "type": "brokerage", "currency": "MXN",
       "owners": [{"person_id": "ana", "share": "0.5"}, {"person_id": "luis", "share": "0.5"}]}
IB = {"id": "ib", "institution": "Interactive Brokers", "type": "taxable", "currency": "USD",
      "owners": [{"person_id": "ana", "share": 1}]}
SCHWAB = {"id": "schwab", "institution": "Schwab", "type": "taxable", "currency": "USD",
          "owners": [{"person_id": "ana", "share": 1}]}
X = {"id": "X", "symbol": "X", "currency": "USD", "asset_class": "equity", "venue": "us"}
VOO_US = {"id": "VOO", "symbol": "VOO", "currency": "USD", "asset_class": "equity", "venue": "us",
          "issuer_domicile": "US"}
VOO_SIC = {"id": "VOO-SIC", "symbol": "VOO", "currency": "MXN", "asset_class": "equity", "venue": "sic",
           "underlying_symbol": "VOO", "issuer_domicile": "US"}


def expense(day, amount, description, account="bank", **extra):
    return {"account_id": account, "kind": "expense", "date": day, "amount": amount, "currency": "MXN",
            "description": description, **extra}


def test_posting_is_idempotent_masks_numbers_and_rejects_reused_batch_ids(store):
    first = L.post(store, "c", batch("jan", [
        {"account_id": "bank", "kind": "deposit", "date": "2026-01-02", "amount": "10000", "currency": "MXN",
         "description": "SPEI recibido 012180001234567890"},
    ], accounts=[BANK]))
    again = store.post_ledger("c", batch("jan", [
        {"account_id": "bank", "kind": "deposit", "date": "2026-01-02", "amount": "10000", "currency": "MXN",
         "description": "SPEI recibido 012180001234567890"},
    ], accounts=[BANK]))
    assert again["replayed"] is True and again["posted"] == first["posted"]
    entry = store.ledger("c")["entries"][0]
    assert entry["description"] == "SPEI recibido ****7890"
    assert any("masked" in w for w in first["warnings"])
    with pytest.raises(RequestConflictError):
        store.post_ledger("c", batch("jan", [expense("2026-01-03", -1, "x")], accounts=[BANK]))
    with pytest.raises(ValidationError, match="identifier"):
        store.post_ledger("c", batch("bad", accounts=[dict(BANK, id="b2", clabe="012180001234567890")]))
    with pytest.raises(ValidationError, match="not a ledger account"):
        store.post_ledger("c", batch("orphan", [expense("2026-01-03", -1, "x", account="nope")]))
    with pytest.raises(ValidationError, match="must be negative"):
        store.post_ledger("c", batch("sign", [{"account_id": "bank", "kind": "withdrawal", "date": "2026-01-03",
                                               "amount": 5, "currency": "MXN"}]))


def test_overlapping_statements_dedupe_exact_lines_and_hold_fuzzy_ones(store):
    january = [
        expense("2026-01-10", "-85.50", "OXXO SUC 1234 CDMX"),
        expense("2026-01-10", "-85.50", "OXXO SUC 1234 CDMX"),  # two genuine identical purchases
        expense("2026-01-20", "-1200", "CFE SUMINISTRADOR"),
        expense("2026-01-25", "-349", "NETFLIX.COM"),
    ]
    first = store.post_ledger("c", batch("jan", january, accounts=[BANK]))
    assert len(first["posted"]) == 4
    overlap = [
        expense("2026-01-10", "-85.5", "OXXO SUC 1234 CDMX"),
        expense("2026-01-10", "-85.5", "OXXO SUC 1234 CDMX"),
        expense("2026-01-20", "-1200.00", "CFE SUMINISTRADOR"),
        expense("2026-01-26", "-349", "NETFLIX COM MX"),
        expense("2026-02-03", "-500", "WALMART SUPERCENTER"),
    ]
    second = store.post_ledger("c", batch("feb", overlap, file_hash=B))
    assert [d["line"] for d in second["duplicates"]] == [0, 1, 2]
    assert [h["line"] for h in second["held"]] == [3]
    assert len(second["posted"]) == 1
    assert len(store.ledger("c")["entries"]) == 5
    confirmed = store.post_ledger("c", batch("feb-confirm", [
        dict(overlap[3], confirm_not_duplicate=True)], file_hash=B))
    assert len(confirmed["posted"]) == 1 and not confirmed["held"]
    # Re-posting the whole second statement later changes nothing.
    replay = store.post_ledger("c", batch("feb-again", overlap, file_hash=B))
    assert replay["posted"] == [] and len(replay["duplicates"]) == 5


def test_balance_assertions_report_breaks_and_reversals_correct_append_only(store):
    receipt = L.post(store, "c", batch("jan", [
        {"account_id": "bank", "kind": "opening_balance", "date": "2026-01-01", "amount": 1000, "currency": "MXN"},
        expense("2026-01-05", -300, "RENTA ENERO"),
    ], accounts=[BANK], balance_assertions=[{"account_id": "bank", "date": "2026-01-31", "currency": "MXN",
                                             "balance": 800}]))
    assert receipt["reconciliation"]["status"] == "partial"
    brk = receipt["reconciliation"]["breaks"][0]
    assert (brk["expected"], brk["derived"], brk["difference"]) == ("800", "700", "-100")
    wrong = receipt["posted"][1]
    fix = L.post(store, "c", batch("fix", [
        {"account_id": "bank", "kind": "reversal", "date": "2026-02-01", "reverses_id": wrong},
        expense("2026-01-05", -200, "RENTA ENERO corregida"),
    ], file_hash=B))
    ledger = store.ledger("c")
    assert len(ledger["entries"]) == 4  # nothing edited or deleted
    assert L.reconcile(ledger)["status"] == "ready"
    with pytest.raises(ValidationError, match="already reversed"):
        store.post_ledger("c", batch("fix2", [{"account_id": "bank", "kind": "reversal", "date": "2026-02-02",
                                                "reverses_id": wrong}], file_hash=B))
    with pytest.raises(ValidationError, match="unknown entry"):
        store.post_ledger("c", batch("fix3", [{"account_id": "bank", "kind": "reversal", "date": "2026-02-02",
                                                "reverses_id": "tx_missing"}], file_hash=B))
    assert fix["posted"]


def _us_ledger(store):
    store.post_ledger("c", batch("ib", [
        {"account_id": "ib", "kind": "deposit", "date": "2025-01-02", "amount": 5000, "currency": "USD"},
        {"account_id": "ib", "kind": "buy", "date": "2025-01-10", "instrument_id": "X", "quantity": 10,
         "price": 100, "currency": "USD"},
        {"account_id": "ib", "kind": "buy", "date": "2025-06-01", "instrument_id": "X", "quantity": 10,
         "price": 120, "currency": "USD"},
        {"account_id": "ib", "kind": "split", "date": "2025-07-01", "instrument_id": "X", "ratio": 2},
        {"account_id": "ib", "kind": "sell", "date": "2026-02-01", "instrument_id": "X", "quantity": 25,
         "price": 70, "currency": "USD"},
    ], accounts=[IB, SCHWAB], instruments=[X], fx=[
        {"date": "2025-01-10", "base": "USD", "quote": "MXN", "rate": "20.5", "source": "Banxico FIX"},
        {"date": "2026-02-01", "base": "USD", "quote": "MXN", "rate": "17.25", "source": "Banxico FIX"},
    ]))
    return store.ledger("c")


def test_split_then_partial_sale_relieves_fifo_with_holding_periods_and_fx(store):
    ledger = _us_ledger(store)
    held = L.holdings(ledger, "2026-02-02")
    lots = held["result"]["lots"]
    assert len(lots) == 1
    assert (lots[0]["quantity"], lots[0]["cost_basis"], lots[0]["acquired_on"]) == ("15", "900.00", "2025-06-01")
    assert lots[0]["cost_basis_mxn"] is None  # no rate near 2025-06-01: unknown, not zero
    assert any(m["key"].startswith("fx.USD/MXN@2025-06-01") for m in held["missing"])
    cash = {(c["account_id"], c["currency"]): c["balance"] for c in held["result"]["cash"]}
    assert cash[("ib", "USD")] == str(5000 - 1000 - 1200 + 1750) + ".00"
    gains = L.realized_gains(ledger, year=2026)
    sales = gains["result"]["sales"]
    assert [(s["lot_id"][:3], s["quantity"], s["gain"], s["holding"]) for s in sales] == [
        ("tx_", "20", "400.00", "long"), ("tx_", "5", "50.00", "short")]
    # MXN gain of the first lot: 1400 * 17.25 - 1000 * 20.5; the second lacks a basis-date rate.
    assert sales[0]["gain_mxn"] == "3650.00"
    assert sales[1]["gain_mxn"] is None
    assert gains["result"]["years"]["2026"]["gain_mxn"] is None
    assert gains["result"]["years"]["2026"]["gain_by_currency_and_holding"] == {"USD:long": "400.00", "USD:short": "50.00"}


def test_specific_identification_and_oversell_are_explicit(store):
    ledger = _us_ledger(store)
    second_lot = next(e["id"] for e in ledger["entries"] if e["kind"] == "buy" and e["date"] == "2025-06-01")
    ledger["entries"][-1] = dict(ledger["entries"][-1], lot_selection=[{"lot_id": second_lot, "quantity": "20"}],
                                 quantity="20", amount="1400")
    sales = L.realized_gains(ledger, lot_method="specific")["result"]["sales"]
    assert [(s["lot_id"], s["gain"]) for s in sales] == [(second_lot, "200.00")]
    oversold = dict(ledger["entries"][-1], quantity="50", amount="3500")
    oversold.pop("lot_selection")
    ledger["entries"][-1] = oversold
    held = L.holdings(ledger, "2026-03-01")
    assert held["status"] == "partial"
    assert held["result"]["breaks"][0]["kind"] == "oversold"
    assert L.realized_gains(ledger)["result"]["years"]["2026"]["unknown_basis_lots"] == 1


def test_transfers_between_own_accounts_match_and_carry_lots(store):
    _us_ledger(store)
    store.post_ledger("c", batch("move", [
        {"account_id": "ib", "kind": "transfer", "date": "2026-03-01", "instrument_id": "X", "quantity": -15},
        {"account_id": "schwab", "kind": "transfer", "date": "2026-03-03", "instrument_id": "X", "quantity": 15},
        {"account_id": "ib", "kind": "transfer", "date": "2026-03-04", "amount": -1000, "currency": "USD"},
        {"account_id": "schwab", "kind": "transfer", "date": "2026-03-05", "amount": 1000, "currency": "USD"},
    ], file_hash=B))
    ledger = store.ledger("c")
    in_transit = L.holdings(ledger, "2026-03-02")
    assert not [l for l in in_transit["result"]["lots"] if l["instrument_id"] == "X"]
    held = L.holdings(ledger, "2026-03-10")
    assert len(held["result"]["transfers"]) == 2 and not held["result"]["unmatched_transfers"]
    (lot,) = held["result"]["lots"]
    assert (lot["account_id"], lot["quantity"], lot["cost_basis"], lot["acquired_on"]) == (
        "schwab", "15", "900.00", "2025-06-01")


def test_household_output_is_canonical_and_omits_unknown_basis_lots(store):
    store.post_ledger("c", batch("mix", [
        {"account_id": "gbm", "kind": "opening_balance", "date": "2026-01-01", "amount": 5000, "currency": "MXN"},
        {"account_id": "gbm", "kind": "opening_balance", "date": "2026-01-01", "instrument_id": "VOO-SIC",
         "quantity": 2},
        {"account_id": "ib", "kind": "opening_balance", "date": "2026-01-01", "amount": 100, "currency": "USD"},
        {"account_id": "ib", "kind": "buy", "date": "2026-01-10", "instrument_id": "VOO", "quantity": 1,
         "price": 500, "currency": "USD"},
    ], accounts=[GBM, IB], instruments=[VOO_US, VOO_SIC], fx=[
        {"date": "2026-01-09", "base": "USD", "quote": "MXN", "rate": "18", "source": "Banxico FIX"}]))
    prices = L.price_table({"VOO": [{"date": "2026-01-30", "price": 520}], "VOO-SIC": {"2026-01-30": "9400"}})
    out = L.to_household(store.ledger("c"), "2026-01-31", "MXN", prices)
    household = out["result"]["household"]
    validate_household(household)
    assert {p["id"] for p in household["positions"]} == {"gbm:VOO-SIC", "ib:VOO", "gbm:cash:MXN"}
    (lot,) = household["lots"]
    assert (lot["instrument_id"], lot["cost_basis"], lot["currency"], lot["cost_basis_mxn"]) == (
        "VOO", "500.00", "USD", "9000.00")
    assert any(m["key"].endswith("cost_basis") for m in out["missing"])
    gbm = next(a for a in household["accounts"] if a["id"] == "gbm")
    assert gbm["owner_id"] == "ana" and len(gbm["owner_shares"]) == 2
    assert household["liabilities"][0]["id"] == "ib:USD"  # cash went negative after the buy
    exposure = household_run("exposure", {"household": household})
    assert exposure["status"] in {"ready", "partial"}


def test_sic_lots_stay_in_mxn_and_group_by_underlying_or_venue(store):
    store.post_ledger("c", batch("sic", [
        {"account_id": "gbm", "kind": "buy", "date": "2026-01-10", "instrument_id": "VOO-SIC", "quantity": 1,
         "amount": -9000, "currency": "MXN"},
        {"account_id": "ib", "kind": "buy", "date": "2026-01-10", "instrument_id": "VOO", "quantity": 1,
         "amount": -500, "currency": "USD"},
    ], accounts=[GBM, IB], instruments=[VOO_US, VOO_SIC], fx=[
        {"date": "2026-01-30", "base": "USD", "quote": "MXN", "rate": "18", "source": "Banxico FIX"}]))
    with pytest.raises(ValidationError, match="SIC listing"):
        store.post_ledger("c", batch("bad", [
            {"account_id": "gbm", "kind": "buy", "date": "2026-01-11", "instrument_id": "VOO-SIC", "quantity": 1,
             "amount": -500, "currency": "USD"}], file_hash=B))
    with pytest.raises(ValidationError, match="MXN"):
        store.post_ledger("c", batch("bad2", instruments=[dict(VOO_SIC, id="V2", currency="USD")], file_hash=B))
    ledger = store.ledger("c")
    prices = L.price_table({"VOO": {"2026-01-30": 520}, "VOO-SIC": {"2026-01-30": 9360}})
    by_underlying = L.exposure_groups(ledger, "2026-01-31", "MXN", prices)["result"]["groups"]
    assert list(by_underlying) == ["VOO"] and by_underlying["VOO"]["value"] == "18720.00"
    by_venue = L.exposure_groups(ledger, "2026-01-31", "MXN", prices, by="venue")["result"]["groups"]
    assert sorted(by_venue) == ["sic", "us"]
    lots = {l["instrument_id"]: l for l in L.holdings(ledger, "2026-01-31")["result"]["lots"]}
    assert lots["VOO-SIC"]["currency"] == "MXN" and lots["VOO"]["currency"] == "USD"
    positions = L.holdings(ledger, "2026-01-31")["result"]["positions"]
    assert {(p["instrument_id"], p["venue"], p["underlying_symbol"]) for p in positions} == {
        ("VOO", "us", "VOO"), ("VOO-SIC", "sic", "VOO")}


def test_spin_off_without_allocation_and_merger_carry_basis(store):
    store.post_ledger("c", batch("ca", [
        {"account_id": "ib", "kind": "buy", "date": "2025-01-10", "instrument_id": "X", "quantity": 10,
         "amount": -1000, "currency": "USD"},
        {"account_id": "ib", "kind": "spin_off", "date": "2025-05-01", "instrument_id": "X", "ratio": "0.5",
         "new_instrument_id": "Y"},
        {"account_id": "ib", "kind": "merger", "date": "2025-06-01", "instrument_id": "Y", "ratio": 2,
         "new_instrument_id": "Z"},
    ], accounts=[IB], instruments=[X, dict(X, id="Y", symbol="Y"), dict(X, id="Z", symbol="Z")]))
    held = L.holdings(store.ledger("c"), "2025-07-01")
    lots = {l["instrument_id"]: l for l in held["result"]["lots"]}
    assert lots["X"]["cost_basis"] is None and lots["Z"]["quantity"] == "10"
    assert lots["Z"]["acquired_on"] == "2025-01-10"
    assert any("basis_allocation" in m["key"] for m in held["missing"])


def _perf_ledger():
    return {
        "accounts": [dict(IB, id="brk")], "instruments": [X], "fx": [],
        "entries": [
            {"id": "d1", "account_id": "brk", "kind": "deposit", "date": "2025-01-01", "amount": "1000", "currency": "USD"},
            {"id": "b1", "account_id": "brk", "kind": "buy", "date": "2025-01-01", "instrument_id": "X",
             "quantity": "100", "amount": "-1000", "currency": "USD"},
            {"id": "d2", "account_id": "brk", "kind": "deposit", "date": "2025-07-02", "amount": "1500", "currency": "USD"},
            {"id": "b2", "account_id": "brk", "kind": "buy", "date": "2025-07-02", "instrument_id": "X",
             "quantity": "100", "amount": "-1500", "currency": "USD"},
        ],
    }


def test_twr_and_xirr_known_answers_differ_when_flows_are_badly_timed():
    prices = L.price_table({"X": {"2025-01-01": 10, "2025-07-02": 15, "2026-01-01": 12}})
    result = L.performance(_perf_ledger(), "2024-12-31", "2026-01-01", "USD", prices,
                           benchmark=lambda day: L.price_table({"B": {"2024-12-31": 100, "2025-01-01": 100,
                                                                      "2025-07-02": 110, "2026-01-01": 121}})("B", day),
                           benchmark_name="B")
    assert result["status"] == "ready"
    assert result["result"]["twr"]["period"] == "0.200000"
    irr = Decimal(result["result"]["xirr_annual"])
    assert irr < 0  # more money arrived just before the fall
    flows = [("2025-01-01", -1000), ("2025-07-02", -1500), ("2026-01-01", 2400)]
    npv = sum(c / (1 + float(irr)) ** ((L.derive.date.fromisoformat(d) - L.derive.date(2025, 1, 1)).days / 365) for d, c in flows)
    assert abs(npv) < 0.01
    decomposition = result["result"]["decomposition"]
    assert (decomposition["net_flows"], decomposition["total_gain"]) == ("2500.00", "-100.00")
    bench = result["result"]["benchmark"]
    assert bench["period_return"] == "0.210000"
    # Same flows in the benchmark: 10 units at 100 plus 1500/110 units, worth 121 each.
    expected = ((Decimal(10) + Decimal(1500) / Decimal(110)) * Decimal(121)).quantize(Decimal("0.01"))
    assert bench["same_flows_ending_value"] == str(expected)


def test_simple_year_matches_and_modified_dietz_known_answer():
    ledger = {"accounts": [dict(IB, id="brk")], "instruments": [X], "fx": [], "entries": [
        {"id": "d", "account_id": "brk", "kind": "deposit", "date": "2024-12-31", "amount": "1000", "currency": "USD"},
        {"id": "b", "account_id": "brk", "kind": "buy", "date": "2024-12-31", "instrument_id": "X",
         "quantity": "100", "amount": "-1000", "currency": "USD"},
    ]}
    prices = L.price_table({"X": {"2024-12-31": 10, "2025-12-31": 11}})
    result = L.performance(ledger, "2024-12-31", "2025-12-31", "USD", prices)["result"]
    assert result["twr"]["period"] == "0.100000" and result["twr"]["annualized"] == "0.100000"
    assert result["xirr_annual"] == "0.100000"
    assert L.xirr([("2025-01-01", Decimal(-1000)), ("2026-01-01", Decimal(1100))]) == pytest.approx(0.1, abs=1e-9)

    ledger["entries"] = [
        {"id": "d", "account_id": "brk", "kind": "deposit", "date": "2025-05-31", "amount": "1000", "currency": "USD"},
        {"id": "b", "account_id": "brk", "kind": "buy", "date": "2025-05-31", "instrument_id": "X",
         "quantity": "100", "amount": "-1000", "currency": "USD"},
        {"id": "d2", "account_id": "brk", "kind": "deposit", "date": "2025-06-15", "amount": "500", "currency": "USD"},
    ]
    prices = L.price_table({"X": {"2025-06-01": 10, "2025-06-15": 10.5, "2025-06-30": 11}})
    dietz = L.performance(ledger, "2025-06-01", "2025-06-30", "USD", prices, method="modified_dietz")["result"]
    expected = (Decimal(1600) - 1000 - 500) / (Decimal(1000) + Decimal(500) * Decimal(15) / Decimal(29))
    assert dietz["twr"]["period"] == format(expected.quantize(Decimal("0.000001")), "f")
    assert dietz["twr"]["annualized"] is None  # under a year: not annualized


def test_missing_price_or_fx_is_a_gap_not_zero():
    prices = L.price_table({"X": {"2025-01-01": 10, "2025-07-02": 15}})
    result = L.performance(_perf_ledger(), "2024-12-31", "2026-01-01", "USD", prices)
    assert result["status"] == "partial"
    assert result["result"]["twr"]["period"] is None and result["result"]["end_value"] is None
    assert any("price X on 2026-01-01" in m["key"] for m in result["missing"])
    full = L.price_table({"X": {"2025-01-01": 10, "2025-07-02": 15, "2026-01-01": 12}})
    in_mxn = L.performance(_perf_ledger(), "2024-12-31", "2026-01-01", "MXN", full)
    assert in_mxn["result"]["twr"]["period"] is None
    assert any(m["key"].startswith("fx USD/MXN") for m in in_mxn["missing"])
    series = L.valuation_series(_perf_ledger(), ["2025-03-01"], "USD", full)["points"][0]
    assert series["value"] is None and series["gaps"] == ["price X on 2025-03-01"]


def test_income_by_year_converts_at_transaction_date(store):
    store.post_ledger("c", batch("div", [
        {"account_id": "ib", "kind": "dividend", "date": "2026-03-15", "instrument_id": "X", "amount": 10,
         "currency": "USD"},
        {"account_id": "ib", "kind": "tax_withheld", "date": "2026-03-15", "instrument_id": "X", "amount": -3,
         "currency": "USD"},
        {"account_id": "ib", "kind": "interest", "date": "2026-04-30", "amount": 2, "currency": "USD"},
    ], accounts=[IB], instruments=[X], fx=[
        {"date": "2026-03-13", "base": "USD", "quote": "MXN", "rate": "17", "source": "Banxico FIX"}]))
    report = L.investment_income(store.ledger("c"))
    year = report["result"]["years"]["2026"]
    assert year["native"]["dividend"] == {"USD": "10.00"}
    assert year["usd"] == {"dividend": "10.00", "interest": "2.00", "tax_withheld": "-3.00"}
    assert year["mxn"] is None  # interest date has no nearby rate
    assert report["status"] == "partial"


def test_version_one_database_migrates_to_ledger_schema(tmp_path):
    db = tmp_path / "old.sqlite3"
    WealthStore(db).close()
    connection = sqlite3.connect(db)
    for table in ("ledger_accounts", "ledger_instruments", "ledger_entries", "ledger_fx", "ledger_assertions",
                  "ledger_batches", "ledger_rules", "ledger_labels"):
        connection.execute(f"DROP TABLE {table}")
    connection.execute("UPDATE metadata SET value = '1' WHERE key = 'schema_version'")
    connection.commit()
    connection.close()
    with WealthStore(db) as migrated:
        migrated.create_client("c", "Client")
        migrated.post_ledger("c", batch("x", [expense("2026-01-02", -10, "OXXO")], accounts=[BANK]))
        assert len(migrated.ledger("c")["entries"]) == 1
        assert migrated.export_client("c")["ledger"]["batches"][0]["batch_id"] == "x"
    connection = sqlite3.connect(db)
    assert connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0] == "3"
    connection.close()


def test_run_envelope_views():
    ledger = _perf_ledger()
    assert L.run("ledger", {})["status"] == "needs_input"
    held = L.run("ledger", {"ledger": ledger, "as_of": "2025-12-31"})
    assert held["result"]["positions"][0]["quantity"] == "200"
    perf = L.run("performance", {"ledger": ledger, "start": "2024-12-31", "end": "2026-01-01", "currency": "USD",
                                 "prices": {"X": [{"date": "2025-01-01", "price": 10}, {"date": "2025-07-02", "price": 15},
                                                  {"date": "2026-01-01", "price": 12}]}, "by_account": True})
    assert perf["result"]["accounts"]["brk"]["twr"]["period"] == "0.200000"
