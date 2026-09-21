from __future__ import annotations

from decimal import Decimal

import pytest

from wealth import cashflow
from wealth.store import WealthStore


BANK = {"id": "bank", "institution": "BBVA", "type": "checking", "currency": "MXN"}
GBM = {"id": "gbm", "institution": "GBM", "type": "brokerage", "currency": "MXN"}


def _e(entry_id, day, kind, amount, description=None, account="bank", **extra):
    entry = {"id": entry_id, "account_id": account, "kind": kind, "date": day, "amount": str(amount),
             "currency": "MXN", "confidence": "reported", "source": {"kind": "document", "ref": "estado de cuenta"}}
    if description:
        entry["description"] = description
    entry.update(extra)
    return entry


def ledger():
    entries = []
    for month in ("01", "02", "03"):
        entries += [
            _e(f"sal{month}", f"2026-{month}-15", "income", 30000, "PAGO DE NOMINA ACME SA", subtype="other"),
            _e(f"rent{month}", f"2026-{month}-01", "expense", -12000, "RENTA DEPTO ROMA"),
            _e(f"nfx{month}", f"2026-{month}-20", "expense", -299, "NETFLIX.COM"),
        ]
    entries += [
        _e("oxxo", "2026-01-08", "expense", -85, "OXXO SUC 12 CDMX"),
        _e("wf", "2026-01-22", "expense", -600, "WHOLE FOODS MARKET #1023"),
        _e("cfe", "2026-02-10", "expense", -800, "CFE SUMINISTRADOR DE SERVICIOS"),
        _e("uber", "2026-02-11", "expense", -150, "UBER TRIP HELP.UBER.COM"),
        _e("school", "2026-02-05", "expense", -5000, "COLEGIATURA FEBRERO"),
        _e("fee", "2026-02-28", "fee", -100, "COMISION MANEJO DE CUENTA"),
        _e("brkfee", "2026-02-28", "fee", -40, "CUSTODIA", account="gbm"),
        _e("eats", "2026-03-03", "expense", -300, "UBER EATS PENDING"),
        _e("pharm", "2026-03-09", "expense", -400, "FARMACIAS DEL AHORRO"),
        _e("odd", "2026-03-12", "expense", -1000, "TIENDA RARA 123"),
        _e("wmt", "2026-03-14", "expense", -700, "Walmart Supercenter"),
        _e("ptu", "2026-03-28", "income", 8000, "PTU 2025"),
        _e("t-out", "2026-03-02", "transfer", -5000, "TRASPASO A GBM"),
        _e("t-in", "2026-03-02", "transfer", 5000, "DEPOSITO", account="gbm"),
        _e("loan", "2026-03-05", "loan_payment", -3000, "PAGO CREDITO AUTO", principal="2500", interest="500"),
    ]
    return {"accounts": [BANK, GBM], "instruments": [], "fx": [], "entries": entries, "labels": [],
            "category_rules": []}


def test_categorisation_rules_cover_english_and_mexican_merchants():
    labels = cashflow.categorize(ledger())
    expected = {"rent01": "housing", "nfx01": "subscriptions", "oxxo": "convenience", "wf": "groceries",
                "cfe": "utilities", "uber": "transport", "eats": "food_delivery", "school": "education",
                "pharm": "health", "wmt": "groceries", "sal01": "salary", "ptu": "ptu", "fee": "bank_fees",
                "odd": "uncategorized"}
    assert {k: labels[k]["category"] for k in expected} == expected
    assert labels["odd"]["status"] == "uncategorized" and labels["rent01"]["status"] == "rule"
    assert "brkfee" not in labels and "t-out" not in labels and "loan" not in labels


def test_precedence_confirmed_then_user_rules_then_statement_then_builtin_then_model():
    data = ledger()
    data["entries"].append(_e("stmt", "2026-03-15", "expense", -50, "OXXO GAS", category="transport"))
    data["category_rules"] = [{"id": "r1", "pattern": "netflix", "category": "entertainment", "source": "user"}]
    data["labels"] = [{"entry_id": "nfx02", "category": "education", "status": "confirmed", "source": "user"},
                      {"entry_id": "odd", "category": "shopping", "status": "inferred", "source": "model:x"}]
    labels = cashflow.categorize(data)
    assert labels["nfx02"]["category"] == "education"          # confirmed label
    assert labels["nfx01"]["category"] == "entertainment"      # user rule beats built-in
    assert labels["stmt"] == {"category": "transport", "status": "reported", "basis": "statement", "kind": "expense"}
    assert labels["odd"]["status"] == "inferred"
    proposals = cashflow.suggest_categories(ledger(), lambda entry, allowed: "groceries" if "TIENDA" in entry["description"] else "nonsense")
    assert proposals == [{"entry_id": "odd", "category": "groceries", "status": "inferred"}]


def test_monthly_spending_excludes_transfers_loans_and_brokerage_fees():
    report = cashflow.spending_report(ledger(), "2026-01-01", "2026-03-31", "MXN")
    months = report["result"]["months"]
    assert months["2026-01"]["total"] == "12984.00"
    assert months["2026-02"]["total"] == "18349.00"
    assert months["2026-03"]["total"] == "14699.00"
    assert months["2026-02"]["by_category"]["education"] == "5000.00"
    assert months["2026-03"]["unknown_essentiality"] == "1000.00"
    assert months["2026-01"]["fixed"] == "12299.00"  # rent + subscription
    assert any("uncategorized" in w for w in report["warnings"])


