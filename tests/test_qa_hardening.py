"""Regressions for the coherence and security QA findings (bounds, confirm, ledger, privacy, web errors)."""
from __future__ import annotations

import json
import multiprocessing
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.parse import quote

import pytest

from tests.fixtures.ingest import statements as fx
from wealth import situation, web
from wealth.agent import safe_diagnostic
from wealth.profile import profile_view
from wealth.service import WealthService, upload_dir
from wealth.store import ValidationError, WealthStore

from test_web_streaming import _post, serving

TODAY = datetime.now(timezone.utc).date()


def said(key, value, observed_on=None, **extra):
    return {"key": key, "value": value,
            "source": {"kind": "user", "ref": "chat", "observed_on": observed_on or TODAY.isoformat()}, **extra}


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.delenv("WEALTH_UPLOAD_DIR", raising=False)
    monkeypatch.delenv("WEALTH_UPLOAD_RETENTION_DAYS", raising=False)
    monkeypatch.delenv("WEALTH_KEEP_CONFIRMED_UPLOADS", raising=False)
    wealth = WealthService(tmp_path / "w.sqlite3")
    wealth.create("ana", "Ana")
    upload_dir("ana", wealth.db_path).mkdir(parents=True)
    return wealth


def upload(service, name, data):
    (upload_dir("ana", service.db_path) / name).write_bytes(data)
    return service.ingest("ana", "file", {"path": name})


def confirm(service, shown):
    return service.ingest("ana", "confirm", {"proposal_id": shown["result"]["proposal_id"],
                                             "acknowledge_discrepancies": shown["status"] == "needs_review"})


def schwab(period, rows, cash, total, positions_value):
    return fx.render([
        ("text", "Charles Schwab & Co., Inc."), ("text", "Brokerage Statement"), ("text", f"Statement Period: {period}"),
        ("blank",), ("text", "Individual Brokerage Account    Account Number: 1234-5678"), ("text", "Positions"),
        *fx.table(fx.US_COLUMNS, rows),
        ("row", [("Total Positions", 106, "L"), ("", 24, "R"), (positions_value, 30, "R")]),
        ("text", f"Cash & Cash Investments                                  {cash}"),
        ("text", f"Total Account Value                                      {total}")])


SEPTEMBER = ("August 15, 2026 - September 15, 2026",
             [["VTI", "Vanguard Total Stock Market ETF", "110", "260.00", "28,600.00", "22,500.00"],
              ["BND", "Vanguard Total Bond Market ETF", "150", "66.667", "10,000.00", "10,500.00"]],
             "1,000.00", "39,600.00", "38,600.00")


def ledger_holdings(service, as_of):
    held = service.run("ledger", {"view": "holdings", "as_of": as_of}, client_id="ana")["result"]
    return ({p["instrument_id"]: p["quantity"] for p in held["positions"]},
            {(c["account_id"], c["currency"]): c["balance"] for c in held["cash"]})


# ------------------------------------------------------------------ 1. bounded numbers, resilient reads

@pytest.mark.parametrize("key, value, message", [
    ("cash.checking", {"amount": 1e308, "currency": "MXN"}, "too large"),
    ("cash.checking", {"amount": 10 ** 26, "currency": "MXN"}, "too large"),
    ("investment.gbm", {"amount": -1e20, "currency": "MXN"}, "too large"),
    ("goals", [{"id": "casa", "name": "Casa", "target_amount": 1e16, "currency": "MXN"}], "too large"),
    ("reserve", {"target_months": 5000}, "too large"),
    ("liability.card", {"kind": "card", "balance": 100, "currency": "MXN", "annual_rate": 5000}, "too large"),
    ("household", {"positions": [{"quantity": 1e30}]}, "too large"),  # non-canonical keys are bounded too
])
def test_out_of_range_numbers_are_rejected_when_saved(service, key, value, message):
    with pytest.raises(ValidationError, match=message):
        service.remember("ana", [said(key, value)])
    assert service.inspect("ana")["facts"] == []


def _corrupt(db, key, value):
    """Rewrite a stored value directly, as a fact saved before the bounds existed would be."""
    connection = sqlite3.connect(db)
    connection.execute("UPDATE facts SET value_json = ? WHERE key = ?", (value, key))
    connection.commit()
    connection.close()


