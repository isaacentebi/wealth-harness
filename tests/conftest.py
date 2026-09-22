"""Shared test setup: no test reaches a market-data provider over the network.

``WEALTH_OFFLINE=1`` makes :class:`wealth.prices.PriceProvider` read its cache only;
tests of the provider inject a fake fetcher and ``offline=False`` explicitly.
``WEALTH_RUNTIME=exec`` keeps turns on the exec path unless a test chooses the app-server fake.
Derived instruction files go to a private per-run cache (``XDG_CACHE_HOME``), not the user's.
"""
import os
import tempfile

os.environ["WEALTH_OFFLINE"] = "1"
# Tests substitute ``agent._stream_process`` with recorded exec output; the app-server runtime is tested with a fake
# app-server (tests/test_appserver.py), so no test ever starts the real one.
os.environ["WEALTH_RUNTIME"] = "exec"
os.environ["XDG_CACHE_HOME"] = tempfile.mkdtemp(prefix="wealth-test-cache-")
