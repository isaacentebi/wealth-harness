"""Shared test setup: no test reaches a market-data provider over the network.

``WEALTH_OFFLINE=1`` makes :class:`wealth.prices.PriceProvider` read its cache only;
tests of the provider inject a fake fetcher and ``offline=False`` explicitly.
Reference rates (wealth/rates.py) never reach Treasury, Banxico or the keychain, even with
``WEALTH_OFFLINE`` unset: their tests inject a fake transport.
``WEALTH_RUNTIME=exec`` keeps turns on the exec path unless a test chooses the app-server fake.
Derived instruction files go to a private per-run cache (``XDG_CACHE_HOME``), not the user's.
"""
import os
import tempfile

import pytest

os.environ["WEALTH_OFFLINE"] = "1"
# Tests substitute ``agent._stream_process`` with recorded exec output; the app-server runtime is tested with a fake
# app-server (tests/test_appserver.py), so no test ever starts the real one.
os.environ["WEALTH_RUNTIME"] = "exec"
os.environ["XDG_CACHE_HOME"] = tempfile.mkdtemp(prefix="wealth-test-cache-")


@pytest.fixture(autouse=True)
def _no_reference_rate_network(monkeypatch):
    from wealth import rates

    def refuse(*_, **__):
        raise OSError("network access is disabled in tests")
    monkeypatch.setattr(rates, "default_transport", lambda: refuse)
    monkeypatch.setattr(rates, "banxico_token", lambda *a, **k: None)
