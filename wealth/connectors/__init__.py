"""Registry of read-only account connectors.

A connector pulls on demand (never in the background), reads its credential
only from the environment or the OS keychain, and returns the same proposal as
a statement upload (see :class:`wealth.ingest.Connector`).  Saving still needs
the person's yes through ``ingest action=confirm``.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any, Callable, Mapping

_REGISTRY = {
    "alpaca": "wealth.connectors.alpaca",
    "cuenca": "wealth.connectors.cuenca",
    "ibkr_flex": "wealth.connectors.ibkr_flex",
}
# Non-secret options each connector accepts (the credential is never an option).
OPTIONS = {
    "alpaca": ("paper", "since", "sic_listed"),
    "cuenca": ("since",),
    "ibkr_flex": ("query_id", "sic_listed"),
}


def names() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


def _module(name: str):
    if name not in _REGISTRY:
        raise ValueError(f"connector name must be one of {', '.join(names())}")
    return import_module(_REGISTRY[name])


def connector(name: str, **options: Any):
    """Instantiate a connector; ``options`` are its non-secret settings (see :data:`OPTIONS`)."""
    module = _module(name)
    unknown = sorted(set(options) - set(OPTIONS[name]))
    if unknown:
        raise ValueError(f"connector {name} does not take {', '.join(unknown)}; it takes {', '.join(OPTIONS[name]) or 'nothing'}")
    if name == "ibkr_flex":
        return module.IbkrFlexConnector(**options)
    if name == "alpaca":
        return module.AlpacaConnector(**options)
    if name == "cuenca":
        return module.CuencaConnector(**options)
    raise ValueError(f"connector {name!r} has no constructor")  # pragma: no cover


def status(name: str | None = None) -> list[dict[str, Any]]:
    """Whether each connector's credential is present (never the credential itself)."""
    return [_module(item).status() for item in ([name] if name else names())]


def batch_mapper(proposal: Mapping[str, Any]) -> Callable[..., dict[str, Any]] | None:
    """The ledger mapper for a connector proposal, or ``None`` for the generic statement mapper."""
    provenance = ((proposal.get("result") or {}).get("provenance") or {})
    provider = provenance.get("provider")
    if provenance.get("kind") == "connector" and provider in _REGISTRY:
        return _module(provider).ledger_batch
    return None


__all__ = ["OPTIONS", "batch_mapper", "connector", "names", "status"]