@pytest.mark.parametrize("stored", [
    '{"amount":1e308,"currency":"MXN"}',     # out of range: filtered before the picture is built
    '{"amount":"1e400","currency":"MXN"}',   # in range as JSON, but breaks the arithmetic: isolated after
])
def test_one_unreadable_legacy_fact_is_skipped_and_named(service, stored):
    service.remember("ana", [said("cash.checking", {"amount": 5000, "currency": "MXN"}),
                             said("cash.savings", {"amount": 7000, "currency": "MXN"}),
                             said("spending.monthly", {"total": 20000, "currency": "MXN"})])
    _corrupt(service.db_path, "cash.checking", stored)

    sit = service.situation("ana")
    assert [item["key"] for item in sit["invalid_facts"]] == ["cash.checking"]
    assert sit["net_worth"]["total"] == 7000  # the rest of the picture still stands
    assert "Situation" in situation.brief(sit, "en")
    view = profile_view(service, "ana", language="en")
    assert [item["key"] for item in view["invalid_facts"]] == ["cash.checking"]
    [card] = [i for i in view["memory"]["en"]["review"] if i.get("invalid")]
    assert card["key"] == "cash.checking" and card["forget"] == {"field": None}
    assert "cash.checking" not in card["text"]
    ran = service.run("plan", {}, client_id="ana")
    assert any("cash.checking" in w for w in ran["warnings"])

    chat = web.Chat(service.db_path, "ana")
    with serving(chat) as (base, _):
        from urllib.request import urlopen
        assert urlopen(base + "/api/state", timeout=10).status == 200
        assert urlopen(base + "/api/profile", timeout=10).status == 200
        # The person can remove it from the page.
        revision = service.inspect("ana")["client"]["revision"]
        body = json.dumps({"action": "delete", "expected_revision": revision}).encode()
        with _post(base, "/api/facts/" + quote("cash.checking", safe=""), chat.token, body) as response:
            assert response.status == 200
    assert service.situation("ana")["invalid_facts"] == []


# ------------------------------------------------------------------ 2. confirm: atomic, idempotent, no conflicts

def test_a_confirm_that_fails_part_way_leaves_nothing_and_can_be_retried(service, monkeypatch):
    shown = upload(service, "g.pdf", fx.gbm_multicurrency())
    original = WealthStore.update_auxiliary
    calls = {"n": 0}

    def failing(self, client_id, namespace, update):
        if namespace == "ingest" and calls["n"] == 0:
            calls["n"] += 1
            raise OSError("disk full after the ledger post")
        return original(self, client_id, namespace, update)

    monkeypatch.setattr(WealthStore, "update_auxiliary", failing)
    with pytest.raises(OSError):
        confirm(service, shown)
    with WealthStore(service.db_path) as store:
        assert store.ledger("ana")["entries"] == []  # the ledger post was rolled back with everything else
        assert not [f for f in store.snapshot("ana")["facts"] if f["key"].startswith("account.")]
        assert shown["result"]["proposal_id"] in store.auxiliary("ana", "ingest")["pending"]
    retried = confirm(service, shown)
    assert retried["status"] == "saved" and not retried.get("replayed")
    again = confirm(service, shown)
    assert again["replayed"] is True and again["result"]["summary"] == retried["result"]["summary"]


def test_concurrent_confirms_of_one_proposal_save_once_and_share_the_receipt(service):
    shown = upload(service, "g.pdf", fx.gbm_multicurrency())
    results, errors = [], []

    def go():
        try:
            results.append(WealthService(service.db_path).ingest(
                "ana", "confirm", {"proposal_id": shown["result"]["proposal_id"]}))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=go) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []
    assert sorted(bool(r.get("replayed")) for r in results) == [False, True, True, True]
    assert len({r["result"]["summary"] for r in results}) == 1
    with WealthStore(service.db_path) as store:
        entries = store.ledger("ana")["entries"]
    assert len(entries) == len({e["id"] for e in entries}) > 0


def test_confirm_does_not_conflict_with_unrelated_writes(service):
    (upload_dir("ana", service.db_path) / "bbva.pdf").write_bytes(fx.bbva_checking())
    shown = service.ingest("ana", "file", {"path": "bbva.pdf"})
    service.remember("ana", [said("thread.a", {"kind": "question", "text": "¿Y el ahorro?", "status": "open"})])
    stop = threading.Event()

    def writer():
        other, i = WealthService(service.db_path), 0
        while not stop.is_set():
            other.remember("ana", [said(f"thread.w{i}", {"kind": "question", "text": "nota", "status": "open"})])
            i += 1

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        saved = confirm(service, shown)
    finally:
        stop.set()
        thread.join()
    assert saved["status"] == "saved"
    assert "account.bbva-6789" in saved["result"]["saved"]["keys"]


# ------------------------------------------------------------------ 3. forgetting a statement account

