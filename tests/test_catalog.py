"""Every catalog example and variant runs through the public service, offline."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from wealth import legacy, research
from wealth.catalog import CATALOG
from wealth.service import SERVICE_TASKS, TASK_MODULES, WealthService


def _synthetic_prices(tickers, years=5, currency=None, align=True, **_):
    dates = pd.bdate_range(end="2026-09-18", periods=int(252 * years))
    rng = np.random.default_rng(7)
    market = rng.normal(0.0004, 0.008, len(dates))
    frame = pd.DataFrame({
        ticker: 100 * np.exp(np.cumsum((0.3 + 0.2 * i) * market + rng.normal(0.0002, 0.005, len(dates))))
        for i, ticker in enumerate(dict.fromkeys(tickers))
    }, index=dates)
    frame.attrs["retrieved"] = "2026-09-18"
    return frame, []


def _synthetic_factors(model=3):
    dates = pd.bdate_range(end="2026-09-18", periods=252 * 6)
    rng = np.random.default_rng(11)
    names = ["Mkt-RF", "SMB", "HML"] + (["RMW", "CMA"] if model == 5 else [])
    frame = pd.DataFrame({name: rng.normal(0.0002, 0.006, len(dates)) for name in names}, index=dates)
    frame["RF"] = 0.00015
    return frame


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def refuse(*_, **__):
        raise OSError("network access is disabled in catalog tests")

    monkeypatch.setattr(legacy, "_load_prices", _synthetic_prices)
    monkeypatch.setattr(legacy, "_load_factors", _synthetic_factors)
    monkeypatch.setattr(legacy, "_fetch_one", refuse)
    monkeypatch.setattr(research, "_fetch_yfinance", refuse)


def _cases():
    for task, entry in CATALOG.items():
        yield pytest.param(task, entry["example"], id=f"{task}-example")
        for name, variant in (entry.get("variants") or {}).items():
            yield pytest.param(task, variant, id=f"{task}-{name}")


def test_catalog_covers_exactly_the_runnable_tasks():
    assert set(CATALOG) == set(TASK_MODULES) | set(SERVICE_TASKS)
    for task, entry in CATALOG.items():
        assert entry["purpose"] and entry["required"] and "example" in entry, task


@pytest.mark.parametrize("task, inputs", list(_cases()))
def test_catalog_example_runs_offline(tmp_path, task, inputs):
    service = WealthService(tmp_path / "catalog.sqlite3")
    client_id = None
    if task == "monitor":
        service.create("catalog", "Catalog")
        client_id = "catalog"
    report = service.run(task, inputs=inputs, client_id=client_id)
    assert report["status"] in {"ready", "partial"}, (report["status"], report.get("missing"), report.get("warnings"))
