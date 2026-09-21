"""Shared test setup: no test reaches a market-data provider over the network.

``WEALTH_OFFLINE=1`` makes :class:`wealth.prices.PriceProvider` read its cache only;
tests of the provider inject a fake fetcher and ``offline=False`` explicitly.
Derived instruction files go to a private per-run cache (``XDG_CACHE_HOME``), not the user's.
"""
import os
import tempfile

os.environ["WEALTH_OFFLINE"] = "1"
os.environ["XDG_CACHE_HOME"] = tempfile.mkdtemp(prefix="wealth-test-cache-")