def test_forgetting_a_statement_account_retires_its_ledger_data_everywhere(service):
    service.remember("ana", [said("cash.checking", {"amount": 5000, "currency": "USD"})])
    confirm(service, upload(service, "schwab.pdf", fx.us_brokerage()))
    assert service.situation("ana")["net_worth"]["total"] > 5000
    snapshot = service.inspect("ana")
    receipt = service.remember("ana", [said("account.schwab-5678", None)], snapshot["client"]["revision"])
    assert receipt["ledger"]["accounts"] == ["schwab-5678"] and receipt["ledger"]["reversed"] > 0

    sit = service.situation("ana")
    assert sit["net_worth"]["total"] == 5000 and sit["net_worth"]["complete"] is True
    assert sit["net_worth"]["unvalued_accounts"] == []
    assert "Schwab" not in situation.brief(sit, "en")
    positions, cash = ledger_holdings(service, TODAY.isoformat())
    assert positions == {} and cash == {}
    with WealthStore(service.db_path) as store:  # append-only: the history is still there
        entries = store.ledger("ana")["entries"]
    reversals = [e for e in entries if e["kind"] == "reversal"]
    assert reversals and len(reversals) == len(entries) - len(reversals)


# ------------------------------------------------------------------ 4. a newer statement reconciles the ledger

def test_a_newer_statement_reconciles_the_ledger_and_names_what_disappeared(service):
    confirm(service, upload(service, "aug.pdf", fx.us_brokerage()))
    positions, cash = ledger_holdings(service, "2026-09-15")
    assert positions == {"AAPL": "10.5", "BND": "150", "VTI": "100"} and cash == {("schwab-5678", "USD"): "1500.00"}

    shown = upload(service, "sep.pdf", schwab(*SEPTEMBER))
    [preview] = shown["result"]["reconciliation"]["missing_positions"]
    assert (preview["symbol"], preview["quantity"]) == ("AAPL", "10.5")
    saved = confirm(service, shown)

    positions, cash = ledger_holdings(service, "2026-09-15")
    assert positions == {"BND": "150", "VTI": "110"} and cash == {("schwab-5678", "USD"): "1000.00"}
    ledger = saved["result"]["ledger"]
    assert ledger["reconciliation"]["breaks"] == []
    assert "Balances agree with the statement" in ledger["plain"] and "disagree" not in ledger["plain"]
    changes = {(c["symbol"] or c["currency"]): c["change"] for c in ledger["adjustments"]}
    assert changes == {"USD": "-500", "VTI": "10", "AAPL": "-10.5"}
    assert [g["symbol"] for g in saved["result"]["reconciliation"]["missing_positions"]] == ["AAPL"]
    assert "AAPL" in saved["result"]["summary"]
    with WealthStore(service.db_path) as store:
        labelled = [e for e in store.ledger("ana")["entries"] if "Statement reconciliation" in (e.get("description") or "")]
    assert len(labelled) == 3

    # An older statement afterwards adds balance checks only; it never rewrites the newer picture.
    older = fx.render([("text", "Charles Schwab & Co., Inc."), ("text", "Brokerage Statement"),
                       ("text", "Statement Period: May 1, 2026 - May 31, 2026"), ("blank",),
                       ("text", "Individual Brokerage Account    Account Number: 1234-5678"), ("text", "Positions"),
                       *fx.table(fx.US_COLUMNS, [["VTI", "Vanguard Total Stock Market ETF", "90", "250.00",
                                                  "22,500.00", "20,000.00"]]),
                       ("row", [("Total Positions", 106, "L"), ("", 24, "R"), ("22,500.00", 30, "R")]),
                       ("text", "Cash & Cash Investments                                  1,000.00"),
                       ("text", "Total Account Value                                      23,500.00")])
    shown = upload(service, "may.pdf", older)
    assert shown["result"]["reconciliation"]["missing_positions"] == []
    old = confirm(service, shown)
    assert old["result"]["reconciliation"] == {"adjustments": [], "missing_positions": []}
    assert ledger_holdings(service, "2026-09-15")[0] == {"BND": "150", "VTI": "110"}


# ------------------------------------------------------------------ 5. uploads are purged

def test_deleting_a_client_securely_removes_its_uploads(service):
    confirm(service, upload(service, "g.pdf", fx.gbm_multicurrency()))
    (upload_dir("ana", service.db_path) / "pending.pdf").write_bytes(fx.bbva_checking())
    assert service.forget("ana", "ana")["uploads_removed"] >= 1
    assert not upload_dir("ana", service.db_path).exists()