def test_recurring_detection_and_income_stability():
    series = cashflow.recurring(ledger())["result"]["series"]
    found = {(s["merchant"], s["cadence"], s["kind"]) for s in series}
    assert ("renta depto roma", "monthly", "expense") in found
    assert ("netflix com", "monthly", "expense") in found
    assert ("pago de nomina", "monthly", "income") in found
    netflix = next(s for s in series if s["merchant"] == "netflix com")
    assert netflix["next_expected"] == "2026-04-20" and netflix["typical_amount"] == "299.00"
    income = cashflow.income_report(ledger(), "2026-01-01", "2026-03-31", "MXN")["result"]
    assert income["regular_monthly_mean"] == "30000.00" and income["stability"] == "stable"
    assert income["irregular_total"] == "8000.00"


def test_surplus_needs_the_reserve_target_and_is_conservative():
    missing = cashflow.investable_surplus(ledger(), "2026-01-01", "2026-03-31", "MXN")
    assert missing["status"] == "needs_input"
    assert [m["key"] for m in missing["missing"]] == ["plan.resources"]
    assert missing["result"]["monthly"]["investable_surplus"] is None
    essential = (Decimal(12600) + Decimal(17950) + Decimal(14100)) / 3
    before = Decimal(30000) - essential - Decimal(1000)
    assert missing["result"]["monthly"]["surplus_before_reserve"] == str(before.quantize(Decimal("0.01")))

    resources = {"currency": "MXN", "monthly_essentials": 15000, "reserve_months": 6}
    no_balance = cashflow.investable_surplus(ledger(), "2026-01-01", "2026-03-31", "MXN", plan_resources=resources)
    assert [m["key"] for m in no_balance["missing"]] == ["reserve_balance"]
    ready = cashflow.investable_surplus(ledger(), "2026-01-01", "2026-03-31", "MXN", plan_resources=resources,
                                        reserve_balance=66000)
    monthly = ready["result"]["monthly"]
    assert monthly["reserve_top_up"] == "2000.00"
    assert monthly["investable_surplus"] == str((before - 2000).quantize(Decimal("0.01")))
    assert ready["result"]["savings_rate"] == str(((Decimal(98000) - 46032 - 500) / 98000).quantize(Decimal("0.0001")))
    mismatch = cashflow.investable_surplus(ledger(), "2026-01-01", "2026-03-31", "MXN",
                                           plan_resources=dict(resources, currency="USD"), reserve_balance=0)
    assert mismatch["result"]["monthly"]["investable_surplus"] is None
    from_accounts = cashflow.investable_surplus(ledger(), "2026-01-01", "2026-03-31", "MXN", plan_resources=resources,
                                                reserve_account_ids=["gbm"])
    assert from_accounts["result"]["reserve"]["current"] == "4960.00"


def test_foreign_currency_spending_without_fx_is_missing_not_zero():
    data = ledger()
    data["entries"].append(dict(_e("usd", "2026-03-20", "expense", -50, "SPOTIFY USA"), currency="USD"))
    report = cashflow.spending_report(data, "2026-03-01", "2026-03-31", "MXN")
    assert report["missing"][0]["key"] == "fx.USD/MXN@2026-03-20"
    data["fx"] = [{"date": "2026-03-19", "base": "USD", "quote": "MXN", "rate": "17", "source": "Banxico"}]
    report = cashflow.spending_report(data, "2026-03-01", "2026-03-31", "MXN")
    assert report["result"]["months"]["2026-03"]["by_category"]["subscriptions"] == "1149.00"


def test_user_override_is_remembered_as_a_rule(tmp_path):
    with WealthStore(tmp_path / "w.sqlite3") as store:
        store.create_client("c", "C")
        source = {"kind": "document", "ref": "estado", "file_hash": "c" * 64}
        receipt = store.post_ledger("c", {"batch_id": "b1", "source": source, "accounts": [BANK], "transactions": [
            {"account_id": "bank", "kind": "expense", "date": "2026-03-12", "amount": -1000, "currency": "MXN",
             "description": "TIENDA RARA 123"}]})
        saved = cashflow.remember_override(store, "c", receipt["posted"][0], "groceries")
        assert saved["rule"]["pattern"] == "tienda rara"
        store.post_ledger("c", {"batch_id": "b2", "source": source, "transactions": [
            {"account_id": "bank", "kind": "expense", "date": "2026-04-12", "amount": -900, "currency": "MXN",
             "description": "TIENDA RARA 456"}]})
        labels = cashflow.categorize(store.ledger("c"))
        assert {v["category"] for v in labels.values()} == {"groceries"}
        assert {v["status"] for v in labels.values()} == {"confirmed"}
        with pytest.raises(ValueError, match="unknown category"):
            cashflow.remember_override(store, "c", receipt["posted"][0], "vibes")
        assert cashflow.run("spending", {"view": "surplus", "start": "2026-03-01", "end": "2026-04-30", "currency": "MXN"},
                            {"ledger": store.ledger("c")})["status"] == "needs_input"
