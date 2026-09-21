"""Extension point for account aggregators.  No adapter ships with Wealth.

A connector pulls on demand (never in the background), takes credentials only
from environment variables or the OS keychain (never SQLite, logs or model
context), and returns the same proposal as every other ingestion path by
building a statement and calling :func:`wealth.ingest.model.build_proposal`
with ``kind="connector"``.  Stable ids come from the provider's account and
instrument ids so :func:`wealth.ingest.model.diff_proposals` reports changes
between pulls.  Saving still requires the person's confirmation.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Connector(Protocol):
    provider: str
    countries: tuple[str, ...]

    def proposal(self, *, owner_id: str = "self", previous: dict[str, Any] | None = None) -> dict[str, Any]:
        """Pull accounts, balances, holdings and transactions and return an ingest proposal."""
        ...
