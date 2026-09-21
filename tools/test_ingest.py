# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "yfinance>=0.2.50",
#   "pandas",
#   "numpy",
#   "scipy",
#   "statsmodels",
#   "pytest",
#   "openpyxl",
#   "pypdf",
# ]
# ///
"""Offline tests for `wm.py ingest`: statement parsing and position
normalization. No network — price, meta and FX lookups are stubbed.

Run:  uv run --with pytest tools/test_wm.py tools/test_safety.py tools/test_ingest.py
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

_spec = importlib.util.spec_from_file_location("wm", Path(__file__).with_name("wm.py"))
wm = importlib.util.module_from_spec(_spec)
sys.modules["wm"] = wm
_spec.loader.exec_module(wm)

_LAST = {"AAPL": 250.0, "ASML.AS": 800.0, "VODL.L": 70.0}
_QCCY = {"ASML.AS": "EUR", "VODL.L": "GBp"}
_FX = {("EUR", "USD"): 1.10, ("GBP", "USD"): 1.27}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def fake_fetch(t):
        if t in _LAST:
            return pd.Series([240.0, _LAST[t]],
                             index=pd.bdate_range("2026-09-15", periods=2))
        raise RuntimeError("no data")

    def fake_fx(frm, to):
        rate = _FX.get((frm, to))
        if rate is None:
            return None
        return pd.Series([rate], index=[pd.Timestamp.today().normalize()])

    monkeypatch.setattr(wm, "_fetch_one", fake_fetch)
    monkeypatch.setattr(wm, "_ticker_meta", lambda ts: {
        t: {"currency": _QCCY.get(t, "USD"), "expense_ratio": None} for t in ts})
    monkeypatch.setattr(wm, "_fx_series", fake_fx)


def _ingest_file(tmp_path, text, name="stmt.csv", currency="USD", capsys=None):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    wm.main(["ingest", str(path), "--currency", currency])
    return json.loads(capsys.readouterr().out)


# --------------------------------------------------------------------------
# primitives
# --------------------------------------------------------------------------
def test_normalize_symbol():
    assert wm.normalize_symbol("brk/b") == "BRK-B"
    assert wm.normalize_symbol("  $aapl** ") == "AAPL"
    assert wm.normalize_symbol("NAFTRAC.MX") == "NAFTRAC.MX"
    assert wm.normalize_symbol("VOD.L") == "VOD.L"  # .L is the exchange, not a class
    assert wm.normalize_symbol("--") == ""


def test_parse_amount_statement_formats():
    assert wm._parse_amount("$1,234.56") == 1234.56
    assert wm._parse_amount("(1,234.56)") == -1234.56
    assert wm._parse_amount("500-") == -500.0
    assert wm._parse_amount("12.5%") == 12.5
    assert wm._parse_amount("") is None and wm._parse_amount("n/a") is None
    assert wm._parse_amount(10) == 10.0


def test_classify_rows():
    assert wm._classify("SPAXX", "Fidelity Government MM") == "cash"
    assert wm._classify("", "Pending Cash") == "cash"
    assert wm._classify("AAPL230616C00150000", "") == "option"
    assert wm._classify("", "AAPL JAN 15 2027 250 CALL") == "option"
    assert wm._classify("037833100", "US TREASURY NOTE 4.25% 11/15/2034") == "fixed_income"
    assert wm._classify("037833100", "APPLE INC") == "unknown"  # cusip
    assert wm._classify("AAPL", "APPLE INC") == "security"
    assert wm._classify("", "MYSTERY FUND") == "unknown"


# --------------------------------------------------------------------------
# flat statements
# --------------------------------------------------------------------------
GENERIC = """Account summary,,,,,
Symbol,Description,Quantity,Last Price,Market Value
AAPL,APPLE INC,10,$250.00,"$2,500.00"
MSFT,MICROSOFT CORP,5,$400.00,"$2,000.00"
"""


def test_generic_csv(tmp_path, capsys):
    out = _ingest_file(tmp_path, GENERIC, capsys=capsys)
    assert out["total_value"] == 4500.0
    assert {p["symbol"] for p in out["positions"]} == {"AAPL", "MSFT"}
    assert out["analysis_ready"] == pytest.approx(
        {"AAPL": 2500 / 4500, "MSFT": 2000 / 4500})
    assert out["analysis_coverage"] == 1.0


SCHWAB = """"Positions for account ...1234 as of 08/01/2026"
"Symbol","Description","Quantity","Price","Market Value","% Of Account"
"AAPL","APPLE INC","10","$250.00","$2,500.00","50%"
"SWVXX","SCHWAB VALUE ADVANTAGE MONEY","1500","$1.00","$1,500.00","30%"
"","Cash & Cash Investments","--","--","$500.00","10%"
"Account Total","","","","$5,000.00","100%"
"""


def test_schwab_cash_and_footer(tmp_path, capsys):
    out = _ingest_file(tmp_path, SCHWAB, capsys=capsys)
    assert out["source"]["as_of"] == "2026-08-01"
    cash = [p for p in out["positions"] if p["type"] == "cash"]
    assert sum(p["value"] for p in cash) == 2000.0  # mmkt + literal cash merged under own keys
    assert out["cash_weight"] == pytest.approx(2000 / 4500)
    assert out["total_value"] == 4500.0  # footer not double counted
    assert out["skipped_rows"] >= 1
    assert any("dated" in w for w in out["warnings"])  # stale statement flagged


FIDELITY = """Account Name/Number,,,,
Brokerage - 123456789,,,,
Symbol,Description,Quantity,Last Price,Current Value
AAPL,APPLE INC,10,250.00,2500.00
SPAXX**,FIDELITY GOVERNMENT MONEY MARKET,1000,1.00,1000.00
Disclaimer text that should be skipped,,,,
"""


def test_fidelity_star_suffix_cash(tmp_path, capsys):
    out = _ingest_file(tmp_path, FIDELITY, capsys=capsys)
    types = {p["symbol"]: p["type"] for p in out["positions"]}
    assert types == {"AAPL": "security", "SPAXX": "cash"}
    assert out["cash_weight"] == pytest.approx(1000 / 3500)


# --------------------------------------------------------------------------
# sectioned (IBKR-style) report
# --------------------------------------------------------------------------
IBKR = """Statement,Header,Field Name,Field Value
Statement,Data,Broker Name,Example Broker
Positions,Header,DataDiscriminator,Asset Category,Currency,Symbol,Description,Quantity,Last Price,Position Value
Positions,Data,Order,Stocks,USD,AAPL,APPLE INC,10,250,2500
Positions,Data,Order,Stocks,EUR,ASML.AS,ASML HOLDING,5,800,
Trades,Header,DataDiscriminator,Asset Category,Currency,Symbol
Trades,Data,Order,Stocks,USD,MSFT
"""


def test_sectioned_report(tmp_path, capsys):
    out = _ingest_file(tmp_path, IBKR, capsys=capsys)
    assert out["source"]["layout_hint"] == "sectioned-report"
    aapl = next(p for p in out["positions"] if p["symbol"] == "AAPL")
    asml = next(p for p in out["positions"] if p["symbol"] == "ASML.AS")
    assert aapl["value"] == 2500.0
    # no stated value; statement price x quantity in row currency, FX-converted
    assert asml["value"] == pytest.approx(5 * 800 * 1.10)
    assert asml["value_source"] == "stated-price"
    assert sum(out["analysis_ready"].values()) == pytest.approx(1.0)


# --------------------------------------------------------------------------
# positions JSON mode
# --------------------------------------------------------------------------
def test_positions_json_priced_and_fx(tmp_path, capsys):
    doc = [{"symbol": "AAPL", "quantity": 10},                     # 10 x 250 USD
           {"symbol": "VODL.L", "quantity": 100},                  # 100 x 70p -> GBP x 1.27
           {"symbol": "SPAXX", "value": 5000, "type": "cash"}]
    path = tmp_path / "pos.json"
    path.write_text(json.dumps(doc))
    wm.main(["ingest", "--positions", str(path), "--currency", "USD"])
    out = json.loads(capsys.readouterr().out)
    by_sym = {p["symbol"]: p for p in out["positions"]}
    assert by_sym["AAPL"]["value"] == 2500.0
    assert by_sym["VODL.L"]["value"] == pytest.approx(100 * 0.70 * 1.27)
    assert by_sym["SPAXX"]["type"] == "cash"


def test_positions_stdin(monkeypatch, capsys):
    import io as _io
    monkeypatch.setattr("sys.stdin", _io.StringIO(
        json.dumps([{"symbol": "AAPL", "value": 1000}])))
    wm.main(["ingest", "--positions", "-", "--currency", "USD"])
    out = json.loads(capsys.readouterr().out)
    assert out["total_value"] == 1000.0


def test_stated_price_fallback(tmp_path, capsys):
    doc = [{"symbol": "UNKNOWNCO", "quantity": 10, "price": 50}]
    path = tmp_path / "p.json"
    path.write_text(json.dumps(doc))
    wm.main(["ingest", "--positions", str(path), "--currency", "USD"])
    out = json.loads(capsys.readouterr().out)
    assert out["positions"][0]["value"] == 500.0
    assert out["positions"][0]["value_source"] == "stated-price"


def test_unpriced_symbol_is_unresolved(tmp_path, capsys):
    doc = [{"symbol": "NOPE", "quantity": 10}, {"symbol": "AAPL", "value": 1000}]
    path = tmp_path / "p.json"
    path.write_text(json.dumps(doc))
    wm.main(["ingest", "--positions", str(path), "--currency", "USD"])
    out = json.loads(capsys.readouterr().out)
    assert out["unresolved"][0]["symbol"] == "NOPE"
    assert out["unresolved"][0]["included_in_total"] is False
    assert out["total_value"] == 1000.0


def test_option_and_cusip_flagged_but_counted(tmp_path, capsys):
    doc = [{"symbol": "AAPL230616C00150000", "value": 500},
           {"symbol": "037833100", "name": "US TREASURY NOTE 4.25%", "value": 3000},
           {"symbol": "AAPL", "value": 6500}]
    path = tmp_path / "p.json"
    path.write_text(json.dumps(doc))
    wm.main(["ingest", "--positions", str(path), "--currency", "USD"])
    out = json.loads(capsys.readouterr().out)
    assert out["total_value"] == 10000.0
    flagged = {u["type"] for u in out["unresolved"]}
    assert {"option", "fixed_income"} <= flagged
    assert all(u["included_in_total"] for u in out["unresolved"])
    assert out["analysis_ready"] == {"AAPL": 1.0}
    assert out["analysis_coverage"] == pytest.approx(0.65)


def test_duplicate_symbols_merge(tmp_path, capsys):
    doc = [{"symbol": "AAPL", "value": 1000}, {"symbol": "AAPL", "value": 1500},
           {"symbol": "MSFT", "value": 2500}]
    path = tmp_path / "p.json"
    path.write_text(json.dumps(doc))
    wm.main(["ingest", "--positions", str(path), "--currency", "USD"])
    out = json.loads(capsys.readouterr().out)
    assert len(out["positions"]) == 2
    assert out["analysis_ready"] == {"AAPL": 0.5, "MSFT": 0.5}


def test_foreign_stated_value_converts(tmp_path, capsys):
    doc = [{"symbol": "ASML.AS", "value": 800, "currency": "EUR"},
           {"symbol": "AAPL", "value": 1120}]
    path = tmp_path / "p.json"
    path.write_text(json.dumps(doc))
    wm.main(["ingest", "--positions", str(path), "--currency", "USD"])
    out = json.loads(capsys.readouterr().out)
    asml = next(p for p in out["positions"] if p["symbol"] == "ASML.AS")
    assert asml["value"] == pytest.approx(880.0)
    assert out["total_value"] == pytest.approx(2000.0)


# --------------------------------------------------------------------------
# failure paths
# --------------------------------------------------------------------------
def test_no_table_errors(tmp_path):
    path = tmp_path / "junk.csv"
    path.write_text("hello world\nthis is not a statement\n")
    with pytest.raises(ValueError, match="no positions table"):
        wm.main(["ingest", str(path), "--currency", "USD"])


def test_file_or_positions_not_both(tmp_path):
    path = tmp_path / "x.csv"
    path.write_text(GENERIC)
    with pytest.raises(ValueError, match="not both"):
        wm.main(["ingest", str(path), "--positions", str(path),
                 "--currency", "USD"])


def test_empty_positions_errors():
    with pytest.raises(ValueError, match="no positions"):
        wm.ingest_positions([], "USD")


def test_pdf_routes_to_agent_payload(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(wm, "_pdf_payload",
                        lambda p: {"needs_agent": True, "file": p.name})
    path = tmp_path / "stmt.pdf"
    path.write_bytes(b"%PDF-1.4 fake")
    wm.main(["ingest", str(path), "--currency", "USD"])
    out = json.loads(capsys.readouterr().out)
    assert out["needs_agent"] is True


def test_xlsx_input(tmp_path, capsys):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Symbol", "Description", "Market Value"])
    ws.append(["AAPL", "APPLE INC", 2500.0])
    ws.append(["MSFT", "MICROSOFT", 2500.0])
    path = tmp_path / "stmt.xlsx"
    wb.save(path)
    wm.main(["ingest", str(path), "--currency", "USD"])
    out = json.loads(capsys.readouterr().out)
    assert out["total_value"] == 5000.0
    assert out["analysis_ready"] == {"AAPL": 0.5, "MSFT": 0.5}


if __name__ == "__main__":
    _extra = [a for a in sys.argv[1:] if not a.startswith("-")]
    _flags = [a for a in sys.argv[1:] if a.startswith("-")]
    sys.exit(pytest.main([__file__, *_extra, *(_flags or ["-q"])]))