def test_deleting_through_the_store_also_removes_uploads(service):
    (upload_dir("ana", service.db_path) / "raw.pdf").write_bytes(b"%PDF-1.4 RFC GODE561231GR8")
    with WealthStore(service.db_path) as store:
        store.delete_client("ana", "ana")
    assert not upload_dir("ana", service.db_path).exists()


def test_a_confirmed_upload_is_purged_and_old_ones_expire(service, monkeypatch):
    folder = upload_dir("ana", service.db_path)
    shown = upload(service, "g.pdf", fx.gbm_multicurrency())
    (folder / "g.json").write_text("{}")  # the browser chat's metadata sidecar
    (folder / "other.pdf").write_bytes(fx.bbva_checking())
    saved = confirm(service, shown)
    assert saved["result"]["upload_removed"] is True
    assert sorted(p.name for p in folder.iterdir()) == ["other.pdf"]

    stale = time.time() - 31 * 86_400
    os.utime(folder / "other.pdf", (stale, stale))
    (folder / "fresh.pdf").write_bytes(b"%PDF-1.4")
    service.ingest("ana", "connector_status", {})  # any ingest call sweeps expired uploads
    assert sorted(p.name for p in folder.iterdir()) == ["fresh.pdf"]

    monkeypatch.setenv("WEALTH_UPLOAD_RETENTION_DAYS", "0")
    monkeypatch.setenv("WEALTH_KEEP_CONFIRMED_UPLOADS", "1")
    os.utime(folder / "fresh.pdf", (stale, stale))
    kept = upload(service, "b.pdf", fx.bbva_checking())
    confirm(service, kept)
    assert sorted(p.name for p in folder.iterdir()) == ["b.pdf", "fresh.pdf"]


# ------------------------------------------------------------------ 6. diagnostics never carry identifiers

def test_safe_diagnostic_masks_government_ids_and_account_numbers():
    text = ("ERROR: wealth_ingest failed on RFC GODE561231GR8 CURP GODE561231HDFRRN09 CLABE 012180001234567891 "
            "SSN 123-45-6789 card 5555 4444 3333 1234 account 0123456789")
    line = safe_diagnostic(text)
    for secret in ("GODE561231GR8", "GODE561231HDFRRN09", "012180001234567891", "123-45-6789",
                   "5555 4444 3333", "0123456789"):
        assert secret not in line
    assert "ERROR: wealth_ingest failed" in line


# ------------------------------------------------------------------ 7. migration under concurrency

def _downgrade_to_v1(db):
    with WealthStore(db) as fresh:
        fresh.create_client("ana", "Ana")
        fresh.remember("ana", [said("income.salary", {"amount": 60000, "currency": "MXN", "frequency": "monthly"},
                                    observed_on="2026-03-10"),
                               said("goals", [{"id": "casa", "name": "Casa"}], observed_on="2026-03-10")])
    connection = sqlite3.connect(db)
    connection.executescript("""
        DROP INDEX facts_client_valid;
        DROP TABLE contradictions;
        ALTER TABLE facts DROP COLUMN status;
        ALTER TABLE facts DROP COLUMN valid_to;
        ALTER TABLE facts DROP COLUMN valid_from;
        DROP TABLE ledger_accounts; DROP TABLE ledger_instruments; DROP TABLE ledger_entries; DROP TABLE ledger_fx;
        DROP TABLE ledger_assertions; DROP TABLE ledger_batches; DROP TABLE ledger_rules; DROP TABLE ledger_labels;
        UPDATE metadata SET value = '1' WHERE key = 'schema_version';
        UPDATE facts SET expires_on = date(observed_on, '+365 days') WHERE key = 'income.salary';
    """)
    connection.commit()
    connection.close()


def _open(db, results):
    try:
        with WealthStore(db) as store:
            results.put(("ok", len(store.snapshot("ana")["facts"])))
    except Exception as exc:  # noqa: BLE001
        results.put(("error", f"{type(exc).__name__}: {exc}"))


def test_old_databases_migrate_once_when_opened_concurrently(tmp_path):
    context = multiprocessing.get_context("spawn")
    for trial in range(3):
        db = tmp_path / f"v1-{trial}.sqlite3"
        _downgrade_to_v1(db)
        results = context.Queue()
        processes = [context.Process(target=_open, args=(db, results)) for _ in range(4)]
        [p.start() for p in processes]
        [p.join(60) for p in processes]
        outcomes = [results.get(timeout=5) for _ in processes]
        assert outcomes == [("ok", 2)] * 4, outcomes


