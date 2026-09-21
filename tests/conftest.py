"""Shared test setup: no test reaches a market-data provider over the network.

``WEALTH_OFFLINE=1`` makes :class:`wealth.prices.PriceProvider` read its cache only;
tests of the provider inject a fake fetcher and ``offline=False`` explicitly.
"""
import os

os.environ["WEALTH_OFFLINE"] = "1"