def test_version_one_review_dates_are_recomputed_by_kind(tmp_path):
    db = tmp_path / "v1.sqlite3"
    _downgrade_to_v1(db)
    with WealthStore(db) as store:
        facts = {f["key"]: f for f in store.snapshot("ana")["facts"]}
        store.remember("ana", [said("thread.x", {"kind": "question", "text": "ok", "status": "open"})])  # usable
    assert facts["income.salary"]["expires_on"] == "2026-06-08"  # 90 days, like a salary saved today
    assert facts["goals"]["expires_on"] == "2027-03-10"         # 365 days is still the goals horizon


# ------------------------------------------------------------------ 8. web error mapping

def test_web_maps_validation_lookup_and_nesting_errors_to_4xx(tmp_path):
    db = tmp_path / "w.sqlite3"
    service = WealthService(db)
    service.create("personal", "personal")
    service.remember("personal", [said("cash.checking", {"amount": 5000, "currency": "MXN"})])
    chat = web.Chat(db, "personal")
    path = "/api/facts/" + quote("cash.checking", safe="")
    with serving(chat) as (base, _):
        cases = [
            (path, b'{"action": "delete"}', 400, "Reload"),
            ("/api/profile/contradictions/abc", b'{"choice": "keep"}', 404, "no longer there"),
            (path, b'{"action":"edit","value":{"amount":1e308,"currency":"MXN"}}', 400, "too large"),
            ("/api/turns", b'{"message":' + b"[" * 5000 + b"]" * 5000 + b"}", 400, "nested"),
        ]
        for route, body, status, words in cases:
            with pytest.raises(HTTPError) as failure:
                _post(base, route, chat.token, body)
            payload = json.load(failure.value)
            assert failure.value.code == status, (route, payload)
            assert words in payload["error"] and payload["kind"] != "storage"
    assert web.json_depth(b'{"a": "[[[[", "b": [[1]]}') == 3


# ------------------------------------------------------------------ 9. questions in the person's words

def test_contradiction_questions_use_human_labels_in_the_persons_language(service):
    service.remember("ana", [said("client.profile", {"language": "es"}),
                             said("investment.brokerage", {"amount": 200000, "currency": "MXN", "institution": "GBM",
                                                           "approximate": True}, observed_on="2026-08-01")])
    saved = confirm(service, upload(service, "gbm.pdf", fx.gbm_multicurrency()))
    [question] = [q["question"] for q in saved["result"]["needs_user"]]
    # Codes appear only because the statement holds two currencies.
    assert question.startswith("Dijiste unos $200,000 MXN en GBM, pero el estado de cuenta del 31 de agosto de 2026")
    assert "investment.brokerage" not in question and "¿Mantengo tu cifra" in question

    with WealthStore(service.db_path) as store:
        store.remember("ana", [said("income.salary", {"amount": 85000, "currency": "MXN", "frequency": "monthly"})])
        receipt = store.remember("ana", [{"key": "income.salary", "value": {"amount": 90000, "currency": "MXN",
                                                                             "frequency": "monthly"},
                                          "source": {"kind": "document", "ref": "nomina.pdf",
                                                     "observed_on": TODAY.isoformat()}}])
    [held] = receipt["needs_user"]
    assert held["question"].startswith("Me dijiste que tu sueldo es de $85,000 al mes")
    assert "income.salary" not in held["question"] and "nomina.pdf" not in held["question"]


# ------------------------------------------------------------------ 10. one sentence per stale statement account

def test_a_stale_statement_account_appears_once_in_the_memory(service):
    old = fx.render([("text", "Charles Schwab & Co., Inc."), ("text", "Brokerage Statement"),
                     ("text", "Statement Period: May 1, 2026 - May 31, 2026"), ("blank",),
                     ("text", "Individual Brokerage Account    Account Number: 1234-5678"), ("text", "Positions"),
                     *fx.table(fx.US_COLUMNS, [["VTI", "Vanguard Total Stock Market ETF", "100", "250.00",
                                                "25,000.00", "20,000.00"]]),
                     ("row", [("Total Positions", 106, "L"), ("", 24, "R"), ("25,000.00", 30, "R")]),
                     ("text", "Cash & Cash Investments                                  1,000.00"),
                     ("text", "Total Account Value                                      26,000.00")])
    confirm(service, upload(service, "old.pdf", old))
    view = profile_view(service, "ana", language="en")
    memory = view["memory"]["en"]
    rows = [f for g in memory["groups"] for f in g["facts"]] + memory["review"]
    texts = [f["text"] for f in rows if f["key"] == "account.schwab-5678"]
    assert len(texts) == 1 and texts[0].startswith("At Charles Schwab")
